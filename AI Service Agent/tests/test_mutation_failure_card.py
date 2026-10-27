"""Mutation failure card: symmetric copy with the success card, one button only.

Covers:
1. copy contract — 7 write tools, 状态/原因 bullets, 「重新填写」 only (never 「知道了」)
2. projection — cards items + thin send + ``WriteFailureCapture`` from events
3. gate — failure card is delivered even without a follow-up bubble; long
   ``done.final`` / cold-start greeting never rides along
4. AgentBot — write failure projects card + short retry bubble
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ai_service_agent.graph.workflows.copy import (
    _FAILURE_CARD_TITLES,
    _SUCCESS_CARD_TITLES,
    build_mutation_failure_card,
    build_mutation_success_card,
    mutation_failure_reason,
    mutation_failure_retry_payload,
    mutation_tool_failed_text,
)
from ai_service_agent.harness.agent_bot import handle_agent_bot_message
from ai_service_agent.harness.mutation_projection import (
    WriteFailureCapture,
    mutation_failure_card_items,
    send_mutation_failure_card,
)
from ai_service_agent.harness.outbound_gate import (
    DeliveryDecision,
    TurnOrigin,
    decide_delivery,
)

_WRITE_TOOLS = (
    "update_address",
    "update_phone",
    "update_order_note",
    "create_invoice",
    "create_service_ticket",
    "cancel_order",
    "refund_apply",
)


# --- 1. copy contract --------------------------------------------------------


def test_failure_titles_cover_the_same_seven_tools_as_success():
    assert set(_FAILURE_CARD_TITLES) == set(_SUCCESS_CARD_TITLES) == set(_WRITE_TOOLS)
    assert _FAILURE_CARD_TITLES != _SUCCESS_CARD_TITLES


@pytest.mark.parametrize("tool", _WRITE_TOOLS)
def test_failure_card_shape_and_single_retry_action(tool):
    card = build_mutation_failure_card(tool, "请填写有效地址", kind="form_rejected")
    assert card is not None
    assert card["tool"] == tool
    assert card["title"] == _FAILURE_CARD_TITLES[tool]
    assert card["description"].startswith("• 状态：未完成")
    assert "• 原因：请填写有效地址" in card["description"]
    assert [action["text"] for action in card["actions"]] == ["重新填写"]
    assert card["actions"][0]["type"] == "postback"
    assert card["actions"][0]["payload"] == f"{tool}_failure_retry"
    assert card["actions"][0]["payload"] == mutation_failure_retry_payload(tool)
    assert "知道了" not in str(card)


def test_failure_card_keeps_identity_facts_and_drops_echo_fields():
    card = build_mutation_failure_card(
        "update_address",
        "请填写有效地址",
        kind="form_rejected",
        facts={"order_id": "ORD-PAID-1", "address": "x", "request_id": "r1", "ok": True},
    )
    assert card is not None
    assert "• 订单号：ORD-PAID-1" in card["description"]
    assert "• 收货地址：x" in card["description"]
    assert "r1" not in card["description"]


def test_failure_reason_falls_back_without_echoing_the_error_code():
    card = build_mutation_failure_card("update_phone", None, kind="tool_failed")
    assert card is not None
    assert "请核对后重新填写" in card["description"]
    assert mutation_failure_reason("data_source_unavailable") == "订单系统暂时不可用，请稍后再试"
    assert mutation_failure_reason("some_new_code") == "本次操作未能完成"
    assert "some_new_code" not in mutation_failure_reason("some_new_code")


def test_failure_card_rejects_unknown_kind_and_unknown_tool():
    with pytest.raises(ValueError):
        build_mutation_failure_card("update_address", "x", kind="nope")
    assert build_mutation_failure_card("not_a_tool", "x") is None


def test_success_and_failure_cards_are_mutually_exclusive_in_copy():
    """同一工具两套卡：标题与按钮文案不得互相串用。"""
    for tool in _WRITE_TOOLS:
        success = build_mutation_success_card(tool, {"order_id": "ORD-1"}, None)
        failure = build_mutation_failure_card(tool, "x")
        assert success is not None and failure is not None
        assert success.title != failure["title"]
        assert success.button_text == "知道了"
        assert failure["actions"][0]["text"] == "重新填写"


# --- 2. projection -----------------------------------------------------------


def test_failure_card_items_are_one_chatwoot_card():
    card = build_mutation_failure_card("create_invoice", "该订单已经开过发票", kind="tool_failed")
    assert card is not None
    items = mutation_failure_card_items(card)
    assert len(items) == 1
    assert items[0]["title"] == card["title"]
    assert items[0]["description"] == card["description"]
    assert items[0]["actions"] == card["actions"]


@pytest.mark.asyncio
async def test_send_failure_card_uses_cards_content_type():
    chatwoot = AsyncMock()
    card = build_mutation_failure_card("update_address", "请填写有效地址")
    assert card is not None
    await send_mutation_failure_card(chatwoot, "c1", card)
    args = chatwoot.send_interactive_message.await_args.args
    assert args[0] == "c1"
    assert args[1] == "地址未能修改"
    assert args[2] == "cards"
    assert args[3]["items"][0]["title"] == "地址未能修改"
    chatwoot.send_bot_message.assert_not_awaited()


def test_write_failure_capture_reads_failed_tool_result():
    capture = WriteFailureCapture()
    capture.observe({"type": "tool_call", "data": {"name": "update_address", "args": {"order_id": "ORD-1", "address": "新址"}}})
    assert capture.tool is None
    capture.observe(
        {
            "type": "tool_result",
            "data": {
                "name": "update_address",
                "ok": False,
                "summary": "data_source_unavailable",
                "result": {"error": "data_source_unavailable"},
            },
        }
    )
    assert capture.tool == "update_address"
    assert capture.error == "data_source_unavailable"
    card = capture.card()
    assert card is not None
    assert card["kind"] == "tool_failed"
    assert "订单系统暂时不可用" in card["description"]
    assert "• 订单号：ORD-1" in card["description"]
    assert capture.followup() == mutation_tool_failed_text("update_address", "data_source_unavailable")


def test_write_failure_capture_ignores_success_and_non_write_tools():
    capture = WriteFailureCapture()
    capture.observe({"type": "tool_call", "data": {"name": "kb_search", "args": {}}})
    capture.observe({"type": "tool_result", "data": {"name": "kb_search", "ok": False}})
    capture.observe({"type": "tool_result", "data": {"name": "update_address", "ok": True}})
    assert capture.tool is None
    assert capture.card() is None
    assert capture.followup() is None


# --- 3. gate -----------------------------------------------------------------


def test_failure_card_is_delivered_without_any_bubble():
    decision = decide_delivery(
        mode="ai",
        origin=TurnOrigin.AI_TURN,
        failure_spec={"followup": None},
        final="很抱歉，您的操作未成功……",
    )
    assert decision == DeliveryDecision(
        persist=None, outbound=True, card=True, notice=None, card_kind="failure"
    )


def test_failure_card_with_short_bubble_persists_the_bubble_not_the_final():
    decision = decide_delivery(
        mode="ai",
        origin=TurnOrigin.AI_TURN,
        failure_spec={"followup": "可以稍后再试"},
        final="您好，我是澄元客服，很高兴为您服务。",
    )
    assert decision.persist == "可以稍后再试"
    assert decision.card_kind == "failure"


def test_success_card_suppresses_cold_start_greeting():
    class _Spec:
        followup = "已帮您更新。发货前都会按新地址安排，有问题随时找我。"

    decision = decide_delivery(
        mode="ai",
        origin=TurnOrigin.AI_TURN,
        success_spec=_Spec(),
        final="您好，我是澄元智能家电客服，请问有什么可以帮您？",
    )
    assert decision.persist == _Spec.followup
    assert "您好" not in str(decision.persist)
    assert decision.card is True


def test_human_ai_turn_still_drops_the_failure_card():
    decision = decide_delivery(
        mode="human",
        origin=TurnOrigin.AI_TURN,
        failure_spec={"followup": "x"},
        final="y",
    )
    assert decision.card is False
    assert decision.outbound is False


# --- 4. AgentBot: write failure ---------------------------------------------


def _contact_first_stores(tmp_path):
    from ai_service_agent.harness.chatwoot import ChatwootMappingStore
    from ai_service_agent.harness.session import ConversationStatusStore

    conn = sqlite3.connect(str(tmp_path / "agentbot-failure-card.db"))
    return ConversationStatusStore(conn), ChatwootMappingStore(conn), conn


def _incoming_text_payload(*, message_id: str, cw_conv_id: int, contact: dict, content: str):
    return {
        "event": "message_created",
        "id": message_id,
        "message_type": "incoming",
        "content_type": "text",
        "content": content,
        "conversation": {"id": cw_conv_id},
        "contact": contact,
    }


@pytest.mark.asyncio
async def test_agent_bot_write_failure_sends_card_then_short_bubble(monkeypatch, tmp_path):
    status, mapping, _conn = _contact_first_stores(tmp_path)
    chatwoot = AsyncMock()
    long_done = "很抱歉，您的收货地址修改操作未成功（处理编号：deadbeef）。请不要担心……"

    async def fake_event_stream(*_args, **_kwargs):
        yield {
            "type": "tool_call",
            "data": {"name": "update_address", "args": {"order_id": "ORD-PAID-1", "address": "新址"}},
        }
        yield {
            "type": "tool_result",
            "data": {
                "name": "update_address",
                "ok": False,
                "summary": "data_source_unavailable",
                "result": {"error": "data_source_unavailable"},
            },
        }
        yield {"type": "done", "data": {"final": long_done, "message_id": "m-fail"}}

    monkeypatch.setattr(
        "ai_service_agent.harness.agent_bot.event_stream", fake_event_stream
    )

    result = await handle_agent_bot_message(
        _incoming_text_payload(
            message_id="tool-failed-1",
            cw_conv_id=9910,
            contact={"id": 91, "identifier": "u1", "name": "郑军"},
            content="我要改个地址",
        ),
        chatwoot,
        graph=SimpleNamespace(checkpointer=None),
        status=status,
        mapping=mapping,
    )

    assert result is not None
    chatwoot.send_interactive_message.assert_awaited_once()
    args = chatwoot.send_interactive_message.await_args.args
    assert args[2] == "cards"
    card = args[3]["items"][0]
    assert card["title"] == "地址未能修改"
    assert "订单系统暂时不可用" in card["description"]
    assert [action["text"] for action in card["actions"]] == ["重新填写"]
    # 短引导气泡只发一次，且不是长 done.final
    chatwoot.send_bot_message.assert_awaited_once()
    bubble = chatwoot.send_bot_message.await_args.args[1]
    assert bubble == mutation_tool_failed_text("update_address", "data_source_unavailable")
    assert result["response"] == bubble
