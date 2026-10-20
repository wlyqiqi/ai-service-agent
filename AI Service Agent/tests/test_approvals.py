import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from ai_service_agent.harness.graph import make_ai_graph
from ai_service_agent.harness import server as server_module
from ai_service_agent.harness.server import create_app
from ai_service_agent.testing import ApprovalFlowFakeModel

SEAT_SECRET = "test-seat-approval-secret"
SEAT_HEADERS = {
    "X-Seat-Approval-Credential": SEAT_SECRET,
    "X-Seat-Actor-Id": "seat-1",
}

from ai_service_agent.harness.approvals import (
    ApprovalAlreadyProcessed,
    ApprovalExpired,
    ApprovalNotFound,
    ApprovalService,
)
from ai_service_agent.harness.approval_side_channel import make_approval_interrupt_callback
import importlib.util
import pytest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def _load_seed_module():
    spec = importlib.util.spec_from_file_location(
        'seed_mock_db_approvals', ROOT / 'scripts' / 'seed_mock_db.py'
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def isolated_mock_db(monkeypatch, tmp_path):
    module = _load_seed_module()
    db_path = tmp_path / 'mock_ecommerce.db'
    module.build_database(str(db_path))
    from ai_service_agent.tools import write as _write, orders as _orders
    from ai_service_agent.graph import builder as _builder
    class _S:
        mock_ecommerce_db_path = str(db_path)

        def __init__(self, real):
            self.__dict__.update(real.__dict__)
            self.mock_ecommerce_db_path = str(db_path)

    real_settings = _builder.get_settings()
    real_server_settings = server_module.get_settings()
    monkeypatch.setattr(_write, 'get_settings', lambda: _S(real_settings))
    monkeypatch.setattr(_orders, 'get_settings', lambda: _S(real_settings))
    monkeypatch.setattr(_builder, 'get_settings', lambda: _S(real_settings))
    monkeypatch.setattr(server_module, 'get_settings', lambda: _S(real_server_settings))



def _svc(tmp_path, **kw):
    conn = sqlite3.connect(tmp_path / "a.db")
    return ApprovalService(conn, **kw), conn


def _payload(approval_id="A-1", expires_delta=timedelta(minutes=30)):
    now = datetime.now(timezone.utc)
    return {
        "type": "approval_required",
        "approval_id": approval_id,
        "summary": "退款",
        "requester_user_id": "u1",
        "created_at": now.isoformat(),
        "expires_at": (now + expires_delta).isoformat(),
    }


def test_insert_if_absent_idempotent(tmp_path):
    svc, conn = _svc(tmp_path)
    rec = svc.insert_if_absent("A-1", "c1", "t1", _payload())
    assert rec["status"] == "pending" and rec["thread_id"] == "t1"
    again = svc.insert_if_absent("A-1", "c1", "t1", _payload())
    rows = conn.execute("SELECT COUNT(*) FROM approval_record").fetchone()[0]
    assert rows == 1 and again == rec
    conn.close()


def test_get_none_and_precheck_not_found(tmp_path):
    svc, conn = _svc(tmp_path)
    assert svc.get("nope") is None
    try:
        svc.precheck("nope")
        assert False, "expected ApprovalNotFound"
    except ApprovalNotFound:
        pass
    conn.close()


def test_mark_decided_then_precheck_conflicts_and_idempotent(tmp_path):
    svc, conn = _svc(tmp_path)
    svc.insert_if_absent("A-1", "c1", "t1", _payload())
    svc.mark_decided("A-1", "approved", decided_by="approver")
    rec = svc.get("A-1")
    assert rec["status"] == "approved" and rec["decided_by"] == "approver"
    try:
        svc.precheck("A-1")
        assert False, "expected ApprovalAlreadyProcessed"
    except ApprovalAlreadyProcessed:
        pass
    svc.mark_decided("A-1", "rejected", decided_by="x")  # 幂等：非 pending 不覆盖
    assert svc.get("A-1")["status"] == "approved"
    conn.close()


def test_lazy_expire(tmp_path):
    svc, conn = _svc(tmp_path)
    svc.insert_if_absent("A-1", "c1", "t1", _payload(expires_delta=timedelta(minutes=-1)))
    rec = svc.get("A-1")
    assert rec["status"] == "expired"
    try:
        svc.precheck("A-1")
        assert False, "expected ApprovalExpired"
    except ApprovalExpired:
        pass
    conn.close()


def test_pending_for_conversation_and_list(tmp_path):
    svc, conn = _svc(tmp_path)
    svc.insert_if_absent("A-1", "c1", "t1", _payload())
    svc.insert_if_absent("A-2", "c1", "t1", _payload("A-2"))
    svc.insert_if_absent("A-3", "c2", "t2", _payload("A-3"))
    assert {r["approval_id"] for r in svc.pending_for_conversation("c1")} == {"A-1", "A-2"}
    assert len(svc.list_pending()) == 3
    conn.close()


def test_interrupt_callback_persists_and_emits(tmp_path):
    svc, conn = _svc(tmp_path)
    cb = make_approval_interrupt_callback(svc, "c1", "t1")
    ev = asyncio.run(cb({"type": "approval_required", "approval_id": "A-9", "summary": "s",
                         "expires_at": "2099-01-01T00:00:00+00:00", "requester_user_id": "u1"}))
    assert ev["type"] == "approval_required"
    assert svc.get("A-9")["summary"] == "s"
    conn.close()


def test_interrupt_callback_simulate_approval_marks_approved(tmp_path):
    svc, conn = _svc(tmp_path, simulate_approval=True)
    cb = make_approval_interrupt_callback(svc, "c1", "t1")
    asyncio.run(cb({"type": "approval_required", "approval_id": "A-10", "summary": "s",
                    "expires_at": "2099-01-01T00:00:00+00:00", "requester_user_id": "u1"}))
    assert svc.get("A-10")["status"] == "approved"
    conn.close()


def test_approval_endpoint_approve_completes_graph(tmp_path):
    db = str(tmp_path / "e.db")
    client = TestClient(
        create_app(
            db,
            model=ApprovalFlowFakeModel(responses=[]),
            seat_approval_secret=SEAT_SECRET,
        )
    )
    session_id = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    with client.stream("POST", "/chat", json={"session_id": session_id, "message": "给 ORD-PAID-1 申请退款"}) as r:
        text = "".join(r.iter_text())
    assert '"approval_required"' in text

    conn = sqlite3.connect(db)
    approval_id, thread_id = conn.execute("SELECT approval_id, thread_id FROM approval_record LIMIT 1").fetchone()
    conn.close()

    r = client.post(f"/approvals/{approval_id}/approve", headers=SEAT_HEADERS)
    assert r.status_code == 200
    assert r.json()["status"] == "approved"

    r2 = client.post(f"/approvals/{approval_id}/approve", headers=SEAT_HEADERS)  # 幂等：重复 decision -> 409
    assert r2.status_code == 409

    async def final_state():
        async with AsyncSqliteSaver.from_conn_string(db) as saver:
            graph = make_ai_graph(ApprovalFlowFakeModel(responses=[]), saver)
            st = await graph.aget_state({"configurable": {"thread_id": thread_id}})
            return st.interrupts, st.values.get("execution_status")
    interrupts, status = asyncio.run(final_state())
    assert not interrupts and status == "completed"


def test_approval_endpoint_reject_no_tool_and_404(tmp_path):
    db = str(tmp_path / "r.db")
    client = TestClient(
        create_app(
            db,
            model=ApprovalFlowFakeModel(responses=[]),
            seat_approval_secret=SEAT_SECRET,
            auth_enabled=True,
            jwt_secret="test-secret-with-at-least-32-bytes!!",
        )
    )
    from ai_service_agent.harness.auth import UserStore

    conn = sqlite3.connect(db)
    UserStore(conn).upsert("u1", "+65-81234567", "张伟", "123456")
    conn.close()
    token = client.post("/auth/login", json={"phone": "+65-81234567", "password": "123456"}).json()["token"]
    auth_headers = {"Authorization": f"Bearer {token}"}

    session_id = client.post("/sessions", json={"user_id": "u1"}, headers=auth_headers).json()["session_id"]
    with client.stream(
        "POST", "/chat", json={"session_id": session_id, "message": "给 ORD-PAID-1 申请退款"}, headers=auth_headers
    ) as r:
        "".join(r.iter_text())
    conn = sqlite3.connect(db)
    approval_id = conn.execute("SELECT approval_id FROM approval_record LIMIT 1").fetchone()[0]
    conn.close()

    r = client.post(
        f"/approvals/{approval_id}/reject", json={"note": "资料不足"}, headers=SEAT_HEADERS
    )
    assert r.status_code == 200
    assert r.json()["status"] == "rejected"
    conn = sqlite3.connect(db)
    rec = conn.execute("SELECT status, decided_by FROM approval_record WHERE approval_id=?", (approval_id,)).fetchone()
    conn.close()
    assert rec == ("rejected", "seat:seat-1")

    assert client.get("/approvals/nope", headers=auth_headers).status_code == 404
    assert client.get(f"/approvals/{approval_id}", headers=auth_headers).json()["status"] == "rejected"


def test_approval_get_and_list_pending(tmp_path):
    db = str(tmp_path / "l.db")
    client = TestClient(
        create_app(
            db,
            model=ApprovalFlowFakeModel(responses=[]),
            auth_enabled=True,
            jwt_secret="test-secret-with-at-least-32-bytes!!",
        )
    )
    from ai_service_agent.harness.auth import UserStore

    conn = sqlite3.connect(db)
    UserStore(conn).upsert("u1", "+65-81234567", "张伟", "123456")
    conn.close()
    token = client.post("/auth/login", json={"phone": "+65-81234567", "password": "123456"}).json()["token"]
    auth_headers = {"Authorization": f"Bearer {token}"}

    session_id = client.post("/sessions", json={"user_id": "u1"}, headers=auth_headers).json()["session_id"]
    with client.stream(
        "POST", "/chat", json={"session_id": session_id, "message": "给 ORD-PAID-1 申请退款"}, headers=auth_headers
    ) as r:
        "".join(r.iter_text())
    body = client.get("/approvals?status=pending", headers=auth_headers).json()
    assert len(body["items"]) == 1 and body["items"][0]["status"] == "pending"

