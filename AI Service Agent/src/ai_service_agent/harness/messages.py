import sqlite3

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage


MESSAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation_message (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
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
        self.conn.commit()

    def append(self, conversation_id: str, role: str, content: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO conversation_message (conversation_id, role, content) VALUES (?, ?, ?)",
            (conversation_id, role, content),
        )
        self.conn.commit()
        return cur.lastrowid

    def list(self, conversation_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, role, content, created_at FROM conversation_message "
            "WHERE conversation_id = ? ORDER BY id",
            (conversation_id,),
        ).fetchall()
        return [
            {"id": r[0], "role": r[1], "content": r[2], "created_at": r[3]}
            for r in rows
        ]

    def hydrate(self, conversation_id: str) -> list[BaseMessage]:
        """水合为 langchain messages（state.messages 工作视图），按写入顺序。"""
        messages: list[BaseMessage] = []
        for row in self.list(conversation_id):
            if row["role"] == "user":
                messages.append(HumanMessage(content=row["content"]))
            elif row["role"] == "assistant":
                messages.append(AIMessage(content=row["content"]))
        return messages
