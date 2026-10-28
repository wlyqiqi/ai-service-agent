"""Approval Side-Channel：投影/解析 + 执行链（纯单测，不起 FastAPI）。"""

import asyncio
import sqlite3
from unittest.mock import AsyncMock

import pytest

from ai_service_agent.harness.approval_side_channel import (
    ApprovalExpiredError,
    ApprovalMissing,
    ApprovalProcessed,
    ApprovalSideChannel,
    SeatAction,
    SeatActor,
    SeatAuthError,
    approval_form_items_from_payload,
    parse_approval_form_submission,
    send_approval_card,
)
from ai_service_agent.harness.approvals import ApprovalService

SEAT_SECRET = "test-seat-approval-secret"

APPROVAL_FACTS = {
    "tool": "refund_apply",
    "action_label": "退款申请",
    "order_id": "ORD-PAID-1",
    "amount": 100.0,
    "currency": "CNY",
    "order_total": 100.0,
    "refunded": 0.0,
    "product_summary": "破壁机 ×1",
    "order_status_label": "已付款 · 未发货",
    "customer": {"name": "张伟", "phone_tail": "4567"},
    "customer_quote": "不想要了",
}


def test_card_renders_readable_facts_instead_of_raw_summary():
    """卡片给坐席看的是结构化事实，不是拼好的 summary 串。"""
    from ai_service_agent.harness.approval_card import render_approval_card

    content = render_approval_card(
        APPROVAL_FACTS,
        approval_id="A-42",
        expires_at="2099-01-01T00:00:00+00:00",
        base_url="http://127.0.0.1:8766",
        token="tok",
    )
    assert "高风险审批" in content
    assert "退款申请 ¥100.00" in content
    assert "张伟" in content and "尾号 4567" in content
    assert "ORD-PAID-1" in content
    assert "已付款 · 未发货" in content
    assert "不想要了" in content
    assert "批准并退款" in content
    # 按钮指向 Runtime 确认页，而不是死链
    assert 'href="http://127.0.0.1:8766/approvals/A-42/decide?t=tok&amp;action=approve"' in content


def test_card_degrades_without_facts():
    from ai_service_agent.harness.approval_card import render_approval_card

    content = render_approval_card(
        {}, approval_id="A-1", expires_at="", base_url="http://h", token="t"
    )
    assert "高风险操作" in content
    assert "客户信息暂缺" in content
    assert "None" not in content


def test_items_expose_approval_id_decision_and_note():
    items = approval_form_items_from_payload({"approval_id": "A-7"})
    by_name = {item["name"]: item for item in items}
    assert set(by_name) == {"approval_id", "decision", "note"}
    assert by_name["approval_id"]["default"] == "A-7"
    assert {opt["value"] for opt in by_name["decision"]["options"]} == {"approve", "reject"}


def test_items_do_not_share_the_decision_template_between_calls():
    first = approval_form_items_from_payload({"approval_id": "A-1"})
    first_decision = next(item for item in first if item["name"] == "decision")
    first_decision["options"].append({"label": "x", "value": "x"})

    second = approval_form_items_from_payload({"approval_id": "A-2"})
    second_decision = next(item for item in second if item["name"] == "decision")
    assert {opt["value"] for opt in second_decision["options"]} == {"approve", "reject"}


@pytest.mark.parametrize(
    "submitted",
    [
        {"approval_id": "A-1", "decision": "approve"},
        [
            {"name": "approval_id", "value": "A-1"},
            {"name": "decision", "value": "approve"},
        ],
    ],
    ids=["object", "chatwoot-array"],
)
def test_parse_accepts_both_submission_shapes(submitted):
    assert parse_approval_form_submission(submitted) == {
        "approval_id": "A-1",
        "action": "approve",
    }


def test_parse_keeps_reject_note_and_trims_it():
    parsed = parse_approval_form_submission(
        {"approval_id": "A-1", "decision": "reject", "note": "  资料不足  "}
    )
    assert parsed == {"approval_id": "A-1", "action": "reject", "note": "资料不足"}


def test_parse_omits_blank_note():
    parsed = parse_approval_form_submission(
        {"approval_id": "A-1", "decision": "reject", "note": "   "}
    )
    assert parsed == {"approval_id": "A-1", "action": "reject"}


@pytest.mark.parametrize(
    "submitted",
    [
        None,
        {},
        [],
        {"decision": "approve"},                      # 缺 approval_id
        {"approval_id": "A-1"},                       # 缺 decision
        {"approval_id": "A-1", "decision": "maybe"},  # 非法动作
        {"approval_id": "  ", "decision": "approve"},
    ],
    ids=["none", "empty-dict", "empty-list", "no-id", "no-decision", "bad-action", "blank-id"],
)
def test_parse_rejects_non_seat_submissions(submitted):
    """Mutation Form 提交 / 客户 Widget 提交都不该被当成坐席决策。"""
    assert parse_approval_form_submission(submitted) is None


@pytest.mark.asyncio
async def test_send_approval_card_is_private_interactive():
    chatwoot = AsyncMock()
    await send_approval_card(
        chatwoot,
        "c1",
        {
            "approval_id": "A-1",
            "summary": "cancel ORD-1",
            "expires_at": "2099-01-01T00:00:00+00:00",
            "x_approval": {"facts": APPROVAL_FACTS},
        },
        base_url="http://127.0.0.1:8766",
        secret=SEAT_SECRET,
    )

    chatwoot.send_interactive_message.assert_awaited_once()
    args, kwargs = chatwoot.send_interactive_message.call_args
    assert args[0] == "c1"
    assert "高风险审批" in args[1]
    assert "ORD-PAID-1" in args[1]
    assert args[2] == "form"
    assert {item["name"] for item in args[3]["items"]} == {"approval_id", "decision", "note"}
    assert kwargs.get("private") is True or (len(args) > 4 and args[4] is True)


# --- 执行链：工牌 / 决策 / 落库门控（fake runner，不起 FastAPI） ------------


class _Status:
    def __init__(self, mode: str = "ai", user_id: str = "u1") -> None:
        self.mode = mode
        self.user_id = user_id

    def get_mode(self, _conversation_id: str) -> str:
        return self.mode

    def get_user_id(self, _conversation_id: str) -> str:
        return self.user_id


class _Messages:
    def __init__(self) -> None:
        self.rows: list[tuple] = []

    def append(self, conversation_id, role, text, message_id=None):
        self.rows.append((conversation_id, role, text, message_id))


def _store() -> ApprovalService:
    return ApprovalService(sqlite3.connect(":memory:"))


def _insert(store: ApprovalService, approval_id: str = "A-1", conversation_id: str = "c1"):
    store.insert_if_absent(
        approval_id,
        conversation_id,
        "t1",
        {
            "summary": "退款 ORD-PAID-1",
            "requester_user_id": "u1",
            "created_at": "2026-10-20T00:00:00+00:00",
            "expires_at": "2099-01-01T00:00:00+00:00",
        },
    )


_WRITE_EVENTS = [
    {"type": "tool_call", "data": {"name": "refund_apply", "args": {"order_id": "ORD-1"}}},
    {"type": "tool_result", "data": {"name": "refund_apply", "ok": True, "result": {}}},
    {"type": "done", "data": {"final": "通过审核的长文案", "message_id": "m1"}},
]


def _channel(
    *,
    store: ApprovalService,
    events: list[dict],
    mode: str = "ai",
    chatwoot=None,
    calls: list | None = None,
    handoff_events: list | None = None,
) -> ApprovalSideChannel:
    calls = [] if calls is None else calls
    handoff_events = [] if handoff_events is None else handoff_events

    async def runner(thread_id, *, resume_payload, on_interrupt, user_id):
        calls.append({"thread_id": thread_id, "resume": resume_payload, "user_id": user_id})
        for event in events:
            yield event

    async def lock_factory(_thread_id):
        return asyncio.Lock()

    async def on_handoff_requested(conversation_id, event):
        handoff_events.append((conversation_id, event))

    return ApprovalSideChannel(
        store=store,
        status=_Status(mode=mode),
        messages=_Messages(),
        chatwoot=chatwoot,
        resume_runner=runner,
        lock_factory=lock_factory,
        interrupt_callbacks=lambda _conv, _thread: (lambda value: None),
        on_handoff_requested=on_handoff_requested,
        seat_secret=SEAT_SECRET,
    )


def test_authenticate_is_fail_closed():
    store = _store()
    without_secret = ApprovalSideChannel(
        store=store,
        status=_Status(),
        messages=_Messages(),
        resume_runner=None,
        lock_factory=None,
        interrupt_callbacks=None,
        seat_secret=None,
    )
    with pytest.raises(SeatAuthError):
        without_secret.authenticate(SEAT_SECRET, "9")

    channel = _channel(store=store, events=[])
    with pytest.raises(SeatAuthError):
        channel.authenticate("wrong", "9")
    with pytest.raises(SeatAuthError):
        channel.authenticate(SEAT_SECRET, "   ")
    with pytest.raises(SeatAuthError):
        channel.authenticate("", "9")

    assert channel.authenticate(SEAT_SECRET, " 9 ") == SeatActor("9")


def test_actor_decided_by_uses_seat_namespace():
    assert SeatActor("42").decided_by == "seat:42"


@pytest.mark.asyncio
async def test_approve_in_ai_mode_resumes_persists_and_sends_card():
    store = _store()
    _insert(store)
    calls: list = []
    chatwoot = AsyncMock()
    channel = _channel(store=store, events=_WRITE_EVENTS, chatwoot=chatwoot, calls=calls)

    result = await channel.decide("A-1", SeatAction.APPROVE, actor=SeatActor("7"))

    assert result == {"approval_id": "A-1", "status": "approved"}
    assert calls == [{"thread_id": "t1", "resume": {"action": "approve"}, "user_id": "u1"}]
    record = store.get("A-1")
    assert record["status"] == "approved"
    assert record["decided_by"] == "seat:7"
    # 卡片 + 短 followup；落库文案与 ai 同源，不是长 done.final
    chatwoot.send_interactive_message.assert_awaited_once()
    persisted = [row[2] for row in channel._messages.rows]
    assert persisted and "通过审核" not in persisted[0]
    assert chatwoot.send_bot_message.await_args.args[1] == persisted[0]


@pytest.mark.asyncio
async def test_reject_carries_note_and_skips_card():
    store = _store()
    _insert(store)
    calls: list = []
    chatwoot = AsyncMock()
    channel = _channel(store=store, events=[{"type": "done", "data": {"final": "未通过审核", "message_id": "m2"}}],
                       chatwoot=chatwoot, calls=calls)

    await channel.decide("A-1", SeatAction.REJECT, actor=SeatActor("7"), note="资料不足")

    assert calls[0]["resume"] == {"action": "reject", "note": "资料不足"}
    assert store.get("A-1")["status"] == "rejected"
    chatwoot.send_interactive_message.assert_not_awaited()
    assert channel._messages.rows[0][2] == "未通过审核"
    assert chatwoot.send_bot_message.await_args.args[1] == "未通过审核"


@pytest.mark.asyncio
async def test_human_mode_persists_without_outbound():
    store = _store()
    _insert(store)
    chatwoot = AsyncMock()
    channel = _channel(store=store, events=_WRITE_EVENTS, mode="human", chatwoot=chatwoot)

    await channel.decide("A-1", SeatAction.APPROVE, actor=SeatActor("7"))

    chatwoot.send_interactive_message.assert_not_awaited()
    chatwoot.send_bot_message.assert_not_awaited()
    assert channel._messages.rows, "Message Store 必须始终落库（ADR-0002）"
    assert "通过审核" not in channel._messages.rows[0][2]


@pytest.mark.asyncio
async def test_second_decision_is_rejected_without_second_resume():
    store = _store()
    _insert(store)
    calls: list = []
    channel = _channel(store=store, events=_WRITE_EVENTS, calls=calls)

    await channel.decide("A-1", SeatAction.APPROVE, actor=SeatActor("7"))
    with pytest.raises(ApprovalProcessed):
        await channel.decide("A-1", SeatAction.APPROVE, actor=SeatActor("8"))

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_unknown_approval_is_missing():
    channel = _channel(store=_store(), events=[])
    with pytest.raises(ApprovalMissing):
        await channel.decide("nope", SeatAction.APPROVE, actor=SeatActor("7"))


@pytest.mark.asyncio
async def test_expired_approval_is_reported_before_resume():
    store = _store()
    store.insert_if_absent(
        "A-exp", "c1", "t1",
        {"summary": "s", "requester_user_id": "u1", "created_at": "", "expires_at": "2000-01-01T00:00:00+00:00"},
    )
    calls: list = []
    channel = _channel(store=store, events=[], calls=calls)

    with pytest.raises(ApprovalExpiredError):
        await channel.decide("A-exp", SeatAction.APPROVE, actor=SeatActor("7"))
    assert calls == []


@pytest.mark.asyncio
async def test_follow_on_handoff_event_is_forwarded():
    store = _store()
    _insert(store)
    handoff_events: list = []
    events = [
        {"type": "handoff_requested", "data": {"reason": "follow_on", "summary": "s"}},
        {"type": "done", "data": {"final": "ok", "message_id": "m3"}},
    ]
    channel = _channel(store=store, events=events, handoff_events=handoff_events)

    await channel.decide("A-1", SeatAction.APPROVE, actor=SeatActor("7"))

    assert handoff_events == [("c1", events[0])]


def _seat_form(approval_id: str = "A-1", *, cw_id: int = 555, decision: str = "approve", sender: int = 91):
    return {
        "event": "message_updated",
        "private": True,
        "content_type": "form",
        "content_attributes": {
            "submitted_values": [
                {"name": "approval_id", "value": approval_id},
                {"name": "decision", "value": decision},
            ]
        },
        "sender": {"id": sender, "type": "user"},
        "conversation": {"id": cw_id},
    }


@pytest.mark.asyncio
async def test_seat_form_payload_is_decided_via_mapping():
    store = _store()
    _insert(store)
    channel = _channel(store=store, events=_WRITE_EVENTS)

    handled = await channel.decide_from_seat_form(
        _seat_form(), conversation_for=lambda cw_id: "c1" if cw_id == 555 else None
    )

    assert handled is True
    assert store.get("A-1")["status"] == "approved"
    assert store.get("A-1")["decided_by"] == "seat:91"


@pytest.mark.asyncio
async def test_seat_form_ignores_non_approval_payloads():
    channel = _channel(store=_store(), events=[])
    payload = _seat_form()
    payload["private"] = False
    assert await channel.decide_from_seat_form(payload, conversation_for=lambda _id: "c1") is False


@pytest.mark.asyncio
async def test_seat_form_rejects_unmapped_conversation():
    store = _store()
    _insert(store)
    channel = _channel(store=store, events=[])

    with pytest.raises(ApprovalMissing):
        await channel.decide_from_seat_form(_seat_form(), conversation_for=lambda _id: None)


@pytest.mark.asyncio
async def test_seat_form_rejects_approval_of_another_conversation():
    store = _store()
    _insert(store, conversation_id="other")
    channel = _channel(store=store, events=[])

    with pytest.raises(ApprovalMissing):
        await channel.decide_from_seat_form(_seat_form(), conversation_for=lambda _id: "c1")


@pytest.mark.asyncio
async def test_seat_form_badge_failure_is_not_swallowed():
    store = _store()
    _insert(store)
    channel = _channel(store=store, events=[])

    with pytest.raises(SeatAuthError):
        await channel.decide_from_seat_form(
            _seat_form(sender=0), conversation_for=lambda _id: "c1"
        )


@pytest.mark.asyncio
async def test_follow_on_approval_is_projected_to_seat_form():
    """approval-spec §3.2.2：resume 后再次中断不得静默——必须推新坐席表单。"""
    store = _store()
    _insert(store)
    chatwoot = AsyncMock()
    events = [
        {
            "type": "approval_required",
            "data": {"approval_id": "A-2", "summary": "第二张单", "expires_at": "2099-01-01T00:00:00+00:00"},
        },
        {"type": "done", "data": {"final": None, "message_id": None}},
    ]
    channel = _channel(store=store, events=events, chatwoot=chatwoot)

    await channel.decide("A-1", SeatAction.APPROVE, actor=SeatActor("7"))

    chatwoot.send_interactive_message.assert_awaited_once()
    args, kwargs = chatwoot.send_interactive_message.call_args
    assert args[2] == "form"
    assert "A-2" in args[1]
    assert kwargs.get("private") is True


@pytest.mark.asyncio
async def test_follow_on_approval_without_chatwoot_is_silent_not_fatal():
    store = _store()
    _insert(store)
    events = [
        {"type": "approval_required", "data": {"approval_id": "A-2", "summary": "s", "expires_at": ""}},
        {"type": "done", "data": {"final": "ok", "message_id": "m4"}},
    ]
    channel = _channel(store=store, events=events, chatwoot=None)

    await channel.decide("A-1", SeatAction.APPROVE, actor=SeatActor("7"))

    assert channel._messages.rows[0][2] == "ok"


# --- 投影幂等（T5：修掉实测的同一审批连发 3 条） ---------------------------


@pytest.mark.asyncio
async def test_project_is_idempotent_per_approval():
    store = _store()
    _insert(store, "A-1")
    chatwoot = AsyncMock()
    channel = _channel(store=store, events=[], chatwoot=chatwoot)
    payload = {
        "approval_id": "A-1",
        "summary": "s",
        "expires_at": "2099-01-01T00:00:00+00:00",
    }

    await channel.project("c1", payload)
    await channel.project("c1", payload)
    await channel.project("c1", payload)

    assert chatwoot.send_interactive_message.await_count == 1
    assert store.get("A-1")["projected_at"] is not None


@pytest.mark.asyncio
async def test_project_prefers_stored_payload_for_richer_card():
    """库里存的原始载荷含 facts；事件 data 只有 summary —— 卡片要用前者。"""
    store = _store()
    store.insert_if_absent(
        "A-9",
        "c1",
        "t1",
        {
            "approval_id": "A-9",
            "summary": "退款申请 ORD-PAID-9",
            "expires_at": "2099-01-01T00:00:00+00:00",
            "x_approval": {
                "facts": {
                    "tool": "refund_apply",
                    "action_label": "退款申请",
                    "order_id": "ORD-PAID-9",
                    "amount": 88.0,
                }
            },
        },
    )
    chatwoot = AsyncMock()
    channel = _channel(store=store, events=[], chatwoot=chatwoot)

    await channel.project("c1", {"approval_id": "A-9", "summary": "只有摘要"})

    content = chatwoot.send_interactive_message.call_args[0][1]
    assert "ORD-PAID-9" in content
    assert "退款申请 ¥88.00" in content
