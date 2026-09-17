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


RUNTIME_MESSAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS chatwoot_runtime_message (
    chatwoot_message_id INTEGER PRIMARY KEY,
    conversation_id TEXT NOT NULL
);
"""


class ChatwootMappingStore:
    """conversation_id（Runtime 侧）<-> chatwoot_conversation_id（Chatwoot 侧）映射。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.conn.execute(MAPPING_SCHEMA)
        self.conn.execute(RUNTIME_MESSAGE_SCHEMA)
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

    def put_runtime_message(self, chatwoot_message_id: int, conversation_id: str) -> None:
        """登记 Runtime 自己创建的 Chatwoot 消息 id（webhook 回显去重，防重复落库）。"""
        self.conn.execute(
            "INSERT OR IGNORE INTO chatwoot_runtime_message (chatwoot_message_id, conversation_id) VALUES (?, ?)",
            (chatwoot_message_id, conversation_id),
        )
        self.conn.commit()

    def is_runtime_message(self, chatwoot_message_id: int) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM chatwoot_runtime_message WHERE chatwoot_message_id = ?",
            (chatwoot_message_id,),
        ).fetchone()
        return row is not None


def verify_chatwoot_signature(payload: bytes, signature: str | None, secret: str, timestamp: str | None) -> bool:
    """Chatwoot v4.x webhook 签名：X-Chatwoot-Signature: sha256=<hex>。

    v4.17.1 实测契约（Webhooks::Trigger#request_headers）：HMAC-SHA256(secret, "<ts>.<raw body>")，
    ts 来自 X-Chatwoot-Timestamp 头；非 sha256= 前缀 / 缺时间戳一律 False。
    """
    if not signature or not signature.startswith("sha256="):
        return False
    if not timestamp:
        return False
    msg = timestamp.encode() + b"." + payload
    expected = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature[len("sha256="):], expected)



class ChatwootClient:
    """Chatwoot API inbox 客户端（httpx）。调用方负责 try/except 降级。"""

    def __init__(
        self,
        base_url: str,
        account_id: int,
        inbox_id: int,
        api_token: str,
        mapping: ChatwootMappingStore,
        client: httpx.AsyncClient | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.account_id = account_id
        self.inbox_id = inbox_id
        self.api_token = api_token
        self.mapping = mapping
        self.client = client or httpx.AsyncClient()

    def _headers(self) -> dict:
        return {"api_access_token": self.api_token}

    async def _create_contact(self, name: str) -> int:
        r = await self.client.post(
            f"{self.base_url}/api/v1/accounts/{self.account_id}/contacts",
            headers=self._headers(),
            json={"inbox_id": self.inbox_id, "name": name},
        )
        r.raise_for_status()
        return r.json()["payload"]["contact"]["id"]  # v4.x: contact 实体在 payload.contact

    async def create_conversation(self, contact_name: str, first_message: str) -> tuple[int, int | None]:
        contact_id = await self._create_contact(contact_name)
        r = await self.client.post(
            f"{self.base_url}/api/v1/accounts/{self.account_id}/conversations",
            headers=self._headers(),
            json={
                "inbox_id": self.inbox_id,
                "contact_id": contact_id,
                "message": {"content": first_message},
            },
        )
        r.raise_for_status()
        data = r.json()
        first_message_id = (data.get("messages") or [{}])[0].get("id")
        return data["id"], first_message_id

    async def ensure_conversation(self, conversation_id: str, summary: str) -> int | None:
        rec = self.mapping.get(conversation_id)
        if rec is not None:
            return rec["chatwoot_conversation_id"]
        cw_id, first_message_id = await self.create_conversation(f"user-{conversation_id[:8]}", summary)
        if first_message_id is not None:
            # 先登记回显去重，再写映射：任何 webhook 到达时序都不会重复落库
            self.mapping.put_runtime_message(first_message_id, conversation_id)
        self.mapping.put(conversation_id, cw_id)
        return cw_id

    async def send_as_contact(self, conversation_id: str, content: str) -> None:
        rec = self.mapping.get(conversation_id)
        if rec is None:
            return  # 未建立 Chatwoot conversation：跳过，不阻塞 Runtime
        r = await self.client.post(
            f"{self.base_url}/api/v1/accounts/{self.account_id}/conversations/{rec['chatwoot_conversation_id']}/messages",
            headers=self._headers(),
            json={"content": content, "sender_type": "Contact", "message_type": "incoming"},  # v4.x: content 在顶层
        )
        r.raise_for_status()
        msg_id = r.json().get("id")
        if msg_id is not None:
            self.mapping.put_runtime_message(msg_id, conversation_id)
