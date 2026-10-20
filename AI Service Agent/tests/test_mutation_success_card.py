"""Shared write-success card contract for all seven mutation + high_risk tools."""

from __future__ import annotations

import pytest

from ai_service_agent.graph.workflows.copy import (
    _COMPLETION_TEXTS,
    build_mutation_success_card,
    format_success_card_description,
    update_address_success_followup_text,
)
from ai_service_agent.harness.mutation_projection import mutation_success_card_items


def _description(tool: str, args: dict | None = None, result: dict | None = None) -> str:
    spec = build_mutation_success_card(tool, args, result)
    assert spec is not None
    return spec.description


def test_description_uses_bullets_and_blank_lines():
    text = format_success_card_description([("状态", "已完成"), ("订单号", "ORD-1"), ("备注内容", "")])
    assert text == "• 状态：已完成\n\n• 订单号：ORD-1"
    assert "\n\n" in text
    assert all(line.startswith("• ") for line in text.split("\n\n"))


@pytest.mark.parametrize(
    ("tool", "title"),
    [
        ("update_address", "地址已修改成功"),
        ("update_phone", "手机号已修改成功"),
        ("update_order_note", "订单备注已更新"),
        ("create_invoice", "发票已开具成功"),
        ("create_service_ticket", "售后工单已创建"),
        ("cancel_order", "订单已取消成功"),
        ("refund_apply", "退款申请已提交"),
    ],
)
def test_every_write_tool_has_status_title_button_and_followup(tool, title):
    spec = build_mutation_success_card(tool, {"order_id": "ORD-1"}, None)
    assert spec is not None
    assert spec.title == title
    assert spec.description.startswith("• 状态：已完成")
    assert spec.button_text == "知道了"
    items = mutation_success_card_items(spec)
    assert items[0]["actions"][0]["text"] == "知道了"
    assert items[0]["actions"][0]["payload"] == f"{tool}_success_ack"
    if tool == "update_address":
        assert spec.followup == update_address_success_followup_text()
    else:
        assert spec.followup == _COMPLETION_TEXTS[tool]


def test_empty_optional_lines_are_omitted():
    cancel = _description("cancel_order", {"order_id": "ORD-1", "reason": ""}, {"result": {"reason": None}})
    assert "取消原因" not in cancel
    assert "• 订单号：ORD-1" in cancel
    assert "• 退款路径提示：退款将按原支付路径退回" in cancel

    phone = _description("update_phone", {"phone": "18600001111"})
    assert "订单号" not in phone
    assert "• 新手机号：18600001111" in phone
    assert "• 通知号码：已切换" in phone

    phone_with_order = _description("update_phone", {"phone": "18600001111", "order_id": "ORD-9"})
    assert "• 订单号：ORD-9" in phone_with_order

    invoice = _description(
        "create_invoice",
        {"order_id": "ORD-1", "company": "星辰科技", "tax_no": "9111", "amount": 99},
        {"ok": True, "result": {"invoice_id": "INV-1", "order_id": "ORD-1", "amount": 99}},
    )
    assert "• 发票号：INV-1" in invoice
    assert "• 抬头：星辰科技" in invoice
    assert "• 税号：9111" in invoice
    assert "• 金额：99" in invoice
    assert "• 下载提示：" in invoice

    no_invoice_no = _description(
        "create_invoice",
        {"order_id": "ORD-1", "company": "星辰科技", "tax_no": "9111", "amount": 12.5},
    )
    assert "发票号" not in no_invoice_no
    assert "• 金额：12.5" in no_invoice_no


def test_ticket_refund_and_note_fields():
    ticket = _description(
        "create_service_ticket",
        {"order_id": "ORD-1", "reason": "无法开机"},
        {"result": {"ticket_id": "TKT-1", "order_id": "ORD-1", "reason": "无法开机"}},
    )
    assert ticket.startswith("• 状态：已完成")
    assert "• 工单号：TKT-1" in ticket
    assert "• 问题描述：无法开机" in ticket
    assert "• 后续联系：" in ticket

    note = _description("update_order_note", {"order_id": "ORD-1", "note": "放驿站"})
    assert "• 备注内容：放驿站" in note

    refund = _description(
        "refund_apply",
        {"order_id": "ORD-1", "reason": "质量问题"},
        {"result": {"order_id": "ORD-1", "amount": 150.0, "reason": "质量问题"}},
    )
    assert "• 申请金额：150" in refund
    assert "• 原因：质量问题" in refund
    assert "• 进度提示：请留意到账通知" in refund


def test_unknown_tool_has_no_success_card():
    assert build_mutation_success_card("order_lookup", {"order_id": "ORD-1"}, None) is None


def test_address_card_matches_locked_bullets():
    text = _description(
        "update_address",
        {"order_id": "ORD-PAID-1", "address": "山东省青岛市市南区示例路 88 号"},
    )
    assert text == (
        "• 状态：已完成\n\n"
        "• 订单号：ORD-PAID-1\n\n"
        "• 新收货地址：山东省青岛市市南区示例路 88 号\n\n"
        "• 账户地址：已同步更新"
    )
