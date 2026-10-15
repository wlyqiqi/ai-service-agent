"""人工车道端到端单链路（离线）：申请挂起 → Take over → 坐席消息 → resolve。

用真实 ChatwootClient + httpx.MockTransport；Seat-gated：显式转人工只挂起，
Account Webhook open 才进 human + Joined Notice。
"""

import hashlib
import hmac
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from httpx import AsyncClient, MockTransport, Response
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from ai_service_agent.harness.chatwoot import ChatwootClient
from ai_service_agent.graph.workflows.copy import (
    handoff_joined_notice_text,
    handoff_request_confirm_text,
)
from ai_service_agent.harness.messages import MessageStore
from ai_service_agent.harness.server import create_app
from ai_service_agent.harness.session import ConversationStatusStore
from ai_service_agent.testing import ToolCallingFakeModel, router_json_message

import time

TS = str(int(time.time()))


def _sig(payload: bytes, secret: str = "sec") -> str:
    return "sha256=" + hmac.new(secret.encode(), TS.encode() + b"." + payload, hashlib.sha256).hexdigest()


def parse_sse(text: str) -> list[dict]:
    events = []
    for block in text.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


class HandoffE2EFakeModel(ToolCallingFakeModel):
    """Router 恒为 unknown/read -> Fallback（显式转人工由入口层 detector 拦截，不进图）；
    fallback explain_llm 返回友好兜底文案（FALLBACK_SYSTEM_PROMPT）。"""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        system = next((m.content for m in messages if getattr(m, "type", "") == "system"), "")
        if "无法直接处理" in system:
            return ChatResult(generations=[ChatGeneration(
                message=AIMessage(content="抱歉，暂时无法直接处理，您可以换个说法或让我为您转接人工客服。")
            )])
        return ChatResult(generations=[ChatGeneration(message=router_json_message("unknown", 0.5))])


class AgentHistoryFakeModel(ToolCallingFakeModel):
    """路由后统计 AI 历史中来自坐席的标记消息，验证 resolve 时上下文已水合。"""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        system = next((m.content for m in messages if getattr(m, "type", "") == "system"), "")
        if "客服路由意图分类器" in system:
            return ChatResult(generations=[ChatGeneration(
                message=AIMessage(content='{"route_intent":"faq","confidence":0.9}')
            )])

        agent_messages = [
            m for m in messages
            if getattr(m, "type", "") == "ai" and "坐席已处理" in (m.content or "")
        ]
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=f"agent-history-{len(agent_messages)}"))])


def _chatwoot_client(db_path, calls: list) -> ChatwootClient:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    from ai_service_agent.harness.chatwoot import ChatwootMappingStore

    mapping = ChatwootMappingStore(conn)

    def handler(request):
        calls.append((request.method, str(request.url), request.content))
        path = request.url.path
        if path.endswith("/contacts"):
            return Response(200, json={"payload": {"contact": {"id": 900}}})
        if path.endswith("/conversations"):
            return Response(200, json={"id": 111, "messages": [{"id": 500, "content": "首条"}]})
        if request.method == "GET" and path.endswith("/conversations/111/labels"):
            return Response(200, json={"payload": []})
        if request.method == "POST" and path.endswith("/conversations/111/labels"):
            return Response(200, json={"payload": ["handoff-request"]})
        if request.method == "POST" and path.endswith("/toggle_status"):
            return Response(200, json={"payload": {"success": True}})
        if request.method == "GET" and "/conversations/111/messages" in path:
            return Response(200, json={"payload": [
                {"id": 502, "content": "坐席已处理地址", "message_type": 1,
                 "private": False, "sender": {"type": "user"}},
                {"id": 503, "content": "坐席已处理退款", "message_type": 1,
                 "private": False, "sender": {"type": "user"}},
            ]})
        if request.method == "POST" and "/conversations/111/messages" in path:
            return Response(200, json={"id": 501, "content": "x"})
        return Response(404)

    http = AsyncClient(transport=MockTransport(handler))
    return ChatwootClient("http://cw.test", 3, 2, "tok", mapping, client=http)


def _mode(db_path, conv_id) -> str:
    return ConversationStatusStore(sqlite3.connect(db_path)).get_mode(conv_id)


def _rows(db_path, conv_id) -> list[dict]:
    return MessageStore(sqlite3.connect(db_path)).list(conv_id)


def _request_then_takeover(client, session_id: str) -> str:
    with client.stream("POST", "/chat", json={"session_id": session_id, "message": "我要转人工"}) as r:
        events = parse_sse("".join(r.iter_text()))
    handoff_id = next(e["data"]["handoff_id"] for e in events if e["type"] == "handoff_requested")
    raw_open = json.dumps({"event": "conversation_status_changed", "id": 111, "status": "open"}).encode()
    assert client.post(
        "/webhooks/chatwoot",
        content=raw_open,
        headers={"X-Chatwoot-Signature": _sig(raw_open), "X-Chatwoot-Timestamp": TS},
    ).status_code == 200
    return handoff_id


def test_resolve_syncs_chatwoot_agent_messages_into_ai_history(tmp_path):
    db = str(tmp_path / "sync.db")
    calls: list = []
    cw = _chatwoot_client(db, calls)
    model = AgentHistoryFakeModel(responses=[])
    client = TestClient(create_app(db, model=model, chatwoot=cw, chatwoot_webhook_secret="sec"))

    session_id = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    handoff_id = _request_then_takeover(client, session_id)

    assert client.post(f"/handoffs/{handoff_id}/resolve").status_code == 200

    rows = _rows(db, session_id)
    agent_rows = [row for row in rows if row["message_id"] in {"chatwoot-502", "chatwoot-503"}]
    assert [row["content"] for row in agent_rows] == ["坐席已处理地址", "坐席已处理退款"]

    with client.stream("POST", "/chat", json={"session_id": session_id, "message": "你好"}) as r:
        text = "".join(r.iter_text())

    assert "agent-history-2" in text


def test_repeated_resolve_does_not_sync_chatwoot_again(tmp_path):
    db = str(tmp_path / "repeat.db")
    calls: list = []
    cw = _chatwoot_client(db, calls)
    model = AgentHistoryFakeModel(responses=[])
    client = TestClient(create_app(db, model=model, chatwoot=cw, chatwoot_webhook_secret="sec"))

    session_id = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    handoff_id = _request_then_takeover(client, session_id)

    first = client.post(f"/handoffs/{handoff_id}/resolve")
    assert first.status_code == 200
    assert first.json()["handoff_id"] == handoff_id and first.json()["mode"] == "ai"
    assert first.json()["event"]["type"] == "handoff_resolved"
    sync_calls = sum(method == "GET" and "/conversations/111/messages" in url for method, url, _ in calls)
    assert sync_calls == 1

    second = client.post(f"/handoffs/{handoff_id}/resolve")
    assert second.status_code == 200 and second.json()["mode"] == "ai"
    assert sum(method == "GET" and "/conversations/111/messages" in url for method, url, _ in calls) == sync_calls


def test_resolve_survives_chatwoot_sync_failure(tmp_path):
    db = str(tmp_path / "failure.db")
    calls: list = []
    cw = _chatwoot_client(db, calls)

    async def failing_list_messages(conversation_id):
        calls.append(("list", conversation_id))
        raise RuntimeError("chatwoot unavailable")

    cw.list_messages = failing_list_messages
    model = AgentHistoryFakeModel(responses=[])
    client = TestClient(create_app(db, model=model, chatwoot=cw, chatwoot_webhook_secret="sec"))

    session_id = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    handoff_id = _request_then_takeover(client, session_id)

    response = client.post(f"/handoffs/{handoff_id}/resolve")
    assert response.status_code == 200
    assert response.json()["handoff_id"] == handoff_id and response.json()["mode"] == "ai"
    assert _mode(db, session_id) == "ai"
    assert ("list", session_id) in calls


def test_resolve_sync_does_not_duplicate_webhook_persisted_agent_message(tmp_path):
    db = str(tmp_path / "dedupe.db")
    calls: list = []
    cw = _chatwoot_client(db, calls)
    model = AgentHistoryFakeModel(responses=[])
    client = TestClient(create_app(db, model=model, chatwoot=cw, chatwoot_webhook_secret="sec"))

    session_id = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    with client.stream("POST", "/chat", json={"session_id": session_id, "message": "我要转人工"}) as r:
        events = parse_sse("".join(r.iter_text()))
    handoff_id = next(e["data"]["handoff_id"] for e in events if e["type"] == "handoff_requested")
    raw_open = json.dumps({"event": "conversation_status_changed", "id": 111, "status": "open"}).encode()
    assert client.post(
        "/webhooks/chatwoot", content=raw_open,
        headers={"X-Chatwoot-Signature": _sig(raw_open), "X-Chatwoot-Timestamp": TS},
    ).status_code == 200

    payload = {
        "event": "message_created",
        "message_type": "outgoing",
        "content": "坐席已处理地址",
        "private": False,
        "sender": {"type": "user"},
        "conversation": {"id": 111},
        "id": 502,
    }
    raw = json.dumps(payload).encode()
    assert client.post(
        "/webhooks/chatwoot", content=raw,
        headers={"X-Chatwoot-Signature": _sig(raw), "X-Chatwoot-Timestamp": TS},
    ).status_code == 200

    assert client.post(f"/handoffs/{handoff_id}/resolve").status_code == 200
    rows = [row for row in _rows(db, session_id) if row["message_id"] == "chatwoot-502"]
    assert len(rows) == 1
    assert rows[0]["role"] == "assistant"
    assert rows[0]["content"] == "坐席已处理地址"


def test_human_lane_end_to_end(tmp_path):
    db = str(tmp_path / "s.db")
    calls: list = []
    cw = _chatwoot_client(db, calls)
    model = HandoffE2EFakeModel(responses=[])
    client = TestClient(create_app(db, model=model, chatwoot=cw, chatwoot_webhook_secret="sec"))
    confirm = handoff_request_confirm_text()
    joined = handoff_joined_notice_text()

    r = client.post("/sessions", json={"user_id": "u1"})
    assert r.status_code == 200
    body = r.json()
    sid = body["session_id"]
    assert body["mode"] == "ai"

    # 1) Handoff Request hang — mode stays ai
    with client.stream("POST", "/chat", json={"session_id": sid, "message": "我要转人工"}) as r:
        events = parse_sse("".join(r.iter_text()))
    types = [e["type"] for e in events]
    assert "handoff_requested" in types, f"missing handoff_requested: {types}"
    assert "handoff_created" not in types
    handoff_id = next(e["data"]["handoff_id"] for e in events if e["type"] == "handoff_requested")
    assert _mode(db, sid) == "ai"
    row = sqlite3.connect(db).execute(
        "SELECT chatwoot_conversation_id FROM chatwoot_conversation WHERE conversation_id=?", (sid,)
    ).fetchone()
    assert row is not None and row[0] == 111

    # 2) Seat Takeover via Account Webhook open
    raw_open = json.dumps({"event": "conversation_status_changed", "id": 111, "status": "open"}).encode()
    assert client.post(
        "/webhooks/chatwoot", content=raw_open,
        headers={"X-Chatwoot-Signature": _sig(raw_open), "X-Chatwoot-Timestamp": TS},
    ).status_code == 200
    assert _mode(db, sid) == "human"

    # 3) Human lane message
    with client.stream("POST", "/chat", json={"session_id": sid, "message": "在吗"}) as r:
        events = parse_sse("".join(r.iter_text()))
    assert events[-1]["type"] == "done"
    rows3 = _rows(db, sid)
    assert any(m["role"] == "user" and m["content"] == "在吗" for m in rows3)
    assert sum(m["content"] == confirm for m in rows3) == 1
    assert sum(m["content"] == joined for m in rows3) == 1
    assert any("/conversations/111/messages" in url for (_, url, _) in calls)

    # 4) Seat agent webhook reply
    payload = {
        "event": "message_created",
        "message_type": "outgoing",
        "content": "您好，已为您加急处理",
        "private": False,
        "sender": {"type": "user"},
        "conversation": {"id": 111},
        "id": 502,
    }
    raw = json.dumps(payload).encode()
    r = client.post(
        "/webhooks/chatwoot", content=raw,
        headers={"X-Chatwoot-Signature": _sig(raw), "X-Chatwoot-Timestamp": TS},
    )
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert _rows(db, sid)[-1]["role"] == "assistant" and _rows(db, sid)[-1]["content"] == "您好，已为您加急处理"

    r = client.get(f"/conversations/{sid}/messages")
    data = r.json()
    assert data["mode"] == "human"
    contents = [m["content"] for m in data["items"]]
    assert contents == [
        "我要转人工",
        confirm,
        joined,
        "在吗",
        "您好，已为您加急处理",
    ]

    r = client.post(f"/handoffs/{handoff_id}/resolve")
    assert r.status_code == 200
    assert r.json()["mode"] == "ai"
    assert _mode(db, sid) == "ai"
    rec = sqlite3.connect(db).execute(
        "SELECT status FROM handoff_record WHERE handoff_id=?", (handoff_id,)
    ).fetchone()
    assert rec is not None and rec[0] == "resolved"

    with client.stream("POST", "/chat", json={"session_id": sid, "message": "你好"}) as r:
        events = parse_sse("".join(r.iter_text()))
    assert events[-1]["type"] == "done"
    assert _mode(db, sid) == "ai"
    rows7 = _rows(db, sid)
    assert rows7[-1]["role"] == "assistant"
    assert rows7[-1]["content"] == "抱歉，暂时无法直接处理，您可以换个说法或让我为您转接人工客服。"



