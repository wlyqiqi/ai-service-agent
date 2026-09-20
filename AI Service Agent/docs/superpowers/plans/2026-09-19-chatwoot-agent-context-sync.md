# Chatwoot Agent Context Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让人工坐席在 Chatwoot 中的公开回复进入 Message Store，并在 Resolve 后成为 LangGraph AI 上下文的一部分。

**Architecture:** Message Store 继续作为会话消息唯一真相源；Chatwoot webhook 与 Resolve 前同步都只向其幂等写入 `assistant` 消息。LangGraph 不在人工模式中实时执行，而是在 Resolve 或下一轮用户消息时通过 `MessageStore.hydrate()` 获得完整上下文。

**Tech Stack:** Python 3.14, FastAPI, SQLite, LangGraph, Chatwoot Application API v1, pytest。

**Spec:** `docs/specs/handoff-spec.md` §5.2（Message Store 是唯一真相源）、`docs/specs/handoff-spec.md` §1.3（Resolve 后主动 resume）。

## Global Constraints

- 不新增第三方依赖。
- `conversation_message.message_id` 为非空时必须全局唯一。
- Chatwoot 坐席消息必须同时满足：`message_type == outgoing/1`、`private is False`、`sender.type == "user"`、`content` 非空。
- 私密备注、系统事件、模板消息、bot 消息不得进入 AI 历史。
- Chatwoot 幂等消息 ID 格式固定为 `chatwoot-{Chatwoot message id}`。
- Resolve 前必须先同步 Chatwoot 可见坐席消息，再 hydrate 并 resume 被中断图；Chatwoot 同步失败只记日志，不阻塞 Resolve。
- 人工车道每次用户发言只写入一条 `user` 消息，不得重复写入“已转人工”占位回复。
- 测试数据库必须使用 `tmp_path` 或运行期隔离数据库，不得写入 `data/mock_ecommerce.db`。

---

### Task 1: Message Store 通用幂等写入

**Files:**
- Modify: `src/ai_service_agent/harness/messages.py`
- Test: `tests/test_messages.py`

**Interfaces:**
- Consumes: 现有 `MessageStore.append(conversation_id, role, content, message_id=None) -> int`。
- Produces: `MessageStore.append_if_absent(message_id: str, conversation_id: str, role: str, content: str) -> int`；返回已存在或新插入行的 `id`。

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_messages.py`:

```python
def test_append_if_absent_is_idempotent(tmp_path):
    store, conn = _store(tmp_path)
    first = store.append_if_absent("chatwoot-502", "c1", "assistant", "您好，已处理")
    second = store.append_if_absent("chatwoot-502", "c1", "assistant", "您好，已处理")
    rows = store.list("c1")

    assert first == second
    assert len(rows) == 1
    assert rows[0]["message_id"] == "chatwoot-502"
    assert rows[0]["content"] == "您好，已处理"
    conn.close()


def test_append_if_absent_rejects_empty_message_id(tmp_path):
    store, conn = _store(tmp_path)

    import pytest

    with pytest.raises(ValueError, match="message_id is required"):
        store.append_if_absent("", "c1", "assistant", "hello")

    conn.close()


def test_unique_message_id_migrates_existing_duplicates(tmp_path):
    conn = sqlite3.connect(tmp_path / "dup.db")
    conn.execute(
        "CREATE TABLE conversation_message (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "conversation_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, "
        "message_id TEXT, created_at TEXT DEFAULT (datetime('now')))"
    )
    conn.execute(
        "INSERT INTO conversation_message (conversation_id, role, content, message_id) "
        "VALUES ('c1', 'assistant', 'old-1', 'chatwoot-1')"
    )
    conn.execute(
        "INSERT INTO conversation_message (conversation_id, role, content, message_id) "
        "VALUES ('c1', 'assistant', 'old-2', 'chatwoot-1')"
    )
    conn.commit()

    store = MessageStore(conn)
    third = store.append_if_absent("chatwoot-1", "c1", "assistant", "old-2")
    rows = store.list("c1")

    assert len(rows) == 1
    assert rows[0]["id"] == third
    assert rows[0]["content"] == "old-2"
    conn.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --no-sync pytest tests/test_messages.py -q`
Expected: FAIL with `AttributeError: 'MessageStore' object has no attribute 'append_if_absent'` 或唯一索引相关失败。

- [ ] **Step 3: Implement generic idempotency**

Modify `src/ai_service_agent/harness/messages.py`:

```python
MESSAGE_ID_UNIQUE_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS ux_conversation_message_message_id
ON conversation_message(message_id)
WHERE message_id IS NOT NULL;
"""
```

Change `MessageStore.__init__` to:

```python
def __init__(self, conn: sqlite3.Connection):
    self.conn = conn
    self.conn.execute(MESSAGE_SCHEMA)
    self._ensure_message_id_column()
    self._ensure_unique_message_ids()
    self.conn.commit()
```

Add the migration and generic write method after `_ensure_message_id_column`:

```python
def _ensure_unique_message_ids(self) -> None:
    self.conn.execute(
        "DELETE FROM conversation_message "
        "WHERE message_id IS NOT NULL AND id NOT IN ("
        "  SELECT MAX(id) FROM conversation_message "
        "  WHERE message_id IS NOT NULL GROUP BY message_id"
        ")"
    )
    self.conn.execute(MESSAGE_ID_UNIQUE_INDEX)
```

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --no-sync pytest tests/test_messages.py -q`
Expected: PASS。

- [ ] **Step 5: Commit**

```powershell
git add src/ai_service_agent/harness/messages.py tests/test_messages.py
git commit -m "feat: add idempotent message store writes"
```

### Task 2: Chatwoot 坐席消息契约与列表 API

**Files:**
- Modify: `src/ai_service_agent/harness/chatwoot.py`
- Test: `tests/test_chatwoot.py`

**Interfaces:**
- Produces: `is_agent_message(message: dict) -> bool`。
- Produces: `chatwoot_message_id(message: dict) -> str`，返回 `chatwoot-{message["id"]}`。
- Produces: `ChatwootClient.list_messages(conversation_id: str) -> list[dict]`。

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_chatwoot.py`:

```python
def test_agent_message_contract():
    from ai_service_agent.harness.chatwoot import is_agent_message

    assert is_agent_message({
        "id": 502,
        "content": "坐席回复",
        "message_type": "outgoing",
        "private": False,
        "sender": {"type": "user"},
    })
    assert is_agent_message({
        "id": 503,
        "content": "坐席回复",
        "message_type": 1,
        "private": False,
        "sender": {"type": "user"},
    })
    assert not is_agent_message({
        "id": 504,
        "content": "私密备注",
        "message_type": 1,
        "private": True,
        "sender": {"type": "user"},
    })
    assert not is_agent_message({
        "id": 505,
        "content": "系统事件",
        "message_type": 2,
        "private": False,
        "sender": {"type": "user"},
    })
    assert not is_agent_message({
        "id": 506,
        "content": "bot 回复",
        "message_type": 1,
        "private": False,
        "sender": {"type": "agent_bot"},
    })
    assert not is_agent_message({
        "id": 507,
        "content": None,
        "message_type": 1,
        "private": False,
        "sender": {"type": "user"},
    })


def test_chatwoot_message_id_format():
    from ai_service_agent.harness.chatwoot import chatwoot_message_id

    assert chatwoot_message_id({"id": 502}) == "chatwoot-502"


@pytest.mark.asyncio
async def test_list_messages_returns_application_api_payload(tmp_path):
    cw, mapping, calls = _client_with(
        lambda request: Response(200, json={"payload": [
            {"id": 502, "content": "坐席回复", "message_type": 1,
             "private": False, "sender": {"type": "user"}}
        ]}),
        tmp_path,
    )
    mapping.put("c1", 555)

    messages = await cw.list_messages("c1")

    assert [m["id"] for m in messages] == [502]
    assert len(calls) == 1
    assert calls[0]["method"] == "GET"
    assert calls[0]["url"].endswith("/accounts/3/conversations/555/messages")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --no-sync pytest tests/test_chatwoot.py -q`
Expected: FAIL with `ImportError` 或 `AttributeError: 'ChatwootClient' object has no attribute 'list_messages'`。

- [ ] **Step 3: Implement the Chatwoot contract**

Modify `src/ai_service_agent/harness/chatwoot.py` above `ChatwootMappingStore`:

```python
def is_agent_message(message: dict) -> bool:
    """只接受人工坐席发送的公开文本回复；拒绝备注、系统事件、模板与 bot。"""
    sender = message.get("sender") or {}
    content = message.get("content")
    return (
        message.get("message_type") in ("outgoing", 1)
        and message.get("private", False) is False
        and sender.get("type") == "user"
        and isinstance(content, str)
        and bool(content.strip())
    )


def chatwoot_message_id(message: dict) -> str:
    return f"chatwoot-{int(message['id'])}"
```

Add to `ChatwootClient` before `send_as_contact`:

```python
async def list_messages(self, conversation_id: str) -> list[dict]:
    """Application API：读取已映射 Chatwoot 会话的全部消息。"""
    rec = self.mapping.get(conversation_id)
    if rec is None:
        return []

    chatwoot_conversation_id = rec["chatwoot_conversation_id"]
    r = await self.client.get(
        f"{self.base_url}/api/v1/accounts/{self.account_id}/conversations/"
        f"{chatwoot_conversation_id}/messages",
        headers=self._headers(),
    )
    r.raise_for_status()
    return r.json().get("payload", [])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --no-sync pytest tests/test_chatwoot.py -q`
Expected: PASS。

- [ ] **Step 5: Commit**

```powershell
git add src/ai_service_agent/harness/chatwoot.py tests/test_chatwoot.py
git commit -m "feat: define chatwoot agent message contract"
```

### Task 3: Webhook 与人工车道接入幂等 Store

**Files:**
- Modify: `src/ai_service_agent/harness/server.py`
- Modify: `tests/test_server.py`
- Modify: `tests/test_e2e_human_lane.py`

**Interfaces:**
- Consumes: `MessageStore.append_if_absent(message_id, conversation_id, role, content)`。
- Consumes: `is_agent_message(message: dict) -> bool` 和 `chatwoot_message_id(message: dict) -> str`。
- Produces: Chatwoot webhook 将坐席公开回复写入 `assistant`，稳定 ID 为 `chatwoot-{id}`。

- [ ] **Step 1: Write the failing tests**

In `tests/test_server.py`, replace the assertion in `test_human_lane_after_handoff`:

```python
assert [r["role"] for r in rows] == ["user"]
```

Append these tests:

```python
def test_chatwoot_webhook_agent_message_is_idempotent(tmp_path):
    import hashlib
    import hmac
    import json

    from ai_service_agent.harness.chatwoot import ChatwootMappingStore

    db = str(tmp_path / "cw.db")
    client = TestClient(create_app(db, model="fake", chatwoot_webhook_secret="sec"))
    session_id = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]

    conn = sqlite3.connect(db)
    ChatwootMappingStore(conn).put(session_id, 111)
    ConversationStatusStore(conn).set_mode(session_id, "human")
    conn.close()

    payload = {
        "event": "message_created",
        "id": 502,
        "content": "您好，已加急处理",
        "message_type": "outgoing",
        "private": False,
        "sender": {"type": "user"},
        "conversation": {"id": 111},
    }
    raw = json.dumps(payload).encode()
    timestamp = "1700000000"
    signature = "sha256=" + hmac.new(
        b"sec", timestamp.encode() + b"." + raw, hashlib.sha256
    ).hexdigest()
    headers = {"X-Chatwoot-Signature": signature, "X-Chatwoot-Timestamp": timestamp}

    assert client.post("/webhooks/chatwoot", content=raw, headers=headers).status_code == 200
    assert client.post("/webhooks/chatwoot", content=raw, headers=headers).status_code == 200

    rows = MessageStore(sqlite3.connect(db)).list(session_id)
    assert len(rows) == 1
    assert rows[0]["role"] == "assistant"
    assert rows[0]["message_id"] == "chatwoot-502"


def test_chatwoot_webhook_rejects_private_note(tmp_path):
    import hashlib
    import hmac
    import json

    from ai_service_agent.harness.chatwoot import ChatwootMappingStore

    db = str(tmp_path / "cw-private.db")
    client = TestClient(create_app(db, model="fake", chatwoot_webhook_secret="sec"))
    session_id = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]

    conn = sqlite3.connect(db)
    ChatwootMappingStore(conn).put(session_id, 111)
    ConversationStatusStore(conn).set_mode(session_id, "human")
    conn.close()

    payload = {
        "event": "message_created",
        "id": 503,
        "content": "内部备注",
        "message_type": "outgoing",
        "private": True,
        "sender": {"type": "user"},
        "conversation": {"id": 111},
    }
    raw = json.dumps(payload).encode()
    timestamp = "1700000000"
    signature = "sha256=" + hmac.new(
        b"sec", timestamp.encode() + b"." + raw, hashlib.sha256
    ).hexdigest()

    response = client.post(
        "/webhooks/chatwoot",
        content=raw,
        headers={"X-Chatwoot-Signature": signature, "X-Chatwoot-Timestamp": timestamp},
    )

    assert response.status_code == 200
    assert MessageStore(sqlite3.connect(db)).list(session_id) == []
```

In `tests/test_e2e_human_lane.py`, update Step 3 and Step 5 assertions:

```python
rows3 = _rows(db, sid)
assert any(m["role"] == "user" and m["content"] == "在吗" for m in rows3)
assert sum(m["content"] == "已转人工，坐席将尽快回复。" for m in rows3) == 1
```

```python
contents = [m["content"] for m in data["items"]]
assert contents == [
    "我要转人工",
    HANDOFF_NOTICE,
    "在吗",
    "您好，已为您加急处理",
]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --no-sync pytest tests/test_server.py::test_human_lane_after_handoff tests/test_e2e_human_lane.py tests/test_server.py::test_chatwoot_webhook_agent_message_is_idempotent -q`
Expected: FAIL，重复提示与缺失 `chatwoot-502` 相关断言失败。

- [ ] **Step 3: Modify Runtime**

Change the Chatwoot import in `src/ai_service_agent/harness/server.py`:

```python
from ai_service_agent.harness.chatwoot import (
    ChatwootClient,
    ChatwootMappingStore,
    chatwoot_message_id,
    is_agent_message,
    verify_chatwoot_signature,
)
```

In the `human` branch of `/chat`, delete:

```python
messages_store.append(conv_id, "assistant", "已转人工，坐席将尽快回复。")
```

Replace the webhook acceptance block after signature validation with:

```python
try:
    data = json.loads(payload)
except ValueError:
    return {"ok": True}

if data.get("event") != "message_created" or not is_agent_message(data):
    return {"ok": True}

cw_id = (data.get("conversation") or {}).get("id")
if not cw_id:
    return {"ok": True}

rec = mapping.find(int(cw_id))
if rec is None:
    return {"ok": True}

conv_id = rec["conversation_id"]
if status.get_mode(conv_id) != "human":
    return {"ok": True}

msg_id = data.get("id")
if msg_id is not None and mapping.is_runtime_message(int(msg_id)):
    return {"ok": True}

messages_store.append_if_absent(
    chatwoot_message_id(data),
    conv_id,
    "assistant",
    data["content"],
)
return {"ok": True}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --no-sync pytest tests/test_messages.py tests/test_chatwoot.py tests/test_server.py tests/test_e2e_human_lane.py -q`
Expected: PASS。

- [ ] **Step 5: Commit**

```powershell
git add src/ai_service_agent/harness/server.py tests/test_server.py tests/test_e2e_human_lane.py
git commit -m "fix: persist chatwoot agent replies idempotently"
```

### Task 4: Resolve 前同步人工上下文

**Files:**
- Modify: `src/ai_service_agent/harness/handoff.py`
- Modify: `src/ai_service_agent/harness/server.py`
- Test: `tests/test_e2e_human_lane.py`

**Interfaces:**
- Consumes: `ChatwootClient.list_messages(conversation_id) -> list[dict]`。
- Consumes: `is_agent_message(message: dict) -> bool`、`chatwoot_message_id(message: dict) -> str`、`MessageStore.append_if_absent(...)`。
- Produces: `HandoffService.get(handoff_id: str) -> dict | None`。

- [ ] **Step 1: Write the failing test**

Add this fake model to `tests/test_e2e_human_lane.py` after `HandoffE2EFakeModel`:

```python
class AgentHistoryFakeModel(ToolCallingFakeModel):
    """路由后统计 AI 历史中来自坐席的标记消息，验证 resolve 时上下文已水合。"""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        system = next((m.content for m in messages if getattr(m, "type", "") == "system"), "")
        if "路由器" in system:
            return ChatResult(generations=[ChatGeneration(
                message=AIMessage(content='{"capability":"faq","risk_level":"read","confidence":0.9}')
            )])

        agent_messages = [
            m for m in messages
            if getattr(m, "type", "") == "ai" and "坐席已处理" in (m.content or "")
        ]
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=f"agent-history-{len(agent_messages)}"))])
```

Change the GET branch in `_chatwoot_client.handler` so the same path can serve GET and POST:

```python
if request.method == "GET" and "/conversations/111/messages" in request.url.path:
    return Response(200, json={"payload": [
        {"id": 502, "content": "坐席已处理地址", "message_type": 1,
         "private": False, "sender": {"type": "user"}},
        {"id": 503, "content": "坐席已处理退款", "message_type": 1,
         "private": False, "sender": {"type": "user"}},
    ]})
if request.method == "POST" and "/conversations/111/messages" in request.url.path:
    return Response(200, json={"id": 501, "content": "x"})
```

Append the test:

```python
def test_resolve_syncs_chatwoot_agent_messages_into_ai_history(tmp_path):
    db = str(tmp_path / "sync.db")
    calls: list = []
    cw = _chatwoot_client(db, calls)
    model = AgentHistoryFakeModel(responses=[])
    client = TestClient(create_app(db, model=model, chatwoot=cw, chatwoot_webhook_secret="sec"))

    session_id = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    with client.stream("POST", "/chat", json={"session_id": session_id, "message": "我要转人工"}) as r:
        events = parse_sse("".join(r.iter_text()))
    handoff_id = next(e["data"]["handoff_id"] for e in events if e["type"] == "handoff_created")

    assert client.post(f"/handoffs/{handoff_id}/resolve").status_code == 200

    rows = _rows(db, session_id)
    agent_rows = [row for row in rows if row["message_id"] in {"chatwoot-502", "chatwoot-503"}]
    assert [row["content"] for row in agent_rows] == ["坐席已处理地址", "坐席已处理退款"]

    with client.stream("POST", "/chat", json={"session_id": session_id, "message": "你好"}) as r:
        text = "".join(r.iter_text())

    assert "agent-history-2" in text
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --no-sync pytest tests/test_e2e_human_lane.py::test_resolve_syncs_chatwoot_agent_messages_into_ai_history -q`
Expected: FAIL，找不到 `chatwoot-502` / `chatwoot-503` 或模型返回不是 `agent-history-2`。

- [ ] **Step 3: Add Handoff lookup and Resolve sync**

Add to `HandoffService` after `get_by_conversation`:

```python
def get(self, handoff_id: str) -> dict | None:
    row = self.conn.execute(
        "SELECT handoff_id, conversation_id, status FROM handoff_record WHERE handoff_id = ?",
        (handoff_id,),
    ).fetchone()
    if row is None:
        return None
    return {"handoff_id": row[0], "conversation_id": row[1], "status": row[2]}
```

Inside `create_app`, add a helper after `_emit_handoff`:

```python
async def _sync_chatwoot_agent_messages(conv_id: str) -> None:
    """Resolve 前兜底同步；webhook 延迟或丢失时仍能补齐人工上下文。"""
    if chatwoot is None:
        return
    try:
        for message in await chatwoot.list_messages(conv_id):
            if not is_agent_message(message):
                continue
            messages_store.append_if_absent(
                chatwoot_message_id(message),
                conv_id,
                "assistant",
                message["content"],
            )
    except Exception:
        logger.exception("chatwoot resolve sync failed: conv=%s", conv_id)
```

Change the beginning of `/handoffs/{handoff_id}/resolve` from resolving immediately to:

```python
pending_handoff = handoff.get(handoff_id)
if pending_handoff is None:
    raise HTTPException(status_code=404, detail="handoff not found")

await _sync_chatwoot_agent_messages(pending_handoff["conversation_id"])

try:
    result = handoff.resolve_handoff(handoff_id)
except KeyError:
    raise HTTPException(status_code=404, detail="handoff not found")
```

Keep the existing `graph.aget_state()` → `messages_store.hydrate(conv_id)` → `Command(resume=...)` logic unchanged.

- [ ] **Step 4: Run targeted tests**

Run: `uv run --no-sync pytest tests/test_server.py tests/test_e2e_human_lane.py -q`
Expected: PASS。

- [ ] **Step 5: Commit**

```powershell
git add src/ai_service_agent/harness/handoff.py src/ai_service_agent/harness/server.py tests/test_e2e_human_lane.py
git commit -m "feat: sync chatwoot context before handoff resolve"
```

### Task 5: 文档更新、全量回归与真机验证

**Files:**
- Modify: `docs/specs/handoff-spec.md`

**Interfaces:**
- Consumes: Tasks 1–4 的实现与测试。
- Produces: 权威 spec 中记录 Chatwoot 坐席消息过滤、幂等 ID 与 Resolve 前同步规则。

- [ ] **Step 1: Update handoff spec**

In `docs/specs/handoff-spec.md` §5.2, after the Message Store truth-source bullet list, add:

```markdown
### 5.2.1 Chatwoot 坐席消息回传（2026-09-19）

- Chatwoot `message_created` 中，只有 `message_type=outgoing/1`、`private=false`、`sender.type=user` 且文本非空的公开坐席回复，才写入 Message Store 并映射为 `assistant`。
- 私密备注、系统事件、模板消息、bot/system sender、Runtime 回显均不得进入 AI 历史。
- Chatwoot 消息使用 `message_id = chatwoot-{Chatwoot message id}` 幂等落库；该 ID 同时作为 LangChain `add_messages` 的稳定 ID。
- Resolve 时先通过 Chatwoot Application API 幂等同步最近公开坐席消息，再执行 `MessageStore.hydrate()` 与 `Command(resume=...)`；同步失败只记日志，不阻塞 Resolve。
- 人工车道用户消息只追加一条 `user` 记录；`HANDOFF_NOTICE` 仅在 handoff 创建时写入一次。
```

- [ ] **Step 2: Run full regression**

Run: `uv run --no-sync pytest -q`
Expected: 全部 PASS，无新增 skip 或 error。

- [ ] **Step 3: Verify spec coverage**

Check all requirements against implementation:

```powershell
rg -n "append_if_absent|is_agent_message|chatwoot_message_id|list_messages|chatwoot-\{" src/ai_service_agent/harness docs/specs/handoff-spec.md
```

Expected: Message Store、Chatwoot contract、webhook、Resolve sync 与 spec 都命中。

- [ ] **Step 4: Commit documentation**

```powershell
git add docs/specs/handoff-spec.md
git commit -m "docs: specify chatwoot agent context sync"
```

- [ ] **Step 5: Run real-machine Chatwoot E2E**

1. Start an isolated Runtime with the real Chatwoot webhook secret from `GET /api/v1/accounts/3/webhooks`:

```powershell
$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"
$env:UV_LINK_MODE="copy"
$env:DB_PATH="$PWD\.e2e-runtime.db"
$env:CHATWOOT_WEBHOOK_SECRET="<chatwoot-webhook-secret>"
uv run --no-sync python -m ai_service_agent.main
```

2. Run the browser E2E in another shell:

```powershell
$env:CHROME_PATH='C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
node scripts/page_e2e_smoke.mjs
```

Acceptance:

- 页面登录并发送“我要转人工”后，Chatwoot conversation 自动创建。
- Chatwoot 坐席发送两条公开回复；即使 Runtime 只收到一条 webhook，Resolve 前同步也能补齐两条。
- 用户页面能看到坐席回复。
- Resolve 后用户发送一条新消息，AI 回复能引用或反映人工期间上下文。
- SQLite 中没有重复坐席消息、私密备注或 bot 消息。
