import hashlib
import hmac
import json
import sqlite3

from fastapi.testclient import TestClient

from ai_service_agent.harness.chatwoot import ChatwootMappingStore
from ai_service_agent.harness.messages import MessageStore
from ai_service_agent.harness.server import create_app
from ai_service_agent.harness.session import ConversationStatusStore


import time

# 每个请求用**新鲜**时间戳：签名验证有 300s 重放窗口，模块级 TS 在长跑全量时
# 会过期，导致所有签名请求 401（与 test_seat_gated_handoff 同一根因）。
def _signed_headers(payload: bytes, secret: str = "sec") -> dict[str, str]:
    ts = str(int(time.time()))
    signature = "sha256=" + hmac.new(
        secret.encode(), ts.encode() + b"." + payload, hashlib.sha256
    ).hexdigest()
    return {"X-Chatwoot-Signature": signature, "X-Chatwoot-Timestamp": ts}


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
                    headers={"X-Chatwoot-Signature": "sha256=deadbeef", "X-Chatwoot-Timestamp": str(int(time.time()))})
    assert r.status_code == 401


def test_webhook_agent_reply_persists(tmp_path):
    conv_id = _human_session(tmp_path)
    client = TestClient(create_app(str(tmp_path / "s.db"), model="fake", chatwoot_webhook_secret="sec"))
    body = {
        "event": "message_created",
        "id": 502,
        "message_type": "outgoing",
        "content": "您好，订单问题请提供订单号",
        "private": False,
        "sender": {"type": "user"},
        "conversation": {"id": 555},
    }
    raw = json.dumps(body).encode()
    r = client.post("/webhooks/chatwoot", content=raw,
                    headers=_signed_headers(raw))
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    rows = MessageStore(sqlite3.connect(tmp_path / "s.db")).list(conv_id)
    assert rows[-1]["role"] == "assistant"
    assert rows[-1]["content"] == "您好，订单问题请提供订单号"
    assert rows[-1]["message_id"] == "chatwoot-502"


def test_webhook_ignores_agent_message_without_id(tmp_path):
    conv_id = _human_session(tmp_path)
    client = TestClient(create_app(str(tmp_path / "s.db"), model="fake", chatwoot_webhook_secret="sec"))
    body = {
        "event": "message_created",
        "message_type": "outgoing",
        "content": "您好，订单问题请提供订单号",
        "private": False,
        "sender": {"type": "user"},
        "conversation": {"id": 555},
    }
    raw = json.dumps(body).encode()
    r = client.post("/webhooks/chatwoot", content=raw,
                    headers=_signed_headers(raw))

    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert MessageStore(sqlite3.connect(tmp_path / "s.db")).list(conv_id) == []


def test_webhook_ignores_non_agent_and_private(tmp_path):
    conv_id = _human_session(tmp_path)
    client = TestClient(create_app(str(tmp_path / "s.db"), model="fake", chatwoot_webhook_secret="sec"))
    for body in [
        {"event": "message_created", "message_type": "incoming", "content": "hi",
         "private": False, "sender": {"type": "Contact"}, "conversation": {"id": 555}},
        {"event": "message_created", "message_type": "outgoing", "content": "内部备注",
         "private": True, "sender": {"type": "user"}, "conversation": {"id": 555}},
        {"event": "conversation.created", "message_type": "outgoing", "content": "x",
         "private": False, "sender": {"type": "user"}, "conversation": {"id": 555}},
    ]:
        raw = json.dumps(body).encode()
        r = client.post("/webhooks/chatwoot", content=raw, headers=_signed_headers(raw))
        assert r.status_code == 200
    rows = MessageStore(sqlite3.connect(tmp_path / "s.db")).list(conv_id)
    assert rows == []


def test_webhook_ignores_unknown_conversation_and_ai_mode(tmp_path):
    client = TestClient(create_app(str(tmp_path / "s.db"), model="fake", chatwoot_webhook_secret="sec"))
    body = {"event": "message_created", "message_type": "outgoing", "content": "hi",
            "private": False, "sender": {"type": "user"}, "conversation": {"id": 99999}}
    raw = json.dumps(body).encode()
    r = client.post("/webhooks/chatwoot", content=raw, headers=_signed_headers(raw))
    assert r.status_code == 200

    conn = sqlite3.connect(tmp_path / "s.db")
    status = ConversationStatusStore(conn)
    info = status.create_session("u1")
    ChatwootMappingStore(conn).put(info["conversation_id"], 777)
    conn.close()
    client2 = TestClient(create_app(str(tmp_path / "s.db"), model="fake", chatwoot_webhook_secret="sec"))
    body2 = {"event": "message_created", "message_type": "outgoing", "content": "ai 车道不应收到",
             "private": False, "sender": {"type": "user"}, "conversation": {"id": 777}}
    raw2 = json.dumps(body2).encode()
    r2 = client2.post("/webhooks/chatwoot", content=raw2, headers=_signed_headers(raw2))
    assert r2.status_code == 200
    rows = MessageStore(sqlite3.connect(tmp_path / "s.db")).list(info["conversation_id"])
    assert rows == []



def test_webhook_skips_runtime_echo(tmp_path):
    """Runtime 自己创建的 Chatwoot 消息回显（message_created webhook）不应重复落库。"""
    conn = sqlite3.connect(tmp_path / "s.db")
    status = ConversationStatusStore(conn)
    info = status.create_session("u1")
    status.set_mode(info["conversation_id"], "human")
    mapping = ChatwootMappingStore(conn)
    mapping.put(info["conversation_id"], 555)
    mapping.put_runtime_message(88, info["conversation_id"])
    conn.close()

    client = TestClient(create_app(str(tmp_path / "s.db"), model="fake", chatwoot_webhook_secret="sec"))
    body = {
        "id": 88,
        "event": "message_created",
        "message_type": "outgoing",
        "content": "已为你转接人工客服，坐席将尽快回复。",
        "private": False,
        "sender": {"type": "user"},
        "conversation": {"id": 555},
    }
    raw = json.dumps(body).encode()
    r = client.post("/webhooks/chatwoot", content=raw,
                    headers=_signed_headers(raw))
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    rows = MessageStore(sqlite3.connect(tmp_path / "s.db")).list(info["conversation_id"])
    assert rows == []
