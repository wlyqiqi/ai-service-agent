import sqlite3

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from ai_service_agent.harness.handoff import HandoffService
from ai_service_agent.harness.messages import MessageStore
from ai_service_agent.harness.server import create_app
from ai_service_agent.harness.session import ConversationStatusStore
from ai_service_agent.testing import ToolCallingFakeModel


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


class HistoryEchoFakeModel(ToolCallingFakeModel):
    """离线：read agent 回复中回显收到的 user 消息条数，用于验证跨 turn 水合。"""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        system = next((m.content for m in messages if getattr(m, "type", "") == "system"), "")
        users = [m for m in messages if getattr(m, "type", "") == "human"]
        if "路由器" in system:
            payload = '{"capability": "faq", "risk_level": "read", "confidence": 0.9}'
        else:
            payload = f"收到{len(users)}条用户消息"
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=payload))])


def test_full_handoff_loop(tmp_path):
    db = str(tmp_path / "e2e.db")
    conn = sqlite3.connect(db)
    store = ConversationStatusStore(conn)
    info = store.create_session("u1")
    conn.close()

    client = TestClient(create_app(db, model="fake"))
    # 模拟子图抛 handoff_request 由外层接管（验收 #4/#6）
    conn2 = sqlite3.connect(db)
    handoff = HandoffService(conn2, ConversationStatusStore(conn2))
    rec = handoff.create_handoff(info["conversation_id"], "reason", "summary")
    conn2.close()

    conn3 = sqlite3.connect(db)
    mode = ConversationStatusStore(conn3).get_mode(info["conversation_id"])
    conn3.close()
    assert mode == "human"

    r = client.post(f"/handoffs/{rec['handoff_id']}/resolve")
    assert r.json()["mode"] == "ai"


def test_chat_ai_lane_streams_token_and_done(tmp_path):
    client = TestClient(create_app(str(tmp_path / "e2e.db"), model="fake"))
    r = client.post("/sessions", json={"user_id": "u1"})
    session_id = r.json()["session_id"]

    with client.stream("POST", "/chat", json={"session_id": session_id, "message": "hi"}) as resp:
        text = "".join(resp.iter_text())
    assert '"type": "token"' in text
    assert '"done"' in text


def test_handoff_interrupt_roundtrip(tmp_path):
    """验收 #4/#6：显式转人工 -> 图内 interrupt -> 外层建 Record + 原子切 human + handoff_created。"""
    client = TestClient(create_app(str(tmp_path / "h.db"), model=KeywordHandoffFakeModel(responses=[])))
    r = client.post("/sessions", json={"user_id": "u1"})
    session_id = r.json()["session_id"]

    with client.stream("POST", "/chat", json={"session_id": session_id, "message": "我要转人工"}) as resp:
        text = "".join(resp.iter_text())
    assert "handoff_created" in text

    conn = sqlite3.connect(str(tmp_path / "h.db"))
    mode = ConversationStatusStore(conn).get_mode(session_id)
    conn.close()
    assert mode == "human"


def test_ai_chat_hydrates_history_across_turns(tmp_path):
    """Message Store 唯一真相源：每 turn 从 Store 水合 state.messages，跨 turn 历史可见。"""
    client = TestClient(create_app(str(tmp_path / "h2.db"), model=HistoryEchoFakeModel(responses=[])))
    r = client.post("/sessions", json={"user_id": "u1"})
    session_id = r.json()["session_id"]

    with client.stream("POST", "/chat", json={"session_id": session_id, "message": "第一轮"}) as resp:
        "".join(resp.iter_text())
    with client.stream("POST", "/chat", json={"session_id": session_id, "message": "第二轮"}) as resp:
        text2 = "".join(resp.iter_text())
    assert "收到2条用户消息" in text2

    conn = sqlite3.connect(str(tmp_path / "h2.db"))
    rows = MessageStore(conn).list(session_id)
    conn.close()
    assert len(rows) == 4
    assert [r["role"] for r in rows] == ["user", "assistant", "user", "assistant"]
