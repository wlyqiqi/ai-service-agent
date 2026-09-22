from typing import Mapping

import bcrypt
import jwt
from fastapi import HTTPException

USERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    phone TEXT PRIMARY KEY,
    user_id TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);
"""


class UserStore:
    def __init__(self, conn):
        self.conn = conn
        self.conn.execute(USERS_SCHEMA)
        self.conn.commit()

    def upsert(self, user_id: str, phone: str, display_name: str, password: str) -> None:
        password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        self.conn.execute(
            "INSERT INTO users (phone, user_id, password_hash, display_name) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(phone) DO UPDATE SET user_id = excluded.user_id, "
            "password_hash = excluded.password_hash, display_name = excluded.display_name",
            (phone, user_id, password_hash, display_name),
        )
        self.conn.commit()

    def authenticate(self, phone: str, password: str) -> dict | None:
        row = self.conn.execute(
            "SELECT phone, user_id, password_hash, display_name FROM users WHERE phone = ?", (phone,)
        ).fetchone()
        if row is None:
            return None
        if not bcrypt.checkpw(password.encode(), row[2].encode()):
            return None
        return {"phone": row[0], "user_id": row[1], "name": row[3]}


def create_access_token(user_id: str, secret: str, expires_minutes: int = 60) -> str:
    import time

    now = int(time.time())
    return jwt.encode(
        {"sub": user_id, "iat": now, "exp": now + expires_minutes * 60}, secret, algorithm="HS256"
    )


def verify_bearer(headers: Mapping[str, str], secret: str) -> str:
    auth_header = headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = auth_header.removeprefix("Bearer ").strip()
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(status_code=401, detail="token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail="invalid token") from exc
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="token missing subject")
    return user_id
