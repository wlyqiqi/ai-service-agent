"""High-confidence Mutation Select phrase table. No model involved."""

import pytest

from ai_service_agent.graph.workflows.mutation_force_form import (
    partial_args_from_utterance,
    unique_mutation_tool,
)

_PROFILE = {"update_address", "update_phone"}
_ORDER = {"update_order_note", "create_invoice", "create_service_ticket"}
_ALL = _PROFILE | _ORDER


@pytest.mark.parametrize(
    "text",
    [
        "我要改收货地址",
        "我要改个地址",
        "改地址",
        "修改地址",
        "换个地址",
        "帮我改一下收货地址",
        "更换收货地址",
        "收货地址改成北京市朝阳区望京SOHO",
    ],
)
def test_unique_mutation_tool_maps_address_phrases(text):
    assert unique_mutation_tool(text, _PROFILE) == "update_address"
    assert unique_mutation_tool(text, _ALL) == "update_address"


@pytest.mark.parametrize(
    ("text", "tool"),
    [
        ("改电话", "update_phone"),
        ("换手机号", "update_phone"),
        ("修改手机号", "update_phone"),
        ("我要换绑手机号", "update_phone"),
        ("帮我换个手机号", "update_phone"),
        ("改备注", "update_order_note"),
        ("加备注", "update_order_note"),
        ("订单备注", "update_order_note"),
        ("给订单加个备注", "update_order_note"),
        ("开发票", "create_invoice"),
        ("开票", "create_invoice"),
        ("要发票", "create_invoice"),
        ("给我开一张发票", "create_invoice"),
        # 「开」+ 量词/填充词的口语形态（2026-10-27 真实会话回归）：
        # 用户说「开个发票吧」，模型只回文本追问槽位，force-form 未命中 -> 无表单。
        ("开个发票", "create_invoice"),
        ("开个发票吧", "create_invoice"),
        ("开个票", "create_invoice"),
        ("开张票", "create_invoice"),
        ("开一张票", "create_invoice"),
        ("开一下发票", "create_invoice"),
        ("给我开个发票", "create_invoice"),
        ("申请售后", "create_service_ticket"),
        ("开售后工单", "create_service_ticket"),
        ("我要申请售后", "create_service_ticket"),
        ("开个售后", "create_service_ticket"),
        ("帮我开个售后", "create_service_ticket"),
        ("提交个售后", "create_service_ticket"),
        ("我要开个售后工单", "create_service_ticket"),
    ],
)
def test_unique_mutation_tool_maps_other_write_phrases(text, tool):
    assert unique_mutation_tool(text, _ALL) == tool


@pytest.mark.parametrize(
    "text",
    [
        "",
        "帮我改一下订单",
        "帮我弄一下订单",
        "我要退款",
        "改联系方式",
        "不要改收货地址",
        "不用改地址",
        "不要开个发票",
        "别开个售后",
    ],
)
def test_unique_mutation_tool_does_not_force_vague_or_negated(text):
    assert unique_mutation_tool(text, _ALL) is None


def test_unique_mutation_tool_skips_tool_outside_allowlist():
    assert unique_mutation_tool("我要改收货地址", {"update_phone"}) is None
    assert unique_mutation_tool("改联系方式", {"update_address"}) is None


def test_unique_mutation_tool_requires_a_single_hit():
    assert unique_mutation_tool("我要改收货地址还要换个手机号", _PROFILE) is None
    assert (
        unique_mutation_tool("订单 ORD-10086 既要开票又要加备注", _ORDER) is None
    )


def test_contact_ambiguity_blocks_only_when_both_profile_tools_are_allowed():
    assert unique_mutation_tool("改联系方式", _PROFILE) is None
    assert unique_mutation_tool("帮我改一下联系方式", _PROFILE) is None
    # A specific noun still wins: this is an address change, not a bare contact phrase.
    assert (
        unique_mutation_tool("改联系方式，收货地址改成北京市朝阳区望京SOHO", _PROFILE)
        == "update_address"
    )


def test_partial_args_extracts_only_fields_present_in_the_utterance():
    args = partial_args_from_utterance(
        "update_address",
        "订单 ORD-10086 收货地址改成北京市朝阳区望京SOHO",
    )
    assert args == {"order_id": "ORD-10086", "address": "北京市朝阳区望京SOHO"}

    assert partial_args_from_utterance("update_address", "我要改收货地址") == {}
    assert partial_args_from_utterance("update_address", "我要改个地址") == {}

    phone = partial_args_from_utterance(
        "update_phone", "帮我换个手机号，新号 13800138000"
    )
    assert phone == {"phone": "13800138000"}
    assert partial_args_from_utterance("update_phone", "我要换绑手机号") == {}

    note = partial_args_from_utterance(
        "update_order_note", "给订单 ORD-10086 加个备注：放驿站就行"
    )
    assert note == {"order_id": "ORD-10086", "note": "放驿站就行"}

    invoice = partial_args_from_utterance(
        "create_invoice",
        "订单 ORD-10086 开票，抬头星辰科技有限公司，税号 91110108MA01AB12CD",
    )
    assert invoice == {
        "order_id": "ORD-10086",
        "company": "星辰科技有限公司",
        "tax_no": "91110108MA01AB12CD",
    }
    assert "amount" not in invoice

    ticket = partial_args_from_utterance(
        "create_service_ticket", "订单 ORD-10086 申请售后，屏幕有裂痕"
    )
    assert ticket == {"order_id": "ORD-10086", "reason": "屏幕有裂痕"}
