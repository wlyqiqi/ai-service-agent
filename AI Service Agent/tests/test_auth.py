import sqlite3

from fastapi import HTTPException
import pytest

from ai_service_agent.harness.auth import UserStore, create_access_token, verify_bearer


def test_user_store_authenticates_phone(tmp_path):
    conn = sqlite3.connect(tmp_path / "auth.db")
    store = UserStore(conn)
    store.upsert("u1", "+65-81234567", "张伟", "123456")

    assert store.authenticate("+65-81234567", "123456")["user_id"] == "u1"
    assert store.authenticate("+65-81234567", "wrong") is None
    assert store.authenticate("+65-not-found", "123456") is None


def test_bearer_token_round_trip():
    secret = "test-secret-with-at-least-32-bytes!!"
    token = create_access_token("u1", secret, expires_minutes=60)

    assert token.count(".") == 2
    assert verify_bearer({"Authorization": f"Bearer {token}"}, secret) == "u1"


def test_expired_token_is_rejected():
    secret = "test-secret-with-at-least-32-bytes!!"
    token = create_access_token("u1", secret, expires_minutes=-1)

    with pytest.raises(HTTPException) as exc_info:
        verify_bearer({"Authorization": f"Bearer {token}"}, secret)
    assert exc_info.value.status_code == 401
