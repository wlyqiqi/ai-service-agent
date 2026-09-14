import sqlite3
import uuid
from typing import Callable

from ai_service_agent.harness.session import ConversationStatusStore


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
        return {"handoff_id": handoff_id, "mode": "ai"}

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
