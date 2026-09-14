import sqlite3

from fastapi.testclient import TestClient

from ai_service_agent.harness.handoff import HandoffService
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
