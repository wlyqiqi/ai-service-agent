# Chatwoot AgentBot Widget Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the self-built chat UI with Chatwoot's official WebWidget and integrate LangGraph as a Chatwoot AgentBot, using Chatwoot's native handoff for human agent transitions.

**Architecture:** Chatwoot Website Channel + AgentBot webhook → our FastAPI backend invokes LangGraph → AI response sent back via Chatwoot API → Widget displays. Handoff is Chatwoot-native (`pending ↔ open`). Interactive forms use Chatwoot's native `form`/`input_select` content types. LangGraph runtime (Router, subgraphs, Policy, Checkpointer) unchanged.

**Tech Stack:** Chatwoot v4.x (Docker:3000), FastAPI (existing), LangGraph 1.2.10 (existing), Chatwoot Widget SDK (script embed)

**Spec:** `docs/superpowers/plans/2026-10-08-chatwoot-agentbot.md` (this doc)

## Global Constraints

- Python 3.14, LangGraph 1.2.10, langchain-core 1.5.3
- Chatwoot v4.x at `http://localhost:3000`, account_id=3
- FastAPI is Transport only; business orchestration stays in LangGraph (AGENTS.md §4)
- `conversation_mode` explicit two-state; lightweight `conversation_status` store (AGENTS.md §22)
- Message Store (`conversation_message`) remains the single source of truth
- Checkpointer (AsyncSqliteSaver) unchanged; thread_id mapping unchanged
- All tool execution boundaries and hooks unchanged
- No new pip dependencies

---

### Task 1: Extend ChatwootClient for AgentBot

**Files:**
- Modify: `src/ai_service_agent/harness/chatwoot.py`
- Test: `tests/test_chatwoot.py`

**Interfaces:**
- Consumes: existing `ChatwootClient`, `ChatwootMappingStore`
- Produces: `send_bot_message(conversation_id, content, content_type=None, content_attributes=None)` — sends outgoing message as bot; `send_interactive_message(conversation_id, content, content_type, content_attributes)` — sends form/input_select/cards

- [ ] **Step 1: Write failing tests for send_bot_message and send_interactive_message**

```python
# tests/test_chatwoot.py — append these tests

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

@pytest.mark.asyncio
async def test_send_bot_message_outgoing():
    """send_bot_message sends outgoing message via Chatwoot API."""
    mapping = MagicMock()
    mapping.get.return_value = {"conversation_id": "c1", "chatwoot_conversation_id": 42, "contact_id": None, "created_at": ""}
    mapping.put_runtime_message = MagicMock()
    client = ChatwootClient("http://localhost:3000", 3, 2, "tok", mapping)
    mock_response = MagicMock()
    mock_response.json.return_value = {"id": 100}
    mock_response.raise_for_status = MagicMock()
    with patch.object(client, "client") as mock_http:
        mock_http.post = AsyncMock(return_value=mock_response)
        await client.send_bot_message("c1", "Hello from AI")
        mock_http.post.assert_awaited_once()
        call_args = mock_http.post.call_args
        body = call_args.kwargs.get("json", call_args[1].get("json", {}))
        assert body["content"] == "Hello from AI"
        assert body["message_type"] == "outgoing"

@pytest.mark.asyncio
async def test_send_interactive_message_form():
    """send_interactive_message sends form content_type with content_attributes."""
    mapping = MagicMock()
    mapping.get.return_value = {"conversation_id": "c1", "chatwoot_conversation_id": 42, "contact_id": None, "created_at": ""}
    mapping.put_runtime_message = MagicMock()
    client = ChatwootClient("http://localhost:3000", 3, 2, "tok", mapping)
    mock_response = MagicMock()
    mock_response.json.return_value = {"id": 101}
    mock_response.raise_for_status = MagicMock()
    with patch.object(client, "client") as mock_http:
        mock_http.post = AsyncMock(return_value=mock_response)
        attrs = {"items": [{"name": "addr", "type": "text", "label": "Address"}]}
        await client.send_interactive_message("c1", "Fill address", "form", attrs)
        body = mock_http.post.call_args.kwargs.get("json", mock_http.post.call_args[1].get("json", {}))
        assert body["content_type"] == "form"
        assert body["content_attributes"] == attrs
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --no-sync pytest tests/test_chatwoot.py::test_send_bot_message_outgoing tests/test_chatwoot.py::test_send_interactive_message_form -v`
Expected: FAIL with `AttributeError: 'ChatwootClient' object has no attribute 'send_bot_message'`

- [ ] **Step 3: Implement send_bot_message and send_interactive_message in ChatwootClient**

```python
# src/ai_service_agent/harness/chatwoot.py — append to ChatwootClient class

    async def send_bot_message(
        self, conversation_id: str, content: str,
        content_type: str | None = None, content_attributes: dict | None = None,
    ) -> None:
        """Send outgoing message as bot (AgentBot pattern)."""
        rec = self.mapping.get(conversation_id)
        if rec is None:
            return
        body: dict = {"content": content, "message_type": "outgoing"}
        if content_type:
            body["content_type"] = content_type
        if content_attributes:
            body["content_attributes"] = content_attributes
        r = await self.client.post(
            f"{self.base_url}/api/v1/accounts/{self.account_id}/conversations/{rec['chatwoot_conversation_id']}/messages",
            headers=self._headers(), json=body,
        )
        r.raise_for_status()
        msg_id = r.json().get("id")
        if msg_id is not None:
            self.mapping.put_runtime_message(msg_id, conversation_id)

    async def send_interactive_message(
        self, conversation_id: str, content: str,
        content_type: str, content_attributes: dict,
    ) -> None:
        """Send interactive message (form / input_select / cards)."""
        await self.send_bot_message(conversation_id, content, content_type, content_attributes)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --no-sync pytest tests/test_chatwoot.py::test_send_bot_message_outgoing tests/test_chatwoot.py::test_send_interactive_message_form -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/ai_service_agent/harness/chatwoot.py tests/test_chatwoot.py
git commit -m "feat(chatwoot): add send_bot_message and send_interactive_message for AgentBot"
```

---

### Task 2: Add AgentBot settings and webhook handler

**Files:**
- Modify: `src/ai_service_agent/config.py:65` (after chatwoot_webhook_secret)
- Create: `src/ai_service_agent/harness/agent_bot.py`
- Modify: `src/ai_service_agent/harness/server.py` (register webhook endpoint)
- Test: `tests/test_agent_bot.py`

**Interfaces:**
- Consumes: `ChatwootClient.send_bot_message`, `event_stream`, `make_ai_graph`, `MessageStore`, `ConversationStatusStore`, `verify_chatwoot_signature`
- Produces: `handle_agent_bot_message(payload: dict, chatwoot: ChatwootClient, ...) -> dict` — main handler called from FastAPI endpoint

- [ ] **Step 1: Add AgentBot config settings**

```python
# src/ai_service_agent/config.py — add after chatwoot_webhook_secret line
    chatwoot_agent_bot_secret: str | None = Field(default=None, validation_alias="CHATWOOT_AGENT_BOT_SECRET")
    chatwoot_website_token: str | None = Field(default=None, validation_alias="CHATWOOT_WEBSITE_TOKEN")
```

- [ ] **Step 2: Write failing test for handle_agent_bot_message**

```python
# tests/test_agent_bot.py

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from ai_service_agent.harness.agent_bot import handle_agent_bot_message


@pytest.mark.asyncio
async def test_handle_agent_bot_message_incoming_text():
    """handle_agent_bot_message processes incoming message and invokes graph."""
    payload = {
        "event": "message_created",
        "id": "100",
        "content": "你好",
        "message_type": "incoming",
        "content_type": "text",
        "conversation": {"id": 42},
        "contact": {"id": 1, "name": "test-user"},
    }
    chatwoot = AsyncMock()
    chatwoot.send_bot_message = AsyncMock()
    result = await handle_agent_bot_message(payload, chatwoot)
    assert result is not None


@pytest.mark.asyncio
async def test_handle_agent_bot_message_ignores_outgoing():
    """handle_agent_bot_message ignores outgoing (bot/agent) messages to prevent loops."""
    payload = {
        "event": "message_created",
        "id": "101",
        "content": "AI reply",
        "message_type": "outgoing",
        "content_type": "text",
        "conversation": {"id": 42},
        "contact": {"id": 1, "name": "test-user"},
    }
    chatwoot = AsyncMock()
    result = await handle_agent_bot_message(payload, chatwoot)
    assert result is None  # no processing for outgoing messages
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run --no-sync pytest tests/test_agent_bot.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ai_service_agent.harness.agent_bot'`

- [ ] **Step 4: Implement agent_bot.py**

```python
# src/ai_service_agent/harness/agent_bot.py

"""Chatwoot AgentBot webhook handler.

Receives message_created events from Chatwoot for conversations in pending status.
Routes to LangGraph and sends AI response back via Chatwoot API.
"""

import logging
from typing import Any

from ai_service_agent.harness.chatwoot import ChatwootClient

logger = logging.getLogger("ai_service_agent")


async def handle_agent_bot_message(
    payload: dict[str, Any],
    chatwoot: ChatwootClient,
    graph=None,
    status=None,
    messages_store=None,
    thread_id: str | None = None,
) -> dict | None:
    """Process an incoming AgentBot message.

    Returns a dict with the response info, or None if the message should be ignored.
    """
    event = payload.get("event")
    if event != "message_created":
        return None

    message_type = payload.get("message_type")
    if message_type != "incoming":
        return None  # ignore bot/agent outgoing messages

    content = payload.get("content", "").strip()
    if not content:
        return None

    conversation = payload.get("conversation") or {}
    cw_conv_id = conversation.get("id")
    if not cw_conv_id:
        return None

    contact = payload.get("contact") or {}
    contact_name = contact.get("name", "widget-user")

    # Route to LangGraph and get response
    # For now, respond with an echo to prove the pipeline works.
    # In the next task, this will invoke make_ai_graph and event_stream.
    response_text = f"[AgentBot] Received: {content}"
    await chatwoot.send_bot_message(str(cw_conv_id), response_text)
    return {"cw_conv_id": cw_conv_id, "response": response_text}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run --no-sync pytest tests/test_agent_bot.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/ai_service_agent/config.py src/ai_service_agent/harness/agent_bot.py tests/test_agent_bot.py
git commit -m "feat(agentbot): add AgentBot webhook handler skeleton and config settings"
```

---

### Task 3: Wire AgentBot handler to LangGraph

**Files:**
- Modify: `src/ai_service_agent/harness/agent_bot.py`
- Modify: `src/ai_service_agent/harness/server.py` (register endpoint)
- Test: `tests/test_agent_bot.py`

**Interfaces:**
- Consumes: `make_ai_graph`, `event_stream`, `MessageStore`, `ConversationStatusStore`, `ChatwootMappingStore`
- Produces: FastAPI endpoint `POST /webhooks/agent_bot` that processes incoming widget messages and routes them through LangGraph

- [ ] **Step 1: Write failing test for graph integration**

```python
# tests/test_agent_bot.py — append

@pytest.mark.asyncio
async def test_agent_bot_invokes_graph():
    """handle_agent_bot_message invokes LangGraph and sends AI response via Chatwoot API."""
    from ai_service_agent.testing import make_fake_model, router_json_message
    from ai_service_agent.harness.graph import make_ai_graph

    model = make_fake_model()
    graph = make_ai_graph(model, None)

    payload = {
        "event": "message_created",
        "id": "102",
        "content": "你好",
        "message_type": "incoming",
        "content_type": "text",
        "conversation": {"id": 43},
        "contact": {"id": 2, "name": "test-user"},
    }
    chatwoot = AsyncMock()
    chatwoot.send_bot_message = AsyncMock()

    result = await handle_agent_bot_message(payload, chatwoot, graph=graph)
    assert result is not None
    chatwoot.send_bot_message.assert_awaited_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --no-sync pytest tests/test_agent_bot.py::test_agent_bot_invokes_graph -v`
Expected: FAIL (no graph parameter handling in current implementation)

- [ ] **Step 3: Implement graph invocation in agent_bot.py**

```python
# src/ai_service_agent/harness/agent_bot.py — update handle_agent_bot_message

async def handle_agent_bot_message(
    payload: dict[str, Any],
    chatwoot: ChatwootClient,
    graph=None,
    status=None,
    messages_store=None,
    thread_id: str | None = None,
    db_path: str | None = None,
) -> dict | None:
    event = payload.get("event")
    if event != "message_created":
        return None

    message_type = payload.get("message_type")
    if message_type != "incoming":
        return None

    content = payload.get("content", "").strip()
    if not content:
        return None

    conversation = payload.get("conversation") or {}
    cw_conv_id = conversation.get("id")
    if not cw_conv_id:
        return None

    if graph is None:
        # No graph: simple echo for testing
        response_text = f"[AgentBot] Received: {content}"
        await chatwoot.send_bot_message(str(cw_conv_id), response_text)
        return {"cw_conv_id": cw_conv_id, "response": response_text}

    # Build state for graph invocation
    from langchain_core.messages import HumanMessage
    from ai_service_agent.harness.streaming import event_stream

    conv_key = f"agentbot-{cw_conv_id}"
    history = []
    if messages_store:
        history = messages_store.hydrate(conv_key)

    messages = history + [HumanMessage(content=content)]
    config = {"configurable": {"thread_id": thread_id or conv_key}}
    extra = {"user_id": f"contact-{payload.get('contact', {}).get('id', 'unknown')}"}

    final_text = None
    async for ev in event_stream(graph, config, messages, extra_state=extra):
        if ev.get("type") == "done" and ev.get("data", {}).get("final"):
            final_text = ev["data"]["final"]

    if final_text:
        await chatwoot.send_bot_message(str(cw_conv_id), final_text)
        if messages_store:
            messages_store.append(conv_key, "user", content)
            messages_store.append(conv_key, "assistant", final_text)
        return {"cw_conv_id": cw_conv_id, "response": final_text}

    return {"cw_conv_id": cw_conv_id, "response": None}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --no-sync pytest tests/test_agent_bot.py -v`
Expected: PASS

- [ ] **Step 5: Register AgentBot endpoint in server.py**

```python
# src/ai_service_agent/harness/server.py — add after the existing chatwoot_webhook endpoint

    @app.post("/webhooks/agent_bot")
    async def agent_bot_webhook(request: Request):
        payload_bytes = await request.body()
        import json as _json
        try:
            data = _json.loads(payload_bytes)
        except ValueError:
            return {"ok": True}
        from ai_service_agent.harness.agent_bot import handle_agent_bot_message
        if chatwoot is not None:
            await handle_agent_bot_message(
                data, chatwoot, db_path=db_path,
                messages_store=messages_store, status=status,
            )
        return {"ok": True}
```

- [ ] **Step 6: Run full test suite**

Run: `uv run --no-sync pytest -q`
Expected: PASS (all existing + new tests)

- [ ] **Step 7: Commit**

```bash
git add src/ai_service_agent/harness/agent_bot.py src/ai_service_agent/harness/server.py tests/test_agent_bot.py
git commit -m "feat(agentbot): wire AgentBot webhook to LangGraph and register FastAPI endpoint"
```

---

### Task 4: Convert MutationGraph clarification to Chatwoot interactive messages

**Files:**
- Modify: `src/ai_service_agent/harness/agent_bot.py`
- Test: `tests/test_agent_bot.py`

**Interfaces:**
- Consumes: `clarification_required` event data (missing fields list), `confirmation_required` event data
- Produces: `send_clarify_form(chatwoot, conv_id, missing_fields, profile, orders)` and `send_confirmation(chatwoot, conv_id, tool, args)` — converts LangGraph events to Chatwoot interactive messages

- [ ] **Step 1: Write failing test for clarify form conversion**

```python
# tests/test_agent_bot.py — append

@pytest.mark.asyncio
async def test_send_clarify_form_address():
    """MutationGraph clarification_required with address fields sends Chatwoot form."""
    from ai_service_agent.harness.agent_bot import send_clarify_form
    chatwoot = AsyncMock()
    chatwoot.send_interactive_message = AsyncMock()

    await send_clarify_form(
        chatwoot, "42", ["order_id", "address"],
        profile={"address": "No.119 langGraph street", "phone": "+65-89136375"},
        orders=[{"order_id": "ORD-1", "status_display": "已付款"}, {"order_id": "ORD-2", "status_display": "已付款"}],
    )
    chatwoot.send_interactive_message.assert_awaited_once()
    args = chatwoot.send_interactive_message.call_args
    assert args[0][1] == "修改收货地址"  # content
    assert args[0][2] == "form"  # content_type
    attrs = args[0][3]
    assert len(attrs["items"]) >= 2
    select_field = next(i for i in attrs["items"] if i.get("type") == "select")
    assert len(select_field["options"]) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --no-sync pytest tests/test_agent_bot.py::test_send_clarify_form_address -v`
Expected: FAIL with `ImportError: cannot import name 'send_clarify_form'`

- [ ] **Step 3: Implement send_clarify_form**

```python
# src/ai_service_agent/harness/agent_bot.py — append

from ai_service_agent.graph.workflows.copy import FIELD_LABELS


async def send_clarify_form(
    chatwoot: ChatwootClient,
    cw_conv_id: str,
    missing_fields: list[str],
    profile: dict | None = None,
    orders: list[dict] | None = None,
) -> None:
    """Convert clarification_required missing fields to a Chatwoot form message."""
    items = []

    if "order_id" in missing_fields:
        options = []
        for o in (orders or []):
            label = f"{o['order_id']} · {o.get('status_display', '')}"
            options.append({"label": label, "value": o["order_id"]})
        items.append({"name": "order_id", "label": "选择订单", "type": "select", "options": options})

    if "address" in missing_fields:
        old_addr = (profile or {}).get("address", "")
        items.append({
            "name": "address", "label": f"新收货地址（当前：{old_addr}）",
            "type": "text", "placeholder": "请输入新地址",
        })

    if "phone" in missing_fields:
        old_phone = (profile or {}).get("phone", "")
        items.append({
            "name": "phone", "label": f"新手机号（当前：{old_phone}）",
            "type": "text", "placeholder": "请输入新手机号",
        })

    if "address" in missing_fields and "order_id" not in missing_fields:
        title = "修改收货地址"
    elif "phone" in missing_fields and "order_id" not in missing_fields:
        title = "修改联系电话"
    else:
        title = "请补充以下信息"

    await chatwoot.send_interactive_message(cw_conv_id, title, "form", {"items": items})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --no-sync pytest tests/test_agent_bot.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/ai_service_agent/harness/agent_bot.py tests/test_agent_bot.py
git commit -m "feat(agentbot): convert MutationGraph clarification to Chatwoot interactive form"
```

---

### Task 5: Embed Chatwoot Widget in frontend

**Files:**
- Modify: `web/index.html` (add widget script, hide chat view)
- Test: `tests/test_user_page.py` (verify widget script exists)

**Interfaces:**
- Consumes: Chatwoot `websiteToken` from `.env` (injected as `window.CHATWOOT_WEBSITE_TOKEN` by server or hardcoded for dev)
- Produces: Chatwoot Widget available at page load; `window.$chatwoot.setUser()` called after login

- [ ] **Step 1: Write failing test**

```python
# tests/test_user_page.py — append

def test_chatwoot_widget_script_embedded(html):
    """Chatwoot widget SDK script is embedded in the HTML."""
    assert "chatwootSDK" in html
    assert "BASE_URL" in html
    assert "websiteToken" in html


def test_chatwoot_widget_hidden_bubble(html):
    """Widget bubble is hidden by default (only opens programmatically)."""
    assert "hideMessageBubble" in html
    assert "true" in html
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --no-sync pytest tests/test_user_page.py::test_chatwoot_widget_script_embedded -v`
Expected: FAIL

- [ ] **Step 3: Add Chatwoot Widget SDK to index.html**

Add before `</body>`:

```html
<!-- Chatwoot Widget -->
<script>
  window.chatwootSettings = {
    hideMessageBubble: true,
    position: "right",
    locale: "zh-CN",
    type: "standard",
    darkMode: "auto",
  };
  (function(d, t) {
    var BASE_URL = "http://localhost:3000";
    var g = d.createElement(t), s = d.getElementsByTagName(t)[0];
    g.src = BASE_URL + "/packs/js/sdk.js";
    g.defer = true; g.async = true;
    s.parentNode.insertBefore(g, s);
    g.onload = function() {
      window.chatwootSDK.run({
        websiteToken: window.CHATWOOT_WEBSITE_TOKEN || "__WEBSITE_TOKEN__",
        baseUrl: BASE_URL,
      });
    };
  })(document, "script");
</script>
```

Add to login success handler (after `saveAuth(auth)` and `store.auth = auth`):

```javascript
if (window.$chatwoot && auth.user) {
  window.$chatwoot.setUser(auth.user.id || auth.user.name, {
    name: auth.user.name || "用户",
  });
  window.$chatwoot.toggleBubbleVisibility("show");
  window.$chatwoot.toggle("open");
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --no-sync pytest tests/test_user_page.py::test_chatwoot_widget_script_embedded tests/test_user_page.py::test_chatwoot_widget_hidden_bubble -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add web/index.html tests/test_user_page.py
git commit -m "feat(frontend): embed Chatwoot Widget SDK with hideMessageBubble and setUser"
```

---

### Task 6: Full regression and real-model E2E

**Files:** No new files; verification only.

- [ ] **Step 1: Run full test suite**

```bash
uv run --no-sync pytest -q
```

Expected: ALL PASS

- [ ] **Step 2: Start server with Website Channel Inbox**

```bash
# .env must have CHATWOOT_WEBSITE_TOKEN set to the new Website Inbox token
# AI_SERVICE_PORT=8000
```

- [ ] **Step 3: Browser E2E test**

1. Open `http://127.0.0.1:8000/`
2. Login with `+65-89136375 / 123456`
3. Chatwoot Widget opens
4. Send "你好" → AI replies via AgentBot
5. Send "我要修改地址" → Widget shows form with order dropdown + address input
6. Fill and submit → MutationGraph executes → success message in Widget
7. Send "转人工" → Chatwoot handoff (pending → open) → agent dashboard shows conversation
8. Agent replies → Widget shows agent message
9. Agent resolves → status back to pending → AI responds again
10. Send "我刚才说的订单问题怎么样了？" → AI understands context

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "feat: complete Chatwoot AgentBot widget integration with form and handoff"
```

---

### Execution Order

Tasks 1-5 are sequential (each builds on the previous). Task 6 is the final verification.

**Prerequisites before starting:**
- User must create a Website Channel Inbox in Chatwoot Dashboard and provide the `websiteToken`
- User must create an AgentBot in Chatwoot pointing to `http://172.99.46.165:8000/webhooks/agent_bot`
