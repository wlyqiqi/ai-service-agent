"""Approval Side-Channel 投影/解析（纯单测，不起 FastAPI / 不落库）。"""

from unittest.mock import AsyncMock

import pytest

from ai_service_agent.harness.approval_side_channel import (
    approval_form_items_from_payload,
    approval_form_title_from_payload,
    parse_approval_form_submission,
    send_approval_form,
)


def test_title_carries_summary_approval_id_and_expiry():
    title = approval_form_title_from_payload(
        {
            "approval_id": "A-42",
            "summary": "退款 ORD-PAID-1 · ¥100",
            "expires_at": "2099-01-01T00:00:00+00:00",
        }
    )
    assert "高风险审批 · 退款 ORD-PAID-1 · ¥100" in title
    assert "单号 A-42" in title
    assert "截止 2099-01-01T00:00:00+00:00" in title


def test_title_falls_back_when_summary_missing():
    title = approval_form_title_from_payload({})
    assert title == "高风险审批 · 高风险操作待审批"


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
async def test_send_approval_form_is_private_interactive():
    chatwoot = AsyncMock()
    await send_approval_form(
        chatwoot,
        "c1",
        {"approval_id": "A-1", "summary": "cancel ORD-1", "expires_at": "2099-01-01T00:00:00+00:00"},
    )

    chatwoot.send_interactive_message.assert_awaited_once()
    args, kwargs = chatwoot.send_interactive_message.call_args
    assert args[0] == "c1"
    assert "cancel ORD-1" in args[1]
    assert args[2] == "form"
    assert {item["name"] for item in args[3]["items"]} == {"approval_id", "decision", "note"}
    assert kwargs.get("private") is True or (len(args) > 4 and args[4] is True)
