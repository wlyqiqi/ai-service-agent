"""HTTP Approval API: Seat HTTP badge authz, decided_by, fail-closed, GET filter (#62)."""

import importlib.util
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_service_agent.harness import server as server_module
from ai_service_agent.harness.approvals import ApprovalService
from ai_service_agent.harness.auth import UserStore
from ai_service_agent.harness.server import create_app
from ai_service_agent.testing import ApprovalFlowFakeModel

SEAT_SECRET = "test-seat-approval-secret"
SEAT_ID = "cw-agent-7"
SEAT_HEADERS = {
    "X-Seat-Approval-Credential": SEAT_SECRET,
    "X-Seat-Actor-Id": SEAT_ID,
}

_ROOT = Path(__file__).resolve().parents[1]


def _load_seed_module():
    spec = importlib.util.spec_from_file_location(
        "seed_mock_db_seat_badge", _ROOT / "scripts" / "seed_mock_db.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def isolated_mock_db(monkeypatch, tmp_path):
    module = _load_seed_module()
    db_path = tmp_path / "mock_ecommerce.db"
    module.build_database(str(db_path))
    from ai_service_agent.graph import builder as _builder
    from ai_service_agent.tools import orders as _orders
    from ai_service_agent.tools import write as _write

    class _S:
        def __init__(self, real):
            self.__dict__.update(real.__dict__)
            self.mock_ecommerce_db_path = str(db_path)

    real_settings = _builder.get_settings()
    real_server_settings = server_module.get_settings()
    monkeypatch.setattr(_write, "get_settings", lambda: _S(real_settings))
    monkeypatch.setattr(_orders, "get_settings", lambda: _S(real_settings))
    monkeypatch.setattr(_builder, "get_settings", lambda: _S(real_settings))
    monkeypatch.setattr(server_module, "get_settings", lambda: _S(real_server_settings))


def _seed_pending_approval(db: str, *, requester_user_id: str = "u1", approval_id: str = "A-seat") -> None:
    conn = sqlite3.connect(db)
    ApprovalService(conn).insert_if_absent(
        approval_id,
        "c1",
        "t1",
        {
            "summary": "refund",
            "requester_user_id": requester_user_id,
            "expires_at": "2099-01-01T00:00:00+00:00",
        },
    )
    conn.close()


def _auth_client(tmp_path, *, seat_secret: str | None = SEAT_SECRET):
    db = str(tmp_path / "seat.db")
    conn = sqlite3.connect(db)
    UserStore(conn).upsert("u1", "+65-81234567", "张伟", "123456")
    UserStore(conn).upsert("u2", "+65-87654321", "李四", "123456")
    conn.close()
    app = create_app(
        db,
        model="fake",
        auth_enabled=True,
        jwt_secret="test-secret-with-at-least-32-bytes!!",
        seat_approval_secret=seat_secret,
    )
    return TestClient(app), db


def _login(client: TestClient, phone: str = "+65-81234567") -> dict[str, str]:
    token = client.post("/auth/login", json={"phone": phone, "password": "123456"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_customer_jwt_cannot_approve_own_approval(tmp_path):
    client, db = _auth_client(tmp_path)
    _seed_pending_approval(db, requester_user_id="u1", approval_id="A-own")
    headers = _login(client)

    assert client.post("/approvals/A-own/approve", headers=headers).status_code == 403
    assert client.post("/approvals/A-own/reject", json={"note": "no"}, headers=headers).status_code == 403
    assert ApprovalService(sqlite3.connect(db)).get("A-own")["status"] == "pending"


def test_seat_badge_approve_sets_decided_by_seat(tmp_path):
    """Valid seat badge can approve; decided_by is seat:{id}, not legacy 'approver'."""
    db = str(tmp_path / "flow.db")
    client = TestClient(
        create_app(
            db,
            model=ApprovalFlowFakeModel(responses=[]),
            seat_approval_secret=SEAT_SECRET,
        )
    )
    session_id = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    with client.stream("POST", "/chat", json={"session_id": session_id, "message": "给 ORD-PAID-1 申请退款"}) as r:
        assert '"approval_required"' in "".join(r.iter_text())

    conn = sqlite3.connect(db)
    approval_id = conn.execute("SELECT approval_id FROM approval_record LIMIT 1").fetchone()[0]
    conn.close()

    response = client.post(f"/approvals/{approval_id}/approve", headers=SEAT_HEADERS)
    assert response.status_code == 200
    assert response.json()["status"] == "approved"

    rec = ApprovalService(sqlite3.connect(db)).get(approval_id)
    assert rec["status"] == "approved"
    assert rec["decided_by"] == f"seat:{SEAT_ID}"


def test_seat_badge_reject_sets_decided_by_seat(tmp_path):
    db = str(tmp_path / "rej.db")
    client = TestClient(
        create_app(
            db,
            model=ApprovalFlowFakeModel(responses=[]),
            seat_approval_secret=SEAT_SECRET,
        )
    )
    session_id = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    with client.stream("POST", "/chat", json={"session_id": session_id, "message": "给 ORD-PAID-1 申请退款"}) as r:
        "".join(r.iter_text())
    conn = sqlite3.connect(db)
    approval_id = conn.execute("SELECT approval_id FROM approval_record LIMIT 1").fetchone()[0]
    conn.close()

    response = client.post(
        f"/approvals/{approval_id}/reject",
        json={"note": "资料不足"},
        headers=SEAT_HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
    rec = ApprovalService(sqlite3.connect(db)).get(approval_id)
    assert rec["decided_by"] == f"seat:{SEAT_ID}"


def test_missing_or_wrong_seat_badge_returns_403(tmp_path):
    client, db = _auth_client(tmp_path)
    _seed_pending_approval(db, approval_id="A-badge")

    assert client.post("/approvals/A-badge/approve").status_code == 403
    assert client.post(
        "/approvals/A-badge/approve",
        headers={"X-Seat-Actor-Id": SEAT_ID},
    ).status_code == 403
    assert client.post(
        "/approvals/A-badge/approve",
        headers={"X-Seat-Approval-Credential": SEAT_SECRET},
    ).status_code == 403
    assert client.post(
        "/approvals/A-badge/approve",
        headers={
            "X-Seat-Approval-Credential": "wrong-secret",
            "X-Seat-Actor-Id": SEAT_ID,
        },
    ).status_code == 403
    assert ApprovalService(sqlite3.connect(db)).get("A-badge")["status"] == "pending"


def test_auth_disabled_human_approve_403_simulate_still_auto_approves(tmp_path):
    db = str(tmp_path / "sim.db")
    # Human HTTP path: no seat badge / no secret → 403 (fail-closed).
    open_client = TestClient(
        create_app(db, model=ApprovalFlowFakeModel(responses=[]), auth_enabled=False)
    )
    session_id = open_client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    with open_client.stream(
        "POST", "/chat", json={"session_id": session_id, "message": "给 ORD-PAID-1 申请退款"}
    ) as r:
        assert '"approval_required"' in "".join(r.iter_text())
    conn = sqlite3.connect(db)
    approval_id = conn.execute("SELECT approval_id FROM approval_record LIMIT 1").fetchone()[0]
    conn.close()
    assert open_client.post(f"/approvals/{approval_id}/approve").status_code == 403

    # simulate_approval path still auto-approves with decided_by=simulate.
    sim_db = str(tmp_path / "sim-on.db")
    sim_client = TestClient(
        create_app(
            sim_db,
            model=ApprovalFlowFakeModel(responses=[]),
            auth_enabled=False,
            simulate_approval=True,
        )
    )
    sid = sim_client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    with sim_client.stream("POST", "/chat", json={"session_id": sid, "message": "给 ORD-PAID-1 申请退款"}) as r:
        assert '"approval_required"' in "".join(r.iter_text())
    with sim_client.stream("POST", "/chat", json={"session_id": sid, "message": "好的"}) as r:
        text = "".join(r.iter_text())
    assert '"error"' not in text
    conn = sqlite3.connect(sim_db)
    rec = conn.execute("SELECT status, decided_by FROM approval_record LIMIT 1").fetchone()
    conn.close()
    assert rec == ("approved", "simulate")


def test_get_approvals_no_unauthenticated_dump_and_filters_owner(tmp_path):
    client, db = _auth_client(tmp_path)
    _seed_pending_approval(db, requester_user_id="u1", approval_id="A-u1")
    _seed_pending_approval(db, requester_user_id="u2", approval_id="A-u2")

    # Unauthenticated: no full-store dump.
    assert client.get("/approvals").status_code == 401
    assert client.get("/approvals/A-u1").status_code == 401

    # Auth off app: still no dump.
    open_db = str(tmp_path / "open.db")
    conn = sqlite3.connect(open_db)
    ApprovalService(conn).insert_if_absent(
        "A-open",
        "c-open",
        "t-open",
        {
            "summary": "s",
            "requester_user_id": "u9",
            "expires_at": "2099-01-01T00:00:00+00:00",
        },
    )
    conn.close()
    open_client = TestClient(create_app(open_db, model="fake", auth_enabled=False))
    assert open_client.get("/approvals").json() == {"items": []}
    assert open_client.get("/approvals/A-open").status_code == 403

    # Authenticated customer sees only own rows.
    headers = _login(client)
    listed = client.get("/approvals", headers=headers).json()["items"]
    assert [item["approval_id"] for item in listed] == ["A-u1"]
    assert client.get("/approvals/A-u1", headers=headers).status_code == 200
    assert client.get("/approvals/A-u2", headers=headers).status_code == 403
