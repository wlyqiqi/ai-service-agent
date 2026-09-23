import sqlite3
import uuid
from typing import Callable

from ai_service_agent.harness.session import ConversationStatusStore


HANDOFF_NOTICE = "好的，已为您转接人工客服，请稍候。人工客服会继续在这个对话里为您服务。"


SCHEMA = """
CREATE TABLE IF NOT EXISTS handoff_record (
    handoff_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    reason TEXT,
    summary TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT DEFAULT (datetime('now')),
    resolved_at TEXT
);
"""


class HandoffService:
    """外层（Conversation Runtime）接管：建 Record + 原子切 mode + resolve。

    子图只抛 interrupt(handoff_request)，不直接调用本服务（graph-spec.md §6.5）。
    """

    def __init__(self, conn: sqlite3.Connection, status: ConversationStatusStore):
        self.conn = conn
        self.status = status
        self.conn.execute(SCHEMA)
        self.conn.commit()

    def create_handoff(self, conversation_id: str, reason: str, summary: str) -> dict:
        """建 Handoff Record + 切 mode=human：同一事务（handoff-spec.md §1.2）。"""
        handoff_id = str(uuid.uuid4())
        with self.conn:  # sqlite 单连接事务块，退出时统一 commit
            self.conn.execute(
                "INSERT INTO handoff_record (handoff_id, conversation_id, reason, summary) VALUES (?, ?, ?, ?)",
                (handoff_id, conversation_id, reason, summary),
            )
            self.status.set_mode(conversation_id, "human", commit=False)
        return {"handoff_id": handoff_id, "conversation_id": conversation_id, "status": "active"}

    def resolve_handoff(self, handoff_id: str) -> dict:
        row = self.conn.execute(
            "SELECT conversation_id FROM handoff_record WHERE handoff_id = ?", (handoff_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"handoff not found: {handoff_id}")
        conversation_id = row[0]
        with self.conn:
            self.conn.execute(
                "UPDATE handoff_record SET status = 'resolved', resolved_at = datetime('now') WHERE handoff_id = ?",
                (handoff_id,),
            )
            self.status.set_mode(conversation_id, "ai", commit=False)
        return {"handoff_id": handoff_id, "conversation_id": conversation_id, "mode": "ai"}

    def get_by_conversation(self, conversation_id: str) -> dict | None:
        """该会话最新一条 handoff 记录（含 active/resolved），无则 None。"""
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

    def get_active(self, conversation_id: str) -> dict | None:
        """当前会话最新 active handoff（幂等复用；无则 None）。"""
        row = self.conn.execute(
            "SELECT handoff_id, conversation_id, reason, summary FROM handoff_record "
            "WHERE conversation_id=? AND status='active' ORDER BY created_at DESC LIMIT 1",
            (conversation_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "handoff_id": row[0],
            "conversation_id": row[1],
            "reason": row[2],
            "summary": row[3],
        }

    def request_handoff(self, conversation_id: str, reason: str, summary: str) -> dict:
        """入口层显式转人工统一入口（P0 D3）：幂等复用 active -> create_handoff -> 返回 handoff_created 事件。

        graph 动态路径经 make_interrupt_callback 复用 create_handoff；side effects
        （NOTICE 落库 / Chatwoot ensure / 事件发送）由 server 的 _emit_handoff 统一完成。
        """
        active = self.get_active(conversation_id)
        if active is not None:
            return {"type": "handoff_created", "data": active}
        rec = self.create_handoff(conversation_id, reason, summary)
        return {"type": "handoff_created", "data": rec}

    def make_interrupt_callback(self, conversation_id: str) -> Callable:
        """包装 on_interrupt：handoff_request -> create_handoff + 产出 handoff_created 事件。"""
        async def on_interrupt(value: dict) -> dict | None:
            if isinstance(value, dict) and value.get("type") == "handoff_request":
                rec = self.create_handoff(
                    conversation_id,
                    value.get("reason", "unknown"),
                    value.get("summary", ""),
                )
                return {"type": "handoff_created", "data": rec}
            return None

        return on_interrupt
