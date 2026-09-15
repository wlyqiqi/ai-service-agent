import sqlite3

from fastapi.testclient import TestClient

from ai_service_agent.harness.handoff import HandoffService
from ai_service_agent.harness.messages import MessageStore
from ai_service_agent.harness.server import create_app
from ai_service_agent.harness.session import ConversationStatusStore


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
    assert [r["role"] for r in rows] == ["user", "assistant"]


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
            payload = '{"capability": "unknown", "risk_level": "read", "confidence": 0.2}'
        else:
            payload = '{"capability": "faq", "risk_level": "read", "confidence": 0.9}'
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=payload))])


class ResolveProactiveFakeModel(ToolCallingFakeModel):
    """路由器区分转人工；read agent 回显收到的用户消息条数。"""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        from langchain_core.outputs import ChatGeneration, ChatResult
        system = next((m.content for m in messages if getattr(m, "type", "") == "system"), "")
        user = next((m.content for m in reversed(messages) if getattr(m, "type", "") == "human"), "")
        users = [m for m in messages if getattr(m, "type", "") == "human"]
        if "路由器" in system:
            payload = '{"capability": "unknown", "risk_level": "read", "confidence": 0.2}' if "转人工" in user else '{"capability": "faq", "risk_level": "read", "confidence": 0.9}'
        else:
            payload = f"echo-{len(users)}"
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=payload))])


def test_resolve_proactively_resumes_then_next_chat_is_answered(tmp_path):
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

    # resolve 后 checkpoint 应已无挂起中断（主动 resume 完成）
    async def check_no_interrupt():
        async with AsyncSqliteSaver.from_conn_string(db) as saver:
            graph = make_ai_graph(make_fake_model(), saver)
            st = await graph.aget_state({"configurable": {"thread_id": thread_id}})
            return st.interrupts, st.values.get("execution_status")
    interrupts, status = asyncio.run(check_no_interrupt())
    assert not interrupts
    assert status == "completed"

    # resolve 后第一条消息走正常 Router 并得到回复（echo-2，两条 user）
    with client.stream("POST", "/chat", json={"session_id": session_id, "message": "你好"}) as r:
        text2 = "".join(r.iter_text())
    assert "echo-2" in text2
    assert '"error"' not in text2


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
