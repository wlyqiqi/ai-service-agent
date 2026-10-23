"""Mutation Select seam (CONTEXT.md / ADR 0004 follow-on): dedicated node, prompts, graph name."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from ai_service_agent.graph.builder import build_graph
from ai_service_agent.prompts import (
    MUTATION_SELECT_BINDTOOLS_TEMPLATE,
    MUTATION_SELECT_SYSTEM_TEMPLATE,
)
from ai_service_agent.testing import make_fake_model, select_json_message
from ai_service_agent.tools import DEFAULT_TOOL_REGISTRY
from ai_service_agent.tools.registry import ToolMeta, ToolRegistry
from ai_service_agent.tools.write import UpdatePhoneArgs


def _select_result(coro):
    return asyncio.run(coro)


def test_mutation_select_prompts_are_mutation_only():
    """Dedicated prompts: write-ops clarify; no HighRisk refund/cancel / tier_clarify framing."""
    assert "写操作选择器" in MUTATION_SELECT_BINDTOOLS_TEMPLATE
    assert "不要发起 tool_call" in MUTATION_SELECT_BINDTOOLS_TEMPLATE
    assert "表单" in MUTATION_SELECT_BINDTOOLS_TEMPLATE
    assert "refund_apply" not in MUTATION_SELECT_BINDTOOLS_TEMPLATE
    assert "cancel_order" not in MUTATION_SELECT_BINDTOOLS_TEMPLATE
    assert "tier_clarify" not in MUTATION_SELECT_BINDTOOLS_TEMPLATE
    assert "tier_mismatch" not in MUTATION_SELECT_BINDTOOLS_TEMPLATE

    assert '"tool": null' in MUTATION_SELECT_SYSTEM_TEMPLATE or '"tool":null' in MUTATION_SELECT_SYSTEM_TEMPLATE
    assert "content" in MUTATION_SELECT_SYSTEM_TEMPLATE
    assert "refund_apply" not in MUTATION_SELECT_SYSTEM_TEMPLATE
    assert "cancel_order" not in MUTATION_SELECT_SYSTEM_TEMPLATE


def test_mutation_select_templates_share_write_and_business_boundaries():
    """Fake / Real 规则同语义：写操作边界、追问约束、列表外退款/取消；输出通道各自保留。"""
    for template in (MUTATION_SELECT_SYSTEM_TEMPLATE, MUTATION_SELECT_BINDTOOLS_TEMPLATE):
        assert "写操作边界" in template
        assert "已修改" in template
        assert "已提交" in template
        assert "已办妥" in template
        assert "request_id" in template
        assert "严禁编造" in template
        assert "只列举当前" in template
        assert "退款" in template
        assert "取消" in template
        assert "改联系方式" in template
        assert "开票" in template
        assert "加备注" in template
        assert "refund_apply" not in template
        assert "cancel_order" not in template
    assert "只输出 JSON" in MUTATION_SELECT_SYSTEM_TEMPLATE
    assert "只输出 JSON" not in MUTATION_SELECT_BINDTOOLS_TEMPLATE
    assert '{"tool":' not in MUTATION_SELECT_BINDTOOLS_TEMPLATE
    assert "示例仅说明意图与参数" in MUTATION_SELECT_BINDTOOLS_TEMPLATE
    assert "tool_call 或纯文本" in MUTATION_SELECT_BINDTOOLS_TEMPLATE
    rendered = MUTATION_SELECT_SYSTEM_TEMPLATE.format(tool_list="- update_phone: 修改联系电话", examples="（示例）")
    assert '{"tool": null' in rendered or '{"tool":null' in rendered


def test_make_mutation_select_node_has_no_tier_parameter():
    from ai_service_agent.graph.workflows.mutation_select import make_mutation_select_node
    import inspect

    params = inspect.signature(make_mutation_select_node).parameters
    assert "tier" not in params


def test_mutation_select_fake_null_tool_clarifies_inline(monkeypatch):
    from ai_service_agent.graph.workflows.mutation_select import make_mutation_select_node
    from ai_service_agent.testing import ToolCallingFakeModel

    events = []
    monkeypatch.setattr(
        "ai_service_agent.graph.workflows.mutation_select.get_stream_writer",
        lambda: events.append,
    )
    model = ToolCallingFakeModel(
        responses=[
            AIMessage(content='{"tool": null, "content": "您是要改地址还是开票？"}'),
        ]
    )
    node = make_mutation_select_node(
        model,
        DEFAULT_TOOL_REGISTRY.for_risk_level("mutation"),
        DEFAULT_TOOL_REGISTRY,
    )
    output = _select_result(
        node({"messages": [HumanMessage(content="帮我弄一下订单")], "context": {}})
    )
    assert output["execution_status"] == "clarifying"
    assert output["messages"][-1].content == "您是要改地址还是开票？"
    assert events == [
        {"type": "clarification_required", "data": {"missing": [], "reason": "operation_unclear"}}
    ]
    assert "pending_tool" not in output["context"]


def test_mutation_select_bindtools_uses_dedicated_template():
    from ai_service_agent.graph.workflows.mutation_select import make_mutation_select_node

    bound_model = AsyncMock()
    bound_model.ainvoke.return_value = AIMessage(
        content="",
        tool_calls=[{"name": "update_phone", "args": {"phone": "13800138000"}, "id": "call_1"}],
    )
    model = MagicMock()
    model.bind_tools.return_value = bound_model
    tool = MagicMock()
    tool.name = "update_phone"
    registry = ToolRegistry()
    registry.register(
        ToolMeta(
            name="update_phone",
            description="修改联系电话",
            min_risk_level="mutation",
            schema=UpdatePhoneArgs,
        ),
        tool,
    )
    node = make_mutation_select_node(model, [tool], registry)
    output = _select_result(
        node(
            {
                "messages": [HumanMessage(content="把手机号改成13800138000")],
                "user_id": "u1",
                "context": {},
            }
        )
    )
    assert model.bind_tools.call_args == call([tool], tool_choice="auto")
    system = bound_model.ainvoke.await_args.args[0][0].content
    assert "写操作选择器" in system
    assert "表单" in system
    assert output["context"]["pending_tool"]["tool"] == "update_phone"


def test_mutation_graph_node_is_mutation_select_llm():
    from ai_service_agent.tools.kb import kb_search

    graph = build_graph(make_fake_model(), tools=[kb_search])
    mutation_graph = graph.nodes["mutation_graph"].bound
    assert "mutation_select_llm" in mutation_graph.nodes
    assert "select_llm" not in mutation_graph.nodes
    high_risk_graph = graph.nodes["high_risk_graph"].bound
    assert "select_llm" in high_risk_graph.nodes
    assert "mutation_select_llm" not in high_risk_graph.nodes


def test_mutation_select_uses_mutation_system_template_for_fake():
    from ai_service_agent.graph.workflows.mutation_select import make_mutation_select_node
    from ai_service_agent.testing import ToolCallingFakeModel

    class Recording(ToolCallingFakeModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            object.__setattr__(
                self,
                "seen_system",
                next(
                    (m.content for m in messages if getattr(m, "type", "") == "system"),
                    "",
                ),
            )
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    model = Recording(responses=[select_json_message("update_phone", {"phone": "13800138000"})])
    node = make_mutation_select_node(
        model,
        DEFAULT_TOOL_REGISTRY.for_risk_level("mutation"),
        DEFAULT_TOOL_REGISTRY,
    )
    _select_result(
        node({"messages": [HumanMessage(content="换手机号 13800138000")], "context": {}})
    )
    assert "写操作选择器" in model.seen_system or "tool\": null" in model.seen_system.replace(" ", "")
    assert "refund_apply" not in model.seen_system


def _example_section(system: str) -> str:
    marker = "## 示例"
    index = system.rfind(marker)
    assert index != -1
    return system[index:]


def _seen_fake_system(allowed_tools=None) -> str:
    from ai_service_agent.graph.workflows.mutation_select import make_mutation_select_node
    from ai_service_agent.testing import ToolCallingFakeModel

    class Recording(ToolCallingFakeModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            object.__setattr__(
                self,
                "seen_system",
                next(
                    (m.content for m in messages if getattr(m, "type", "") == "system"),
                    "",
                ),
            )
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    model = Recording(responses=[select_json_message("update_phone", {"phone": "13800138000"})])
    node = make_mutation_select_node(
        model,
        DEFAULT_TOOL_REGISTRY.for_risk_level("mutation"),
        DEFAULT_TOOL_REGISTRY,
    )
    state = {"messages": [HumanMessage(content="换手机号 13800138000")], "context": {}}
    if allowed_tools is not None:
        state["allowed_tools"] = allowed_tools
    _select_result(node(state))
    return model.seen_system


def test_fake_invoice_example_keeps_only_fields_in_the_user_sentence():
    """Schema 字段是 company / tax_no；用户句没有金额，示例不编造 amount。"""
    examples = _example_section(_seen_fake_system())
    assert "订单 ORD-10086 开票，抬头星辰科技有限公司，税号 91110108MA01AB12CD" in examples
    assert '"tool": "create_invoice"' in examples
    assert '"order_id": "ORD-10086"' in examples
    assert '"company": "星辰科技有限公司"' in examples
    assert '"tax_no": "91110108MA01AB12CD"' in examples
    assert "amount" not in examples
    assert "199" not in examples
    assert "title" not in examples
    assert "tax_id" not in examples
    assert "request_id" not in examples


def test_fake_missing_arg_examples_cover_profile_and_order_tools():
    examples = _example_section(_seen_fake_system())
    assert "我想改个收货地址（参数不完整）" in examples
    assert '"tool": "update_address", "args": {}' in examples
    assert "给订单加个备注（参数不完整）" in examples
    assert '"tool": "update_order_note", "args": {}' in examples


def test_fake_clarify_examples_list_only_current_capabilities():
    examples = _example_section(_seen_fake_system())
    assert "帮我改一下订单" in examples
    assert "我要退款" in examples
    assert '"tool": null' in examples
    assert "改收货地址" in examples
    assert "改联系电话" in examples
    assert "加订单备注" in examples
    assert "开发票" in examples
    assert "申请售后" in examples
    assert "帮我改一下联系方式" in examples
    assert "既要开票又要加备注" in examples
    assert "refund_apply" not in examples
    assert "cancel_order" not in examples


def test_profile_allowlist_examples_omit_order_capabilities():
    examples = _example_section(_seen_fake_system(["update_address", "update_phone"]))
    assert "我想改个收货地址（参数不完整）" in examples
    assert '"tool": "update_address"' in examples
    assert "帮我改一下联系方式" in examples
    assert "改收货地址" in examples
    assert "改联系电话" in examples
    assert "update_order_note" not in examples
    assert "create_invoice" not in examples
    assert "create_service_ticket" not in examples
    assert "开发票" not in examples
    assert "加订单备注" not in examples
    assert "申请售后" not in examples
    assert "既要开票又要加备注" not in examples
    assert "refund_apply" not in examples


def test_order_allowlist_examples_include_sparse_order_args_only():
    examples = _example_section(
        _seen_fake_system(["update_order_note", "create_invoice", "create_service_ticket"])
    )
    assert "给订单加个备注（参数不完整）" in examples
    assert '"tool": "update_order_note", "args": {}' in examples
    assert "既要开票又要加备注" in examples
    assert "update_address" not in examples
    assert "update_phone" not in examples
    assert "改收货地址" not in examples
    assert "改联系电话" not in examples
    assert "帮我改一下联系方式" not in examples
    assert "amount" not in examples
    assert "199" not in examples


def test_bind_tools_examples_describe_tool_calls_without_json_payloads():
    from ai_service_agent.graph.workflows.mutation_select import make_mutation_select_node

    bound_model = AsyncMock()
    bound_model.ainvoke.return_value = AIMessage(
        content="",
        tool_calls=[{"name": "update_phone", "args": {"phone": "13800138000"}, "id": "call_1"}],
    )
    model = MagicMock()
    model.bind_tools.return_value = bound_model
    node = make_mutation_select_node(
        model,
        DEFAULT_TOOL_REGISTRY.for_risk_level("mutation"),
        DEFAULT_TOOL_REGISTRY,
    )
    _select_result(
        node(
            {
                "messages": [HumanMessage(content="换手机号 13800138000")],
                "user_id": "u1",
                "context": {},
            }
        )
    )
    system = bound_model.ainvoke.await_args.args[0][0].content
    examples = _example_section(system)
    assert "示例仅说明意图与参数" in system
    assert "不要输出 JSON" in system
    assert '{"tool":' not in system
    assert "发起 tool_call：update_address(order_id=" in examples
    assert "发起 tool_call：create_invoice(" in examples
    assert 'company="星辰科技有限公司"' in examples
    assert 'tax_no="91110108MA01AB12CD"' in examples
    assert "amount" not in examples
    assert "199" not in examples
    assert "我想改个收货地址（参数不完整）" in examples
    assert "发起 tool_call：update_address()" in examples
    assert "给订单加个备注（参数不完整）" in examples
    assert "发起 tool_call：update_order_note()" in examples
    assert "不发起 tool_call，纯文本追问：" in examples
    assert "我要退款" in examples
    assert "退款不在当前能办的范围内" in examples
    assert "refund_apply" not in system
    assert "cancel_order" not in system
    assert "request_id" not in examples
