import sqlite3
import uuid
from typing import Callable

from ai_service_agent.graph.workflows.copy import (
    handoff_joined_notice_text,
    handoff_request_confirm_text,
)
from ai_service_agent.harness.session import ConversationStatusStore

# Hang confirm / Joined Notice — single source in copy.py (CONTEXT glossary).
HANDOFF_REQUEST_CONFIRM = handoff_request_confirm_text()
HANDOFF_JOINED_NOTICE = handoff_joined_notice_text()
# Backward-compat: old name now means Joined Notice (not request hang tip).
HANDOFF_NOTICE = HANDOFF_JOINED_NOTICE

STATUS_AWAITING = "awaiting_seat"
STATUS_ACTIVE = "active"
STATUS_RESOLVED = "resolved"
STATUS_CANCELLED = "cancelled"


SCHEMA = """
CREATE TABLE IF NOT EXISTS handoff_record (
    handoff_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    reason TEXT,
    summary TEXT,
    status TEXT NOT NULL DEFAULT 'awaiting_seat',
    created_at TEXT DEFAULT (datetime('now')),
    resolved_at TEXT
);
"""


class HandoffService:
    """外层（Conversation Runtime）：Handoff Request 挂起 / Seat Takeover / Resolve。

    子图只抛 interrupt(handoff_request)，不直接调用本服务（graph-spec.md §6.5）。
    Seat-gated（ADR-0003）：申请挂起 ≠ mode=human；仅 Seat Takeover 切 human。
    """

    def __init__(self, conn: sqlite3.Connection, status: ConversationStatusStore):
        self.conn = conn
        self.status = status
        self.conn.execute(SCHEMA)
        self.conn.commit()

    def _row_to_rec(self, row) -> dict:
        return {
            "handoff_id": row[0],
            "conversation_id": row[1],
            "reason": row[2],
            "summary": row[3],
            "status": row[4] if len(row) > 4 else STATUS_AWAITING,
        }

    def create_handoff_request(self, conversation_id: str, reason: str, summary: str) -> dict:
        """建 Handoff Request 挂起（awaiting_seat）；不切 conversation_mode。"""
        handoff_id = str(uuid.uuid4())
        with self.conn:
            self.conn.execute(
                "INSERT INTO handoff_record (handoff_id, conversation_id, reason, summary, status) "
                "VALUES (?, ?, ?, ?, ?)",
                (handoff_id, conversation_id, reason, summary, STATUS_AWAITING),
            )
        return {
            "handoff_id": handoff_id,
            "conversation_id": conversation_id,
            "reason": reason,
            "summary": summary,
            "status": STATUS_AWAITING,
        }

    def create_handoff(self, conversation_id: str, reason: str, summary: str) -> dict:
        """兼容旧名：创建 Handoff Request 挂起（不切 mode）。Prefer create_handoff_request."""
        return self.create_handoff_request(conversation_id, reason, summary)

    def seat_takeover(
        self,
        conversation_id: str,
        reason: str = "",
        summary: str = "",
    ) -> dict:
        """Seat Takeover：挂起→active（或懒建 active）并与 mode=human 同事务。"""
        awaiting = self.get_awaiting(conversation_id)
        with self.conn:
            if awaiting is not None:
                self.conn.execute(
                    "UPDATE handoff_record SET status = ? WHERE handoff_id = ?",
                    (STATUS_ACTIVE, awaiting["handoff_id"]),
                )
                self.status.set_mode(conversation_id, "human", commit=False)
                return {
                    "handoff_id": awaiting["handoff_id"],
                    "conversation_id": conversation_id,
                    "reason": awaiting.get("reason") or reason,
                    "summary": awaiting.get("summary") or summary,
                    "status": STATUS_ACTIVE,
                }
            active = self.get_active(conversation_id)
            if active is not None:
                self.status.set_mode(conversation_id, "human", commit=False)
                return {**active, "status": STATUS_ACTIVE}
            handoff_id = str(uuid.uuid4())
            self.conn.execute(
                "INSERT INTO handoff_record (handoff_id, conversation_id, reason, summary, status) "
                "VALUES (?, ?, ?, ?, ?)",
                (handoff_id, conversation_id, reason or "seat_takeover", summary, STATUS_ACTIVE),
            )
            self.status.set_mode(conversation_id, "human", commit=False)
        return {
            "handoff_id": handoff_id,
            "conversation_id": conversation_id,
            "reason": reason or "seat_takeover",
            "summary": summary,
            "status": STATUS_ACTIVE,
        }

    def cancel_handoff_request(self, conversation_id: str) -> dict | None:
        """清挂起的 Handoff Request；不切 Conversation Mode。无挂起则 None。"""
        awaiting = self.get_awaiting(conversation_id)
        if awaiting is None:
            return None
        with self.conn:
            self.conn.execute(
                "UPDATE handoff_record SET status = ?, resolved_at = datetime('now') WHERE handoff_id = ?",
                (STATUS_CANCELLED, awaiting["handoff_id"]),
            )
        return {**awaiting, "status": STATUS_CANCELLED}

    def resolve_handoff(self, handoff_id: str) -> dict:
        row = self.conn.execute(
            "SELECT conversation_id, status FROM handoff_record WHERE handoff_id = ?",
            (handoff_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"handoff not found: {handoff_id}")
        conversation_id, current = row[0], row[1]
        if current in (STATUS_RESOLVED, STATUS_CANCELLED):
            return {"handoff_id": handoff_id, "conversation_id": conversation_id, "mode": "ai"}
        with self.conn:
            self.conn.execute(
                "UPDATE handoff_record SET status = ?, resolved_at = datetime('now') WHERE handoff_id = ?",
                (STATUS_RESOLVED, handoff_id),
            )
            self.status.set_mode(conversation_id, "ai", commit=False)
        return {"handoff_id": handoff_id, "conversation_id": conversation_id, "mode": "ai"}

    def get_by_conversation(self, conversation_id: str) -> dict | None:
        """该会话最新一条 handoff 记录（含各状态），无则 None。"""
        row = self.conn.execute(
            "SELECT handoff_id, conversation_id, reason, summary, status, created_at, resolved_at "
            "FROM handoff_record WHERE conversation_id = ? ORDER BY created_at DESC LIMIT 1",
            (conversation_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "handoff_id": row[0],
            "conversation_id": row[1],
            "reason": row[2],
            "summary": row[3],
            "status": row[4],
            "created_at": row[5],
            "resolved_at": row[6],
        }

    def get(self, handoff_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT handoff_id, conversation_id, status FROM handoff_record WHERE handoff_id = ?",
            (handoff_id,),
        ).fetchone()
        if row is None:
            return None
        return {"handoff_id": row[0], "conversation_id": row[1], "status": row[2]}

    def get_awaiting(self, conversation_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT handoff_id, conversation_id, reason, summary, status FROM handoff_record "
            "WHERE conversation_id=? AND status=? ORDER BY created_at DESC LIMIT 1",
            (conversation_id, STATUS_AWAITING),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_rec(row)

    def get_active(self, conversation_id: str) -> dict | None:
        """当前会话最新 active handoff（Takeover 后）；无则 None。"""
        row = self.conn.execute(
            "SELECT handoff_id, conversation_id, reason, summary, status FROM handoff_record "
            "WHERE conversation_id=? AND status=? ORDER BY created_at DESC LIMIT 1",
            (conversation_id, STATUS_ACTIVE),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_rec(row)

    def request_handoff(self, conversation_id: str, reason: str, summary: str) -> dict:
        """入口/图外层统一申请：幂等复用 awaiting → 返回 handoff_requested（mode 仍 ai）。"""
        awaiting = self.get_awaiting(conversation_id)
        if awaiting is not None:
            return {"type": "handoff_requested", "data": awaiting}
        active = self.get_active(conversation_id)
        if active is not None:
            # Already in human lane via Takeover — idempotent tip, no new record.
            return {"type": "handoff_requested", "data": active}
        rec = self.create_handoff_request(conversation_id, reason, summary)
        return {"type": "handoff_requested", "data": rec}

    def make_interrupt_callback(self, conversation_id: str) -> Callable:
        """包装 on_interrupt：handoff_request → 同一挂起门控 + handoff_requested。"""

        async def on_interrupt(value: dict) -> dict | None:
            if isinstance(value, dict) and value.get("type") == "handoff_request":
                return self.request_handoff(
                    conversation_id,
                    value.get("reason", "unknown"),
                    value.get("summary", ""),
                )
            return None

        return on_interrupt
