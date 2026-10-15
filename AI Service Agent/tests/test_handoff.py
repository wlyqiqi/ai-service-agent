import asyncio
import sqlite3

from ai_service_agent.harness.handoff import (
    STATUS_AWAITING,
    HandoffService,
)
from ai_service_agent.harness.session import ConversationStatusStore


def _svc(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    status = ConversationStatusStore(conn)
    return HandoffService(conn, status), status, conn


def test_create_handoff_request_keeps_mode_ai(tmp_path):
    svc, status, conn = _svc(tmp_path)
    info = status.create_session("u1")
    rec = svc.create_handoff(info["conversation_id"], "r", "s")
    assert rec["status"] == STATUS_AWAITING
    assert status.get_mode(info["conversation_id"]) == "ai"
    conn.close()


def test_seat_takeover_switches_mode_same_transaction(tmp_path):
    svc, status, conn = _svc(tmp_path)
    info = status.create_session("u1")
    svc.create_handoff_request(info["conversation_id"], "r", "s")
    rec = svc.seat_takeover(info["conversation_id"])
    assert rec["status"] == "active"
    assert status.get_mode(info["conversation_id"]) == "human"
    conn.close()


def test_resolve_handoff_switches_back(tmp_path):
    svc, status, conn = _svc(tmp_path)
    info = status.create_session("u1")
    svc.create_handoff_request(info["conversation_id"], "r", "s")
    rec = svc.seat_takeover(info["conversation_id"])
    out = svc.resolve_handoff(rec["handoff_id"])
    assert out["mode"] == "ai"
    assert status.get_mode(info["conversation_id"]) == "ai"
    conn.close()


def test_resolve_unknown_handoff_raises(tmp_path):
    svc, status, conn = _svc(tmp_path)
    try:
        svc.resolve_handoff("nope")
        assert False, "expected KeyError"
    except KeyError:
        pass
    conn.close()


def test_callback_creates_handoff_request(tmp_path):
    svc, status, conn = _svc(tmp_path)
    info = status.create_session("u1")
    cb = svc.make_interrupt_callback(info["conversation_id"])
    ev = asyncio.run(cb({"type": "handoff_request", "reason": "r", "summary": "s"}))
    assert ev["type"] == "handoff_requested"
    assert status.get_mode(info["conversation_id"]) == "ai"
    assert svc.get_awaiting(info["conversation_id"]) is not None
    conn.close()


def test_get_active_none_until_takeover(tmp_path):
    svc, status, conn = _svc(tmp_path)
    info = status.create_session("u1")
    assert svc.get_active(info["conversation_id"]) is None
    svc.create_handoff_request(info["conversation_id"], "r", "s")
    assert svc.get_active(info["conversation_id"]) is None
    rec = svc.seat_takeover(info["conversation_id"])
    active = svc.get_active(info["conversation_id"])
    assert active["handoff_id"] == rec["handoff_id"]
    conn.close()


def test_request_handoff_idempotent_reuses_awaiting(tmp_path):
    svc, status, conn = _svc(tmp_path)
    info = status.create_session("u1")
    ev1 = svc.request_handoff(info["conversation_id"], "r1", "s1")
    ev2 = svc.request_handoff(info["conversation_id"], "r2", "s2")
    assert ev1["type"] == ev2["type"] == "handoff_requested"
    assert ev1["data"]["handoff_id"] == ev2["data"]["handoff_id"]
    n = conn.execute(
        "SELECT COUNT(*) FROM handoff_record WHERE conversation_id=? AND status=?",
        (info["conversation_id"], STATUS_AWAITING),
    ).fetchone()[0]
    assert n == 1
    conn.close()


def test_cancel_handoff_request(tmp_path):
    svc, status, conn = _svc(tmp_path)
    info = status.create_session("u1")
    svc.request_handoff(info["conversation_id"], "r", "s")
    out = svc.cancel_handoff_request(info["conversation_id"])
    assert out["status"] == "cancelled"
    assert svc.get_awaiting(info["conversation_id"]) is None
    assert status.get_mode(info["conversation_id"]) == "ai"
    conn.close()
