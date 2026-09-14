import asyncio
import sqlite3

from ai_service_agent.harness.handoff import HandoffService
from ai_service_agent.harness.session import ConversationStatusStore


def _svc(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    status = ConversationStatusStore(conn)
    return HandoffService(conn, status), status, conn


def test_create_handoff_switches_mode_same_transaction(tmp_path):
    svc, status, conn = _svc(tmp_path)
    info = status.create_session("u1")
    rec = svc.create_handoff(info["conversation_id"], "r", "s")
    assert rec["status"] == "active"
    assert status.get_mode(info["conversation_id"]) == "human"
    conn.close()


def test_resolve_handoff_switches_back(tmp_path):
    svc, status, conn = _svc(tmp_path)
    info = status.create_session("u1")
    rec = svc.create_handoff(info["conversation_id"], "r", "s")
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


def test_callback_creates_handoff(tmp_path):
    svc, status, conn = _svc(tmp_path)
    info = status.create_session("u1")
    cb = svc.make_interrupt_callback(info["conversation_id"])
    ev = asyncio.run(cb({"type": "handoff_request", "reason": "r", "summary": "s"}))
    assert ev["type"] == "handoff_created"
    assert status.get_mode(info["conversation_id"]) == "human"
    conn.close()
