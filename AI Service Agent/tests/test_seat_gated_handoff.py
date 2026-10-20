"""#81 Seat-gated handoff — confirmed seams: AgentBot/entry + Account Webhook."""

import hashlib
import hmac
import json
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from ai_service_agent.graph.handoff_rules import detect_handoff_cancel
from ai_service_agent.graph.workflows.copy import (
    handoff_cancel_confirm_text,
    handoff_form_cancelled_customer_text,
    handoff_form_cancelled_seat_note_text,
    handoff_joined_notice_text,
    handoff_request_confirm_text,
    handoff_request_idempotent_tip_text,
    handoff_returned_to_bot_text,
)
from ai_service_agent.harness.chatwoot import ChatwootMappingStore
from ai_service_agent.harness.handoff import STATUS_AWAITING, HandoffService
from ai_service_agent.harness.messages import MessageStore
from ai_service_agent.harness.server import create_app
from ai_service_agent.harness.session import ConversationStatusStore
from ai_service_agent.testing import ToolCallingFakeModel


BOT_ERROR = "Conversation was marked open by system due to an error with the agent bot."


def _human_open(cw_id: int, *, nested: bool = False) -> dict:
    """Status open that carries a human assignee — a real seat Take over."""
    meta = {
        "assignee_type": "User",
        "assignee": {"id": 7, "name": "Seat", "type": "user"},
    }
    if nested:
        return {
            "event": "conversation_status_changed",
            "conversation": {"id": cw_id, "status": "open", "meta": meta},
        }
    return {
        "event": "conversation_status_changed",
        "id": cw_id,
        "status": "open",
        "meta": meta,
    }


def _signed_headers(payload: bytes, secret: str = "sec") -> dict[str, str]:
    """Sign one webhook request with a *fresh* timestamp.

    The replay window is 300s, so a module-level timestamp goes stale in long
    full-suite runs and every signed call becomes a 401 — this was the source
    of the order-dependent failures in this file. Signature and header must
    share the same timestamp value.
    """
    ts = str(int(time.time()))
    signature = "sha256=" + hmac.new(
        secret.encode(), ts.encode() + b"." + payload, hashlib.sha256
    ).hexdigest()
    return {"X-Chatwoot-Signature": signature, "X-Chatwoot-Timestamp": ts}


def _parse_sse(text: str) -> list[dict]:
    events = []
    for block in text.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


class ExplodingModel(ToolCallingFakeModel):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise AssertionError("graph must not run for explicit handoff request/cancel")


def _fake_chatwoot():
    return SimpleNamespace(
        ensure_conversation=AsyncMock(return_value=111),
        prompt_handoff_request=AsyncMock(),
        send_bot_message=AsyncMock(),
        set_conversation_status=AsyncMock(),
        list_messages=AsyncMock(return_value=[]),
        send_as_contact=AsyncMock(),
    )


# --- Seam 1: /chat entry ---


def test_explicit_request_hangs_mode_stays_ai(tmp_path):
    cw = _fake_chatwoot()
    client = TestClient(
        create_app(str(tmp_path / "s.db"), model=ExplodingModel(responses=[]), chatwoot=cw)
    )
    sid = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    with client.stream("POST", "/chat", json={"session_id": sid, "message": "我要转人工"}) as resp:
        events = _parse_sse("".join(resp.iter_text()))
    types = [e["type"] for e in events]
    assert "handoff_requested" in types
    assert "handoff_created" not in types
    assert types[-1] == "done"
    assert ConversationStatusStore(sqlite3.connect(str(tmp_path / "s.db"))).get_mode(sid) == "ai"
    tip = handoff_request_confirm_text()
    assert any(e.get("type") == "token" and e["data"].get("text") == tip for e in events)
    cw.prompt_handoff_request.assert_awaited()
    cw.set_conversation_status.assert_not_awaited()


def test_after_hang_non_gate_message_still_ai(tmp_path):
    client = TestClient(create_app(str(tmp_path / "s.db"), model="fake"))
    sid = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    with client.stream("POST", "/chat", json={"session_id": sid, "message": "我要转人工"}) as resp:
        "".join(resp.iter_text())
    with client.stream("POST", "/chat", json={"session_id": sid, "message": "查一下订单"}) as resp:
        types = [e["type"] for e in _parse_sse("".join(resp.iter_text()))]
    assert "token" in types and "done" in types
    assert ConversationStatusStore(sqlite3.connect(str(tmp_path / "s.db"))).get_mode(sid) == "ai"


def test_entry_cancel_clears_hang(tmp_path):
    client = TestClient(
        create_app(str(tmp_path / "s.db"), model=ExplodingModel(responses=[]))
    )
    sid = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    with client.stream("POST", "/chat", json={"session_id": sid, "message": "我要转人工"}) as resp:
        "".join(resp.iter_text())
    with client.stream("POST", "/chat", json={"session_id": sid, "message": "取消转人工"}) as resp:
        events = _parse_sse("".join(resp.iter_text()))
    assert ConversationStatusStore(sqlite3.connect(str(tmp_path / "s.db"))).get_mode(sid) == "ai"
    conn = sqlite3.connect(str(tmp_path / "s.db"))
    status = conn.execute(
        "SELECT status FROM handoff_record WHERE conversation_id=?", (sid,)
    ).fetchone()[0]
    assert status == "cancelled"
    tip = handoff_cancel_confirm_text()
    assert any(e.get("data", {}).get("text") == tip or e.get("data", {}).get("final") == tip for e in events)


def test_re_request_while_awaiting_idempotent(tmp_path):
    from ai_service_agent.graph.workflows.copy import handoff_request_idempotent_tip_text

    cw = _fake_chatwoot()
    client = TestClient(create_app(str(tmp_path / "s.db"), model="fake", chatwoot=cw))
    sid = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    with client.stream("POST", "/chat", json={"session_id": sid, "message": "我要转人工"}) as resp:
        "".join(resp.iter_text())
    assert cw.prompt_handoff_request.await_count == 1
    with client.stream("POST", "/chat", json={"session_id": sid, "message": "再转一次人工"}) as resp:
        events = _parse_sse("".join(resp.iter_text()))
    assert cw.prompt_handoff_request.await_count == 1  # no second strong prompt
    tip = handoff_request_idempotent_tip_text()
    assert any(e.get("data", {}).get("text") == tip or e.get("data", {}).get("final") == tip for e in events)
    conn = sqlite3.connect(str(tmp_path / "s.db"))
    n = conn.execute(
        "SELECT COUNT(*) FROM handoff_record WHERE conversation_id=? AND status=?",
        (sid, STATUS_AWAITING),
    ).fetchone()[0]
    assert n == 1


def test_detect_handoff_cancel_phrases():
    assert detect_handoff_cancel("取消转人工") is True
    assert detect_handoff_cancel("不用转人工了") is True
    assert detect_handoff_cancel("查订单") is False


def test_handoff_requested_sse_carries_no_control_fields(tmp_path):
    """created 是 Runtime 内部控制位，不得进入对外事件（event-spec §2 形状）。"""
    cw = _fake_chatwoot()
    client = TestClient(create_app(str(tmp_path / "s.db"), model="fake", chatwoot=cw))
    sid = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    with client.stream("POST", "/chat", json={"session_id": sid, "message": "我要转人工"}) as resp:
        events = _parse_sse("".join(resp.iter_text()))
    handoff_events = [e for e in events if e["type"] == "handoff_requested"]
    assert len(handoff_events) == 1
    assert set(handoff_events[0]) == {"type", "data"}


def test_chat_re_request_after_resolve_keeps_strong_confirm(tmp_path):
    """Resolve 之后再申请是新申请：强确认 + 坐席 private note（handoff-spec §2）。"""
    cw = _fake_chatwoot()
    client = TestClient(create_app(str(tmp_path / "s.db"), model="fake", chatwoot=cw))
    sid = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]

    with client.stream("POST", "/chat", json={"session_id": sid, "message": "我要转人工"}) as resp:
        "".join(resp.iter_text())
    conn = sqlite3.connect(str(tmp_path / "s.db"))
    handoff_id = conn.execute(
        "SELECT handoff_id FROM handoff_record WHERE conversation_id=? AND status=?",
        (sid, STATUS_AWAITING),
    ).fetchone()[0]
    conn.close()
    assert client.post(f"/handoffs/{handoff_id}/resolve").status_code == 200
    cw.prompt_handoff_request.reset_mock()

    with client.stream("POST", "/chat", json={"session_id": sid, "message": "我要转人工"}) as resp:
        events = _parse_sse("".join(resp.iter_text()))
    tips = [e["data"].get("text") for e in events if e["type"] == "token"]
    assert tips == [handoff_request_confirm_text()]
    assert cw.prompt_handoff_request.await_count == 1


# --- Seam 2: Account Webhook ---


def test_webhook_open_seat_takeover(tmp_path):
    db = str(tmp_path / "s.db")
    cw = _fake_chatwoot()
    client = TestClient(create_app(db, model="fake", chatwoot=cw, chatwoot_webhook_secret="sec"))
    sid = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    with client.stream("POST", "/chat", json={"session_id": sid, "message": "我要转人工"}) as resp:
        "".join(resp.iter_text())
    ChatwootMappingStore(sqlite3.connect(db)).put(sid, 555)

    body = _human_open(555)
    raw = json.dumps(body).encode()
    r = client.post(
        "/webhooks/chatwoot",
        content=raw,
        headers=_signed_headers(raw),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["event"]["type"] == "handoff_created"
    assert ConversationStatusStore(sqlite3.connect(db)).get_mode(sid) == "human"
    rows = MessageStore(sqlite3.connect(db)).list(sid)
    assert any(m["content"] == handoff_joined_notice_text() for m in rows)
    conn = sqlite3.connect(db)
    svc = HandoffService(conn, ConversationStatusStore(conn))
    assert svc.get_active(sid) is not None
    assert svc.get_awaiting(sid) is None
    conn.close()


def test_webhook_open_unilateral_takeover(tmp_path):
    db = str(tmp_path / "s.db")
    cw = _fake_chatwoot()
    client = TestClient(create_app(db, model="fake", chatwoot=cw, chatwoot_webhook_secret="sec"))
    sid = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    ChatwootMappingStore(sqlite3.connect(db)).put(sid, 777)
    body = _human_open(777, nested=True)
    raw = json.dumps(body).encode()
    assert client.post(
        "/webhooks/chatwoot",
        content=raw,
        headers=_signed_headers(raw),
    ).status_code == 200
    assert ConversationStatusStore(sqlite3.connect(db)).get_mode(sid) == "human"


def test_webhook_resolved_returns_ai_via_toggle_status(tmp_path):
    db = str(tmp_path / "s.db")
    cw = _fake_chatwoot()
    client = TestClient(create_app(db, model="fake", chatwoot=cw, chatwoot_webhook_secret="sec"))
    sid = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    ChatwootMappingStore(sqlite3.connect(db)).put(sid, 555)
    # request + takeover
    with client.stream("POST", "/chat", json={"session_id": sid, "message": "我要转人工"}) as resp:
        "".join(resp.iter_text())
    raw_open = json.dumps(_human_open(555)).encode()
    client.post(
        "/webhooks/chatwoot",
        content=raw_open,
        headers=_signed_headers(raw_open),
    )
    cw.set_conversation_status.reset_mock()
    raw_res = json.dumps({"event": "conversation_status_changed", "id": 555, "status": "resolved"}).encode()
    r = client.post(
        "/webhooks/chatwoot",
        content=raw_res,
        headers=_signed_headers(raw_res),
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["event"]["type"] == "handoff_resolved"
    assert ConversationStatusStore(sqlite3.connect(db)).get_mode(sid) == "ai"
    cw.set_conversation_status.assert_awaited_with(sid, "pending")


def test_webhook_pending_ignored_when_already_ai(tmp_path):
    """pending while mode is ai is the anti-loop (baseline or echo after toggle)."""
    db = str(tmp_path / "s.db")
    client = TestClient(create_app(db, model="fake", chatwoot_webhook_secret="sec"))
    sid = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    ChatwootMappingStore(sqlite3.connect(db)).put(sid, 555)
    raw = json.dumps({"event": "conversation_status_changed", "id": 555, "status": "pending"}).encode()
    assert client.post(
        "/webhooks/chatwoot",
        content=raw,
        headers=_signed_headers(raw),
    ).status_code == 200
    assert ConversationStatusStore(sqlite3.connect(db)).get_mode(sid) == "ai"


def test_webhook_pending_returns_human_to_ai_without_toggle(tmp_path):
    """Official return: status pending while human → mode ai, no second toggle."""
    db = str(tmp_path / "s.db")
    cw = _fake_chatwoot()
    client = TestClient(create_app(db, model="fake", chatwoot=cw, chatwoot_webhook_secret="sec"))
    sid = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    ChatwootMappingStore(sqlite3.connect(db)).put(sid, 555)
    with client.stream("POST", "/chat", json={"session_id": sid, "message": "我要转人工"}) as resp:
        "".join(resp.iter_text())
    raw_open = json.dumps(_human_open(555)).encode()
    client.post(
        "/webhooks/chatwoot",
        content=raw_open,
        headers=_signed_headers(raw_open),
    )
    assert ConversationStatusStore(sqlite3.connect(db)).get_mode(sid) == "human"
    cw.set_conversation_status.reset_mock()
    raw_pending = json.dumps(
        {"event": "conversation_status_changed", "id": 555, "status": "pending"}
    ).encode()
    r = client.post(
        "/webhooks/chatwoot",
        content=raw_pending,
        headers=_signed_headers(raw_pending),
    )
    assert r.status_code == 200
    assert r.json()["event"]["type"] == "handoff_resolved"
    assert ConversationStatusStore(sqlite3.connect(db)).get_mode(sid) == "ai"
    cw.set_conversation_status.assert_not_awaited()
    rows = MessageStore(sqlite3.connect(db)).list(sid)
    assert any(m["content"] == handoff_returned_to_bot_text() for m in rows)
    # Echo while already ai must not tip again.
    client.post(
        "/webhooks/chatwoot",
        content=raw_pending,
        headers=_signed_headers(raw_pending),
    )
    rows = MessageStore(sqlite3.connect(db)).list(sid)
    assert sum(1 for m in rows if m["content"] == handoff_returned_to_bot_text()) == 1


def _post_webhook(client: TestClient, body: dict):
    raw = json.dumps(body).encode()
    return client.post(
        "/webhooks/chatwoot",
        content=raw,
        headers=_signed_headers(raw),
    )


def test_open_cancels_pending_form_so_late_submit_cannot_write(tmp_path, monkeypatch):
    """Enter human clears a Mutation Form Interrupt; late confirm does not write."""
    import asyncio
    import importlib.util
    from pathlib import Path

    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    from ai_service_agent.domain.orders import SQLiteOrderRepository
    from ai_service_agent.harness.graph import make_ai_graph
    from ai_service_agent.testing import router_json_message, select_json_message
    from ai_service_agent.tools import write as write_mod

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "seed_mock_db_handoff", root / "scripts" / "seed_mock_db.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    business_db = tmp_path / "mock_ecommerce.db"
    module.build_database(str(business_db))

    class _WriteSettings:
        mock_ecommerce_db_path = str(business_db)

    monkeypatch.setattr(write_mod, "get_settings", lambda: _WriteSettings())

    def customer_address() -> str:
        conn = sqlite3.connect(business_db)
        try:
            row = conn.execute(
                "SELECT address FROM customers WHERE customer_id = ?", ("u1",)
            ).fetchone()
        finally:
            conn.close()
        return row[0]

    original = customer_address()
    model = ToolCallingFakeModel(responses=[
        router_json_message("account_profile_update", 0.9),
        select_json_message(
            "update_address",
            {"order_id": "ORD-PAID-1", "address": "不应在接管前写入"},
        ),
    ])
    db = str(tmp_path / "s.db")
    cw = _fake_chatwoot()
    repo = SQLiteOrderRepository(str(business_db))
    client = TestClient(
        create_app(
            db,
            model=model,
            chatwoot=cw,
            chatwoot_webhook_secret="sec",
            chatwoot_agent_bot_secret="sec",
            order_repository=repo,
        )
    )
    sid = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    with client.stream("POST", "/chat", json={"session_id": sid, "message": "改收货地址"}) as resp:
        events = _parse_sse("".join(resp.iter_text()))
    assert any(e.get("type") == "form_required" for e in events)
    assert customer_address() == original

    conn = sqlite3.connect(db)
    thread_id = conn.execute(
        "SELECT thread_id FROM conversation_status WHERE conversation_id = ?",
        (sid,),
    ).fetchone()[0]
    conn.close()

    async def pending_form():
        async with AsyncSqliteSaver.from_conn_string(db) as saver:
            graph = make_ai_graph(model, saver, order_repository=repo)
            state = await graph.aget_state({"configurable": {"thread_id": thread_id}})
            return state.interrupts

    assert asyncio.run(pending_form())
    ChatwootMappingStore(sqlite3.connect(db)).put(sid, 555)
    opened = _post_webhook(
        client, _human_open(555)
    )
    assert opened.status_code == 200
    assert ConversationStatusStore(sqlite3.connect(db)).get_mode(sid) == "human"
    assert list(asyncio.run(pending_form())) == []
    assert customer_address() == original
    rows = MessageStore(sqlite3.connect(db)).list(sid)
    assert any(m["content"] == handoff_form_cancelled_customer_text() for m in rows)
    cw.send_bot_message.assert_any_await(
        sid, handoff_form_cancelled_seat_note_text(), private=True
    )

    late = client.post(
        "/actions/confirm",
        json={
            "conversation_id": sid,
            "action": "confirm",
            "fields": {"order_id": "ORD-PAID-1", "address": "迟到提交不应写入"},
        },
    )
    assert late.status_code == 409
    assert customer_address() == original

    form_body = {
        "event": "message_updated",
        "id": 9002,
        "message_type": "incoming",
        "content_type": "form",
        "content_attributes": {
            "submitted_values": [
                {"name": "order_id", "value": "ORD-PAID-1"},
                {"name": "address", "value": "Widget迟到提交不应写入"},
                {"name": "action", "value": "confirm"},
            ]
        },
        "conversation": {"id": 555},
        "contact": {"id": 1, "identifier": "u1", "name": "u"},
    }
    raw_form = json.dumps(form_body).encode()
    bot = client.post(
        "/webhooks/agent_bot",
        content=raw_form,
        headers=_signed_headers(raw_form),
    )
    assert bot.status_code == 200
    assert customer_address() == original
    assert list(asyncio.run(pending_form())) == []


def test_http_resolve_shares_toggle_pending(tmp_path):
    cw = _fake_chatwoot()
    client = TestClient(create_app(str(tmp_path / "s.db"), model="fake", chatwoot=cw))
    sid = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    with client.stream("POST", "/chat", json={"session_id": sid, "message": "我要转人工"}) as resp:
        events = _parse_sse("".join(resp.iter_text()))
    handoff_id = next(e["data"]["handoff_id"] for e in events if e["type"] == "handoff_requested")
    # Activate via service then resolve (HTTP resolve of active)
    conn = sqlite3.connect(str(tmp_path / "s.db"))
    HandoffService(conn, ConversationStatusStore(conn)).seat_takeover(sid)
    conn.close()
    cw.set_conversation_status.reset_mock()
    assert client.post(f"/handoffs/{handoff_id}/resolve").status_code == 200
    assert ConversationStatusStore(sqlite3.connect(str(tmp_path / "s.db"))).get_mode(sid) == "ai"
    cw.set_conversation_status.assert_awaited_with(sid, "pending")


@pytest.mark.asyncio
async def test_agent_bot_entry_request_hangs(tmp_path):
    """Seam AgentBot: explicit request hang without Graph / without human mode."""
    from ai_service_agent.harness.agent_bot import handle_agent_bot_message
    from ai_service_agent.harness.messages import MessageStore

    db = str(tmp_path / "ab.db")
    conn = sqlite3.connect(db)
    status = ConversationStatusStore(conn)
    handoff = HandoffService(conn, status)
    mapping = ChatwootMappingStore(conn)
    store = MessageStore(conn)
    session = status.create_session("u-ab")
    mapping.put(session["conversation_id"], 42, contact_id=1)
    cw = AsyncMock()
    cw.send_bot_message = AsyncMock()
    cw.ensure_conversation = AsyncMock(return_value=42)
    cw.prompt_handoff_request = AsyncMock()

    payload = {
        "event": "message_created",
        "id": "9001",
        "content": "我要转人工",
        "message_type": "incoming",
        "content_type": "text",
        "conversation": {"id": 42},
        "contact": {"id": 1, "identifier": "u-ab", "name": "ab"},
    }
    result = await handle_agent_bot_message(
        payload,
        cw,
        status=status,
        messages_store=store,
        mapping=mapping,
        handoff=handoff,
    )
    assert result is not None
    assert status.get_mode(session["conversation_id"]) == "ai"
    assert handoff.get_awaiting(session["conversation_id"]) is not None
    cw.prompt_handoff_request.assert_awaited()
    conn.close()


def _agentbot_prod_env(tmp_path, name="ab-prod.db"):
    """AgentBot + 生产接线（`_agent_bot_callbacks_factory`），用于入口行为回归。"""
    from ai_service_agent.harness.agent_bot import handle_agent_bot_message
    from ai_service_agent.harness.approval_side_channel import ApprovalSideChannel
    from ai_service_agent.harness.approvals import ApprovalService
    from ai_service_agent.harness.server import _agent_bot_callbacks_factory

    conn = sqlite3.connect(str(tmp_path / name))
    status = ConversationStatusStore(conn)
    handoff = HandoffService(conn, status)
    mapping = ChatwootMappingStore(conn)
    store = MessageStore(conn)
    conv = status.create_session("u-ab")["conversation_id"]
    mapping.put(conv, 42, contact_id=1)
    cw = _fake_chatwoot()

    async def _no_resume(*_args, **_kwargs):
        for event in ():
            yield event

    async def _lock(_thread_id):
        import asyncio

        return asyncio.Lock()

    seat = ApprovalSideChannel(
        store=ApprovalService(conn),
        status=status,
        messages=store,
        chatwoot=cw,
        resume_runner=_no_resume,
        lock_factory=_lock,
        interrupt_callbacks=lambda _conv, _thread: (lambda value: None),
    )
    callbacks = _agent_bot_callbacks_factory(handoff, seat, cw, store)
    counter = {"n": 9200}

    async def send(text):
        counter["n"] += 1
        return await handle_agent_bot_message(
            {
                "event": "message_created",
                "id": str(counter["n"]),
                "content": text,
                "message_type": "incoming",
                "content_type": "text",
                "conversation": {"id": 42},
                "contact": {"id": 1, "identifier": "u-ab", "name": "ab"},
            },
            cw,
            status=status,
            messages_store=store,
            mapping=mapping,
            handoff=handoff,
            callbacks_factory=callbacks,
        )

    return SimpleNamespace(
        conn=conn, handoff=handoff, store=store, cw=cw, conv=conv, send=send
    )


@pytest.mark.asyncio
async def test_agent_bot_re_request_after_resolve_prompts_seat_again(tmp_path):
    """Resolve 之后再申请是新申请：强确认 + 坐席 private note（A 修复的主 bug）。"""
    env = _agentbot_prod_env(tmp_path)
    await env.send("我要转人工")
    assert env.store.list(env.conv)[-1]["content"] == handoff_request_confirm_text()
    assert env.cw.prompt_handoff_request.await_count == 1

    env.handoff.resolve_handoff(env.handoff.get_awaiting(env.conv)["handoff_id"])
    env.cw.prompt_handoff_request.reset_mock()

    await env.send("我要转人工")
    assert env.store.list(env.conv)[-1]["content"] == handoff_request_confirm_text()
    assert env.cw.prompt_handoff_request.await_count == 1
    env.conn.close()


@pytest.mark.asyncio
async def test_agent_bot_re_request_after_cancel_prompts_seat_again(tmp_path):
    """Cancel 之后再申请同样不得被历史 tip 误判为重复。"""
    env = _agentbot_prod_env(tmp_path, "ab-cancel.db")
    await env.send("我要转人工")
    await env.send("取消转人工")
    assert env.handoff.get_awaiting(env.conv) is None
    env.cw.prompt_handoff_request.reset_mock()

    await env.send("我要转人工")
    assert env.store.list(env.conv)[-1]["content"] == handoff_request_confirm_text()
    assert env.cw.prompt_handoff_request.await_count == 1
    env.conn.close()


@pytest.mark.asyncio
async def test_toggle_status_uses_post_not_patch(tmp_path):
    from httpx import Response

    from ai_service_agent.harness.chatwoot import ChatwootClient, ChatwootMappingStore

    calls = []

    def handler(request):
        calls.append((request.method, str(request.url.path), json.loads(request.content)))
        return Response(200, json={"payload": {"success": True, "current_status": "pending"}})

    import httpx

    transport = httpx.MockTransport(handler)
    conn = sqlite3.connect(tmp_path / "m.db")
    mapping = ChatwootMappingStore(conn)
    mapping.put("c1", 555)
    async with httpx.AsyncClient(transport=transport) as http:
        cw = ChatwootClient(
            base_url="http://cw.test",
            account_id=1,
            inbox_id=1,
            api_token="t",
            mapping=mapping,
            client=http,
        )
        await cw.set_conversation_status("c1", "pending")
    assert calls[0][0] == "POST"
    assert calls[0][1].endswith("/conversations/555/toggle_status")
    assert calls[0][2] == {"status": "pending"}


def test_bot_error_activity_does_not_enter_human(tmp_path):
    """Timeout auto-open + system activity: no toast, stay ai, revert pending."""
    db = str(tmp_path / "s.db")
    cw = _fake_chatwoot()
    client = TestClient(
        create_app(
            db,
            model="fake",
            chatwoot=cw,
            chatwoot_webhook_secret="sec",
            chatwoot_bot_error_open_grace_seconds=30,
        )
    )
    sid = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    ChatwootMappingStore(sqlite3.connect(db)).put(sid, 555)
    opened = _post_webhook(
        client, {"event": "conversation_status_changed", "id": 555, "status": "open"}
    )
    assert opened.status_code == 200
    assert ConversationStatusStore(sqlite3.connect(db)).get_mode(sid) == "ai"
    assert not any(
        m["content"] == handoff_joined_notice_text()
        for m in MessageStore(sqlite3.connect(db)).list(sid)
    )
    cw.set_conversation_status.assert_not_awaited()

    activity = {
        "event": "message_created",
        "id": 90,
        "message_type": "activity",
        "content": BOT_ERROR,
        "private": False,
        "conversation": {
            "id": 555,
            "status": "open",
            "meta": {"assignee_type": "AgentBot"},
        },
    }
    assert _post_webhook(client, activity).status_code == 200
    assert ConversationStatusStore(sqlite3.connect(db)).get_mode(sid) == "ai"
    assert not any(
        m["content"] == handoff_joined_notice_text()
        for m in MessageStore(sqlite3.connect(db)).list(sid)
    )
    cw.set_conversation_status.assert_awaited_once_with(sid, "pending")


def test_unassigned_open_reverts_when_grace_is_zero(tmp_path):
    db = str(tmp_path / "s.db")
    cw = _fake_chatwoot()
    client = TestClient(
        create_app(
            db,
            model="fake",
            chatwoot=cw,
            chatwoot_webhook_secret="sec",
            chatwoot_bot_error_open_grace_seconds=0,
        )
    )
    sid = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    ChatwootMappingStore(sqlite3.connect(db)).put(sid, 555)
    assert _post_webhook(
        client, {"event": "conversation_status_changed", "id": 555, "status": "open"}
    ).status_code == 200
    assert ConversationStatusStore(sqlite3.connect(db)).get_mode(sid) == "ai"
    assert not any(
        m["content"] == handoff_joined_notice_text()
        for m in MessageStore(sqlite3.connect(db)).list(sid)
    )
    cw.set_conversation_status.assert_awaited_once_with(sid, "pending")
    cw.set_conversation_status.reset_mock()
    assert _post_webhook(
        client, {"event": "conversation_status_changed", "id": 555, "status": "pending"}
    ).status_code == 200
    cw.set_conversation_status.assert_not_awaited()
    assert not any(
        m["content"] == handoff_returned_to_bot_text()
        for m in MessageStore(sqlite3.connect(db)).list(sid)
    )


def test_bot_error_sentence_on_open_reverts_without_waiting(tmp_path):
    db = str(tmp_path / "s.db")
    cw = _fake_chatwoot()
    client = TestClient(
        create_app(
            db,
            model="fake",
            chatwoot=cw,
            chatwoot_webhook_secret="sec",
            chatwoot_bot_error_open_grace_seconds=30,
        )
    )
    sid = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    ChatwootMappingStore(sqlite3.connect(db)).put(sid, 555)
    assert _post_webhook(
        client,
        {
            "event": "conversation_status_changed",
            "id": 555,
            "status": "open",
            "meta": {"assignee_type": "AgentBot"},
            "messages": [{"message_type": "activity", "content": BOT_ERROR}],
        },
    ).status_code == 200
    assert ConversationStatusStore(sqlite3.connect(db)).get_mode(sid) == "ai"
    cw.set_conversation_status.assert_awaited_once_with(sid, "pending")


def test_bot_error_revert_can_be_disabled(tmp_path):
    db = str(tmp_path / "s.db")
    cw = _fake_chatwoot()
    client = TestClient(
        create_app(
            db,
            model="fake",
            chatwoot=cw,
            chatwoot_webhook_secret="sec",
            chatwoot_revert_bot_error_open=False,
            chatwoot_bot_error_open_grace_seconds=0,
        )
    )
    sid = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    ChatwootMappingStore(sqlite3.connect(db)).put(sid, 555)
    assert _post_webhook(
        client, {"event": "conversation_status_changed", "id": 555, "status": "open"}
    ).status_code == 200
    assert ConversationStatusStore(sqlite3.connect(db)).get_mode(sid) == "ai"
    assert not any(
        m["content"] == handoff_joined_notice_text()
        for m in MessageStore(sqlite3.connect(db)).list(sid)
    )
    cw.set_conversation_status.assert_not_awaited()


def test_self_assign_activity_enters_human(tmp_path):
    """Take over UI: open first (no assignee), then the self-assign activity."""
    db = str(tmp_path / "s.db")
    cw = _fake_chatwoot()
    client = TestClient(
        create_app(
            db,
            model="fake",
            chatwoot=cw,
            chatwoot_webhook_secret="sec",
            chatwoot_bot_error_open_grace_seconds=30,
        )
    )
    sid = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    ChatwootMappingStore(sqlite3.connect(db)).put(sid, 555)
    assert _post_webhook(
        client, {"event": "conversation_status_changed", "id": 555, "status": "open"}
    ).status_code == 200
    assert ConversationStatusStore(sqlite3.connect(db)).get_mode(sid) == "ai"
    activity = {
        "event": "message_created",
        "id": 91,
        "message_type": "activity",
        "content": "Qi self-assigned this conversation",
        "conversation": {"id": 555, "status": "open"},
    }
    assert _post_webhook(client, activity).status_code == 200
    assert ConversationStatusStore(sqlite3.connect(db)).get_mode(sid) == "human"
    assert any(
        m["content"] == handoff_joined_notice_text()
        for m in MessageStore(sqlite3.connect(db)).list(sid)
    )
    cw.set_conversation_status.assert_not_awaited()


def test_conversation_updated_with_user_assignee_enters_human(tmp_path):
    db = str(tmp_path / "s.db")
    cw = _fake_chatwoot()
    client = TestClient(
        create_app(
            db,
            model="fake",
            chatwoot=cw,
            chatwoot_webhook_secret="sec",
            chatwoot_bot_error_open_grace_seconds=30,
        )
    )
    sid = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    ChatwootMappingStore(sqlite3.connect(db)).put(sid, 555)
    assert _post_webhook(
        client, {"event": "conversation_status_changed", "id": 555, "status": "open"}
    ).status_code == 200
    updated = _post_webhook(
        client,
        {
            "event": "conversation_updated",
            "id": 555,
            "status": "open",
            "meta": {
                "assignee_type": "User",
                "assignee": {"id": 7, "name": "Qi", "type": "user"},
            },
        },
    )
    assert updated.status_code == 200
    assert updated.json()["event"]["type"] == "handoff_created"
    assert ConversationStatusStore(sqlite3.connect(db)).get_mode(sid) == "human"
    cw.set_conversation_status.assert_not_awaited()


def test_agent_bot_webhook_acks_before_work(tmp_path, monkeypatch):
    from fastapi import BackgroundTasks

    queued: list = []

    def capture(self, func, *args, **kwargs):
        queued.append(func)

    monkeypatch.setattr(BackgroundTasks, "add_task", capture)
    cw = _fake_chatwoot()
    client = TestClient(
        create_app(
            str(tmp_path / "ack.db"),
            model="fake",
            chatwoot=cw,
            chatwoot_agent_bot_secret="sec",
        )
    )
    body = {
        "event": "message_created",
        "id": 1,
        "content": "你好",
        "message_type": "incoming",
        "content_type": "text",
        "conversation": {"id": 555},
        "contact": {"id": 1, "identifier": "u1"},
    }
    raw = json.dumps(body).encode()
    denied = client.post("/webhooks/agent_bot", content=raw)
    assert denied.status_code == 401
    assert queued == []
    ok = client.post(
        "/webhooks/agent_bot",
        content=raw,
        headers=_signed_headers(raw),
    )
    assert ok.status_code == 200
    assert ok.json()["ok"] is True
    assert len(queued) == 1
    cw.send_bot_message.assert_not_awaited()


def test_runtime_never_calls_toggle_status_open():
    """Bot and customer paths must not request Chatwoot status open."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src"
    pattern = re.compile(r"set_conversation_status\([^)]*[\"']open[\"']", re.S)
    offenders = [str(path.relative_to(root)) for path in root.rglob("*.py") if pattern.search(path.read_text())]
    assert offenders == []
