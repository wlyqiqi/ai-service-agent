"""Hybrid Tool Resolution / Operation Clarify (entry layer, issue #21)."""

from ai_service_agent.graph.workflows.copy import operation_clarify_text
from ai_service_agent.graph.workflows.operation_clarify import (
    resolve_mutation_tool,
    needs_operation_clarify,
    make_hybrid_tool_resolve_node,
    make_operation_clarify_node,
)
from langchain_core.messages import HumanMessage


def test_resolve_mutation_tool_high_confidence_address():
    assert resolve_mutation_tool("改收货地址") == "update_address"
    assert resolve_mutation_tool(
        "改收货地址",
        allowed_tools=["update_address", "update_phone"],
    ) == "update_address"


def test_resolve_mutation_tool_vague_is_none():
    assert resolve_mutation_tool("我要改一下订单") is None
    assert resolve_mutation_tool(
        "我要改一下订单",
        allowed_tools=["update_order_note", "create_invoice", "create_service_ticket"],
    ) is None
    assert needs_operation_clarify("我要改一下订单") is True
    assert needs_operation_clarify("改一下收货地址") is False
    assert needs_operation_clarify("把订单状态改成已发货") is False


def test_resolve_address_variants():
    assert resolve_mutation_tool("我要改个地址") == "update_address"
    assert resolve_mutation_tool("我没登录，改一下地址") == "update_address"


def test_resolve_respects_policy_allowlist():
    # Address keywords must not resolve when Policy only allows order_mutation tools.
    assert (
        resolve_mutation_tool(
            "改收货地址",
            allowed_tools=["update_order_note", "create_invoice", "create_service_ticket"],
        )
        is None
    )


def test_hybrid_tool_resolve_marks_operation_clarify_for_vague_mutation():
    node = make_hybrid_tool_resolve_node()
    out = node(
        {
            "target_graph": "mutation_graph",
            "guard_status": "pass",
            "allowed_tools": ["update_order_note", "create_invoice", "create_service_ticket"],
            "messages": [HumanMessage(content="我要改一下订单")],
            "context": {},
        }
    )
    assert out["context"].get("operation_clarify") is True


def test_hybrid_tool_resolve_narrows_allowed_tools_when_unique():
    node = make_hybrid_tool_resolve_node()
    out = node(
        {
            "target_graph": "mutation_graph",
            "guard_status": "pass",
            "allowed_tools": ["update_address", "update_phone"],
            "messages": [HumanMessage(content="改收货地址")],
            "context": {},
        }
    )
    assert out["allowed_tools"] == ["update_address"]
    assert "operation_clarify" not in out.get("context", {})


def test_operation_clarify_node_emits_chat_not_form(monkeypatch):
    events = []
    monkeypatch.setattr(
        "ai_service_agent.graph.workflows.operation_clarify.get_stream_writer",
        lambda: events.append,
    )
    out = make_operation_clarify_node()({"context": {"operation_clarify": True}})
    assert out["execution_status"] == "clarifying"
    assert out["messages"][-1].content == operation_clarify_text()
    assert events == [
        {"type": "clarification_required", "data": {"missing": [], "reason": "operation_unclear"}}
    ]
    assert "operation_clarify" not in out["context"]
