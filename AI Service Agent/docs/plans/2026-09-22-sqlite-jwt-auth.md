# SQLite JWT Auth MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a minimal phone-number login backed by SQLite and signed JWTs, so different demo users can call the API as their own identity.

**Architecture:** Store demo credentials in the runtime database, authenticate against bcrypt hashes, and issue HS256 JWTs whose `sub` is the existing `customer_id`. The server derives `user_id` from the Bearer token for protected requests and keeps the existing LangGraph/tool data-boundary behavior unchanged.

**Tech Stack:** Python 3.14, FastAPI, SQLite, bcrypt, PyJWT.

**Spec:** `docs/specs/architecture.md` and `docs/specs/user-page-spec.md`; production Supabase auth is a future replacement and is out of scope.

## Global Constraints

- Do not introduce Supabase, OAuth, refresh tokens, RBAC, or RLS in this MVP.
- JWT secrets come only from configuration, never a hardcoded production secret.
- Store bcrypt password hashes only; never store plaintext passwords.
- `JWT.sub` must be the existing business `customer_id` (for example `u1`).
- Existing tests must remain compatible; authentication enforcement is opt-in for this MVP via `AUTH_ENABLED=true`.
- Shared seeded databases must not be polluted by tests.

---

### Task 1: Add Auth Foundations

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `src/ai_service_agent/config.py`
- Create: `src/ai_service_agent/harness/auth.py`
- Test: `tests/test_auth.py`

**Interfaces:**
- Consumes: existing `Settings` from `ai_service_agent.config`.
- Produces:
  - `UserStore(conn: sqlite3.Connection)`
  - `UserStore.upsert(user_id: str, phone: str, display_name: str, password: str) -> None`
  - `UserStore.authenticate(phone: str, password: str) -> dict | None`
  - `create_access_token(user_id: str, secret: str, expires_minutes: int = 60) -> str`
  - `verify_bearer(request: Request, secret: str) -> str`

- [ ] **Step 1: Add failing auth unit tests**

Create `tests/test_auth.py`:

```python
import sqlite3

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
    token = create_access_token("u1", "test-secret", expires_minutes=60)

    assert token.count(".") == 2
    assert verify_bearer({"Authorization": f"Bearer {token}"}, "test-secret") == "u1"


def test_expired_token_is_rejected():
    token = create_access_token("u1", "test-secret", expires_minutes=-1)

    with pytest.raises(Exception):
        verify_bearer({"Authorization": f"Bearer {token}"}, "test-secret")
```

- [ ] **Step 2: Run the new tests and verify failure**

```powershell
$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"
uv run --no-sync pytest tests/test_auth.py -q
```

Expected: fail because `ai_service_agent.harness.auth` does not exist.

- [ ] **Step 3: Register dependencies**

Run:

```powershell
uv add PyJWT bcrypt
```

- [ ] **Step 4: Add auth settings**

Add these fields to `Settings` in `src/ai_service_agent/config.py`:

```python
auth_enabled: bool = Field(default=False, validation_alias="AUTH_ENABLED")
jwt_secret: str | None = Field(default=None, validation_alias="JWT_SECRET")
jwt_expires_minutes: int = Field(default=60, validation_alias="JWT_EXPIRES_MINUTES")
```

- [ ] **Step 5: Implement the auth module**

Create `src/ai_service_agent/harness/auth.py`:

```python
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
    return jwt.encode({"sub": user_id, "iat": now, "exp": now + expires_minutes * 60}, secret, algorithm="HS256")


def verify_bearer(headers: dict | object, secret: str) -> str:
    auth_header = headers.get("Authorization") if hasattr(headers, "get") else None
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
```

- [ ] **Step 6: Run auth tests**

```powershell
uv run --no-sync pytest tests/test_auth.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```powershell
git add -- pyproject.toml uv.lock src/ai_service_agent/config.py src/ai_service_agent/harness/auth.py tests/test_auth.py
git commit -m "feat: add sqlite jwt auth foundations"
```

### Task 2: Wire Login and Session Identity

**Files:**
- Modify: `src/ai_service_agent/harness/server.py`
- Test: `tests/test_server_auth.py`

**Interfaces:**
- Consumes: `UserStore`, `create_access_token`, `verify_bearer` from Task 1.
- Produces:
  - `create_app(..., auth_enabled: bool | None = None, jwt_secret: str | None = None)`
  - `/auth/login` accepts `{phone, password}` and remains compatible with legacy `{account, password}`.
  - When auth is enabled, protected endpoints use token `sub` as `user_id`.

- [ ] **Step 1: Write failing server auth tests**

Replace the placeholder login test in `tests/test_server_auth.py` with:

```python
import sqlite3

from fastapi.testclient import TestClient

from ai_service_agent.harness.auth import UserStore
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
```

- [ ] **Step 2: Run server auth tests and verify failure**

```powershell
uv run --no-sync pytest tests/test_server_auth.py -q
```

Expected: fail because `create_app` does not accept auth parameters and the login endpoint still returns placeholder responses.

- [ ] **Step 3: Implement create_app auth settings**

Extend the signature:

```python
def create_app(
    db_path: str,
    model=None,
    simulate_approval: bool | None = None,
    chatwoot: object | None = None,
    chatwoot_webhook_secret: str | None = None,
    auth_enabled: bool | None = None,
    jwt_secret: str | None = None,
):
    settings = get_settings()
    required_auth = settings.auth_enabled if auth_enabled is None else auth_enabled
    secret = jwt_secret or settings.jwt_secret
    if required_auth and not secret:
        raise ValueError("JWT_SECRET is required when authentication is enabled")
    users = UserStore(conn)
```

Add a helper inside `create_app`:

```python
def _auth_user(request):
    if not required_auth:
        return None
    return verify_bearer(request.headers, secret)
```

- [ ] **Step 4: Replace the login endpoint**

Change `LoginBody` to keep compatibility:

```python
class LoginBody(BaseModel):
    phone: str | None = None
    account: str | None = None
    password: str
```

Replace `/auth/login`:

```python
@app.post("/auth/login")
def auth_login(body: LoginBody):
    phone = body.phone or body.account
    if not phone:
        raise HTTPException(status_code=422, detail="phone is required")
    user = users.authenticate(phone, body.password)
    if user is None:
        raise HTTPException(status_code=401, detail="invalid credentials")
    token = create_access_token(user["user_id"], secret, settings.jwt_expires_minutes)
    return {"token": token, "user": {"id": user["user_id"], "name": user["name"]}}
```

- [ ] **Step 5: Enforce identity on sessions**

Add `request: Request` to `create_session`, derive the authenticated user, and reject mismatches:

```python
@app.post("/sessions")
def create_session(body: CreateSessionBody, request: Request):
    auth_user = _auth_user(request)
    if auth_user is not None and body.user_id != auth_user:
        raise HTTPException(status_code=403, detail="user mismatch")
    info = status.create_session(body.user_id)
    return {"session_id": info["conversation_id"], "mode": info["mode"]}
```

Do not change behavior when `required_auth` is false; all existing tests continue to work.

- [ ] **Step 6: Run targeted tests**

```powershell
uv run --no-sync pytest tests/test_server_auth.py tests/test_server.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```powershell
git add -- src/ai_service_agent/harness/server.py tests/test_server_auth.py
git commit -m "feat: wire phone login and session identity"
```

### Task 3: Protect User-Owned Resources

**Files:**
- Modify: `src/ai_service_agent/harness/server.py`
- Test: `tests/test_server_auth.py`

**Interfaces:**
- Consumes: `_auth_user`, `ConversationStatusStore.get_user_id`, `ApprovalService.get`, `HandoffService.get`.
- Produces: ownership checks for chat, confirmation, messages, approvals, and handoffs.

- [ ] **Step 1: Add failing ownership tests**

Append to `tests/test_server_auth.py`:

```python
def test_chat_rejects_other_users_conversation(tmp_path):
    client = _client(tmp_path)
    token = client.post("/auth/login", json={"phone": "+65-81234567", "password": "123456"}).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    session_id = client.post("/sessions", json={"user_id": "u1"}, headers=headers).json()["session_id"]

    other = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    assert client.post("/chat", json={"session_id": other, "message": "hi"}, headers=headers).status_code == 403
    assert client.post("/chat", json={"session_id": session_id, "message": "你好"}, headers=headers).status_code == 200
```

Use a small fake chat assertion only if the existing fake model stream requires it; otherwise the status assertion is sufficient.

- [ ] **Step 2: Run the test and verify failure**

```powershell
uv run --no-sync pytest tests/test_server_auth.py::test_chat_rejects_other_users_conversation -q
```

Expected: fail because `/chat` currently accepts any conversation.

- [ ] **Step 3: Add conversation ownership checks**

For each endpoint, add `request: Request`, call `auth_user = _auth_user(request)`, and when auth is enabled verify:

```python
conversation_user = status.get_user_id(conversation_id)
if auth_user is not None and conversation_user != auth_user:
    raise HTTPException(status_code=403, detail="conversation forbidden")
```

Apply this to:

- `POST /chat`
- `POST /actions/confirm`
- `GET /conversations/{conversation_id}/messages`
- `POST /handoffs/{handoff_id}/resolve`
- `GET /handoffs?conversation_id=...`

- [ ] **Step 4: Add approval ownership checks**

In approval endpoints, load or precheck the record before mutation and compare:

```python
if auth_user is not None and record["requester_user_id"] != auth_user:
    raise HTTPException(status_code=403, detail="approval forbidden")
```

Apply to:

- `POST /approvals/{approval_id}/approve`
- `POST /approvals/{approval_id}/reject`
- `GET /approvals/{approval_id}`
- `GET /approvals` by filtering returned items to `requester_user_id == auth_user`

Keep behavior unchanged when `auth_user is None`.

- [ ] **Step 5: Run targeted tests**

```powershell
uv run --no-sync pytest tests/test_server_auth.py tests/test_server.py tests/test_approvals.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```powershell
git add -- src/ai_service_agent/harness/server.py tests/test_server_auth.py
git commit -m "feat: enforce jwt ownership on user resources"
```

### Task 4: Seed Demo Accounts

**Files:**
- Create: `scripts/seed_auth_users.py`
- Test: `tests/test_seed_auth_users.py`

**Interfaces:**
- Consumes: `mock_ecommerce.db.customers` and `UserStore`.
- Produces: `python scripts/seed_auth_users.py --runtime-db data/ai_service.db --mock-db data/mock_ecommerce.db --password 123456`.

- [ ] **Step 1: Write the failing seed test**

Create `tests/test_seed_auth_users.py`:

```python
import sqlite3

from ai_service_agent.harness.auth import UserStore
from scripts.seed_auth_users import seed_users


def test_seed_users_syncs_customers(tmp_path):
    business = sqlite3.connect(tmp_path / "business.db")
    business.execute("CREATE TABLE customers (customer_id TEXT PRIMARY KEY, name TEXT NOT NULL, phone TEXT NOT NULL)")
    business.execute("INSERT INTO customers VALUES ('u1', '张伟', '+65-81234567')")
    business.commit()
    business.close()

    runtime = sqlite3.connect(tmp_path / "runtime.db")
    count = seed_users(runtime, business, "123456")
    runtime.commit()

    assert count == 1
    assert UserStore(runtime).authenticate("+65-81234567", "123456")["user_id"] == "u1"
```

- [ ] **Step 2: Run the seed test and verify failure**

```powershell
uv run --no-sync pytest tests/test_seed_auth_users.py -q
```

Expected: fail because the script does not exist.

- [ ] **Step 3: Implement the seeding script**

Create `scripts/seed_auth_users.py`:

```python
import argparse
import sqlite3

from ai_service_agent.harness.auth import UserStore


def seed_users(runtime_conn: sqlite3.Connection, business_conn: sqlite3.Connection, password: str) -> int:
    users = UserStore(runtime_conn)
    rows = business_conn.execute(
        "SELECT customer_id, name, phone FROM customers ORDER BY customer_id"
    ).fetchall()
    for user_id, display_name, phone in rows:
        users.upsert(user_id, phone, display_name, password)
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-db", default="data/ai_service.db")
    parser.add_argument("--mock-db", default="data/mock_ecommerce.db")
    parser.add_argument("--password", default="123456")
    args = parser.parse_args()

    with sqlite3.connect(args.runtime_db) as runtime, sqlite3.connect(args.mock_db) as business:
        count = seed_users(runtime, business, args.password)
    print(f"Seeded {count} auth users")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the seed test**

```powershell
uv run --no-sync pytest tests/test_seed_auth_users.py -q
```

Expected: pass.

- [ ] **Step 5: Seed the shared runtime database**

```powershell
uv run --no-sync python scripts/seed_auth_users.py
```

Expected: `Seeded 122 auth users`.

- [ ] **Step 6: Commit**

```powershell
git add -- scripts/seed_auth_users.py tests/test_seed_auth_users.py
git commit -m "feat: seed sqlite demo auth users"
```

### Task 5: Update Frontend Login Contract

**Files:**
- Modify: `web/index.html`
- Test: `tests/test_user_page.py`
- Modify: `docs/specs/user-page-spec.md`

**Interfaces:**
- Consumes: `/auth/login {phone,password}`.
- Produces: frontend sends Bearer tokens and labels the input as 手机号.

- [ ] **Step 1: Write failing frontend contract tests**

Update the existing API test assertion in `tests/test_user_page.py`:

```python
assert json.loads(out["body"]) == {"phone": "a", "password": "b"}
```

Add a static contract assertion:

```python
assert '<label for="login-account">手机号</label>' in html
```

- [ ] **Step 2: Run frontend tests and verify failure**

```powershell
uv run --no-sync pytest tests/test_user_page.py -q
```

Expected: the two changed assertions fail.

- [ ] **Step 3: Update frontend**

In `web/index.html`:

```html
<label for="login-account">手机号</label>
```

Change the hint from “任意账号密码” to “演示账号：客户手机号，默认密码 123456”.

Change `login()` request body:

```javascript
body: JSON.stringify({ phone: account, password: password }),
```

- [ ] **Step 4: Update the user page spec**

In `docs/specs/user-page-spec.md`, replace the login placeholder wording with:

```markdown
MVP 登录使用 SQLite + JWT：`POST /auth/login` 提交 `{phone, password}`；成功返回 JWT 与用户 ID。生产认证后续替换为 Supabase，前端 Bearer 契约保持不变。
```

- [ ] **Step 5: Run targeted tests**

```powershell
uv run --no-sync pytest tests/test_user_page.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```powershell
git add -- web/index.html tests/test_user_page.py docs/specs/user-page-spec.md
git commit -m "feat: use phone login in user page"
```

### Task 6: Full Verification

**Files:**
- No source changes.

- [ ] **Step 1: Run formatting-sensitive checks**

```powershell
git diff --check
```

Expected: no output.

- [ ] **Step 2: Run full regression**

```powershell
$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"
uv run --no-sync pytest -q --basetemp="D:\AI_Project\AI Service Agent\.pytest-tmp"
```

Expected: all tests pass.

- [ ] **Step 3: Run LangGraph validation**

```powershell
langgraph validate
```

Expected: validation succeeds; if the CLI is unavailable in the project venv, record the exact error and use the installed global CLI without bypassing the check.

- [ ] **Step 4: Verify two demo users**

With `AUTH_ENABLED=true` and `JWT_SECRET` set, run the server, then log in with `u1` and `u2` phone numbers and confirm each user can only see its own conversation.

- [ ] **Step 5: Final commit if needed**

```powershell
git status --short
git add -- <explicit-files>
git commit -m "test: verify sqlite jwt auth mvp"
```

Expected: working tree clean or only intentionally untracked runtime databases.

## Self-Review

- **Spec coverage:** Covers SQLite storage, bcrypt hashing, JWT `sub`/`exp`, Bearer extraction, `user_id` injection, opt-in enforcement for test compatibility, phone-based demo accounts, and the future Supabase boundary.
- **No placeholders:** All implementation steps include concrete code or exact edits.
- **Type consistency:** `UserStore.authenticate` returns `{phone,user_id,name}`, which matches the login response mapping; `verify_bearer` accepts a mapping-like headers object, matching FastAPI request headers.
