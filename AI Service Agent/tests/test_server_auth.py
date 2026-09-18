"""Transport 占位契约测试：POST /auth/login（user-page-spec §10）+ GET messages 返回 mode。"""

import sqlite3

from fastapi.testclient import TestClient

from ai_service_agent.harness.server import create_app
from ai_service_agent.harness.session import ConversationStatusStore


def test_auth_login_placeholder(tmp_path):
    client = TestClient(create_app(str(tmp_path / "s.db"), model="fake"))
    r = client.post("/auth/login", json={"account": "alice", "password": "pw"})
    assert r.status_code == 200
    body = r.json()
    assert body["token"]
    assert body["user"]["id"] == "alice"
    assert body["user"]["name"] == "alice"


def test_get_messages_returns_mode(tmp_path):
    conn = sqlite3.connect(tmp_path / "s.db")
    status = ConversationStatusStore(conn)
    info = status.create_session("u1")
    conn.close()

    client = TestClient(create_app(str(tmp_path / "s.db"), model="fake"))
    r = client.get(f"/conversations/{info['conversation_id']}/messages")
    assert r.status_code == 200
    assert r.json()["mode"] == "ai"

    conn = sqlite3.connect(tmp_path / "s.db")
    ConversationStatusStore(conn).set_mode(info["conversation_id"], "human")
    conn.close()
    r2 = client.get(f"/conversations/{info['conversation_id']}/messages")
    assert r2.status_code == 200
    assert r2.json()["mode"] == "human"
