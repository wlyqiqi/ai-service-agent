"""Mutation Form Chatwoot projection, without AgentBot or graph resume.

The builders and ``send_mutation_form`` used to live in ``agent_bot.py``.
These tests call the projection module directly.
"""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from ai_service_agent.graph.workflows.copy import (
    update_address_empty_state_text,
    update_address_form_footnote_text,
    update_address_form_intro_text,
    update_address_form_subtitle_text,
    update_address_form_title_text,
)
from ai_service_agent.harness.mutation_form_projection import (
    mutation_form_items_from_form_data,
    mutation_form_title_from_form_data,
    send_mutation_form,
)

_HARNESS = Path(__file__).resolve().parents[1] / "src" / "ai_service_agent" / "harness"
_MODULE_PATH = _HARNESS / "mutation_form_projection.py"
_AGENT_BOT_PATH = _HARNESS / "agent_bot.py"


def test_projection_module_does_not_import_adapter_or_graph_resume():
    """Projection must not depend on AgentBot, form_gate, or graph resume."""
    tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    imported: list[str] = []
    called: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imported.append(module)
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                called.append(func.id)
            elif isinstance(func, ast.Attribute):
                called.append(func.attr)
    blob = " ".join(imported)
    assert "agent_bot" not in blob
    assert "form_gate" not in blob
    assert "langgraph" not in blob
    assert "Command" not in called
    assert "invoke" not in called
    assert "ainvoke" not in called


def test_agent_bot_no_longer_defines_form_projection():
    source = _AGENT_BOT_PATH.read_text(encoding="utf-8")
    for name in (
        "def mutation_form_title_from_form_data",
        "def mutation_form_items_from_form_data",
        "def _update_address_form_items",
        "def send_mutation_form",
        "def _form_errors_map",
    ):
        assert name not in source


def test_schema_fallback_builds_order_picker_fields_and_action_select():
    """No explicit ``fields``: schema properties become items; enum becomes select."""
    items = mutation_form_items_from_form_data(
        {
            "tool": "update_phone",
            "summary": "修改联系电话",
            "order_candidates": [
                {"order_id": "ORD-1", "product_summary": "鞋 x1", "status_display": "已支付"},
                {"order_id": "", "product_summary": "空订单号"},
                "not-a-candidate",
            ],
            "prefills": {"phone": "13800000000"},
            "response_schema": {
                "properties": {
                    "order_id": {"type": "string"},
                    "phone": {"type": "string"},
                    "action": {"enum": ["confirm", "cancel"]},
                    "request_id": {"type": "string"},
                    "current_address": {"type": "string"},
                },
            },
        }
    )
    assert [item["name"] for item in items] == ["order_id", "phone", "action"]
    assert items[0]["options"] == [{"label": "ORD-1", "value": "ORD-1"}]
    assert items[1]["label"] == "手机号"
    assert items[1]["default"] == "13800000000"
    assert items[1]["placeholder"] == "请输入手机号"
    assert items[2]["type"] == "select"
    assert items[2]["options"] == [
        {"label": "确认", "value": "confirm"},
        {"label": "取消", "value": "cancel"},
    ]


def test_form_level_error_lands_in_title_and_field_errors_paint_items():
    data = {
        "summary": "您即将进行「收货地址修改」操作",
        "order_candidates": [{"order_id": "ORD-PAID-1"}],
        "errors": {
            "_form": "请先核对订单",
            "address": "请填写有效地址",
            "unknown_field": "无法匹配的错误",
        },
        "response_schema": {
            "properties": {
                "order_id": {"type": "string"},
                "address": {"type": "string"},
                "action": {"enum": ["confirm", "cancel"]},
            },
        },
    }
    title = mutation_form_title_from_form_data(data)
    items = mutation_form_items_from_form_data(data)
    assert title.startswith("您即将进行「收货地址修改」操作\n")
    assert "请先核对订单" in title
    assert "无法匹配的错误" in title
    address = next(item for item in items if item["name"] == "address")
    assert address["placeholder"] == "请填写有效地址"
    order_picker = next(item for item in items if item["name"] == "order_id")
    assert "请先核对订单" not in order_picker["label"]


def test_update_address_open_form_locked_layout_without_preselect():
    data = {
        "tool": "update_address",
        "summary": "ignored-for-locked-title",
        "current_address": "123 Chengyuan Avenue",
        "order_candidates": [
            {"order_id": "ORD-PAID-1", "product_summary": "测试商品 x1"},
        ],
        "prefills": {"order_id": "ORD-PAID-1", "address": "旧预填"},
    }
    title = mutation_form_title_from_form_data(data)
    items = mutation_form_items_from_form_data(data)
    assert title == "\n".join(
        [update_address_form_title_text(), update_address_form_subtitle_text()]
    )
    # Z2 减噪：只留业务可提交字段；当前地址与脚注走引导句。
    assert [item["name"] for item in items] == ["order_id", "address"]
    assert "default" not in items[0]
    assert items[0]["options"] == [{"label": "ORD-PAID-1", "value": "ORD-PAID-1"}]
    assert items[1]["default"] == "旧预填"


def test_update_address_form_rejected_keeps_order_draft_and_paints_select():
    items = mutation_form_items_from_form_data(
        {
            "tool": "update_address",
            "current_address": "",
            "order_candidates": [{"order_id": "ORD-PAID-1"}],
            "prefills": {"order_id": "ORD-PAID-1", "address": "太短"},
            "errors": {"order_id": "该订单已不可改", "address": "请填写有效地址"},
        }
    )
    assert [item["name"] for item in items] == ["order_id", "address"]
    order_picker = next(item for item in items if item["name"] == "order_id")
    assert order_picker["default"] == "ORD-PAID-1"
    assert order_picker["label"] == "选择订单（该订单已不可改）"
    address = next(item for item in items if item["name"] == "address")
    assert address["default"] == "太短"
    assert address["placeholder"] == "请填写有效地址"


def test_form_rejected_title_keeps_the_abandon_footnote():
    """重推 form 不重发引导句，脚注（Implicit Cancel 提示）必须留在表单标题里。"""
    base = {
        "tool": "update_address",
        "current_address": "旧址",
        "order_candidates": [{"order_id": "ORD-1"}],
    }
    rejected = mutation_form_title_from_form_data(
        {**base, "errors": {"address": "请填写有效地址"}}
    )
    assert update_address_form_footnote_text() in rejected
    assert "请填写有效地址" in rejected
    # 首次打开：脚注在引导句里，标题不重复。
    assert update_address_form_footnote_text() not in mutation_form_title_from_form_data(base)


@pytest.mark.asyncio
async def test_send_update_address_open_emits_intro_then_form():
    chatwoot = AsyncMock()
    await send_mutation_form(
        chatwoot,
        "c1",
        {
            "tool": "update_address",
            "current_address": "上海",
            "order_candidates": [{"order_id": "ORD-PAID-1"}],
        },
    )
    # 引导句承载当前地址与脚注（已移出 form items）。
    chatwoot.send_bot_message.assert_awaited_once_with(
        "c1", update_address_form_intro_text("上海")
    )
    assert "当前收货地址：上海" in update_address_form_intro_text("上海")
    chatwoot.send_interactive_message.assert_awaited_once()
    args = chatwoot.send_interactive_message.await_args.args
    assert args[0] == "c1"
    assert args[2] == "form"
    assert [item["name"] for item in args[3]["items"]] == ["order_id", "address"]


@pytest.mark.asyncio
async def test_send_update_address_empty_candidates_is_text_only():
    chatwoot = AsyncMock()
    await send_mutation_form(
        chatwoot,
        "c1",
        {"tool": "update_address", "order_candidates": [], "current_address": ""},
    )
    chatwoot.send_bot_message.assert_awaited_once_with(
        "c1", update_address_empty_state_text()
    )
    chatwoot.send_interactive_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_form_rejected_with_empty_candidates_still_sends_form():
    """Empty state is first-open only. form_rejected must re-push the form."""
    chatwoot = AsyncMock()
    await send_mutation_form(
        chatwoot,
        "c1",
        {
            "tool": "update_address",
            "order_candidates": [],
            "current_address": "旧址",
            "errors": {"address": "请填写有效地址"},
            "prefills": {"address": "x"},
        },
    )
    chatwoot.send_bot_message.assert_not_awaited()
    chatwoot.send_interactive_message.assert_awaited_once()
    title = chatwoot.send_interactive_message.await_args.args[1]
    items = chatwoot.send_interactive_message.await_args.args[3]["items"]
    assert update_address_empty_state_text() not in title
    assert update_address_form_intro_text() not in title
    assert "请填写有效地址" in title
    address = next(item for item in items if item["name"] == "address")
    assert address["placeholder"] == "请填写有效地址"


@pytest.mark.asyncio
async def test_send_non_address_form_has_no_intro_bubble():
    chatwoot = AsyncMock()
    await send_mutation_form(
        chatwoot,
        "c9",
        {
            "tool": "create_invoice",
            "summary": "开具发票",
            "order_candidates": [{"order_id": "ORD-1"}],
            "fields": [{"name": "company", "label": "发票抬头", "type": "text"}],
        },
    )
    chatwoot.send_bot_message.assert_not_awaited()
    chatwoot.send_interactive_message.assert_awaited_once()
    args = chatwoot.send_interactive_message.await_args.args
    assert args[1] == "开具发票"
    assert [item["name"] for item in args[3]["items"]] == ["order_id", "company", "action"]
