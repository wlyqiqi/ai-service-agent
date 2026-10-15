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
    handoff_joined_notice_text,
    handoff_request_confirm_text,
)
from ai_service_agent.harness.chatwoot import ChatwootMappingStore
from ai_service_agent.harness.handoff import STATUS_AWAITING, HandoffService
from ai_service_agent.harness.messages import MessageStore
from ai_service_agent.harness.server import create_app
from ai_service_agent.harness.session import ConversationStatusStore
from ai_service_agent.testing import ToolCallingFakeModel


TS = str(int(time.time()))


def _sig(payload: bytes, secret: str = "sec") -> str:
    return "sha256=" + hmac.new(
        secret.encode(), TS.encode() + b"." + payload, hashlib.sha256
    ).hexdigest()


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
    client = TestClient(create_app(str(tmp_path / "s.db"), model="fake"))
    sid = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    for msg in ("我要转人工", "再转一次人工"):
        with client.stream("POST", "/chat", json={"session_id": sid, "message": msg}) as resp:
            "".join(resp.iter_text())
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


# --- Seam 2: Account Webhook ---


def test_webhook_open_seat_takeover(tmp_path):
    db = str(tmp_path / "s.db")
    cw = _fake_chatwoot()
    client = TestClient(create_app(db, model="fake", chatwoot=cw, chatwoot_webhook_secret="sec"))
    sid = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    with client.stream("POST", "/chat", json={"session_id": sid, "message": "我要转人工"}) as resp:
        "".join(resp.iter_text())
    ChatwootMappingStore(sqlite3.connect(db)).put(sid, 555)

    body = {"event": "conversation_status_changed", "id": 555, "status": "open"}
    raw = json.dumps(body).encode()
    r = client.post(
        "/webhooks/chatwoot",
        content=raw,
        headers={"X-Chatwoot-Signature": _sig(raw), "X-Chatwoot-Timestamp": TS},
    )
    assert r.status_code == 200
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
    body = {"event": "conversation_status_changed", "conversation": {"id": 777, "status": "open"}}
    raw = json.dumps(body).encode()
    assert client.post(
        "/webhooks/chatwoot",
        content=raw,
        headers={"X-Chatwoot-Signature": _sig(raw), "X-Chatwoot-Timestamp": TS},
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
    raw_open = json.dumps({"event": "conversation_status_changed", "id": 555, "status": "open"}).encode()
    client.post(
        "/webhooks/chatwoot",
        content=raw_open,
        headers={"X-Chatwoot-Signature": _sig(raw_open), "X-Chatwoot-Timestamp": TS},
    )
    cw.set_conversation_status.reset_mock()
    raw_res = json.dumps({"event": "conversation_status_changed", "id": 555, "status": "resolved"}).encode()
    assert client.post(
        "/webhooks/chatwoot",
        content=raw_res,
        headers={"X-Chatwoot-Signature": _sig(raw_res), "X-Chatwoot-Timestamp": TS},
    ).status_code == 200
    assert ConversationStatusStore(sqlite3.connect(db)).get_mode(sid) == "ai"
    cw.set_conversation_status.assert_awaited_with(sid, "pending")


def test_webhook_pending_ignored(tmp_path):
    db = str(tmp_path / "s.db")
    client = TestClient(create_app(db, model="fake", chatwoot_webhook_secret="sec"))
    sid = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    ChatwootMappingStore(sqlite3.connect(db)).put(sid, 555)
    raw = json.dumps({"event": "conversation_status_changed", "id": 555, "status": "pending"}).encode()
    assert client.post(
        "/webhooks/chatwoot",
        content=raw,
        headers={"X-Chatwoot-Signature": _sig(raw), "X-Chatwoot-Timestamp": TS},
    ).status_code == 200
    assert ConversationStatusStore(sqlite3.connect(db)).get_mode(sid) == "ai"


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
