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

MESSAGE_ID_UNIQUE_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS ux_conversation_message_message_id
ON conversation_message(message_id)
WHERE message_id IS NOT NULL;
"""

MESSAGE_CONVERSATION_ID_INDEX = """
CREATE INDEX IF NOT EXISTS ix_conversation_message_conversation_id_id
ON conversation_message(conversation_id, id);
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
        self._ensure_unique_message_ids()
        self.conn.execute(MESSAGE_CONVERSATION_ID_INDEX)
        self.conn.commit()

    def _ensure_message_id_column(self) -> None:
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(conversation_message)").fetchall()]
        if "message_id" not in cols:
            self.conn.execute("ALTER TABLE conversation_message ADD COLUMN message_id TEXT")

    def _ensure_unique_message_ids(self) -> None:
        self.conn.execute(
            "DELETE FROM conversation_message "
            "WHERE message_id IS NOT NULL AND id NOT IN ("
            "  SELECT MAX(id) FROM conversation_message "
            "  WHERE message_id IS NOT NULL GROUP BY message_id"
            ")"
        )
        self.conn.execute(MESSAGE_ID_UNIQUE_INDEX)

    def get_by_message_id(self, message_id: str) -> dict | None:
        if not message_id:
            return None
        row = self.conn.execute(
            "SELECT id, conversation_id, role, content, message_id, created_at FROM conversation_message WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        if row is None:
            return None
        return {"id": row[0], "conversation_id": row[1], "role": row[2], "content": row[3], "message_id": row[4], "created_at": row[5]}

    def append_if_absent(self, message_id: str, conversation_id: str, role: str, content: str) -> int:
        if not message_id:
            raise ValueError("message_id is required")

        self.conn.execute(
            "INSERT OR IGNORE INTO conversation_message "
            "(conversation_id, role, content, message_id) VALUES (?, ?, ?, ?)",
            (conversation_id, role, content, message_id),
        )
        row = self.conn.execute(
            "SELECT id FROM conversation_message WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        self.conn.commit()
        assert row is not None
        return row[0]

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

    def get(self, conversation_id: str, message_row_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT id, conversation_id, role, content, message_id, created_at "
            "FROM conversation_message WHERE conversation_id = ? AND id = ?",
            (conversation_id, message_row_id),
        ).fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "conversation_id": row[1],
            "role": row[2],
            "content": row[3],
            "message_id": row[4],
            "created_at": row[5],
        }

    def hydrate(self, conversation_id: str) -> list[BaseMessage]:
        """水合为 langchain messages；稳定 id 使 add_messages 按 id 对账。"""
        rows = self.conn.execute(
            "SELECT id, role, content, message_id FROM conversation_message "
            "WHERE conversation_id = ? ORDER BY id",
            (conversation_id,),
        ).fetchall()
        return self._messages_from_rows(rows)

    def hydrate_recent_turns(
        self, conversation_id: str, *, previous_completed_turns: int = 2
    ) -> list[BaseMessage]:
        """新 thread 水合：当前消息 + 最近 N 个已完成用户轮次。

        以 user 消息作为轮次边界，避免按固定行数截断时丢掉同一轮内的坐席消息。
        """
        user_rows = self.conn.execute(
            "SELECT id FROM conversation_message "
            "WHERE conversation_id = ? AND role = 'user' ORDER BY id DESC LIMIT ?",
            (conversation_id, max(1, previous_completed_turns + 1)),
        ).fetchall()
        if not user_rows:
            return []
        start_id = user_rows[-1][0]
        rows = self.conn.execute(
            "SELECT id, role, content, message_id FROM conversation_message "
            "WHERE conversation_id = ? AND id >= ? ORDER BY id",
            (conversation_id, start_id),
        )
        return self._messages_from_rows(rows.fetchall())

    @staticmethod
    def _messages_from_rows(rows) -> list[BaseMessage]:
        messages: list[BaseMessage] = []
        for row in rows:
            msg_id = row[3] or f"store-{row[0]}"
            if row[1] == "user":
                messages.append(HumanMessage(content=row[2], id=msg_id))
            elif row[1] == "assistant":
                messages.append(AIMessage(content=row[2], id=msg_id))
        return messages
