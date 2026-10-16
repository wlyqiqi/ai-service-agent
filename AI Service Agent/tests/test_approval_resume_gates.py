"""HTTP Approval API: resume outbound mode gate + interrupt callbacks (#63)."""

import importlib.util
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from langgraph.types import Command, Interrupt

from ai_service_agent.harness import server as server_module
from ai_service_agent.harness.handoff import HandoffService
from ai_service_agent.harness.messages import MessageStore
from ai_service_agent.harness.server import create_app
from ai_service_agent.harness.session import ConversationStatusStore
from ai_service_agent.testing import ApprovalFlowFakeModel

SEAT_SECRET = "test-seat-approval-secret"
SEAT_HEADERS = {
    "X-Seat-Approval-Credential": SEAT_SECRET,
    "X-Seat-Actor-Id": "seat-gate-1",
}

_ROOT = Path(__file__).resolve().parents[1]


def _load_seed_module():
    spec = importlib.util.spec_from_file_location(
        "seed_mock_db_approval_resume_gates", _ROOT / "scripts" / "seed_mock_db.py"
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


def _pending_approval_via_chat(client: TestClient, db: str) -> tuple[str, str]:
    session_id = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    with client.stream(
        "POST", "/chat", json={"session_id": session_id, "message": "给 ORD-PAID-1 申请退款"}
    ) as r:
        assert '"approval_required"' in "".join(r.iter_text())
    conn = sqlite3.connect(db)
    approval_id = conn.execute("SELECT approval_id FROM approval_record LIMIT 1").fetchone()[0]
    conn.close()
    return session_id, approval_id


def test_seat_approve_while_human_mode_persists_message_store_without_outbound(tmp_path):
    """conversation_mode=human: resume + Message Store OK; no Chatwoot outbound."""
    cw = SimpleNamespace(send_bot_message=AsyncMock())
    db = str(tmp_path / "human-gate.db")
    client = TestClient(
        create_app(
            db,
            model=ApprovalFlowFakeModel(responses=[]),
            chatwoot=cw,
            seat_approval_secret=SEAT_SECRET,
        )
    )
    session_id, approval_id = _pending_approval_via_chat(client, db)

    conn = sqlite3.connect(db)
    ConversationStatusStore(conn).set_mode(session_id, "human")
    conn.close()

    response = client.post(f"/approvals/{approval_id}/approve", headers=SEAT_HEADERS)
    assert response.status_code == 200
    assert response.json()["status"] == "approved"

    rows = MessageStore(sqlite3.connect(db)).list(session_id)
    assert any(row["role"] == "assistant" and "通过审核" in row["content"] for row in rows)
    cw.send_bot_message.assert_not_awaited()


def test_seat_reject_while_human_mode_persists_message_store_without_outbound(tmp_path):
    """conversation_mode=human: reject resume + Message Store OK; no Chatwoot outbound."""
    cw = SimpleNamespace(send_bot_message=AsyncMock())
    db = str(tmp_path / "human-reject.db")
    client = TestClient(
        create_app(
            db,
            model=ApprovalFlowFakeModel(responses=[]),
            chatwoot=cw,
            seat_approval_secret=SEAT_SECRET,
        )
    )
    session_id, approval_id = _pending_approval_via_chat(client, db)

    conn = sqlite3.connect(db)
    ConversationStatusStore(conn).set_mode(session_id, "human")
    conn.close()

    response = client.post(
        f"/approvals/{approval_id}/reject",
        json={"note": "资料不足"},
        headers=SEAT_HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["status"] == "rejected"

    rows = MessageStore(sqlite3.connect(db)).list(session_id)
    assert any(row["role"] == "assistant" for row in rows)
    cw.send_bot_message.assert_not_awaited()


def test_seat_approve_while_ai_mode_still_sends_outbound(tmp_path):
    """conversation_mode=ai: success card + follow-up, not the long approval bubble."""
    cw = SimpleNamespace(send_bot_message=AsyncMock(), send_interactive_message=AsyncMock())
    db = str(tmp_path / "ai-gate.db")
    client = TestClient(
        create_app(
            db,
            model=ApprovalFlowFakeModel(responses=[]),
            chatwoot=cw,
            seat_approval_secret=SEAT_SECRET,
        )
    )
    session_id, approval_id = _pending_approval_via_chat(client, db)
    cw.send_bot_message.reset_mock()
    cw.send_interactive_message.reset_mock()

    response = client.post(f"/approvals/{approval_id}/approve", headers=SEAT_HEADERS)
    assert response.status_code == 200

    rows = MessageStore(sqlite3.connect(db)).list(session_id)
    assert any(row["role"] == "assistant" and "退款申请已提交" in row["content"] for row in rows)
    assert not any("通过审核" in (row["content"] or "") for row in rows if row["role"] == "assistant")
    cw.send_interactive_message.assert_awaited_once()
    assert cw.send_interactive_message.await_args.args[2] == "cards"
    cw.send_bot_message.assert_awaited_once()
    assert "退款申请已提交" in cw.send_bot_message.call_args.args[1]


def test_approval_resume_follow_on_handoff_interrupt_is_persisted(tmp_path, monkeypatch):
    """Approval resume must wire interrupt callbacks; follow-on handoff_request is not silent."""
    real_make = server_module.make_ai_graph

    class _FollowOnGraph:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        async def astream(self, invocation, *args, **kwargs):
            async for item in self._inner.astream(invocation, *args, **kwargs):
                yield item
            if isinstance(invocation, Command) and getattr(invocation, "resume", None) is not None:
                yield (
                    (),
                    "updates",
                    {
                        "__interrupt__": (
                            Interrupt(
                                value={
                                    "type": "handoff_request",
                                    "reason": "follow_on_after_approve",
                                    "summary": "follow-on interrupt",
                                },
                                id="follow-on-handoff",
                            ),
                        )
                    },
                )

    def _wrapped_make(*args, **kwargs):
        return _FollowOnGraph(real_make(*args, **kwargs))

    monkeypatch.setattr(server_module, "make_ai_graph", _wrapped_make)

    db = str(tmp_path / "callback.db")
    client = TestClient(
        create_app(
            db,
            model=ApprovalFlowFakeModel(responses=[]),
            seat_approval_secret=SEAT_SECRET,
        )
    )
    session_id, approval_id = _pending_approval_via_chat(client, db)

    response = client.post(f"/approvals/{approval_id}/approve", headers=SEAT_HEADERS)
    assert response.status_code == 200

    conn = sqlite3.connect(db)
    handoff = HandoffService(conn, ConversationStatusStore(conn)).get_by_conversation(session_id)
    mode = ConversationStatusStore(conn).get_mode(session_id)
    conn.close()
    assert handoff is not None
    assert handoff["status"] == "awaiting_seat"
    assert handoff["reason"] == "follow_on_after_approve"
    assert mode == "ai"
