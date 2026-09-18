# Chatwoot 坐席端接入 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 Chatwoot（已部署 v4.17.1，API inbox）接入为坐席工作台：用户转人工后消息同步到 Chatwoot、坐席回复经 webhook 写回 Message Store、用户侧轮询读取，且 Chatwoot 任何故障不影响 Runtime 主链路。

**Architecture:** Chatwoot 只做坐席 UI/输入层，Runtime 保持唯一权威（Message Store 唯一真相源，handoff-spec §5.2 不变）。用户接入路径不变（`POST /chat` SSE）；人工车道用户消息写 Message Store 后同步推送 Chatwoot（`sender_type=Contact`）；坐席回复经 `POST /webhooks/chatwoot`（HMAC-SHA256 签名校验 + `mode=human` 校验）写回 Message Store；用户侧 `GET /conversations/{id}/messages` 轮询读取。新增 SQLite 映射表 `chatwoot_conversation`（`conversation_id ↔ chatwoot_conversation_id`）。Chatwoot 调用失败只记日志，绝不阻塞/失败 Runtime 主链路。

**Tech Stack:** Python 3.14；LangGraph 1.2.10（现有 pinned）；FastAPI（Transport）；httpx 0.28.1（从 dev 移入主依赖）；SQLite（既有 conn）；pytest + TestClient；Chatwoot v4.17.1（API inbox：account_id=3, inbox_id=2）。无新外部依赖。

**Spec:** `docs/specs/handoff-spec.md`（§1 mode 两态、§5.2 Message Store 权威）、`docs/specs/event-spec.md`、`docs/specs/architecture.md`（§4 Transport 边界）；本计划只扩展 Transport/harness 层，不碰 LangGraph 图内逻辑。

## Global Constraints

- LangGraph 1.2.10 固定；禁止 `langgraph.prebuilt.create_react_agent`；图内逻辑本计划不改动。
- FastAPI 只做 Transport；业务编排留在图内；`conversation_mode` 轻量判断在入口层（handoff-spec §2）。
- `Message Store` 是唯一真相源；`state.messages` 是水合工作视图（handoff-spec §5.2）。
- `conversation_mode` 两态 `ai`/`human`；坐席回复只接受 `human` 车道（webhook 必须校验）。
- 外部调用失败不得影响 Runtime 主链路：Chatwoot 推送异常一律 try/except + 日志，不改变现有响应。
- 新增依赖登记 `pyproject.toml`（AGENTS.md §19）；httpx 0.28.1 已验证 Python 3.14 兼容。
- 测试：pytest；离线 fake 优先（httpx.MockTransport / FakeChatwoot），不依赖真实 Chatwoot / 模型 key。
- 提交在项目目录 `D:\AI_Project\AI Service Agent` 执行，git 根 `D:\AI_Project`；`git add` 只加明确列出的文件，禁止 `git add -A` / `git add .`。
- 沙箱拦截 git 写：commit 命令交用户执行，agent 不自行 commit。
- 测试命令前缀：`$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest ...`

---

### Task 1: Chatwoot Settings + httpx 主依赖

**Files:**
- Modify: `src/ai_service_agent/config.py`（Settings 类内追加字段）
- Modify: `pyproject.toml`（httpx 从 dev 移入 dependencies）
- Test: `tests/test_config.py`（追加两个测试函数）

**Interfaces:**
- Produces: `Settings.chatwoot_enabled: bool`、`Settings.chatwoot_base_url: str | None`、`Settings.chatwoot_account_id: int | None`、`Settings.chatwoot_inbox_id: int | None`、`Settings.chatwoot_api_token: str | None`、`Settings.chatwoot_webhook_secret: str | None`（env 名 `CHATWOOT_*`，经 `validation_alias` 对齐，风格与现有 `ARK_*` 一致）。

- [ ] **Step 1: 写失败测试**

在 `tests/test_config.py` 追加：

```python
def test_chatwoot_settings_defaults(monkeypatch):
    from ai_service_agent.config import Settings
    for k in ("CHATWOOT_ENABLED", "CHATWOOT_BASE_URL", "CHATWOOT_ACCOUNT_ID",
              "CHATWOOT_INBOX_ID", "CHATWOOT_API_TOKEN", "CHATWOOT_WEBHOOK_SECRET"):
        monkeypatch.delenv(k, raising=False)
    s = Settings(_env_file=None)
    assert s.chatwoot_enabled is False
    assert s.chatwoot_base_url is None
    assert s.chatwoot_account_id is None
    assert s.chatwoot_inbox_id is None
    assert s.chatwoot_api_token is None
    assert s.chatwoot_webhook_secret is None


def test_chatwoot_settings_alias(monkeypatch):
    from ai_service_agent.config import Settings
    monkeypatch.setenv("CHATWOOT_ENABLED", "true")
    monkeypatch.setenv("CHATWOOT_BASE_URL", "http://localhost:3000")
    monkeypatch.setenv("CHATWOOT_ACCOUNT_ID", "3")
    monkeypatch.setenv("CHATWOOT_INBOX_ID", "2")
    monkeypatch.setenv("CHATWOOT_API_TOKEN", "tok")
    monkeypatch.setenv("CHATWOOT_WEBHOOK_SECRET", "sec")
    s = Settings(_env_file=None)
    assert s.chatwoot_enabled is True
    assert s.chatwoot_base_url == "http://localhost:3000"
    assert s.chatwoot_account_id == 3
    assert s.chatwoot_inbox_id == 2
    assert s.chatwoot_api_token == "tok"
    assert s.chatwoot_webhook_secret == "sec"
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run --no-sync pytest tests/test_config.py -v`
Expected: FAIL，`AttributeError: 'Settings' object has no attribute 'chatwoot_enabled'`

- [ ] **Step 3: 实现**

在 `src/ai_service_agent/config.py` 的 `Settings` 类 `clarify_max_turns` 之后追加：

```python
    # Chatwoot 坐席端（API inbox；仅人工车道启用；未配置则 chatwoot_enabled=False）
    chatwoot_enabled: bool = False
    chatwoot_base_url: str | None = Field(default=None, validation_alias="CHATWOOT_BASE_URL")
    chatwoot_account_id: int | None = Field(default=None, validation_alias="CHATWOOT_ACCOUNT_ID")
    chatwoot_inbox_id: int | None = Field(default=None, validation_alias="CHATWOOT_INBOX_ID")
    chatwoot_api_token: str | None = Field(default=None, validation_alias="CHATWOOT_API_TOKEN")
    chatwoot_webhook_secret: str | None = Field(default=None, validation_alias="CHATWOOT_WEBHOOK_SECRET")
```

在 `pyproject.toml` 中把 `"httpx>=0.28.1",` 从 `[dependency-groups].dev` 移到 `[project].dependencies`（放在 `"fastapi>=0.141.1",` 之后）；dev 组只留 `"pytest>=9.1.1",`。

- [ ] **Step 4: 运行确认通过**

Run: `uv run --no-sync pytest tests/test_config.py -v`
Expected: PASS（含两个新测试与既有测试）

- [ ] **Step 5: Commit**

```bash
cd "D:\AI_Project\AI Service Agent"
git add src/ai_service_agent/config.py pyproject.toml tests/test_config.py
git commit -m "feat: Chatwoot Settings（CHATWOOT_* env alias）+ httpx 移入主依赖"
```

---

### Task 2: ChatwootMappingStore

**Files:**
- Create: `src/ai_service_agent/harness/chatwoot.py`
- Test: `tests/test_chatwoot.py`

**Interfaces:**
- Produces: `class ChatwootMappingStore(conn: sqlite3.Connection)`，方法：
  - `put(conversation_id: str, chatwoot_conversation_id: int, contact_id: int | None = None) -> None`
  - `get(conversation_id: str) -> dict | None`（返回 `{"conversation_id", "chatwoot_conversation_id", "contact_id", "created_at"}`）
  - `find(chatwoot_conversation_id: int) -> dict | None`（按 Chatwoot 侧 id 反查，webhook 用）

- [ ] **Step 1: 写失败测试**

创建 `tests/test_chatwoot.py`：

```python
import sqlite3

from ai_service_agent.harness.chatwoot import ChatwootMappingStore


def test_mapping_put_get(tmp_path):
    conn = sqlite3.connect(tmp_path / "m.db")
    m = ChatwootMappingStore(conn)
    m.put("c1", 101, contact_id=7)
    rec = m.get("c1")
    assert rec is not None
    assert rec["chatwoot_conversation_id"] == 101
    assert rec["contact_id"] == 7
    assert m.get("nope") is None


def test_mapping_find_reverse(tmp_path):
    conn = sqlite3.connect(tmp_path / "m.db")
    m = ChatwootMappingStore(conn)
    m.put("c1", 101)
    assert m.find(101)["conversation_id"] == "c1"
    assert m.find(999) is None
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run --no-sync pytest tests/test_chatwoot.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'ai_service_agent.harness.chatwoot'`

- [ ] **Step 3: 实现**

创建 `src/ai_service_agent/harness/chatwoot.py`：

```python
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
    chatwoot_conversation_id INTEGER NOT NULL,
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
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run --no-sync pytest tests/test_chatwoot.py -v`
Expected: PASS（2 个测试）

- [ ] **Step 5: Commit**

```bash
cd "D:\AI_Project\AI Service Agent"
git add src/ai_service_agent/harness/chatwoot.py tests/test_chatwoot.py
git commit -m "feat: ChatwootMappingStore（conversation_id <-> chatwoot conversation 映射表）"
```

---

### Task 3: Webhook 签名校验

**Files:**
- Modify: `src/ai_service_agent/harness/chatwoot.py`（追加函数）
- Test: `tests/test_chatwoot.py`（追加三个测试函数）

**Interfaces:**
- Produces: `verify_chatwoot_signature(payload: bytes, signature: str | None, secret: str) -> bool`——校验 `X-Chatwoot-Signature` 头（格式 `sha256=<hex>`，HMAC-SHA256(secret, raw body)），constant-time 比较，缺失/格式错误一律 `False`。

- [ ] **Step 1: 写失败测试**

在 `tests/test_chatwoot.py` 追加：

```python
import hashlib
import hmac

from ai_service_agent.harness.chatwoot import verify_chatwoot_signature


def _sig(payload: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def test_signature_valid():
    payload = b'{"event":"message_created"}'
    assert verify_chatwoot_signature(payload, _sig(payload, "sec"), "sec") is True


def test_signature_tampered_payload():
    payload = b'{"event":"message_created"}'
    assert verify_chatwoot_signature(b'{"event":"evil"}', _sig(payload, "sec"), "sec") is False


def test_signature_missing_or_bad_format():
    assert verify_chatwoot_signature(b"{}", None, "sec") is False
    assert verify_chatwoot_signature(b"{}", "deadbeef", "sec") is False
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run --no-sync pytest tests/test_chatwoot.py -v`
Expected: FAIL，`ImportError: cannot import name 'verify_chatwoot_signature'`

- [ ] **Step 3: 实现**

在 `src/ai_service_agent/harness/chatwoot.py` 中、`ChatwootMappingStore` 类之后追加：

```python
def verify_chatwoot_signature(payload: bytes, signature: str | None, secret: str) -> bool:
    """Chatwoot webhook 签名：X-Chatwoot-Signature: sha256=<hex>；HMAC-SHA256(secret, raw body)。"""
    if not signature or not signature.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature[len("sha256="):], expected)
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run --no-sync pytest tests/test_chatwoot.py -v`
Expected: PASS（5 个测试）

- [ ] **Step 5: Commit**

```bash
cd "D:\AI_Project\AI Service Agent"
git add src/ai_service_agent/harness/chatwoot.py tests/test_chatwoot.py
git commit -m "feat: Chatwoot webhook HMAC-SHA256 签名校验（constant-time）"
```

---

### Task 4: ChatwootClient（API 客户端）

**Files:**
- Modify: `src/ai_service_agent/harness/chatwoot.py`（追加类）
- Test: `tests/test_chatwoot.py`（追加）

**Interfaces:**
- Produces: `class ChatwootClient(base_url: str, account_id: int, inbox_id: int, api_token: str, mapping: ChatwootMappingStore, client: httpx.AsyncClient | None = None)`，方法：
  - `async ensure_conversation(conversation_id: str, summary: str) -> int | None`——映射有则返回既有 id；无则 `_create_contact` + `create_conversation` 并写映射；返回新 conversation id。
  - `async send_as_contact(conversation_id: str, content: str) -> None`——映射无则直接返回（不阻塞）；有则以 `sender_type=Contact, message_type=incoming` 推送。
  - HTTP 异常由调用方 try/except 处理（本类只 `raise_for_status`）。

- [ ] **Step 1: 写失败测试**

在 `tests/test_chatwoot.py` 追加：

```python
import pytest
from httpx import AsyncClient, MockTransport, Response

from ai_service_agent.harness.chatwoot import ChatwootClient, ChatwootMappingStore


def _client_with(handler, tmp_path) -> tuple[ChatwootClient, ChatwootMappingStore, list]:
    calls: list[dict] = []

    async def handler_wrapper(request):
        calls.append({"method": request.method, "url": str(request.url), "json": request.content})
        return handler(request)

    conn = sqlite3.connect(tmp_path / "m.db")
    mapping = ChatwootMappingStore(conn)
    http = AsyncClient(transport=MockTransport(handler_wrapper))
    cw = ChatwootClient("http://cw.test", 3, 2, "tok", mapping, client=http)
    return cw, mapping, calls


@pytest.mark.asyncio
async def test_ensure_conversation_creates_contact_then_conversation(tmp_path):
    def handler(request):
        if request.url.path.endswith("/contacts"):
            return Response(200, json={"payload": {"id": 7}})
        if request.url.path.endswith("/conversations"):
            return Response(200, json={"id": 555})
        return Response(404)

    cw, mapping, calls = _client_with(handler, tmp_path)
    cw_id = await cw.ensure_conversation("c1", "用户已转人工")
    assert cw_id == 555
    assert mapping.get("c1")["chatwoot_conversation_id"] == 555
    # 第二次 ensure 不再发 HTTP 请求
    cw_id2 = await cw.ensure_conversation("c1", "again")
    assert cw_id2 == 555
    assert len(calls) == 2  # contact + conversation，无第三次


@pytest.mark.asyncio
async def test_ensure_conversation_auth_header(tmp_path):
    def handler(request):
        assert request.headers["api_access_token"] == "tok"
        if request.url.path.endswith("/contacts"):
            return Response(200, json={"payload": {"id": 7}})
        return Response(200, json={"id": 555})

    cw, _, _ = _client_with(handler, tmp_path)
    await cw.ensure_conversation("c1", "s")
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run --no-sync pytest tests/test_chatwoot.py -v`
Expected: FAIL，`ImportError: cannot import name 'ChatwootClient'`。若 `pytest.mark.asyncio` 不可用（`pytest-asyncio` 未装），先在 `pyproject.toml` dev 组登记 `pytest-asyncio>=1.0.0` 并注明理由（异步 httpx 测试需要），再继续。

- [ ] **Step 3: 实现**

在 `src/ai_service_agent/harness/chatwoot.py` 中、`verify_chatwoot_signature` 之后追加：

```python
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
        return r.json()["payload"]["id"]

    async def create_conversation(self, contact_name: str, first_message: str) -> int:
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
        return r.json()["id"]

    async def ensure_conversation(self, conversation_id: str, summary: str) -> int | None:
        rec = self.mapping.get(conversation_id)
        if rec is not None:
            return rec["chatwoot_conversation_id"]
        cw_id = await self.create_conversation(f"user-{conversation_id[:8]}", summary)
        self.mapping.put(conversation_id, cw_id)
        return cw_id

    async def send_as_contact(self, conversation_id: str, content: str) -> None:
        rec = self.mapping.get(conversation_id)
        if rec is None:
            return  # 未建立 Chatwoot conversation：跳过，不阻塞 Runtime
        r = await self.client.post(
            f"{self.base_url}/api/v1/accounts/{self.account_id}/conversations/{rec['chatwoot_conversation_id']}/messages",
            headers=self._headers(),
            json={"message": {"content": content}, "sender_type": "Contact", "message_type": "incoming"},
        )
        r.raise_for_status()
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run --no-sync pytest tests/test_chatwoot.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd "D:\AI_Project\AI Service Agent"
git add src/ai_service_agent/harness/chatwoot.py tests/test_chatwoot.py pyproject.toml
git commit -m "feat: ChatwootClient（contact/conversation 创建 + ensure + 用户消息推送）"
```

---

### Task 5: Webhook 端点 POST /webhooks/chatwoot

**Files:**
- Modify: `src/ai_service_agent/harness/server.py`（`create_app` 内追加端点）
- Test: `tests/test_chatwoot_webhook.py`

**Interfaces:**
- Consumes: `ChatwootMappingStore.find`、`verify_chatwoot_signature`（Task 2/3）、`create_app(..., chatwoot_webhook_secret: str | None = None)`（新增参数，默认 None → 读 `settings.chatwoot_webhook_secret`）。
- Produces: `POST /webhooks/chatwoot`——签名失败 401；非 `message_created` / 非 outgoing / `private=true` / sender 非 Agent / 无 content / 未知 conversation / `mode != human` 一律返回 `{"ok": True}` 且不落库；Agent 回复写 `MessageStore.append(conversation_id, "assistant", content)` 后返回 `{"ok": True}`。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_chatwoot_webhook.py`：

```python
import hashlib
import hmac
import json
import sqlite3

from fastapi.testclient import TestClient

from ai_service_agent.harness.chatwoot import ChatwootMappingStore
from ai_service_agent.harness.messages import MessageStore
from ai_service_agent.harness.server import create_app
from ai_service_agent.harness.session import ConversationStatusStore


def _sig(payload: bytes, secret: str = "sec") -> str:
    return "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def _human_session(tmp_path) -> str:
    conn = sqlite3.connect(tmp_path / "s.db")
    status = ConversationStatusStore(conn)
    info = status.create_session("u1")
    status.set_mode(info["conversation_id"], "human")
    ChatwootMappingStore(conn).put(info["conversation_id"], 555)
    conn.close()
    return info["conversation_id"]


def test_webhook_bad_signature_401(tmp_path):
    client = TestClient(create_app(str(tmp_path / "s.db"), model="fake", chatwoot_webhook_secret="sec"))
    r = client.post("/webhooks/chatwoot", content=b'{"event":"message_created"}',
                    headers={"X-Chatwoot-Signature": "sha256=deadbeef"})
    assert r.status_code == 401


def test_webhook_agent_reply_persists(tmp_path):
    conv_id = _human_session(tmp_path)
    client = TestClient(create_app(str(tmp_path / "s.db"), model="fake", chatwoot_webhook_secret="sec"))
    body = {
        "event": "message_created",
        "message_type": "outgoing",
        "content": "您好，订单问题请提供订单号",
        "private": False,
        "sender": {"type": "Agent"},
        "conversation": {"id": 555},
    }
    raw = json.dumps(body).encode()
    r = client.post("/webhooks/chatwoot", content=raw,
                    headers={"X-Chatwoot-Signature": _sig(raw)})
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    rows = MessageStore(sqlite3.connect(tmp_path / "s.db")).list(conv_id)
    assert rows[-1]["role"] == "assistant"
    assert rows[-1]["content"] == "您好，订单问题请提供订单号"


def test_webhook_ignores_non_agent_and_private(tmp_path):
    conv_id = _human_session(tmp_path)
    client = TestClient(create_app(str(tmp_path / "s.db"), model="fake", chatwoot_webhook_secret="sec"))
    for body in [
        {"event": "message_created", "message_type": "incoming", "content": "hi",
         "private": False, "sender": {"type": "Contact"}, "conversation": {"id": 555}},
        {"event": "message_created", "message_type": "outgoing", "content": "内部备注",
         "private": True, "sender": {"type": "Agent"}, "conversation": {"id": 555}},
        {"event": "conversation.created", "message_type": "outgoing", "content": "x",
         "private": False, "sender": {"type": "Agent"}, "conversation": {"id": 555}},
    ]:
        raw = json.dumps(body).encode()
        r = client.post("/webhooks/chatwoot", content=raw, headers={"X-Chatwoot-Signature": _sig(raw)})
        assert r.status_code == 200
    rows = MessageStore(sqlite3.connect(tmp_path / "s.db")).list(conv_id)
    assert rows == []


def test_webhook_ignores_unknown_conversation_and_ai_mode(tmp_path):
    client = TestClient(create_app(str(tmp_path / "s.db"), model="fake", chatwoot_webhook_secret="sec"))
    body = {"event": "message_created", "message_type": "outgoing", "content": "hi",
            "private": False, "sender": {"type": "Agent"}, "conversation": {"id": 99999}}
    raw = json.dumps(body).encode()
    r = client.post("/webhooks/chatwoot", content=raw, headers={"X-Chatwoot-Signature": _sig(raw)})
    assert r.status_code == 200

    conn = sqlite3.connect(tmp_path / "s.db")
    status = ConversationStatusStore(conn)
    info = status.create_session("u1")
    ChatwootMappingStore(conn).put(info["conversation_id"], 777)
    conn.close()
    client2 = TestClient(create_app(str(tmp_path / "s.db"), model="fake", chatwoot_webhook_secret="sec"))
    body2 = {"event": "message_created", "message_type": "outgoing", "content": "ai 车道不应收到",
             "private": False, "sender": {"type": "Agent"}, "conversation": {"id": 777}}
    raw2 = json.dumps(body2).encode()
    r2 = client2.post("/webhooks/chatwoot", content=raw2, headers={"X-Chatwoot-Signature": _sig(raw2)})
    assert r2.status_code == 200
    rows = MessageStore(sqlite3.connect(tmp_path / "s.db")).list(info["conversation_id"])
    assert rows == []
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run --no-sync pytest tests/test_chatwoot_webhook.py -v`
Expected: FAIL（`404 Not Found` 端点不存在，或 `TypeError: create_app() got an unexpected keyword argument 'chatwoot_webhook_secret'`）

- [ ] **Step 3: 实现**

在 `src/ai_service_agent/harness/server.py`：

1. 文件头 import 追加：
```python
from fastapi import Request
from ai_service_agent.harness.chatwoot import ChatwootClient, ChatwootMappingStore, verify_chatwoot_signature
```

2. `create_app` 签名改为：
```python
def create_app(db_path: str, model=None, simulate_approval: bool | None = None,
               chatwoot: object | None = None, chatwoot_webhook_secret: str | None = None):
```
（`chatwoot` 可注入；`None` 表示禁用 Chatwoot 推送。`chatwoot_webhook_secret` 可注入，`None` 时读 settings。）

3. 在 `messages_store = MessageStore(conn)` 之后：
```python
    mapping = ChatwootMappingStore(conn)
    settings_chatwoot = get_settings()
    if chatwoot is None and settings_chatwoot.chatwoot_enabled and settings_chatwoot.chatwoot_base_url \
            and settings_chatwoot.chatwoot_account_id and settings_chatwoot.chatwoot_inbox_id \
            and settings_chatwoot.chatwoot_api_token:
        chatwoot = ChatwootClient(
            base_url=settings_chatwoot.chatwoot_base_url,
            account_id=settings_chatwoot.chatwoot_account_id,
            inbox_id=settings_chatwoot.chatwoot_inbox_id,
            api_token=settings_chatwoot.chatwoot_api_token,
            mapping=mapping,
        )
    webhook_secret = chatwoot_webhook_secret or settings_chatwoot.chatwoot_webhook_secret
```

4. 在 `@app.get("/approvals")` 之后（`return app` 之前）追加端点：
```python
    @app.post("/webhooks/chatwoot")
    async def chatwoot_webhook(request: Request):
        payload = await request.body()
        signature = request.headers.get("X-Chatwoot-Signature")
        if not webhook_secret or not verify_chatwoot_signature(payload, signature, webhook_secret):
            raise HTTPException(status_code=401, detail="invalid signature")
        try:
            data = json.loads(payload)
        except ValueError:
            return {"ok": True}
        if data.get("event") != "message_created":
            return {"ok": True}
        if data.get("message_type") != "outgoing" or data.get("private"):
            return {"ok": True}
        sender = data.get("sender") or {}
        if sender.get("type") != "Agent":
            return {"ok": True}
        cw_id = (data.get("conversation") or {}).get("id")
        content = data.get("content")
        if not cw_id or not content:
            return {"ok": True}
        rec = mapping.find(int(cw_id))
        if rec is None:
            return {"ok": True}
        conv_id = rec["conversation_id"]
        if status.get_mode(conv_id) != "human":
            # 坐席回复只接受人工车道（AI 车道由图负责）
            return {"ok": True}
        messages_store.append(conv_id, "assistant", content)
        return {"ok": True}
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run --no-sync pytest tests/test_chatwoot_webhook.py -v`
Expected: PASS（4 个测试）

- [ ] **Step 5: 回归旧测试**

Run: `uv run --no-sync pytest tests/test_server.py -v`
Expected: PASS（`create_app` 新增参数带默认值，旧调用不受影响）

- [ ] **Step 6: Commit**

```bash
cd "D:\AI_Project\AI Service Agent"
git add src/ai_service_agent/harness/server.py tests/test_chatwoot_webhook.py
git commit -m "feat: POST /webhooks/chatwoot——签名校验+事件过滤+mode 校验+落 Message Store"
```

---

### Task 6: 人工车道 Chatwoot 推送 + GET messages 端点

**Files:**
- Modify: `src/ai_service_agent/harness/server.py`（`chat` 改 `async def`、人工车道推送、`handoff_created` 时 ensure、新增 `GET /conversations/{id}/messages`）
- Test: `tests/test_server_chatwoot.py`

**Interfaces:**
- Consumes: `ChatwootClient.ensure_conversation(conversation_id, summary)`、`ChatwootClient.send_as_contact(conversation_id, content)`（Task 4）、`create_app(chatwoot=...)`（Task 5）。
- Produces: `GET /conversations/{conversation_id}/messages`——会话不存在 404；否则 `{"items": messages_store.list(conversation_id)}`。
- 行为：`handoff_created` 事件分支 `await chatwoot.ensure_conversation(conv_id, HANDOFF_NOTICE)`；人工车道用户消息写 Store 后 `await chatwoot.send_as_contact(conv_id, body.message)`；Chatwoot 调用一律 try/except 记日志，不改变现有 SSE 响应。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_server_chatwoot.py`：

```python
import sqlite3

from fastapi.testclient import TestClient

from ai_service_agent.harness.messages import MessageStore
from ai_service_agent.harness.server import create_app
from ai_service_agent.harness.session import ConversationStatusStore


class FakeChatwoot:
    """离线 fake：记录 ensure/send 调用；可配置抛错验证降级。"""

    def __init__(self, fail: bool = False):
        self.calls: list[tuple[str, str, str]] = []  # (method, conversation_id, content)
        self.fail = fail

    async def ensure_conversation(self, conversation_id: str, summary: str):
        if self.fail:
            raise RuntimeError("chatwoot down")
        self.calls.append(("ensure", conversation_id, summary))
        return 1

    async def send_as_contact(self, conversation_id: str, content: str):
        if self.fail:
            raise RuntimeError("chatwoot down")
        self.calls.append(("send", conversation_id, content))


def test_human_lane_pushes_to_chatwoot(tmp_path):
    conn = sqlite3.connect(tmp_path / "s.db")
    status = ConversationStatusStore(conn)
    info = status.create_session("u1")
    status.set_mode(info["conversation_id"], "human")
    conn.close()

    cw = FakeChatwoot()
    client = TestClient(create_app(str(tmp_path / "s.db"), model="fake", chatwoot=cw))
    with client.stream("POST", "/chat", json={"session_id": info["conversation_id"], "message": "在吗"}) as r:
        text = "".join(r.iter_text())
    assert '"done"' in text
    assert ("send", info["conversation_id"], "在吗") in cw.calls
    rows = MessageStore(sqlite3.connect(tmp_path / "s.db")).list(info["conversation_id"])
    assert rows[0]["role"] == "user" and rows[0]["content"] == "在吗"


def test_chatwoot_failure_degrades_human_lane(tmp_path):
    conn = sqlite3.connect(tmp_path / "s.db")
    status = ConversationStatusStore(conn)
    info = status.create_session("u1")
    status.set_mode(info["conversation_id"], "human")
    conn.close()

    cw = FakeChatwoot(fail=True)
    client = TestClient(create_app(str(tmp_path / "s.db"), model="fake", chatwoot=cw))
    with client.stream("POST", "/chat", json={"session_id": info["conversation_id"], "message": "在吗"}) as r:
        text = "".join(r.iter_text())
    assert r.status_code == 200
    assert '"done"' in text  # Chatwoot 故障不影响人工车道主链路


def test_get_messages_returns_store_and_404(tmp_path):
    conn = sqlite3.connect(tmp_path / "s.db")
    status = ConversationStatusStore(conn)
    info = status.create_session("u1")
    MessageStore(conn).append(info["conversation_id"], "user", "你好")
    conn.close()

    client = TestClient(create_app(str(tmp_path / "s.db"), model="fake"))
    r = client.get(f"/conversations/{info['conversation_id']}/messages")
    assert r.status_code == 200
    assert r.json()["items"][0]["content"] == "你好"
    r2 = client.get("/conversations/nope/messages")
    assert r2.status_code == 404
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run --no-sync pytest tests/test_server_chatwoot.py -v`
Expected: FAIL（人工车道未推送 → `("send", ...) in cw.calls` 失败；`GET /conversations/...` 404）

- [ ] **Step 3: 实现**

在 `src/ai_service_agent/harness/server.py`：

1. `chat` 改为 `async def chat(body: ChatBody):`。
2. 人工车道分支改为：
```python
        if status.get_mode(conv_id) == "human":
            # 人工车道：写 Message Store（唯一真相源）+ 同步推送 Chatwoot（失败只记日志）
            messages_store.append(conv_id, "user", body.message)
            messages_store.append(conv_id, "assistant", "已转人工，坐席将尽快回复。")
            if chatwoot is not None:
                try:
                    await chatwoot.send_as_contact(conv_id, body.message)
                except Exception:
                    logger.exception("chatwoot send failed: conv=%s", conv_id)

            def human_stream():
                yield {"type": "done", "data": {"final": "已转人工，坐席将尽快回复。", "message_id": None}}

            return _sse_response(human_stream())
```
3. `handoff_created` 事件分支（在 `if ev["type"] == "handoff_created":` 内，`yield ev` 之前）追加：
```python
                            if chatwoot is not None:
                                try:
                                    await chatwoot.ensure_conversation(conv_id, HANDOFF_NOTICE)
                                except Exception:
                                    logger.exception("chatwoot ensure failed: conv=%s", conv_id)
```
4. 在 `@app.post("/handoffs/{handoff_id}/resolve")` 之前追加端点：
```python
    @app.get("/conversations/{conversation_id}/messages")
    def get_messages(conversation_id: str):
        if status.get_thread_id(conversation_id) is None:
            raise HTTPException(status_code=404, detail="conversation not found")
        return {"items": messages_store.list(conversation_id)}
```
5. `chat` 内部 `ai_stream` 已是 async generator，`async def chat` 直接返回 `_sse_response(ai_stream())` 不变。

- [ ] **Step 4: 运行确认通过**

Run: `uv run --no-sync pytest tests/test_server_chatwoot.py tests/test_server.py -v`
Expected: PASS（新旧测试全过）

- [ ] **Step 5: Commit**

```bash
cd "D:\AI_Project\AI Service Agent"
git add src/ai_service_agent/harness/server.py tests/test_server_chatwoot.py
git commit -m "feat: 人工车道 Chatwoot 推送 + handoff 时 ensure conversation + GET messages 轮询端点"
```

---

### Task 7: 回归 + 真机联调

**Files:**
- Modify: `.env`（本地配置，gitignore 不提交）
- Modify: `docs/real-model-smoke.md`（追加 Chatwoot 联调记录段）
- Test: 全量 `pytest` + `langgraph validate`

**Interfaces:**
- Consumes: Task 1–6 全部产出。

- [ ] **Step 1: 全量回归**

Run:
```powershell
$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest -q
```
Expected: 全量 PASS（既有 153 + 新增 Chatwoot 测试）
Run: `$env:PYTHONPATH="D:\AI_Project\AI Service Agent\src"; langgraph validate`
Expected: valid

- [ ] **Step 2: 配置 .env（本地，不提交）**

在 `D:\AI_Project\AI Service Agent\.env` 追加（值来自已实测确认的部署参数）：
```
CHATWOOT_ENABLED=true
CHATWOOT_BASE_URL=http://localhost:3000
CHATWOOT_ACCOUNT_ID=3
CHATWOOT_INBOX_ID=2
CHATWOOT_API_TOKEN=<eQXX7UH9u43irU1P3BzVS2Lq>
CHATWOOT_WEBHOOK_SECRET=<Nx2mbVpdLZSh5LdvwXtkoFFS>
```

- [ ] **Step 3: 真机联调（手动步骤 + 断言清单）**

前置：Chatwoot 容器运行中；Runtime 启动：
```powershell
cd "D:\AI_Project\AI Service Agent"
$env:PYTHONPATH="D:\AI_Project\AI Service Agent\src"; uv run --no-sync python -m ai_service_agent.main
```
（监听 `127.0.0.1:8000`。Webhook URL 需在 Chatwoot 后台配置为 `http://host.docker.internal:8000/webhooks/chatwoot`，若 Chatwoot 容器无法解析 `host.docker.internal`，改用宿主机局域网 IP。）

联调步骤：
1. `POST /sessions` 建会话 → 记 `session_id`。
2. `POST /chat` 发"转人工" → SSE 应出现 `handoff_created`；Chatwoot 后台 inbox「澄元AI客服」应出现新 conversation（首条消息为转人工提示）。
3. `POST /chat` 发"在吗"（`mode=human`）→ Chatwoot 后台该 conversation 出现"在吗"（Contact 消息）。
4. Chatwoot 后台以坐席身份回复 → 断言：Runtime 日志无 webhook 401；`GET /conversations/{session_id}/messages` 返回的 `items` 末尾含坐席回复（role=assistant）。
5. `POST /handoffs/{handoff_id}/resolve` → `mode=ai`；再 `POST /chat` 发正常问题 → 走 AI 车道（SSE 出现 `done`）。
6. 异常验证：停掉 Runtime 后坐席回复 → 重启 Runtime 后 webhook 补收（若 Chatwoot 重试未生效，登记为已知限制，不阻塞）。

- [ ] **Step 4: 记录联调结果**

在 `docs/real-model-smoke.md` 追加"§N. Chatwoot 坐席端联调（2026-09-17 实测）"，逐项记录上述步骤 PASS/FAIL 与 SSE 事件顺序。

- [ ] **Step 5: Commit**

```bash
cd "D:\AI_Project\AI Service Agent"
git add docs/real-model-smoke.md
git commit -m "docs: Chatwoot 坐席端真机联调记录"
```

---

## Self-Review 结论

- **Spec 覆盖**：handoff-spec §1.1/§1.2（mode 两态、人工车道只写 Store）→ Task 6；§5.2（Store 权威）→ 全程；event-spec（无新增事件类型，复用 handoff_created/done）→ Task 6；architecture §4（Transport 边界）→ 全部端点均在 server.py。
- **未扩范围**：不改 LangGraph 图内逻辑、不改 Router/Guard/Policy、不建用户侧前端、不引入 Chatwoot widget。
- **已知登记项（本计划不实现）**：坐席上下文摘要增强（Chatwoot 首条消息目前为转人工提示，非完整历史）；webhook 失败重试/补偿；人工消息实时 SSE 推送（当前用轮询）；contact 身份 token（`inbox_identifier` / `user_identity`）暂不使用。
- **占位符扫描**：无 TBD/TODO；所有代码步骤含完整代码。
- **类型一致性**：`ChatwootMappingStore.put/get/find`、`ChatwootClient.ensure_conversation/send_as_contact`、`verify_chatwoot_signature`、`create_app(chatwoot=..., chatwoot_webhook_secret=...)` 在各任务间签名一致。
