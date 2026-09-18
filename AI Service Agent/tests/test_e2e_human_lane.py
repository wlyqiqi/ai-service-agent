"""人工车道端到端单链路（离线）：用户 -> Runtime -> Chatwoot(坐席) -> webhook -> Message Store -> 轮询 -> resolve 恢复。

用真实 ChatwootClient + httpx.MockTransport 覆盖映射表 / 回显去重；
P0 起显式转人工（我要转人工）由入口层 detector 前置拦截 -> request_handoff -> _emit_handoff
（落 NOTICE + Chatwoot ensure）；fake model 仅作 AI 车道占位。
"""

import hashlib
import hmac
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from httpx import AsyncClient, MockTransport, Response
from langchain_core.outputs import ChatGeneration, ChatResult

from ai_service_agent.harness.chatwoot import ChatwootClient
from ai_service_agent.harness.handoff import HANDOFF_NOTICE
from ai_service_agent.harness.messages import MessageStore
from ai_service_agent.harness.server import create_app
from ai_service_agent.harness.session import ConversationStatusStore
from ai_service_agent.testing import ToolCallingFakeModel, router_json_message

TS = "1700000000"


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
    """Router 恒为 unknown/read -> Fallback（显式转人工由入口层 detector 拦截，不进图）。"""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=router_json_message("unknown", "read", 0.5))])


def _chatwoot_client(db_path, calls: list) -> ChatwootClient:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    from ai_service_agent.harness.chatwoot import ChatwootMappingStore

    mapping = ChatwootMappingStore(conn)

    def handler(request):
        calls.append((request.method, str(request.url), request.content))
        if request.url.path.endswith("/contacts"):
            return Response(200, json={"payload": {"contact": {"id": 900}}})
        if request.url.path.endswith("/conversations"):
            return Response(200, json={"id": 111, "messages": [{"id": 500, "content": "首条"}]})
        if "/conversations/111/messages" in request.url.path:
            return Response(200, json={"id": 501, "content": "x"})
        return Response(404)

    http = AsyncClient(transport=MockTransport(handler))
    return ChatwootClient("http://cw.test", 3, 2, "tok", mapping, client=http)


def _mode(db_path, conv_id) -> str:
    return ConversationStatusStore(sqlite3.connect(db_path)).get_mode(conv_id)


def _rows(db_path, conv_id) -> list[dict]:
    return MessageStore(sqlite3.connect(db_path)).list(conv_id)


def test_human_lane_end_to_end(tmp_path):
    db = str(tmp_path / "s.db")
    calls: list = []
    cw = _chatwoot_client(db, calls)
    model = HandoffE2EFakeModel(responses=[])
    client = TestClient(create_app(db, model=model, chatwoot=cw, chatwoot_webhook_secret="sec"))

    # 1) 建会话（mode=ai）
    r = client.post("/sessions", json={"user_id": "u1"})
    assert r.status_code == 200
    body = r.json()
    sid = body["session_id"]
    assert body["mode"] == "ai"

    # 2) 转人工：handoff_created + mode=human + Chatwoot conversation 建立（mapping 落库）
    with client.stream("POST", "/chat", json={"session_id": sid, "message": "我要转人工"}) as r:
        events = parse_sse("".join(r.iter_text()))
    types = [e["type"] for e in events]
    assert "handoff_created" in types, f"missing handoff_created: {types}"
    handoff_id = next(e["data"]["handoff_id"] for e in events if e["type"] == "handoff_created")
    assert _mode(db, sid) == "human"
    row = sqlite3.connect(db).execute(
        "SELECT chatwoot_conversation_id FROM chatwoot_conversation WHERE conversation_id=?", (sid,)
    ).fetchone()
    assert row is not None and row[0] == 111

    # 3) 人工车道发消息：Message Store 落 user + 推 Chatwoot（sender_type=Contact）
    with client.stream("POST", "/chat", json={"session_id": sid, "message": "在吗"}) as r:
        events = parse_sse("".join(r.iter_text()))
    assert events[-1]["type"] == "done"
    rows3 = _rows(db, sid)
    assert rows3[-1]["role"] == "assistant" and rows3[-1]["content"] == "已转人工，坐席将尽快回复。"
    assert any(m["role"] == "user" and m["content"] == "在吗" for m in rows3)
    assert any("/conversations/111/messages" in url for (_, url, _) in calls)

    # 4) 坐席 webhook 回复 -> Message Store 追加 assistant
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

    # 5) GET messages：按序返回 user/assistant + mode=human
    r = client.get(f"/conversations/{sid}/messages")
    data = r.json()
    assert data["mode"] == "human"
    contents = [m["content"] for m in data["items"]]
    assert contents == ["我要转人工", HANDOFF_NOTICE, "在吗", "已转人工，坐席将尽快回复。", "您好，已为您加急处理"]

    # 6) resolve -> mode=ai + record resolved
    r = client.post(f"/handoffs/{handoff_id}/resolve")
    assert r.status_code == 200
    assert r.json()["mode"] == "ai"
    assert _mode(db, sid) == "ai"
    rec = sqlite3.connect(db).execute(
        "SELECT status FROM handoff_record WHERE handoff_id=?", (handoff_id,)
    ).fetchone()
    assert rec is not None and rec[0] == "resolved"

    # 7) resolve 后下一条消息走正常 AI 车道（有 assistant 回复 + done）
    with client.stream("POST", "/chat", json={"session_id": sid, "message": "你好"}) as r:
        events = parse_sse("".join(r.iter_text()))
    assert events[-1]["type"] == "done"
    assert _mode(db, sid) == "ai"
    # fallback explain 为确定性节点不流式 token；断言 Message Store 已落新的 assistant 回复
    rows7 = _rows(db, sid)
    assert rows7[-1]["role"] == "assistant" and rows7[-1]["content"] == "我暂时无法处理，请换个说法或联系人工客服。"



