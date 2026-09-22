"""Server auth contract tests: phone login and protected session identity."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from ai_service_agent.harness.approvals import ApprovalService
from ai_service_agent.harness.auth import UserStore
from ai_service_agent.harness.handoff import HandoffService
from ai_service_agent.harness.session import ConversationStatusStore
from ai_service_agent.harness.server import create_app


def _client(tmp_path, seed=None):
    db = str(tmp_path / "s.db")
    conn = sqlite3.connect(db)
    UserStore(conn).upsert("u1", "+65-81234567", "张伟", "123456")
    seed_result = seed(conn) if seed else None
    conn.close()
    client = TestClient(
        create_app(
            db,
            model="fake",
            auth_enabled=True,
            jwt_secret="test-secret-with-at-least-32-bytes!!",
        )
    )
    return client, seed_result


def test_phone_login_returns_jwt_identity(tmp_path):
    client, _ = _client(tmp_path)
    response = client.post("/auth/login", json={"phone": "+65-81234567", "password": "123456"})

    assert response.status_code == 200
    body = response.json()
    assert body["token"].count(".") == 2
    assert body["user"] == {"id": "u1", "name": "张伟"}


def test_legacy_account_login_still_supported(tmp_path):
    client, _ = _client(tmp_path)
    response = client.post("/auth/login", json={"account": "+65-81234567", "password": "123456"})

    assert response.status_code == 200
    assert response.json()["user"] == {"id": "u1", "name": "张伟"}


def test_invalid_phone_or_password_returns_401(tmp_path):
    client, _ = _client(tmp_path)
    assert client.post("/auth/login", json={"phone": "+65-81234567", "password": "bad"}).status_code == 401
    assert client.post("/auth/login", json={"phone": "+65-none", "password": "123456"}).status_code == 401


def test_protected_session_uses_token_user_and_rejects_mismatch(tmp_path):
    client, _ = _client(tmp_path)
    token = client.post("/auth/login", json={"phone": "+65-81234567", "password": "123456"}).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    created = client.post("/sessions", json={"user_id": "u1"}, headers=headers)
    assert created.status_code == 200

    mismatch = client.post("/sessions", json={"user_id": "u2"}, headers=headers)
    assert mismatch.status_code == 403


def test_protected_endpoint_requires_bearer(tmp_path):
    client, _ = _client(tmp_path)
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
    client, _ = _client(tmp_path)
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


def test_chat_rejects_other_users_conversation(tmp_path):
    def seed(conn):
        status = ConversationStatusStore(conn)
        return {
            "session_id": status.create_session("u1")["conversation_id"],
            "other_id": status.create_session("u2")["conversation_id"],
        }

    client, ids = _client(tmp_path, seed)
    session_id = ids["session_id"]
    other = ids["other_id"]
    token = client.post("/auth/login", json={"phone": "+65-81234567", "password": "123456"}).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    assert client.post("/chat", json={"session_id": other, "message": "hi"}, headers=headers).status_code == 403
    assert client.post("/chat", json={"session_id": session_id, "message": "你好"}, headers=headers).status_code == 200


def test_conversation_endpoints_reject_other_users(tmp_path):
    def seed(conn):
        status = ConversationStatusStore(conn)
        info = status.create_session("u2")
        handoff = HandoffService(conn, status)
        handoff_record = handoff.create_handoff(info["conversation_id"], "r", "s")
        return {"conversation_id": info["conversation_id"], "handoff_id": handoff_record["handoff_id"]}

    client, ids = _client(tmp_path, seed)
    token = client.post("/auth/login", json={"phone": "+65-81234567", "password": "123456"}).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    conversation = ids["conversation_id"]
    handoff_id = ids["handoff_id"]
    assert client.post("/actions/confirm", json={"conversation_id": conversation, "action": "confirm"}, headers=headers).status_code == 403
    assert client.get(f"/conversations/{conversation}/messages", headers=headers).status_code == 403
    assert client.post(f"/handoffs/{handoff_id}/resolve", headers=headers).status_code == 403
    assert client.get("/handoffs", params={"conversation_id": conversation}, headers=headers).status_code == 403


def test_approval_endpoints_reject_other_users(tmp_path):
    def seed(conn):
        approvals = ApprovalService(conn)
        approvals.insert_if_absent(
            "A-other", "c-other", "t-other",
            {"summary": "s", "requester_user_id": "u2", "expires_at": "2099-01-01T00:00:00+00:00"},
        )

    client, _ = _client(tmp_path, seed)
    token = client.post("/auth/login", json={"phone": "+65-81234567", "password": "123456"}).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    assert client.get("/approvals/A-other", headers=headers).status_code == 403
    assert client.get("/approvals", headers=headers).json() == {"items": []}
    assert client.post("/approvals/A-other/approve", headers=headers).status_code == 403
    assert client.post("/approvals/A-other/reject", json={"note": "no"}, headers=headers).status_code == 403
