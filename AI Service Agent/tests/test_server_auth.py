"""Server auth contract tests: phone login and protected session identity."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from ai_service_agent.harness.auth import UserStore
from ai_service_agent.harness.session import ConversationStatusStore
from ai_service_agent.harness.server import create_app


def _client(tmp_path):
    db = str(tmp_path / "s.db")
    conn = sqlite3.connect(db)
    UserStore(conn).upsert("u1", "+65-81234567", "张伟", "123456")
    conn.close()
    return TestClient(create_app(db, model="fake", auth_enabled=True, jwt_secret="test-secret"))


def test_phone_login_returns_jwt_identity(tmp_path):
    client = _client(tmp_path)
    response = client.post("/auth/login", json={"phone": "+65-81234567", "password": "123456"})

    assert response.status_code == 200
    body = response.json()
    assert body["token"].count(".") == 2
    assert body["user"] == {"id": "u1", "name": "张伟"}


def test_legacy_account_login_still_supported(tmp_path):
    client = _client(tmp_path)
    response = client.post("/auth/login", json={"account": "+65-81234567", "password": "123456"})

    assert response.status_code == 200
    assert response.json()["user"] == {"id": "u1", "name": "张伟"}


def test_invalid_phone_or_password_returns_401(tmp_path):
    client = _client(tmp_path)
    assert client.post("/auth/login", json={"phone": "+65-81234567", "password": "bad"}).status_code == 401
    assert client.post("/auth/login", json={"phone": "+65-none", "password": "123456"}).status_code == 401


def test_protected_session_uses_token_user_and_rejects_mismatch(tmp_path):
    client = _client(tmp_path)
    token = client.post("/auth/login", json={"phone": "+65-81234567", "password": "123456"}).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    created = client.post("/sessions", json={"user_id": "u1"}, headers=headers)
    assert created.status_code == 200

    mismatch = client.post("/sessions", json={"user_id": "u2"}, headers=headers)
    assert mismatch.status_code == 403


def test_protected_endpoint_requires_bearer(tmp_path):
    client = _client(tmp_path)
    assert client.post("/sessions", json={"user_id": "u1"}).status_code == 401


def test_missing_secret_is_rejected_when_auth_enabled(tmp_path):
    with pytest.raises(ValueError, match="JWT_SECRET"):
        create_app(str(tmp_path / "s.db"), model="fake", auth_enabled=True)


def test_login_is_unavailable_when_auth_disabled_without_secret(tmp_path):
    db = str(tmp_path / "s.db")
    conn = sqlite3.connect(db)
    UserStore(conn).upsert("u1", "+65-81234567", "张伟", "123456")
    conn.close()
    client = TestClient(create_app(db, model="fake", auth_enabled=False, jwt_secret=None))

    response = client.post("/auth/login", json={"phone": "+65-81234567", "password": "123456"})

    assert response.status_code == 503
    assert response.json()["detail"] == "JWT signing is unavailable"


def test_login_rejects_empty_phone_or_account(tmp_path):
    client = _client(tmp_path)
    assert client.post("/auth/login", json={"phone": "", "password": "123456"}).status_code == 422
    assert client.post("/auth/login", json={"account": "", "password": "123456"}).status_code == 422


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
