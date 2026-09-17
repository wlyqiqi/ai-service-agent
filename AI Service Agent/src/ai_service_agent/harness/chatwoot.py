"""Chatwoot 坐席端接入（Transport/harness 层，AGENTS.md §4）。

Chatwoot 只做坐席 UI/输入层；Message Store 保持唯一真相源（handoff-spec §5.2）。
本模块：映射表 + webhook 签名校验 + API 客户端。外部调用失败只记日志，不影响 Runtime 主链路。
"""

import hashlib
import hmac
import logging
import sqlite3

import httpx

logger = logging.getLogger("ai_service_agent")


MAPPING_SCHEMA = """
CREATE TABLE IF NOT EXISTS chatwoot_conversation (
    conversation_id TEXT PRIMARY KEY,
    chatwoot_conversation_id INTEGER NOT NULL UNIQUE,
    contact_id INTEGER,
    created_at TEXT DEFAULT (datetime('now'))
);
"""


class ChatwootMappingStore:
    """conversation_id（Runtime 侧）<-> chatwoot_conversation_id（Chatwoot 侧）映射。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.conn.execute(MAPPING_SCHEMA)
        self.conn.commit()

    def put(self, conversation_id: str, chatwoot_conversation_id: int, contact_id: int | None = None) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO chatwoot_conversation "
            "(conversation_id, chatwoot_conversation_id, contact_id) VALUES (?, ?, ?)",
            (conversation_id, chatwoot_conversation_id, contact_id),
        )
        self.conn.commit()

    def get(self, conversation_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT conversation_id, chatwoot_conversation_id, contact_id, created_at "
            "FROM chatwoot_conversation WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "conversation_id": row[0],
            "chatwoot_conversation_id": row[1],
            "contact_id": row[2],
            "created_at": row[3],
        }

    def find(self, chatwoot_conversation_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT conversation_id, chatwoot_conversation_id, contact_id, created_at "
            "FROM chatwoot_conversation WHERE chatwoot_conversation_id = ?",
            (chatwoot_conversation_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "conversation_id": row[0],
            "chatwoot_conversation_id": row[1],
            "contact_id": row[2],
            "created_at": row[3],
        }
