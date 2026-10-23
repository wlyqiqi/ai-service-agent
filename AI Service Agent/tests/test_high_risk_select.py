"""HighRisk Select seam (CONTEXT.md / ADR 0005): dedicated node, prompts, graph name."""

import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock, call

from langchain_core.messages import AIMessage, HumanMessage

from ai_service_agent.graph.builder import build_graph
from ai_service_agent.graph.workflows.high_risk_select import make_high_risk_select_node
from ai_service_agent.prompts import (
    HIGH_RISK_SELECT_BINDTOOLS_TEMPLATE,
    HIGH_RISK_SELECT_SYSTEM_TEMPLATE,
)
from ai_service_agent.testing import ToolCallingFakeModel, make_fake_model, select_json_message
from ai_service_agent.tools import make_tool_registry
from ai_service_agent.tools.registry import ToolMeta, ToolRegistry
from ai_service_agent.tools.write import CancelOrderArgs


def _select_result(coro):
    return asyncio.run(coro)


def test_high_risk_select_prompts_are_high_risk_only():
    """Dedicated prompts: cancel/refund only; no Mutation tools / inline Operation Clarify."""
    for template in (HIGH_RISK_SELECT_SYSTEM_TEMPLATE, HIGH_RISK_SELECT_BINDTOOLS_TEMPLATE):
        assert "高风险操作选择器" in template
        assert "cancel_order" in template
        assert "refund_apply" in template
        assert "create_service_ticket" not in template
        assert "update_address" not in template
        assert "update_phone" not in template
        assert "create_invoice" not in template
        assert "operation_unclear" not in template
        assert 'tool": null' not in template and 'tool":null' not in template

    assert "只输出 JSON" in HIGH_RISK_SELECT_SYSTEM_TEMPLATE
    assert "只输出 JSON" not in HIGH_RISK_SELECT_BINDTOOLS_TEMPLATE
    assert "不要发起 tool_call" in HIGH_RISK_SELECT_BINDTOOLS_TEMPLATE
    assert "不要用纯文本追问" in HIGH_RISK_SELECT_BINDTOOLS_TEMPLATE
    assert "示例仅说明意图与参数" in HIGH_RISK_SELECT_BINDTOOLS_TEMPLATE
    assert '{"tool":' not in HIGH_RISK_SELECT_BINDTOOLS_TEMPLATE


def test_make_high_risk_select_node_has_no_tier_parameter():
    params = inspect.signature(make_high_risk_select_node).parameters
    assert "tier" not in params


def test_high_risk_graph_node_is_high_risk_select_llm():
    from ai_service_agent.tools.kb import kb_search

    graph = build_graph(make_fake_model(), tools=[kb_search])
    high_risk_graph = graph.nodes["high_risk_graph"].bound
    assert "high_risk_select_llm" in high_risk_graph.nodes
    assert "select_llm" not in high_risk_graph.nodes
    mutation_graph = graph.nodes["mutation_graph"].bound
    assert "mutation_select_llm" in mutation_graph.nodes
    assert "high_risk_select_llm" not in mutation_graph.nodes


def test_high_risk_select_fake_empty_tool_writes_tier_clarify():
    registry = make_tool_registry(MagicMock())
    model = ToolCallingFakeModel(
        responses=[AIMessage(content='{"tool": "", "args": {}}')],
    )
    node = make_high_risk_select_node(
        model,
        registry.for_risk_level("high_risk"),
        registry,
    )
    output = _select_result(
        node({"messages": [HumanMessage(content="帮我处理一下")], "context": {}})
    )
    assert output["context"].get("tier_clarify") is True
    assert "pending_tool" not in output["context"]
    assert "messages" not in output


def test_high_risk_select_bindtools_uses_dedicated_template():
    bound_model = AsyncMock()
    bound_model.ainvoke.return_value = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "cancel_order",
                "args": {"order_id": "ORD-10086", "reason": "不想要了"},
                "id": "call_1",
            }
        ],
    )
    model = MagicMock()
    model.bind_tools.return_value = bound_model
    tool = MagicMock()
    tool.name = "cancel_order"
    registry = ToolRegistry()
    registry.register(
        ToolMeta(
            name="cancel_order",
            description="取消订单",
            min_risk_level="high_risk",
            schema=CancelOrderArgs,
        ),
        tool,
    )
    node = make_high_risk_select_node(model, [tool], registry)
    output = _select_result(
        node(
            {
                "messages": [
                    HumanMessage(content="订单 ORD-10086 不要了，帮我取消")
                ],
                "user_id": "u1",
                "context": {},
            }
        )
    )
    assert model.bind_tools.call_args == call([tool], tool_choice="auto")
    system = bound_model.ainvoke.await_args.args[0][0].content
    assert "高风险操作选择器" in system
    assert "cancel_order" in system
    assert "示例仅说明意图与参数" in system
    assert '{"tool":' not in system
    assert "发起 tool_call" in system
    assert output["context"]["pending_tool"]["tool"] == "cancel_order"


def test_high_risk_select_uses_system_template_for_fake():
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

    registry = make_tool_registry(MagicMock())
    model = Recording(
        responses=[
            select_json_message(
                "cancel_order",
                {"order_id": "ORD-10086", "reason": "不想要了"},
            )
        ]
    )
    node = make_high_risk_select_node(
        model,
        registry.for_risk_level("high_risk"),
        registry,
    )
    _select_result(
        node(
            {
                "messages": [
                    HumanMessage(content="订单 ORD-10086 不要了，帮我取消")
                ],
                "user_id": "u1",
                "context": {},
            }
        )
    )
    assert "高风险操作选择器" in model.seen_system
    assert "只输出 JSON" in model.seen_system
