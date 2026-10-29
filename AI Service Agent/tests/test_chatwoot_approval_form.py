"""Chatwoot Approval Side-Channel form: project + seat submit (#64).

Seams (issue acceptance):
1. Pending approval_required → private seat-facing Chatwoot form/card with summary.
2. Verified Chatwoot seat submit → shared seat decision entry → decided_by=seat:{id}.
3. Customer Widget / AgentBot contact path cannot self-approve.
4. Human-mode outbound gate from #63 still holds when decide originates from Chatwoot.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from ai_service_agent.harness import server as server_module
from ai_service_agent.harness.approvals import ApprovalService
from ai_service_agent.harness.chatwoot import ChatwootMappingStore
from ai_service_agent.harness.messages import MessageStore
from ai_service_agent.harness.server import create_app
from ai_service_agent.harness.session import ConversationStatusStore
from ai_service_agent.testing import ApprovalFlowFakeModel

SEAT_SECRET = "test-seat-approval-secret"
WEBHOOK_SECRET = "cw-webhook-sec"
# 每个请求用新鲜时间戳：签名有 300s 重放窗口，模块级 TS 在长跑全量时会过期（401）。
_ROOT = Path(__file__).resolve().parents[1]


def _load_seed_module():
    spec = importlib.util.spec_from_file_location(
        "seed_mock_db_chatwoot_approval_form", _ROOT / "scripts" / "seed_mock_db.py"
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


def _signed_headers(payload: bytes, secret: str = WEBHOOK_SECRET) -> dict[str, str]:
    ts = str(int(time.time()))
    signature = "sha256=" + hmac.new(
        secret.encode(), ts.encode() + b"." + payload, hashlib.sha256
    ).hexdigest()
    return {"X-Chatwoot-Signature": signature, "X-Chatwoot-Timestamp": ts}


def _pending_with_mapping(tmp_path, *, cw_conv_id: int = 555) -> tuple[TestClient, str, str, str, object]:
    """Trigger HighRisk approval with Chatwoot mapping + mock client."""
    cw = SimpleNamespace(
        send_bot_message=AsyncMock(),
        send_interactive_message=AsyncMock(),
    )
    db = str(tmp_path / "approval-form.db")
    client = TestClient(
        create_app(
            db,
            model=ApprovalFlowFakeModel(responses=[]),
            chatwoot=cw,
            chatwoot_webhook_secret=WEBHOOK_SECRET,
            seat_approval_secret=SEAT_SECRET,
        )
    )
    session_id = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    conn = sqlite3.connect(db)
    ChatwootMappingStore(conn).put(session_id, cw_conv_id)
    conn.close()
    with client.stream(
        "POST", "/chat", json={"session_id": session_id, "message": "给 ORD-PAID-1 申请退款"}
    ) as r:
        assert '"approval_required"' in "".join(r.iter_text())
    conn = sqlite3.connect(db)
    approval_id = conn.execute("SELECT approval_id FROM approval_record LIMIT 1").fetchone()[0]
    summary = conn.execute("SELECT summary FROM approval_record LIMIT 1").fetchone()[0]
    conn.close()
    return client, db, session_id, approval_id, cw


# --- Seam 1: projection -------------------------------------------------


def test_approval_form_items_include_summary_decision_and_approval_id():
    from ai_service_agent.harness.approval_card import render_approval_card
    from ai_service_agent.harness.approval_side_channel import approval_form_items_from_payload

    data = {
        "approval_id": "A-42",
        "summary": "退款 ORD-PAID-1 · ¥100",
        "expires_at": "2099-01-01T00:00:00+00:00",
        "x_approval": {"facts": {"tool": "refund_apply", "action_label": "退款申请", "order_id": "ORD-PAID-1", "amount": 100}},
    }
    content = render_approval_card(
        data["x_approval"]["facts"],
        approval_id=data["approval_id"],
        expires_at=data["expires_at"],
        base_url="http://127.0.0.1:8766",
        token="tok",
    )
    items = approval_form_items_from_payload(data)
    assert "退款申请 ¥100.00" in content
    assert "ORD-PAID-1" in content
    names = {item["name"] for item in items}
    assert "approval_id" in names
    assert "decision" in names
    decision = next(item for item in items if item["name"] == "decision")
    values = {opt["value"] for opt in decision["options"]}
    assert values == {"approve", "reject"}
    aid = next(item for item in items if item["name"] == "approval_id")
    assert aid.get("default") == "A-42"


@pytest.mark.asyncio
async def test_send_approval_card_is_private_interactive(tmp_path):
    from ai_service_agent.harness.approval_side_channel import send_approval_card

    chatwoot = AsyncMock()
    await send_approval_card(
        chatwoot,
        "c1",
        {
            "approval_id": "A-1",
            "summary": "cancel ORD-1",
            "expires_at": "2099-01-01T00:00:00+00:00",
            "x_approval": {"facts": {"tool": "cancel_order", "action_label": "订单取消", "order_id": "ORD-1"}},
        },
        base_url="http://127.0.0.1:8766",
        secret="test-secret",
    )
    chatwoot.send_interactive_message.assert_awaited_once()
    args, kwargs = chatwoot.send_interactive_message.call_args
    assert args[0] == "c1"
    assert "高风险审批" in args[1] and "ORD-1" in args[1]
    assert args[2] == "form"
    assert "items" in args[3]
    assert kwargs.get("private") is True or (len(args) > 4 and args[4] is True)


def test_pending_approval_projects_private_seat_form_to_chatwoot(tmp_path):
    """acceptance: pending approval_required projected to seat-facing surface."""
    _client, _db, _session_id, approval_id, cw = _pending_with_mapping(tmp_path)

    assert cw.send_interactive_message.await_count >= 1
    _args, kwargs = cw.send_interactive_message.await_args
    assert kwargs.get("private") is True
    body = _args[3] if len(_args) > 3 else kwargs.get("content_attributes")
    items = body["items"]
    aid_item = next(i for i in items if i["name"] == "approval_id")
    assert aid_item.get("default") == approval_id


# --- Seam 2: Chatwoot seat submit → shared decision entry ---------------


def test_chatwoot_seat_form_approve_sets_decided_by_seat_id(tmp_path):
    client, db, session_id, approval_id, cw = _pending_with_mapping(tmp_path)
    cw.send_bot_message.reset_mock()
    cw.send_interactive_message.reset_mock()

    body = {
        "event": "message_updated",
        "id": 9001,
        "message_type": "outgoing",
        "content_type": "form",
        "private": True,
        "content": "approval form",
        "content_attributes": {
            "submitted_values": [
                {"name": "approval_id", "value": approval_id},
                {"name": "decision", "value": "approve"},
            ]
        },
        "sender": {"id": 77, "type": "user"},
        "conversation": {"id": 555},
    }
    raw = json.dumps(body).encode()
    r = client.post(
        "/webhooks/chatwoot",
        content=raw,
        headers=_signed_headers(raw),
    )
    assert r.status_code == 200
    rec = ApprovalService(sqlite3.connect(db)).get(approval_id)
    assert rec["status"] == "approved"
    assert rec["decided_by"] == "seat:77"
    rows = MessageStore(sqlite3.connect(db)).list(session_id)
    assert any(row["role"] == "assistant" for row in rows)


def test_chatwoot_seat_form_reject_with_note(tmp_path):
    client, db, _session_id, approval_id, _cw = _pending_with_mapping(tmp_path)

    body = {
        "event": "message_updated",
        "id": 9002,
        "message_type": "outgoing",
        "content_type": "form",
        "private": True,
        "content_attributes": {
            "submitted_values": [
                {"name": "approval_id", "value": approval_id},
                {"name": "decision", "value": "reject"},
                {"name": "note", "value": "资料不足"},
            ]
        },
        "sender": {"id": 88, "type": "user"},
        "conversation": {"id": 555},
    }
    raw = json.dumps(body).encode()
    r = client.post(
        "/webhooks/chatwoot",
        content=raw,
        headers=_signed_headers(raw),
    )
    assert r.status_code == 200
    rec = ApprovalService(sqlite3.connect(db)).get(approval_id)
    assert rec["status"] == "rejected"
    assert rec["decided_by"] == "seat:88"


def test_chatwoot_public_or_contact_approval_form_cannot_decide(tmp_path):
    """Widget/public form or contact sender must not approve via Chatwoot webhook."""
    client, db, _session_id, approval_id, _cw = _pending_with_mapping(tmp_path)
    submitted = [
        {"name": "approval_id", "value": approval_id},
        {"name": "decision", "value": "approve"},
    ]
    for body in (
        {
            "event": "message_updated",
            "id": 9010,
            "content_type": "form",
            "private": False,
            "content_attributes": {"submitted_values": submitted},
            "sender": {"id": 77, "type": "user"},
            "conversation": {"id": 555},
        },
        {
            "event": "message_updated",
            "id": 9011,
            "content_type": "form",
            "private": True,
            "content_attributes": {"submitted_values": submitted},
            "sender": {"id": 1, "type": "contact"},
            "conversation": {"id": 555},
        },
    ):
        raw = json.dumps(body).encode()
        r = client.post(
            "/webhooks/chatwoot",
            content=raw,
            headers=_signed_headers(raw),
        )
        assert r.status_code == 200
    assert ApprovalService(sqlite3.connect(db)).get(approval_id)["status"] == "pending"

# --- Seam 3: customer Widget cannot self-approve ------------------------


@pytest.mark.asyncio
async def test_agent_bot_contact_approval_form_submit_does_not_decide(tmp_path):
    """Customer Widget path must not self-approve via form submit (Approval SoD)."""
    from ai_service_agent.harness.agent_bot import handle_agent_bot_message
    from ai_service_agent.harness.approval_side_channel import parse_approval_form_submission
    from ai_service_agent.harness.approvals import ApprovalService
    from ai_service_agent.harness.session import ConversationStatusStore

    submitted = [
        {"name": "approval_id", "value": "A-widget"},
        {"name": "decision", "value": "approve"},
    ]
    assert parse_approval_form_submission(submitted) is not None

    db = str(tmp_path / "widget-sod.db")
    conn = sqlite3.connect(db)
    status = ConversationStatusStore(conn)
    session = status.create_session("u1")
    conv_id = session["conversation_id"]
    thread_id, _ = status.ensure_thread(conv_id)
    ApprovalService(conn).insert_if_absent(
        "A-widget",
        conv_id,
        thread_id,
        {
            "summary": "refund",
            "requester_user_id": "u1",
            "expires_at": "2099-01-01T00:00:00+00:00",
        },
    )
    mapping = ChatwootMappingStore(conn)
    mapping.put(conv_id, 42, contact_id=1)
    messages = MessageStore(conn)

    payload = {
        "event": "message_updated",
        "id": "501",
        "message_type": "incoming",
        "content_type": "form",
        "content_attributes": {"submitted_values": submitted},
        "conversation": {"id": 42},
        "contact": {"id": 1, "identifier": "u1", "name": "customer"},
    }
    chatwoot = AsyncMock()
    await handle_agent_bot_message(
        payload,
        chatwoot,
        status=status,
        messages_store=messages,
        mapping=mapping,
    )
    # Contact path must never flip the approval; seat decide lives on /webhooks/chatwoot only.
    assert ApprovalService(conn).get("A-widget")["status"] == "pending"
    conn.close()

# --- Seam 4: #63 human-mode outbound gate via Chatwoot decide -----------


def test_chatwoot_seat_approve_while_human_mode_no_outbound(tmp_path):
    client, db, session_id, approval_id, cw = _pending_with_mapping(tmp_path)
    conn = sqlite3.connect(db)
    ConversationStatusStore(conn).set_mode(session_id, "human")
    conn.close()
    cw.send_bot_message.reset_mock()

    body = {
        "event": "message_updated",
        "id": 9003,
        "message_type": "outgoing",
        "content_type": "form",
        "private": True,
        "content_attributes": {
            "submitted_values": [
                {"name": "approval_id", "value": approval_id},
                {"name": "decision", "value": "approve"},
            ]
        },
        "sender": {"id": 91, "type": "user"},
        "conversation": {"id": 555},
    }
    raw = json.dumps(body).encode()
    r = client.post(
        "/webhooks/chatwoot",
        content=raw,
        headers=_signed_headers(raw),
    )
    assert r.status_code == 200
    assert ApprovalService(sqlite3.connect(db)).get(approval_id)["status"] == "approved"
    rows = MessageStore(sqlite3.connect(db)).list(session_id)
    # D1=A：human 期间落库文案与 ai 同源（短 followup），不再落长 done.final。
    assert any(row["role"] == "assistant" and "退款申请已提交" in row["content"] for row in rows)
    assert not any("通过审核" in (row["content"] or "") for row in rows if row["role"] == "assistant")
    # Projection may have used send_interactive_message earlier; outbound after resume must not send.
    # 坐席私备注（private=True）不算出站，决策留痕仍要发。
    public_messages = [
        call for call in cw.send_bot_message.await_args_list if not call.kwargs.get("private")
    ]
    assert public_messages == []


# --- Seam 5: 审批挂起期间客户侧不能静默 -------------------------------------
#
# 实测缺陷（2026-10-29，会话 283a8834）：客户提交退款后图停在 approval_pause，
# 审批卡只发给坐席（private=True），客户侧一条消息都没有；后续「?」「你好」
# 命中 approval_dispatch == "pending" 直接 return None —— 客户以为机器人死了，
# 最后只能去点转人工。


def _pending_interrupt_value(*, minutes: int = 27) -> dict:
    from datetime import datetime, timedelta, timezone

    return {
        "type": "approval_required",
        "approval_id": "c8b1de48-ab26-449c-883c-055c93d8e17f",
        "summary": "退款申请 ORD-2026-10034｜退款金额 397.56",
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(),
        "x_approval": {
            "facts": {
                "tool": "refund_apply",
                "action_label": "退款申请",
                "order_id": "ORD-2026-10034",
                "amount": 397.56,
                "currency": "CNY",
            }
        },
    }


@pytest.mark.asyncio
async def test_pending_approval_still_answers_customer(tmp_path):
    """挂起期间客户再发消息必须有人回，且落 Message Store。"""
    from ai_service_agent.harness.agent_bot import handle_agent_bot_message

    db = str(tmp_path / "pending-reply.db")
    conn = sqlite3.connect(db)
    status = ConversationStatusStore(conn)
    conv_id = status.create_session("u1")["conversation_id"]
    mapping = ChatwootMappingStore(conn)
    mapping.put(conv_id, 901, contact_id=1)
    messages = MessageStore(conn)

    interrupt_value = _pending_interrupt_value()
    graph = SimpleNamespace(
        checkpointer=object(),
        aget_state=AsyncMock(
            return_value=SimpleNamespace(interrupts=[SimpleNamespace(value=interrupt_value)])
        ),
    )
    chatwoot = AsyncMock()

    result = await handle_agent_bot_message(
        {
            "event": "message_created",
            "id": "9001",
            "message_type": "incoming",
            "content_type": "text",
            "content": "你好",
            "conversation": {"id": 901},
            "contact": {"id": 1, "identifier": "u1", "name": "customer"},
        },
        chatwoot,
        graph=graph,
        status=status,
        messages_store=messages,
        mapping=mapping,
    )

    assert result is not None, "挂起期间不能 return None（客户侧会完全静默）"
    text = result["response"]
    assert text and "审核中" in text
    assert "退款申请" in text
    chatwoot.send_bot_message.assert_awaited_once()
    assert messages.list(conv_id)[-1]["content"] == text
    conn.close()


def test_pending_notice_copy_first_and_follow_up():
    """首次挂起说「已受理」，后续消息说「还在审核」。"""
    from ai_service_agent.harness.agent_bot import _approval_wait_notice

    value = _pending_interrupt_value(minutes=27)
    first = _approval_wait_notice(value, first=True)
    assert "已收到您的退款申请" in first
    assert "ORD-2026-10034" in first and "¥397.56" in first
    assert "分钟内出结果" in first

    later = _approval_wait_notice(value, first=False)
    assert "还在审核中" in later
    assert "c8b1de4" in later


def test_pending_notice_survives_missing_facts():
    """事件 data 只有 summary（老载荷 / 无 x_approval）也不能崩。"""
    from ai_service_agent.harness.agent_bot import _approval_wait_notice

    value = {"type": "approval_required", "approval_id": "A-1", "summary": "退款申请"}
    assert "审核中" in _approval_wait_notice(value, first=True)
    assert "审核中" in _approval_wait_notice(value, first=False)
    assert "审核中" in _approval_wait_notice(None, first=True)


def test_minutes_left_never_promises_a_deadline_for_bad_input():
    from ai_service_agent.harness.agent_bot import _approval_minutes_left

    assert _approval_minutes_left(None) is None
    assert _approval_minutes_left("not-a-time") is None
    assert _approval_minutes_left("2000-01-01T00:00:00+00:00") is None


@pytest.mark.asyncio
async def test_first_approval_interrupt_tells_customer(monkeypatch, tmp_path):
    """图首次停在 approval_pause 时，客户必须收到「已受理」——原来这条路径完全静默。"""
    from ai_service_agent.harness import agent_bot as agent_bot_module

    async def fake_event_stream(graph, config, messages, **kwargs):
        yield {
            "type": "approval_required",
            "data": {
                "approval_id": "A-9",
                "summary": "退款申请 ORD-2026-10034｜退款金额 397.56",
                "expires_at": "2099-01-01T00:00:00+00:00",
            },
        }
        yield {"type": "done", "data": {"final": None, "message_id": None}}

    monkeypatch.setattr(agent_bot_module, "event_stream", fake_event_stream)

    db = str(tmp_path / "first-interrupt.db")
    conn = sqlite3.connect(db)
    status = ConversationStatusStore(conn)
    conv_id = status.create_session("u1")["conversation_id"]
    mapping = ChatwootMappingStore(conn)
    mapping.put(conv_id, 902, contact_id=1)
    messages = MessageStore(conn)

    # project() 的返回值：库里的完整载荷（含 facts），文案要靠它写出订单号与金额
    on_approval_pending = AsyncMock(return_value=_pending_interrupt_value())
    chatwoot = AsyncMock()

    result = await agent_bot_module.handle_agent_bot_message(
        {
            "event": "message_created",
            "id": "9002",
            "message_type": "incoming",
            "content_type": "text",
            "content": "ORD-2026-10034",
            "conversation": {"id": 902},
            "contact": {"id": 1, "identifier": "u1", "name": "customer"},
        },
        chatwoot,
        graph=SimpleNamespace(checkpointer=None),
        status=status,
        messages_store=messages,
        mapping=mapping,
        on_approval_pending=on_approval_pending,
    )

    on_approval_pending.assert_awaited_once()
    text = result["response"]
    assert "已收到您的退款申请" in text
    assert "ORD-2026-10034" in text and "¥397.56" in text
    chatwoot.send_bot_message.assert_awaited_once()
    assert messages.list(conv_id)[-1]["content"] == text
    conn.close()
