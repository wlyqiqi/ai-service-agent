import sqlite3
import uuid


SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation_status (
    conversation_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'ai',
    user_id TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
"""


class ConversationStatusStore:
    """轻量 conversation_status：conversation_id -> mode（两态，ai/human）。

    handoff-spec.md §2：判断必须轻量，不触 checkpointer。
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.conn.execute(SCHEMA)
        self.conn.commit()

    def create_session(self, user_id: str) -> dict:
        conversation_id = str(uuid.uuid4())
        thread_id = str(uuid.uuid4())
        # 显式写 'ai'，不依赖存储层隐式默认值（handoff-spec.md §1.1）
        self.conn.execute(
            "INSERT INTO conversation_status (conversation_id, thread_id, mode, user_id) VALUES (?, ?, 'ai', ?)",
            (conversation_id, thread_id, user_id),
        )
        self.conn.commit()
        return {"conversation_id": conversation_id, "thread_id": thread_id, "mode": "ai"}

    def get_mode(self, conversation_id: str) -> str:
        row = self.conn.execute(
            "SELECT mode FROM conversation_status WHERE conversation_id = ?", (conversation_id,)
        ).fetchone()
        # 两态判断，fail-open：缺失/意外值按 ai 放行（handoff-spec.md §1.1）
        return "human" if row and row[0] == "human" else "ai"

    def set_mode(self, conversation_id: str, mode: str, commit: bool = True) -> None:
        self.conn.execute(
            "UPDATE conversation_status SET mode = ? WHERE conversation_id = ?", (mode, conversation_id)
        )
        if commit:
            self.conn.commit()

    def get_thread_id(self, conversation_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT thread_id FROM conversation_status WHERE conversation_id = ?", (conversation_id,)
        ).fetchone()
        return row[0] if row else None
