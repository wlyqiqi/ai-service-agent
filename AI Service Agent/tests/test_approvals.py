import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

from ai_service_agent.harness.approvals import (
    ApprovalAlreadyProcessed,
    ApprovalExpired,
    ApprovalNotFound,
    ApprovalService,
)


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
    cb = svc.make_interrupt_callback("c1", "t1")
    ev = asyncio.run(cb({"type": "approval_required", "approval_id": "A-9", "summary": "s",
                         "expires_at": "2099-01-01T00:00:00+00:00", "requester_user_id": "u1"}))
    assert ev["type"] == "approval_required"
    assert svc.get("A-9")["summary"] == "s"
    conn.close()


def test_interrupt_callback_simulate_approval_marks_approved(tmp_path):
    svc, conn = _svc(tmp_path, simulate_approval=True)
    cb = svc.make_interrupt_callback("c1", "t1")
    asyncio.run(cb({"type": "approval_required", "approval_id": "A-10", "summary": "s",
                    "expires_at": "2099-01-01T00:00:00+00:00", "requester_user_id": "u1"}))
    assert svc.get("A-10")["status"] == "approved"
    conn.close()
