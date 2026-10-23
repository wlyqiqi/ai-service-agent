"""select 节点字段契约（2026-09-15 真机冒烟修复）：系统提示必须携带 schema 字段清单。"""

import asyncio
import inspect
import time
from typing import Callable
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.types import TimeoutPolicy
from langchain_core.messages import AIMessage, HumanMessage

from ai_service_agent.config import get_settings
from ai_service_agent.graph.fault_tolerance import RETRY_LLM, make_subgraph_error_handler, make_timeout_policy
from ai_service_agent.graph.state import CustomerServiceState
from ai_service_agent.graph.workflows.mutation_select import make_mutation_select_node
from ai_service_agent.graph.workflows.select import make_select_node
from ai_service_agent.testing import ToolCallingFakeModel, select_json_message
from ai_service_agent.tools import DEFAULT_TOOL_REGISTRY
from ai_service_agent.tools.registry import ToolMeta, ToolRegistry
from ai_service_agent.tools.write import UpdatePhoneArgs


class RecordingSelectModel(ToolCallingFakeModel):
    """记录最后一次系统提示（Mutation Select 的 json_mode 契约提示）。"""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        object.__setattr__(self, "seen_system", next(
            (m.content for m in messages if getattr(m, "type", "") == "system"), ""
        ))
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


class SlowSelectFakeModel(ToolCallingFakeModel):
    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        from langchain_core.outputs import ChatGeneration, ChatResult

        await asyncio.sleep(0.2)
        message = select_json_message("update_address", {"address": "朝阳区"})
        return ChatResult(generations=[ChatGeneration(message=message)])


def _select_node():
    model = RecordingSelectModel(responses=[
        select_json_message("update_address", {"address": "朝阳区"}),
    ])
    node = make_mutation_select_node(
        model, DEFAULT_TOOL_REGISTRY.for_risk_level("mutation"), DEFAULT_TOOL_REGISTRY
    )
    return model, node


def _select_result(coro):
    return asyncio.run(coro)


def test_select_system_template_carries_schema_field_contract():
    model, node = _select_node()
    _select_result(node({"messages": [HumanMessage(content="改一下收货地址")], "context": {}}))
    assert "参数：" in model.seen_system
    assert "address" in model.seen_system
    assert "phone" in model.seen_system


@pytest.mark.asyncio
async def test_select_uses_previous_turn_for_clarification():
    captured = {}

    class CaptureToolModel:
        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages, config=None):
            captured["messages"] = messages
            return AIMessage(
                content="",
                tool_calls=[{"name": "update_address", "args": {}, "id": "call-1", "type": "tool_call"}],
            )

    node = make_mutation_select_node(CaptureToolModel(), tools=[], registry=ToolRegistry())
    state = {
        "messages": [
            HumanMessage(content="我要修改地址"),
            AIMessage(content="请提供订单号和新地址"),
            HumanMessage(content="ORD-2026-10099"),
        ],
        "allowed_tools": ["update_address"],
        "user_id": "u1",
        "context": {},
    }

    await node(state)
    human_texts = [
        message.content
        for message in captured["messages"]
        if getattr(message, "type", "") == "human"
    ]
    assert human_texts == ["我要修改地址", "ORD-2026-10099"]


def test_select_system_template_forbids_extra_fields():
    model, node = _select_node()
    _select_result(node({"messages": [HumanMessage(content="改一下收货地址")], "context": {}}))
    assert "不要输出 schema 之外的字段" in model.seen_system


def test_select_system_template_has_example():
    model, node = _select_node()
    _select_result(node({"messages": [HumanMessage(content="改一下收货地址")], "context": {}}))
    assert "示例：" in model.seen_system
    assert "update_address" in model.seen_system


def test_select_system_template_tier_strict_no_read_tools():
    """B2b：写图 select 系统提示只列该档写工具，只读工具不进入允许集。"""
    model, node = _select_node()
    _select_result(node({"messages": [HumanMessage(content="改一下收货地址")], "context": {}}))
    assert "update_address" in model.seen_system
    assert "kb_search" not in model.seen_system
    assert "order_lookup" not in model.seen_system


def test_select_system_template_carries_new_tools_and_schema():
    model, node = _select_node()
    _select_result(node({"messages": [HumanMessage(content="改一下收货地址")], "context": {}}))
    assert "update_address" in model.seen_system
    assert "create_invoice" in model.seen_system
    assert "address" in model.seen_system
    assert "company" in model.seen_system
    assert "account_update" not in model.seen_system


def test_select_system_template_forbids_extra_fields_and_fabrication():
    model, node = _select_node()
    _select_result(node({"messages": [HumanMessage(content="改一下收货地址")], "context": {}}))
    assert "不要输出 schema 之外的字段" in model.seen_system
    assert "严禁编造" in model.seen_system


def test_select_system_template_examples_match_tier():
    model, node = _select_node()
    _select_result(node({"messages": [HumanMessage(content="改一下收货地址")], "context": {}}))
    assert '"tool": "update_address"' in model.seen_system
    assert '"tool": "refund_apply"' not in model.seen_system   # 跨档示例不注入
    assert "参数不完整" in model.seen_system                     # 缺参示例


def test_select_system_template_service_ticket_boundary():
    """Mutation Select：售后工单边界在本档提示词内；不含 HighRisk 退款工具。"""
    model, node = _select_node()
    _select_result(node({"messages": [HumanMessage(content="改一下收货地址")], "context": {}}))
    assert "售后工单" in model.seen_system
    assert "退货" in model.seen_system
    assert "refund_apply" not in model.seen_system
    assert "cancel_order" not in model.seen_system


def test_select_json_tool_list_uses_short_meta_description():
    """JSON Select 的允许工具列表只用 ToolMeta 短描述 + schema 参数契约。"""
    model, node = _select_node()
    _select_result(node({"messages": [HumanMessage(content="改一下收货地址")], "context": {}}))
    assert "- update_address: 修改本人订单关联的收货地址。" in model.seen_system
    assert "- create_service_ticket: 为本人订单创建售后工单。" in model.seen_system
    assert "参数：order_id(必填)，address(必填)" in model.seen_system
    assert "参数：order_id、address（必填）。" not in model.seen_system
    assert "需退款走 refund_apply" not in model.seen_system


def test_select_mutation_bindtools_prompt_allows_inline_clarify():
    """ADR 0004: Mutation bind_tools prompt asks for tool_call or natural-language clarify."""
    bound_model = AsyncMock()
    bound_model.ainvoke.return_value = AIMessage(
        content="",
        tool_calls=[{"name": "update_phone", "args": {"phone": "13800138000"}, "id": "call_1"}],
    )
    model = MagicMock()
    model.bind_tools.return_value = bound_model
    node, tool = _real_model_node(model)
    _select_result(node({
        "messages": [HumanMessage(content="把手机号改成13800138000")],
        "user_id": "u1",
        "context": {},
    }))

    assert model.bind_tools.call_args == call([tool], tool_choice="auto")
    system = bound_model.ainvoke.await_args.args[0][0].content
    assert "写操作选择器" in system
    assert "不要发起 tool_call" in system
    assert "追问" in system
    assert "create_service_ticket" in system
    assert "最接近" not in system
    assert "只输出 JSON" not in system


def test_select_real_model_uses_bind_tools():
    bound_model = AsyncMock()
    bound_model.ainvoke.return_value = AIMessage(
        content="",
        tool_calls=[{"name": "update_phone", "args": {"phone": "13800138000"}, "id": "call_1"}],
    )
    model = MagicMock()
    model.bind_tools.return_value = bound_model

    node, tool = _real_model_node(model)
    output = _select_result(node({
        "messages": [HumanMessage(content="把手机号改成13800138000")],
        "user_id": "u1",
        "context": {},
    }))

    assert model.bind_tools.call_args == call([tool], tool_choice="auto")
    assert bound_model.ainvoke.await_count == 1
    assert output["context"]["pending_tool"]["tool"] == "update_phone"


def test_select_real_model_system_includes_allowlist_and_examples():
    """#110: 真实路径与 fake 共享 allowlist 工具行与业务示例（输出形状仍为 tool_call）。"""
    bound_model = AsyncMock()
    bound_model.ainvoke.return_value = AIMessage(
        content="",
        tool_calls=[{"name": "update_phone", "args": {"phone": "13800138000"}, "id": "call_1"}],
    )
    model = MagicMock()
    model.bind_tools.return_value = bound_model
    node, _ = _real_model_node(model)
    _select_result(node({
        "messages": [HumanMessage(content="把手机号改成13800138000")],
        "user_id": "u1",
        "context": {},
    }))

    system = bound_model.ainvoke.await_args.args[0][0].content
    assert "- update_phone:" in system
    assert "参数：" in system
    assert "phone" in system
    assert "示例" in system
    assert "update_phone" in system
    assert "最接近的工具" not in system
    assert "不要强行映射" in system or "不属于" in system


def _real_model_node(model) -> tuple[Callable[[dict], dict], MagicMock]:
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
    return make_mutation_select_node(model, [tool], registry), tool


def test_select_real_model_no_tool_call_mutation_clarifies_inline(monkeypatch):
    """ADR 0004: Mutation no tool_call → inline Operation Clarify (LLM content), not flag/clarify node."""
    from ai_service_agent.graph.workflows.copy import operation_clarify_text

    events = []
    monkeypatch.setattr(
        "ai_service_agent.graph.workflows.mutation_select.get_stream_writer",
        lambda: events.append,
    )
    bound_model = AsyncMock()
    bound_model.ainvoke.return_value = AIMessage(
        content="请问您是要改地址、开票，还是加备注？",
        tool_calls=[],
    )
    model = MagicMock()
    model.bind_tools.return_value = bound_model
    node, _ = _real_model_node(model)

    output = _select_result(node({
        "messages": [HumanMessage(content="帮我弄一下订单")],
        "user_id": "u1",
        "context": {},
    }))

    assert output["execution_status"] == "clarifying"
    assert output["messages"][-1].content == "请问您是要改地址、开票，还是加备注？"
    assert output["context"].get("clarify_count") == 1
    assert "operation_clarify" not in output["context"]
    assert "pending_tool" not in output["context"]
    assert "precheck_reason" not in output["context"]
    assert events == [
        {"type": "clarification_required", "data": {"missing": [], "reason": "operation_unclear"}}
    ]

    bound_model.ainvoke.return_value = AIMessage(content="", tool_calls=[])
    empty = _select_result(node({
        "messages": [HumanMessage(content="帮我弄一下订单")],
        "user_id": "u1",
        "context": {},
    }))
    assert empty["messages"][-1].content == operation_clarify_text()


def test_select_mutation_low_tier_tool_rejects_unknown_tool():
    """ADR 0004: Mutation unbound / lower-tier tool → unknown_tool, not tier_clarify."""
    model = ToolCallingFakeModel(responses=[
        select_json_message("order_lookup", {"order_id": "ORD-1"}),
    ])
    node = make_mutation_select_node(
        model,
        DEFAULT_TOOL_REGISTRY.for_risk_level("mutation")
        + DEFAULT_TOOL_REGISTRY.for_risk_level("read"),
        DEFAULT_TOOL_REGISTRY,
    )
    # No route allowlist: lower-tier registered tool hits tier gate.
    output = _select_result(node({
        "messages": [HumanMessage(content="查一下订单")],
        "user_id": "u1",
        "context": {},
    }))
    assert output["context"].get("precheck_reason") == "unknown_tool"
    assert "tier_clarify" not in output["context"]
    assert "pending_tool" not in output["context"]


def test_select_real_model_no_tool_call_high_risk_routes_to_tier_clarify():
    """#110: HighRisk 无 tool_call → tier clarify，不是 reject。"""
    bound_model = AsyncMock()
    bound_model.ainvoke.return_value = AIMessage(content="", tool_calls=[])
    model = MagicMock()
    model.bind_tools.return_value = bound_model
    tool = MagicMock()
    tool.name = "cancel_order"
    registry = ToolRegistry()
    registry.register(
        ToolMeta(name="cancel_order", description="取消订单", min_risk_level="high_risk"),
        tool,
    )
    node = make_select_node(model, "high_risk", [tool], registry)

    output = _select_result(node({
        "messages": [HumanMessage(content="帮我处理一下")],
        "user_id": "u1",
        "context": {},
    }))

    assert output["context"].get("tier_clarify")
    assert "precheck_reason" not in output["context"]
    assert "pending_tool" not in output["context"]


def test_select_real_model_provider_failure_propagates_for_retry():
    bound_model = AsyncMock()
    bound_model.ainvoke.side_effect = RuntimeError("provider unavailable")
    model = MagicMock()
    model.bind_tools.return_value = bound_model
    node, _ = _real_model_node(model)

    with pytest.raises(RuntimeError, match="provider unavailable"):
        _select_result(node({
            "messages": [HumanMessage(content="把手机号改成13800138000")],
            "user_id": "u1",
            "context": {},
        }))


def test_select_timeout_routes_to_local_error_handler(monkeypatch):
    monkeypatch.setenv("LLM_NODE_TIMEOUT_SECONDS", "0.05")
    get_settings.cache_clear()
    try:
        model = SlowSelectFakeModel(responses=[
            select_json_message("update_address", {"address": "朝阳区"}),
        ])
        node = make_mutation_select_node(
            model,
            DEFAULT_TOOL_REGISTRY.for_risk_level("mutation"),
            DEFAULT_TOOL_REGISTRY,
        )
        builder = StateGraph(CustomerServiceState)
        builder.set_node_defaults(error_handler=make_subgraph_error_handler("finalize_failed"))
        builder.add_node(
            "mutation_select_llm",
            node,
            retry_policy=RETRY_LLM,
            timeout=make_timeout_policy(get_settings().llm_node_timeout_seconds),
        )
        builder.add_node("finalize_failed", lambda state: None)
        builder.add_edge(START, "mutation_select_llm")
        builder.add_edge("mutation_select_llm", "finalize_failed")
        builder.add_edge("finalize_failed", END)
        graph = builder.compile()

        node_spec = graph.nodes["mutation_select_llm"]
        assert inspect.iscoroutinefunction(node_spec.bound.afunc)
        assert node_spec.timeout == TimeoutPolicy(run_timeout=0.05)

        started = time.monotonic()
        output = asyncio.run(graph.ainvoke({
            "messages": [HumanMessage(content="改一下收货地址")],
            "user_id": "u1",
            "session_id": "timeout-test",
            "context": {},
        }))
        elapsed = time.monotonic() - started

        assert output["execution_status"] == "failed"
        assert elapsed < 3.5
    finally:
        get_settings.cache_clear()
