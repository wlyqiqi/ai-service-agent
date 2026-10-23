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
