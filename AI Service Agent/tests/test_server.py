import sqlite3

from fastapi.testclient import TestClient

from ai_service_agent.harness.approvals import ApprovalService
from ai_service_agent.harness.handoff import HandoffService
from ai_service_agent.harness.messages import MessageStore
from ai_service_agent.harness.server import create_app
from ai_service_agent.harness.session import ConversationStatusStore
from ai_service_agent.testing import ApprovalFlowFakeModel
from ai_service_agent.testing import ToolCallingFakeModel, router_json_message, select_json_message
from ai_service_agent.tools import write as write_mod


def test_sessions_creates_ai_mode(tmp_path):
    client = TestClient(create_app(str(tmp_path / "s.db"), model="fake"))
    r = client.post("/sessions", json={"user_id": "u1"})
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "ai"
    assert body["session_id"]


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
    assert "人工" in text  # 不触图，返回人工车道占位

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
    assert "handoff_created" in text1

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
    class _WriteSettings:
        mock_ecommerce_db_path = str(tmp_path / "isolated-mock.db")

    monkeypatch.setattr(write_mod, "get_settings", lambda: _WriteSettings())
    model = ToolCallingFakeModel(responses=[
        router_json_message("account_profile_update", 0.9),
        select_json_message("update_address", {"order_id": "ORD-PAID-1", "address": "北京市朝阳区测试路 99 号"}),
    ])
    db = str(tmp_path / "confirm.db")
    client = TestClient(create_app(db, model=model))
    session_id = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    with client.stream("POST", "/chat", json={"session_id": session_id, "message": "改收货地址"}) as resp:
        stream_text = "".join(resp.iter_text())
    assert '"confirmation_required"' in stream_text

    r = client.post("/actions/confirm", json={"conversation_id": session_id, "action": "confirmed"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["action"] == "confirm"
    assert "收货地址已修改成功" in body["final"]

    conn = sqlite3.connect(db)
    rows = MessageStore(conn).list(session_id)
    conn.close()
    assert rows[-1]["role"] == "assistant"
    assert "收货地址已修改成功" in rows[-1]["content"]


def test_handoff_turn_persists_outer_notice_and_emits_token(tmp_path):
    from ai_service_agent.harness.handoff import HANDOFF_NOTICE
    client = TestClient(create_app(str(tmp_path / "n.db"), model=KeywordHandoffFakeModel(responses=[])))
    session_id = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    with client.stream("POST", "/chat", json={"session_id": session_id, "message": "我要转人工"}) as r:
        text = "".join(r.iter_text())
    assert '"token"' in text
    assert "handoff_created" in text
    conn = sqlite3.connect(str(tmp_path / "n.db"))
    rows = MessageStore(conn).list(session_id)
    conn.close()
    assert [r["role"] for r in rows] == ["user", "assistant"]
    assert rows[1]["content"] == HANDOFF_NOTICE

def test_chat_and_resolve_share_make_ai_graph_entry(tmp_path, monkeypatch):
    """/chat 与 /handoffs/{id}/resolve 都必须经同一个 make_ai_graph 入口（P2 一致性要求）。"""
    from ai_service_agent.harness import server as server_mod
    from ai_service_agent.harness.graph import make_ai_graph as _real_make_ai_graph

    calls = []

    def spy(model, checkpointer, registry=None):
        calls.append((model, checkpointer))
        return _real_make_ai_graph(model, checkpointer, registry=registry)

    monkeypatch.setattr(server_mod, "make_ai_graph", spy)
    client = TestClient(create_app(str(tmp_path / "g.db"), model="fake"))
    session_id = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]

    with client.stream("POST", "/chat", json={"session_id": session_id, "message": "hi"}) as r:
        "".join(r.iter_text())
    assert len(calls) == 1  # /chat ai 路径经 make_ai_graph

    conn = sqlite3.connect(str(tmp_path / "g.db"))
    status = ConversationStatusStore(conn)
    rec = HandoffService(conn, status).create_handoff(session_id, "r", "s")
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
    with client.stream("POST", "/chat", json={"session_id": session_id, "message": "申请退款"}) as r:
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
    rec = HandoffService(conn, status).create_handoff(info["conversation_id"], "r", "s")
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
    with client.stream("POST", "/chat", json={"session_id": session_id, "message": "申请退款"}) as r:
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
    client = TestClient(create_app(db, model=ApprovalFlowFakeModel(responses=[])))
    session_id = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    with client.stream("POST", "/chat", json={"session_id": session_id, "message": "申请退款"}) as r:
        "".join(r.iter_text())
    conn = sqlite3.connect(db)
    approval_id = conn.execute("SELECT approval_id FROM approval_record LIMIT 1").fetchone()[0]
    conn.close()

    r = client.post(f"/approvals/{approval_id}/approve")
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
