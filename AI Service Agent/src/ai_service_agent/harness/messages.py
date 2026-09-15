import sqlite3

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage


MESSAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation_message (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    message_id TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
"""


class MessageStore:
    """会话消息唯一真相源（user / assistant 轮次）。

    state.messages 是每次 graph 调用时从本 Store 水合的工作视图（handoff-spec.md §5.2）；
    人工接管期间 Graph 不执行，消息只能写本 Store。
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.conn.execute(MESSAGE_SCHEMA)
        self._ensure_message_id_column()
        self.conn.commit()

    def _ensure_message_id_column(self) -> None:
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(conversation_message)").fetchall()]
        if "message_id" not in cols:
            self.conn.execute("ALTER TABLE conversation_message ADD COLUMN message_id TEXT")

    def append(self, conversation_id: str, role: str, content: str, message_id: str | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO conversation_message (conversation_id, role, content, message_id) VALUES (?, ?, ?, ?)",
            (conversation_id, role, content, message_id),
        )
        self.conn.commit()
        return cur.lastrowid

    def list(self, conversation_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, role, content, created_at, message_id FROM conversation_message "
            "WHERE conversation_id = ? ORDER BY id",
            (conversation_id,),
        ).fetchall()
        return [
            {"id": r[0], "role": r[1], "content": r[2], "created_at": r[3], "message_id": r[4]}
            for r in rows
        ]

    def hydrate(self, conversation_id: str) -> list[BaseMessage]:
        """水合为 langchain messages；稳定 id 使 add_messages 按 id 对账（handoff-spec.md §5.2）。"""
        messages: list[BaseMessage] = []
        for row in self.list(conversation_id):
            msg_id = row["message_id"] or f"store-{row['id']}"
            if row["role"] == "user":
                messages.append(HumanMessage(content=row["content"], id=msg_id))
            elif row["role"] == "assistant":
                messages.append(AIMessage(content=row["content"], id=msg_id))
        return messages
