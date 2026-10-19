import sqlite3
import time
import pytest

from fastapi.testclient import TestClient

from ai_service_agent.harness.approvals import ApprovalService
from ai_service_agent.harness.handoff import HandoffService
from ai_service_agent.harness.messages import MessageStore
from ai_service_agent.harness.server import create_app
from ai_service_agent.harness.session import ConversationStatusStore
from ai_service_agent.testing import ApprovalFlowFakeModel

SEAT_SECRET = "test-seat-approval-secret"
SEAT_HEADERS = {
    "X-Seat-Approval-Credential": SEAT_SECRET,
    "X-Seat-Actor-Id": "seat-1",
}
from ai_service_agent.testing import ToolCallingFakeModel, router_json_message, select_json_message
from ai_service_agent.graph.workflows.copy import form_plain_text_refuse_text
from langchain_core.messages import AIMessage
from ai_service_agent.tools import write as write_mod
from ai_service_agent.tools import orders as orders_mod
from ai_service_agent.graph import builder as builder_mod
from ai_service_agent.harness import server as server_module


@pytest.fixture(autouse=True)
def isolated_business_db(monkeypatch, tmp_path):
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("seed_mock_db_server", root / "scripts" / "seed_mock_db.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    db_path = tmp_path / "mock_ecommerce.db"
    module.build_database(str(db_path))
    real_settings = builder_mod.get_settings()

    class _Settings:
        mock_ecommerce_db_path = str(db_path)
        def __init__(self, real):
            self.__dict__.update(real.__dict__)
            self.mock_ecommerce_db_path = str(db_path)

    monkeypatch.setattr(builder_mod, "get_settings", lambda: _Settings(real_settings))
    monkeypatch.setattr(server_module, "get_settings", lambda: _Settings(real_settings))


def test_sessions_creates_ai_mode(tmp_path):
    client = TestClient(create_app(str(tmp_path / "s.db"), model="fake"))
    r = client.post("/sessions", json={"user_id": "u1"})
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "ai"
    assert body["session_id"]


def test_frontend_config_exposes_chatwoot_widget_settings(tmp_path):
    client = TestClient(create_app(str(tmp_path / "s.db"), model="fake"))
    body = client.get("/config").json()
    assert "chatwoot_base_url" in body
    assert "chatwoot_website_token" in body


def test_chatwoot_identity_requires_auth_and_signs_user_id(tmp_path):
    import hashlib
    import hmac
    import sqlite3

    from ai_service_agent.harness.auth import UserStore

    db = str(tmp_path / "identity.db")
    conn = sqlite3.connect(db)
    UserStore(conn).upsert("u1", "+65-81234567", "张伟", "123456")
    conn.close()
    client = TestClient(
        create_app(
            db,
            model="fake",
            auth_enabled=True,
            jwt_secret="test-secret-with-at-least-32-bytes!!",
            chatwoot_hmac_secret="widget-hmac",
        )
    )
    token = client.post("/auth/login", json={"phone": "+65-81234567", "password": "123456"}).json()["token"]

    assert client.get("/me/chatwoot_identity").status_code == 401
    response = client.get("/me/chatwoot_identity", headers={"Authorization": f"Bearer {token}"})

    expected = hmac.new(b"widget-hmac", b"u1", hashlib.sha256).hexdigest()
    assert response.status_code == 200
    assert response.json() == {"identifier": "u1", "identifier_hash": expected}


def test_chatwoot_identity_fails_closed_without_hmac(tmp_path):
    import sqlite3

    from ai_service_agent.harness.auth import UserStore

    db = str(tmp_path / "identity-no-hmac.db")
    conn = sqlite3.connect(db)
    UserStore(conn).upsert("u1", "+65-81234567", "张伟", "123456")
    conn.close()
    client = TestClient(
        create_app(db, model="fake", auth_enabled=True, jwt_secret="test-secret-with-at-least-32-bytes!!")
    )
    token = client.post("/auth/login", json={"phone": "+65-81234567", "password": "123456"}).json()["token"]

    assert client.get("/me/chatwoot_identity", headers={"Authorization": f"Bearer {token}"}).status_code == 503


def test_conversation_mode_header_while_human(tmp_path):
    import sqlite3

    from ai_service_agent.harness.auth import UserStore
    from ai_service_agent.harness.messages import MessageStore
    from ai_service_agent.harness.session import ConversationStatusStore

    db = str(tmp_path / "mode-header.db")
    conn = sqlite3.connect(db)
    UserStore(conn).upsert("u1", "+65-81234567", "张伟", "123456")
    status = ConversationStatusStore(conn)
    info = status.create_session("u1")
    MessageStore(conn).append(info["conversation_id"], "user", "你好")
    conn.close()
    client = TestClient(
        create_app(db, model="fake", auth_enabled=True, jwt_secret="test-secret-with-at-least-32-bytes!!")
    )
    token = client.post("/auth/login", json={"phone": "+65-81234567", "password": "123456"}).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/me/conversation_mode", headers=headers).json() == {"mode": "ai", "header": None}
    conn = sqlite3.connect(db)
    ConversationStatusStore(conn).set_mode(info["conversation_id"], "human")
    conn.close()
    assert client.get("/me/conversation_mode", headers=headers).json() == {
        "mode": "human",
        "header": "人工服务中",
    }


def test_agent_bot_webhook_requires_valid_signature(tmp_path):
    import hashlib
    import hmac
    import json

    payload = {
        "event": "message_created",
        "id": 601,
        "content": "你好",
        "message_type": "incoming",
        "content_type": "text",
        "conversation": {"id": 602},
        "contact": {"id": 1},
    }
    raw = json.dumps(payload).encode()
    timestamp = str(int(time.time()))
    signature = "sha256=" + hmac.new(
        b"agent-bot-secret", timestamp.encode() + b"." + raw, hashlib.sha256
    ).hexdigest()
    client = TestClient(create_app(str(tmp_path / "s.db"), model="fake", chatwoot_agent_bot_secret="agent-bot-secret"))

    missing = client.post("/webhooks/agent_bot", content=raw)
    invalid = client.post(
        "/webhooks/agent_bot",
        content=raw,
        headers={"X-Chatwoot-Signature": "sha256=bad", "X-Chatwoot-Timestamp": timestamp},
    )
    valid = client.post(
        "/webhooks/agent_bot",
        content=raw,
        headers={"X-Chatwoot-Signature": signature, "X-Chatwoot-Timestamp": timestamp},
    )

    assert missing.status_code == 401
    assert invalid.status_code == 401
    assert valid.status_code == 200


def test_chatwoot_webhooks_reject_malformed_signed_payloads(tmp_path):
    import hashlib
    import hmac
    import json
    import time

    secret = "webhook-secret"
    client = TestClient(create_app(str(tmp_path / "s.db"), model="fake", chatwoot_webhook_secret=secret))

    def post(payload):
        raw = json.dumps(payload).encode()
        timestamp = str(int(time.time()))
        signature = "sha256=" + hmac.new(secret.encode(), timestamp.encode() + b"." + raw, hashlib.sha256).hexdigest()
        return client.post(
            "/webhooks/chatwoot", content=raw,
            headers={"X-Chatwoot-Signature": signature, "X-Chatwoot-Timestamp": timestamp},
        )

    assert post([1, 2]).status_code == 200
    assert post({"event": "message_created", "id": 1, "conversation": "bad"}).status_code == 200
    assert post({"event": "message_created", "id": 1, "conversation": {"id": "bad"}}).status_code == 200


def test_resolve_restores_chatwoot_pending(tmp_path):
    from unittest.mock import AsyncMock
    from types import SimpleNamespace

    cw = SimpleNamespace(
        list_messages=AsyncMock(return_value=[]),
        ensure_conversation=AsyncMock(return_value=111),
        set_conversation_status=AsyncMock(),
    )
    client = TestClient(create_app(str(tmp_path / "s.db"), model=KeywordHandoffFakeModel(responses=[]), chatwoot=cw))
    session_id = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    with client.stream("POST", "/chat", json={"session_id": session_id, "message": "我要转人工"}) as r:
        text = "".join(r.iter_text())
    conn = sqlite3.connect(str(tmp_path / "s.db"))
    handoff_id = conn.execute(
        "SELECT handoff_id FROM handoff_record WHERE conversation_id=?", (session_id,)
    ).fetchone()[0]
    conn.close()

    assert client.post(f"/handoffs/{handoff_id}/resolve").status_code == 200
    cw.set_conversation_status.assert_awaited_once_with(session_id, "pending")


def test_approval_decision_sends_result_to_chatwoot(tmp_path):
    from unittest.mock import AsyncMock
    from types import SimpleNamespace

    cw = SimpleNamespace(send_bot_message=AsyncMock(), send_interactive_message=AsyncMock())
    db = str(tmp_path / "approval.db")
    client = TestClient(
        create_app(
            db,
            model=ApprovalFlowFakeModel(responses=[]),
            chatwoot=cw,
            seat_approval_secret=SEAT_SECRET,
        )
    )
    session_id = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    with client.stream("POST", "/chat", json={"session_id": session_id, "message": "给 ORD-PAID-1 申请退款"}) as r:
        "".join(r.iter_text())

    conn = sqlite3.connect(db)
    approval_id = conn.execute("SELECT approval_id FROM approval_record LIMIT 1").fetchone()[0]
    conn.close()
    cw.send_bot_message.reset_mock()
    cw.send_interactive_message.reset_mock()

    response = client.post(f"/approvals/{approval_id}/approve", headers=SEAT_HEADERS)
    assert response.status_code == 200
    cw.send_interactive_message.assert_awaited_once()
    card_args = cw.send_interactive_message.await_args.args
    assert card_args[2] == "cards"
    card = card_args[3]["items"][0]
    assert card["title"] == "退款申请已提交"
    assert card["description"].startswith("• 状态：已完成")
    assert "\n\n" in card["description"]
    assert "ORD-PAID-1" in card["description"]
    assert card["actions"][0]["text"] == "知道了"
    cw.send_bot_message.assert_awaited_once()
    assert "退款申请已提交" in cw.send_bot_message.await_args.args[1]
    assert "通过审核" not in cw.send_bot_message.await_args.args[1]


def test_chat_unknown_session_errors(tmp_path):
    client = TestClient(create_app(str(tmp_path / "s.db"), model="fake"))
    with client.stream("POST", "/chat", json={"session_id": "nope", "message": "hi"}) as r:
        text = "".join(r.iter_text())
    assert "error" in text


def test_human_lane_after_handoff(tmp_path):
    conn = sqlite3.connect(tmp_path / "s.db")
    # 直接构造：先建会话再手动切 human 模拟人工接管
    store = ConversationStatusStore(conn)
    info = store.create_session("u1")
    store.set_mode(info["conversation_id"], "human")
    conn.close()

    client = TestClient(create_app(str(tmp_path / "s.db"), model="fake"))
    with client.stream("POST", "/chat", json={"session_id": info["conversation_id"], "message": "在吗"}) as r:
        text = "".join(r.iter_text())
    assert "已转人工" not in text
    assert '"done"' in text

    # 人工车道消息落 Message Store（唯一真相源）
    conn2 = sqlite3.connect(tmp_path / "s.db")
    rows = MessageStore(conn2).list(info["conversation_id"])
    conn2.close()
    assert [r["role"] for r in rows] == ["user"]


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
    timestamp = str(int(time.time()))
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
    timestamp = str(int(time.time()))
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


def test_ai_chat_persists_user_and_assistant(tmp_path):
    client = TestClient(create_app(str(tmp_path / "s.db"), model="fake"))
    r = client.post("/sessions", json={"user_id": "u1"})
    session_id = r.json()["session_id"]

    with client.stream("POST", "/chat", json={"session_id": session_id, "message": "hi"}) as resp:
        text = "".join(resp.iter_text())
    assert '"done"' in text

    conn = sqlite3.connect(str(tmp_path / "s.db"))
    rows = MessageStore(conn).list(session_id)
    conn.close()
    assert [r["role"] for r in rows] == ["user", "assistant"]
    assert rows[0]["content"] == "hi"
    assert rows[1]["content"]  # assistant 回复已持久化


def test_handoff_resolve_endpoint(tmp_path):
    conn = sqlite3.connect(tmp_path / "s.db")
    status = ConversationStatusStore(conn)
    info = status.create_session("u1")
    handoff = HandoffService(conn, status)
    rec = handoff.create_handoff(info["conversation_id"], "r", "s")
    handoff.seat_takeover(info["conversation_id"])
    conn.close()

    client = TestClient(create_app(str(tmp_path / "s.db"), model="fake"))
    r = client.post(f"/handoffs/{rec['handoff_id']}/resolve")
    assert r.status_code == 200
    assert r.json()["mode"] == "ai"

import asyncio

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from ai_service_agent.harness.graph import make_ai_graph
from ai_service_agent.testing import ToolCallingFakeModel, make_fake_model


class KeywordHandoffFakeModel(ToolCallingFakeModel):
    """离线：按用户输入路由到 fallback，使 check_handoff 关键词命中 -> handoff_request interrupt。"""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        user = ""
        for m in reversed(messages):
            if getattr(m, "type", "") == "human":
                user = m.content or ""
                break
        if "转人工" in user:
            payload = '{"route_intent": "unknown", "confidence": 0.2}'
        else:
            payload = '{"route_intent": "faq", "confidence": 0.9}'
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=payload))])


class ResolveProactiveFakeModel(ToolCallingFakeModel):
    """路由器区分转人工；read agent 回显收到的用户消息条数。"""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        from langchain_core.outputs import ChatGeneration, ChatResult
        system = next((m.content for m in messages if getattr(m, "type", "") == "system"), "")
        user = next((m.content for m in reversed(messages) if getattr(m, "type", "") == "human"), "")
        users = [m for m in messages if getattr(m, "type", "") == "human"]
        if "客服路由意图分类器" in system:
            payload = '{"route_intent": "unknown", "confidence": 0.2}' if "转人工" in user else '{"route_intent": "faq", "confidence": 0.9}'
        else:
            payload = f"echo-{len(users)}"
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=payload))])


def test_resolve_entrance_handoff_then_next_chat_is_answered(tmp_path):
    """P0：入口层显式转人工无图内中断；resolve 切回 ai 后下一条消息走正常 Router 并完成。

    显式转人工在入口层被 detector 拦截（不进图），本 turn 不产生 checkpoint 的
    execution_status；resolve 仅标记 resolved + 切 ai，无图内 resume。
    """
    db = str(tmp_path / "p.db")
    client = TestClient(create_app(db, model=ResolveProactiveFakeModel(responses=[])))
    session_id = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    with client.stream("POST", "/chat", json={"session_id": session_id, "message": "我要转人工"}) as r:
        text1 = "".join(r.iter_text())
    assert "handoff_requested" in text1

    conn = sqlite3.connect(db)
    handoff_id = conn.execute("SELECT handoff_id FROM handoff_record WHERE conversation_id=?", (session_id,)).fetchone()[0]
    thread_id = ConversationStatusStore(conn).get_thread_id(session_id)
    conn.close()

    r = client.post(f"/handoffs/{handoff_id}/resolve")
    assert r.json()["mode"] == "ai"

    # 入口层转人工不产生图内 checkpoint 中断（无子图可 resume）
    async def check_no_interrupt():
        async with AsyncSqliteSaver.from_conn_string(db) as saver:
            graph = make_ai_graph(make_fake_model(), saver)
            st = await graph.aget_state({"configurable": {"thread_id": thread_id}})
            return st.interrupts
    assert not asyncio.run(check_no_interrupt())

    # resolve 后第一条消息走正常 Router 并得到回复（echo-2，两条 user）
    with client.stream("POST", "/chat", json={"session_id": session_id, "message": "你好"}) as r:
        text2 = "".join(r.iter_text())
    assert "echo-2" in text2
    assert '"error"' not in text2

    # 该轮图执行正常完成
    async def check_completed():
        async with AsyncSqliteSaver.from_conn_string(db) as saver:
            graph = make_ai_graph(make_fake_model(), saver)
            st = await graph.aget_state({"configurable": {"thread_id": thread_id}})
            return st.values.get("execution_status")
    assert asyncio.run(check_completed()) == "completed"


def test_actions_confirm_resumes_pending_confirmation(tmp_path, monkeypatch):
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "seed_mock_db_server_confirm", root / "scripts" / "seed_mock_db.py"
    )
    seed_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seed_module)
    real_builder_settings = builder_mod.get_settings()
    class _WriteSettings:
        mock_ecommerce_db_path = str(tmp_path / "isolated-mock.db")

        def __init__(self, real):
            self.__dict__.update(real.__dict__)
            self.mock_ecommerce_db_path = str(tmp_path / "isolated-mock.db")

    isolated_db = tmp_path / "isolated-mock.db"
    seed_module.build_database(str(isolated_db))

    monkeypatch.setattr(write_mod, "get_settings", lambda: _WriteSettings(real_builder_settings))
    monkeypatch.setattr(orders_mod, "get_settings", lambda: _WriteSettings(real_builder_settings))
    monkeypatch.setattr(builder_mod, "get_settings", lambda: _WriteSettings(real_builder_settings))
    model = ToolCallingFakeModel(responses=[
        router_json_message("account_profile_update", 0.9),
        select_json_message("update_address", {"order_id": "ORD-PAID-1", "address": "北京市朝阳区测试路 99 号"}),
    ])
    db = str(tmp_path / "confirm.db")
    client = TestClient(create_app(db, model=model))
    session_id = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    with client.stream("POST", "/chat", json={"session_id": session_id, "message": "改收货地址"}) as resp:
        stream_text = "".join(resp.iter_text())
    assert '"form_required"' in stream_text

    r = client.post(
        "/actions/confirm",
        json={
            "conversation_id": session_id,
            "action": "confirmed",
            "order_id": "ORD-PAID-1",
            "fields": {"address": "北京市朝阳区测试路 99 号"},
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["action"] == "confirm"
    assert "收货地址已修改成功" in body["final"]

    conn = sqlite3.connect(db)
    rows = MessageStore(conn).list(session_id)
    conn.close()
    assert rows[-1]["role"] == "assistant"
    assert "收货地址已修改成功" in rows[-1]["content"]


def test_incomplete_mutation_opens_form_then_confirm_writes(tmp_path, monkeypatch):
    """T7: tool known but empty args → form_required in one /chat turn; Confirm writes once.

    No chat clarification_required / confirmation_required ritual on Mutation happy path.
    """
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "seed_mock_db_server_incomplete_form", root / "scripts" / "seed_mock_db.py"
    )
    seed_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seed_module)
    real_builder_settings = builder_mod.get_settings()

    class _WriteSettings:
        mock_ecommerce_db_path = str(tmp_path / "incomplete-form-mock.db")

        def __init__(self, real):
            self.__dict__.update(real.__dict__)
            self.mock_ecommerce_db_path = str(tmp_path / "incomplete-form-mock.db")

    isolated_db = tmp_path / "incomplete-form-mock.db"
    seed_module.build_database(str(isolated_db))
    monkeypatch.setattr(write_mod, "get_settings", lambda: _WriteSettings(real_builder_settings))
    monkeypatch.setattr(orders_mod, "get_settings", lambda: _WriteSettings(real_builder_settings))
    monkeypatch.setattr(builder_mod, "get_settings", lambda: _WriteSettings(real_builder_settings))

    model = ToolCallingFakeModel(responses=[
        router_json_message("account_profile_update", 0.9),
        select_json_message("update_address", {}),
    ])
    db = str(tmp_path / "incomplete-form.db")
    client = TestClient(create_app(db, model=model))
    session_id = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    with client.stream("POST", "/chat", json={"session_id": session_id, "message": "我要改个地址"}) as resp:
        events = _parse_sse("".join(resp.iter_text()))
    types = [event.get("type") for event in events]
    assert "form_required" in types
    assert "clarification_required" not in types
    assert "confirmation_required" not in types

    r = client.post(
        "/actions/confirm",
        json={
            "conversation_id": session_id,
            "action": "confirmed",
            "order_id": "ORD-PAID-1",
            "fields": {"address": "北京市朝阳区望京 T7 路 1 号"},
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["action"] == "confirm"
    assert "收货地址已修改成功" in body["final"]


def _parse_sse(text: str) -> list[dict]:
    import json

    events = []
    for block in text.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[len("data: "):]))
    return events


def test_known_tool_mutation_form_cancel_resume_does_not_write(tmp_path, monkeypatch):
    """Known-tool Mutation opens a Form Interrupt; Cancel Form Resume does not write.

    Seam: Conversation Runtime /chat + /actions/confirm (issue #16).
    Waiting-on-form is the checkpoint interrupt, not a Form Pending flag.
    """
    import asyncio

    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    from ai_service_agent.domain.orders import SQLiteOrderRepository
    from ai_service_agent.harness.graph import make_ai_graph

    business_db = tmp_path / "mock_ecommerce.db"

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

    original_address = customer_address()
    assert original_address == "123 Chengyuan Avenue #04-01, Singapore"

    model = ToolCallingFakeModel(responses=[
        router_json_message("account_profile_update", 0.9),
        select_json_message("update_address", {"order_id": "ORD-PAID-1", "address": "北京市朝阳区测试路 99 号"}),
    ])
    db = str(tmp_path / "form-cancel.db")
    client = TestClient(create_app(db, model=model))
    session_id = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    with client.stream("POST", "/chat", json={"session_id": session_id, "message": "改收货地址"}) as resp:
        events = _parse_sse("".join(resp.iter_text()))

    form_events = [event for event in events if event.get("type") == "form_required"]
    assert len(form_events) == 1
    form = form_events[0]["data"]
    assert form["tool"] == "update_address"
    assert any(item.get("order_id") == "ORD-PAID-1" for item in form["order_candidates"])
    assert all(item.get("order_id") != "ORD-CANCEL-1" for item in form["order_candidates"])
    assert all(item.get("order_id") != "ORD-1" for item in form["order_candidates"])
    assert form["response_schema"]["properties"]["action"]["enum"] == ["confirm", "cancel"]
    assert not form.get("errors")
    assert "form_pending" not in {event.get("type") for event in events}
    assert "tool_call" not in {event.get("type") for event in events}
    assert customer_address() == original_address

    conn = sqlite3.connect(db)
    thread_id = conn.execute(
        "SELECT thread_id FROM conversation_status WHERE conversation_id = ?",
        (session_id,),
    ).fetchone()[0]
    conn.close()

    async def pending_interrupts():
        async with AsyncSqliteSaver.from_conn_string(db) as saver:
            graph = make_ai_graph(
                model, saver, order_repository=SQLiteOrderRepository(str(business_db)),
            )
            state = await graph.aget_state({"configurable": {"thread_id": thread_id}})
            return state.interrupts

    pending = asyncio.run(pending_interrupts())
    assert pending
    assert pending[0].value["tool"] == "update_address"
    assert pending[0].value["order_candidates"] == form["order_candidates"]
    assert pending[0].response_schema["properties"]["action"]["enum"] == ["confirm", "cancel"]

    with client.stream("POST", "/chat", json={"session_id": session_id, "message": "确认"}) as resp:
        hold_events = _parse_sse("".join(resp.iter_text()))
    assert any(event.get("type") == "form_required" for event in hold_events)
    assert "tool_call" not in {event.get("type") for event in hold_events}
    assert customer_address() == original_address
    assert asyncio.run(pending_interrupts())

    cancelled = client.post(
        "/actions/confirm", json={"conversation_id": session_id, "action": "cancel"},
    )
    assert cancelled.status_code == 200
    body = cancelled.json()
    assert body["ok"] is True and body["action"] == "cancel"
    assert "取消" in body["final"]
    assert "tool_call" not in {event.get("type") for event in body["events"]}
    assert customer_address() == original_address

    cleared = asyncio.run(pending_interrupts())
    assert list(cleared) == []


def test_confirm_form_resume_writes_order_picker_selection_once(tmp_path, monkeypatch):
    """Confirm Form Resume writes the Order Picker selection once through ToolNode.

    Seam: Conversation Runtime /chat + /actions/confirm (issue #17).
    Select may stash different args on pending_tool; the write uses Form Resume
    field values (Form-Only Write), then the Form Interrupt ends.
    """
    import asyncio

    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    from ai_service_agent.domain.orders import SQLiteOrderRepository
    from ai_service_agent.harness.graph import make_ai_graph

    business_db = tmp_path / "mock_ecommerce.db"

    class _WriteSettings:
        mock_ecommerce_db_path = str(business_db)

    monkeypatch.setattr(write_mod, "get_settings", lambda: _WriteSettings())

    select_order_id = "ORD-PAID-1"
    select_address = "SELECT-SHOULD-NOT-WRITE"
    picked_order_id = "ORD-REFUND-1"
    form_address = "表单确认的新地址 88 号"

    def customer_address() -> str:
        conn = sqlite3.connect(business_db)
        try:
            row = conn.execute(
                "SELECT address FROM customers WHERE customer_id = ?", ("u1",)
            ).fetchone()
        finally:
            conn.close()
        return row[0]

    original_address = customer_address()
    model = ToolCallingFakeModel(responses=[
        router_json_message("account_profile_update", 0.9),
        select_json_message(
            "update_address",
            {"order_id": select_order_id, "address": select_address},
        ),
    ])
    db = str(tmp_path / "form-confirm.db")
    client = TestClient(create_app(db, model=model))
    session_id = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    with client.stream("POST", "/chat", json={"session_id": session_id, "message": "改收货地址"}) as resp:
        events = _parse_sse("".join(resp.iter_text()))

    form_events = [event for event in events if event.get("type") == "form_required"]
    assert len(form_events) == 1
    form = form_events[0]["data"]
    candidate_ids = [item.get("order_id") for item in form["order_candidates"]]
    assert select_order_id in candidate_ids
    assert picked_order_id in candidate_ids
    schema_props = form["response_schema"]["properties"]
    assert schema_props["action"]["enum"] == ["confirm", "cancel"]
    assert "order_id" in schema_props
    assert "address" in schema_props
    assert "request_id" not in schema_props
    assert "tool_call" not in {event.get("type") for event in events}
    assert customer_address() == original_address

    confirmed = client.post(
        "/actions/confirm",
        json={
            "conversation_id": session_id,
            "action": "confirm",
            "fields": {
                "order_id": picked_order_id,
                "address": form_address,
                "request_id": "client-forged",
            },
        },
    )
    assert confirmed.status_code == 200
    body = confirmed.json()
    assert body["ok"] is True and body["action"] == "confirm"
    resume_events = body["events"]
    types = [event.get("type") for event in resume_events]
    assert types[-1] == "done"
    tool_calls = [event for event in resume_events if event.get("type") == "tool_call"]
    tool_results = [event for event in resume_events if event.get("type") == "tool_result"]
    assert len(tool_calls) == 1
    assert len(tool_results) == 1
    assert types.index("tool_call") < types.index("tool_result") < types.index("done")
    assert "form_required" not in types
    assert "form_rejected" not in types
    call = tool_calls[0]["data"]
    assert call["name"] == "update_address"
    assert call["args"]["order_id"] == picked_order_id
    assert call["args"]["address"] == form_address
    assert call["args"].get("request_id") not in (None, "", "client-forged")
    assert tool_results[0]["data"]["name"] == "update_address"
    assert tool_results[0]["data"]["ok"] is True
    assert picked_order_id in body["final"]
    assert form_address in body["final"]
    assert select_address not in body["final"]
    assert customer_address() == form_address

    conn = sqlite3.connect(db)
    thread_id = conn.execute(
        "SELECT thread_id FROM conversation_status WHERE conversation_id = ?",
        (session_id,),
    ).fetchone()[0]
    conn.close()

    async def pending_interrupts():
        async with AsyncSqliteSaver.from_conn_string(db) as saver:
            graph = make_ai_graph(
                model, saver, order_repository=SQLiteOrderRepository(str(business_db)),
            )
            state = await graph.aget_state({"configurable": {"thread_id": thread_id}})
            return state.interrupts

    assert list(asyncio.run(pending_interrupts())) == []


def test_confirm_precheck_failure_reissues_form_rejected_same_session(tmp_path, monkeypatch):
    """Confirm validate/precheck failure re-interrupts the same Form session.

    Seam: Conversation Runtime /chat + /actions/confirm (issue #20).
    Client sees form_rejected with errors; form_session_id is unchanged.
    Corrected Form Resume still writes once (Form-Only Write / ToolNode).
    """
    import asyncio

    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    from ai_service_agent.domain.orders import SQLiteOrderRepository
    from ai_service_agent.harness.graph import make_ai_graph

    business_db = tmp_path / "mock_ecommerce.db"

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

    original_address = customer_address()
    model = ToolCallingFakeModel(responses=[
        router_json_message("account_profile_update", 0.9),
        select_json_message(
            "update_address",
            {"order_id": "ORD-PAID-1", "address": "预填地址"},
        ),
    ])
    db = str(tmp_path / "form-rejected.db")
    client = TestClient(create_app(db, model=model))
    session_id = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    with client.stream("POST", "/chat", json={"session_id": session_id, "message": "改收货地址"}) as resp:
        events = _parse_sse("".join(resp.iter_text()))

    form_events = [event for event in events if event.get("type") == "form_required"]
    assert len(form_events) == 1
    session_id_form = form_events[0]["data"]["form_session_id"]
    assert session_id_form
    assert customer_address() == original_address

    rejected = client.post(
        "/actions/confirm",
        json={
            "conversation_id": session_id,
            "action": "confirm",
            "fields": {
                "order_id": "ORD-CANCEL-1",
                "address": "不可写的新地址",
            },
        },
    )
    assert rejected.status_code == 200
    body = rejected.json()
    assert body["ok"] is True and body["action"] == "confirm"
    resume_types = [event.get("type") for event in body["events"]]
    assert "form_rejected" in resume_types
    assert "tool_call" not in resume_types
    assert "safety_rejected" not in resume_types
    assert resume_types[-1] == "done"
    rejected_events = [event for event in body["events"] if event.get("type") == "form_rejected"]
    assert len(rejected_events) == 1
    rejected_data = rejected_events[0]["data"]
    assert rejected_data["form_session_id"] == session_id_form
    assert rejected_data["tool"] == "update_address"
    assert rejected_data.get("errors")
    assert customer_address() == original_address

    conn = sqlite3.connect(db)
    thread_id = conn.execute(
        "SELECT thread_id FROM conversation_status WHERE conversation_id = ?",
        (session_id,),
    ).fetchone()[0]
    conn.close()

    async def pending_interrupts():
        async with AsyncSqliteSaver.from_conn_string(db) as saver:
            graph = make_ai_graph(
                model, saver, order_repository=SQLiteOrderRepository(str(business_db)),
            )
            state = await graph.aget_state({"configurable": {"thread_id": thread_id}})
            return state.interrupts

    pending = asyncio.run(pending_interrupts())
    assert pending
    assert pending[0].value.get("errors")
    assert pending[0].value.get("form_session_id") == session_id_form

    good_address = "修正后的表单地址 66 号"
    confirmed = client.post(
        "/actions/confirm",
        json={
            "conversation_id": session_id,
            "action": "confirm",
            "fields": {
                "order_id": "ORD-PAID-1",
                "address": good_address,
            },
        },
    )
    assert confirmed.status_code == 200
    ok_body = confirmed.json()
    assert ok_body["ok"] is True
    ok_types = [event.get("type") for event in ok_body["events"]]
    assert ok_types[-1] == "done"
    assert "form_rejected" not in ok_types
    tool_calls = [event for event in ok_body["events"] if event.get("type") == "tool_call"]
    assert len(tool_calls) == 1
    assert tool_calls[0]["data"]["name"] == "update_address"
    assert tool_calls[0]["data"]["args"]["order_id"] == "ORD-PAID-1"
    assert tool_calls[0]["data"]["args"]["address"] == good_address
    assert customer_address() == good_address
    assert list(asyncio.run(pending_interrupts())) == []


def test_vague_mutation_operation_clarify_then_form_interrupt(tmp_path, monkeypatch):
    """Vague mutation-like intent uses Operation Clarify outside MutationGraph.

    Seam: Conversation Runtime /chat + /actions/confirm (issue #21).
    Turn 1: chat asks operation type — no form_required / Mutation Form.
    Turn 2: concrete update_address intent opens Form Interrupt; Confirm writes.
    """
    business_db = tmp_path / "mock_ecommerce.db"

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

    original_address = customer_address()
    form_address = "北京市朝阳区望京 Operation Clarify 路 1 号"
    model = ToolCallingFakeModel(responses=[
        router_json_message("order_mutation", 0.9),
        router_json_message("account_profile_update", 0.9),
        select_json_message(
            "update_address",
            {"order_id": "ORD-PAID-1", "address": "select预填勿写入"},
        ),
    ])
    db = str(tmp_path / "operation-clarify.db")
    client = TestClient(create_app(db, model=model))
    session_id = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]

    with client.stream(
        "POST", "/chat", json={"session_id": session_id, "message": "我要改一下订单"},
    ) as resp:
        clarify_events = _parse_sse("".join(resp.iter_text()))

    clarify_types = [event.get("type") for event in clarify_events]
    assert "form_required" not in clarify_types
    assert "form_rejected" not in clarify_types
    assert "tool_call" not in clarify_types
    assert "clarification_required" in clarify_types
    clarify = next(e for e in clarify_events if e.get("type") == "clarification_required")
    assert clarify["data"].get("reason") == "operation_unclear"
    done1 = next(e for e in clarify_events if e.get("type") == "done")
    final1 = done1["data"].get("final") or ""
    assert "地址" in final1 or "发票" in final1
    assert customer_address() == original_address

    with client.stream(
        "POST", "/chat", json={"session_id": session_id, "message": "改收货地址"},
    ) as resp:
        form_events = _parse_sse("".join(resp.iter_text()))

    assert any(event.get("type") == "form_required" for event in form_events)
    form = next(e for e in form_events if e.get("type") == "form_required")["data"]
    assert form["tool"] == "update_address"
    assert customer_address() == original_address

    confirmed = client.post(
        "/actions/confirm",
        json={
            "conversation_id": session_id,
            "action": "confirm",
            "fields": {"order_id": "ORD-PAID-1", "address": form_address},
        },
    )
    assert confirmed.status_code == 200
    body = confirmed.json()
    assert body["ok"] is True
    resume_types = [event.get("type") for event in body["events"]]
    assert resume_types[-1] == "done"
    tool_calls = [e for e in body["events"] if e.get("type") == "tool_call"]
    assert len(tool_calls) == 1
    assert tool_calls[0]["data"]["name"] == "update_address"
    assert tool_calls[0]["data"]["args"]["address"] == form_address
    assert customer_address() == form_address


def test_plain_text_during_form_interrupt_implicit_cancels_and_routes_new_turn(
    tmp_path, monkeypatch
):
    """Plain text while Form Interrupt is pending -> Implicit Cancel (CONTEXT.md / #72).

    Same policy as the AgentBot adapter: the stale form is cancelled silently and
    the message is routed as a normal new turn. No guidance tip, no form re-push,
    and the plain text never writes.
    """
    business_db = tmp_path / "mock_ecommerce.db"

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

    original_address = customer_address()
    model = ToolCallingFakeModel(responses=[
        router_json_message("account_profile_update", 0.9),
        select_json_message("update_address", {"order_id": "ORD-PAID-1", "address": "北京市朝阳区"}),
        router_json_message("faq", 0.9),
        AIMessage(content="FAQ 离线回复：请参考帮助中心。"),
    ])
    db = str(tmp_path / "form-plain-implicit-cancel.db")
    client = TestClient(create_app(db, model=model))
    session_id = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    with client.stream("POST", "/chat", json={"session_id": session_id, "message": "改收货地址"}) as resp:
        open_events = _parse_sse("".join(resp.iter_text()))
    assert any(event.get("type") == "form_required" for event in open_events)

    with client.stream(
        "POST", "/chat", json={"session_id": session_id, "message": "你好"},
    ) as resp:
        new_turn_events = _parse_sse("".join(resp.iter_text()))

    types = [event.get("type") for event in new_turn_events]
    # Stale form is neither re-projected nor replaced by a Form-Only Write tip.
    assert "form_required" not in types
    tokens = "".join(
        event["data"].get("text", "")
        for event in new_turn_events
        if event.get("type") == "token"
    )
    assert form_plain_text_refuse_text() not in tokens

    # The message ran as a normal new turn (Router -> ReadGraph).
    done = next(event for event in new_turn_events if event.get("type") == "done")
    assert "FAQ" in (done["data"].get("final") or "")

    # Interrupt cleared: no pending confirmation left for /actions/confirm.
    r = client.post(
        "/actions/confirm",
        json={"conversation_id": session_id, "action": "confirm"},
    )
    assert r.status_code == 409

    # Plain text never writes.
    assert customer_address() == original_address


def test_handoff_turn_persists_outer_notice_and_emits_token(tmp_path):
    from ai_service_agent.harness.handoff import HANDOFF_REQUEST_CONFIRM
    client = TestClient(create_app(str(tmp_path / "n.db"), model=KeywordHandoffFakeModel(responses=[])))
    session_id = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    with client.stream("POST", "/chat", json={"session_id": session_id, "message": "我要转人工"}) as r:
        text = "".join(r.iter_text())
    assert '"token"' in text
    assert "handoff_requested" in text
    conn = sqlite3.connect(str(tmp_path / "n.db"))
    rows = MessageStore(conn).list(session_id)
    conn.close()
    assert [r["role"] for r in rows] == ["user", "assistant"]
    assert rows[1]["content"] == HANDOFF_REQUEST_CONFIRM

def test_chat_and_resolve_share_make_ai_graph_entry(tmp_path, monkeypatch):
    """/chat 与 /handoffs/{id}/resolve 都必须经同一个 make_ai_graph 入口（P2 一致性要求）。"""
    from ai_service_agent.harness import server as server_mod
    from ai_service_agent.harness.graph import make_ai_graph as _real_make_ai_graph

    calls = []

    def spy(*args, **kwargs):
        calls.append(args)
        return _real_make_ai_graph(*args, **kwargs)

    monkeypatch.setattr(server_mod, "make_ai_graph", spy)
    client = TestClient(create_app(str(tmp_path / "g.db"), model="fake"))
    session_id = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]

    with client.stream("POST", "/chat", json={"session_id": session_id, "message": "hi"}) as r:
        "".join(r.iter_text())
    assert len(calls) == 1  # /chat ai 路径经 make_ai_graph

    conn = sqlite3.connect(str(tmp_path / "g.db"))
    status = ConversationStatusStore(conn)
    svc = HandoffService(conn, status)
    rec = svc.create_handoff(session_id, "r", "s")
    svc.seat_takeover(session_id)
    conn.close()
    r = client.post(f"/handoffs/{rec['handoff_id']}/resolve")
    assert r.status_code == 200
    assert len(calls) == 2  # resolve 路径也走同一入口
    assert all(model is not None and checkpointer is not None for model, checkpointer in calls)


def test_chat_holds_when_approval_pending(tmp_path):
    """OQ-4：审批挂起时用户来消息 -> 不 resume、不新起 run，发 approval_required 提示 + 存消息。"""
    db = str(tmp_path / "hold.db")
    client = TestClient(create_app(db, model=ApprovalFlowFakeModel(responses=[])))
    session_id = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    with client.stream("POST", "/chat", json={"session_id": session_id, "message": "给 ORD-PAID-1 申请退款"}) as r:
        text1 = "".join(r.iter_text())
    assert '"approval_required"' in text1

    with client.stream("POST", "/chat", json={"session_id": session_id, "message": "好了吗"}) as r:
        text2 = "".join(r.iter_text())
    assert '"approval_required"' in text2
    assert '"error"' not in text2

    conn = sqlite3.connect(db)
    rows = MessageStore(conn).list(session_id)
    conn.close()
    assert rows[-1]["role"] == "user" and rows[-1]["content"] == "好了吗"


def test_resolve_with_pending_approval_still_completes(tmp_path):
    """D9：pending-approval 检查是只读信息项（仅日志），不影响 resolve 分支。"""
    db = str(tmp_path / "d9.db")
    conn = sqlite3.connect(db)
    status = ConversationStatusStore(conn)
    info = status.create_session("u1")
    svc = HandoffService(conn, status)
    rec = svc.create_handoff(info["conversation_id"], "r", "s")
    svc.seat_takeover(info["conversation_id"])
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    ApprovalService(conn).insert_if_absent("A-D9", info["conversation_id"], info["thread_id"], {
        "type": "approval_required", "approval_id": "A-D9", "summary": "挂起审批",
        "requester_user_id": "u1", "created_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=30)).isoformat(),
    })
    conn.close()

    client = TestClient(create_app(db, model="fake"))
    r = client.post(f"/handoffs/{rec['handoff_id']}/resolve")
    assert r.status_code == 200
    assert r.json()["mode"] == "ai"

    conn2 = sqlite3.connect(db)
    assert ApprovalService(conn2).get("A-D9")["status"] == "pending"  # 审批不被 resolve 干预
    conn2.close()


def test_chat_simulate_approval_resumes_approve(tmp_path):
    """approval-spec §5：simulate_approval 自动批准；下一消息经同一 resume 路径执行。"""
    db = str(tmp_path / "sim.db")
    client = TestClient(create_app(db, model=ApprovalFlowFakeModel(responses=[]), simulate_approval=True))
    session_id = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    with client.stream("POST", "/chat", json={"session_id": session_id, "message": "给 ORD-PAID-1 申请退款"}) as r:
        text1 = "".join(r.iter_text())
    assert '"approval_required"' in text1

    with client.stream("POST", "/chat", json={"session_id": session_id, "message": "好的"}) as r:
        text2 = "".join(r.iter_text())
    assert '"error"' not in text2
    assert '"done"' in text2
    conn = sqlite3.connect(db)
    rows = MessageStore(conn).list(session_id)
    conn.close()
    assert rows[-1]["content"]  # 审批通过后最终回复已落库


def test_approve_resume_persists_final_to_message_store(tmp_path):
    """D7：/approvals/{id}/approve 直接 ainvoke 后 diff 提取本轮新增 assistant 文本并落 Message Store。"""
    db = str(tmp_path / "d7.db")
    client = TestClient(
        create_app(
            db,
            model=ApprovalFlowFakeModel(responses=[]),
            seat_approval_secret=SEAT_SECRET,
        )
    )
    session_id = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    with client.stream("POST", "/chat", json={"session_id": session_id, "message": "给 ORD-PAID-1 申请退款"}) as r:
        "".join(r.iter_text())
    conn = sqlite3.connect(db)
    approval_id = conn.execute("SELECT approval_id FROM approval_record LIMIT 1").fetchone()[0]
    conn.close()

    r = client.post(f"/approvals/{approval_id}/approve", headers=SEAT_HEADERS)
    assert r.status_code == 200

    conn = sqlite3.connect(db)
    rows = MessageStore(conn).list(session_id)
    conn.close()
    assert any(row["role"] == "assistant" and "通过审核" in row["content"] for row in rows)

import importlib.util
import pytest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

def _load_seed_module():
    spec = importlib.util.spec_from_file_location(
        'seed_mock_db_shared', _ROOT / 'scripts' / 'seed_mock_db.py'
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

@pytest.fixture(autouse=True)
def isolated_mock_db(monkeypatch, tmp_path):
    module = _load_seed_module()
    db_path = tmp_path / 'mock_ecommerce.db'
    module.build_database(str(db_path))
    from ai_service_agent.tools import write as _write, orders as _orders
    class _S:
        mock_ecommerce_db_path = str(db_path)
    monkeypatch.setattr(_write, 'get_settings', lambda: _S())
    monkeypatch.setattr(_orders, 'get_settings', lambda: _S())
