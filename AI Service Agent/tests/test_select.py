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
from ai_service_agent.graph.workflows.select import make_select_node
from ai_service_agent.testing import ToolCallingFakeModel, select_json_message
from ai_service_agent.tools import DEFAULT_TOOL_REGISTRY
from ai_service_agent.tools.registry import ToolMeta, ToolRegistry


class RecordingSelectModel(ToolCallingFakeModel):
    """记录最后一次系统提示（select 的 json_mode 契约提示）。"""

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
    node = make_select_node(
        model, "mutation", DEFAULT_TOOL_REGISTRY.for_risk_level("mutation"), DEFAULT_TOOL_REGISTRY
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

    node = make_select_node(CaptureToolModel(), "mutation", tools=[], registry=ToolRegistry())
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


def test_select_system_template_service_ticket_vs_refund_boundary():
    """售后/退款边界：select prompt 必须包含 create_service_ticket ≠ refund_apply 的对照示例。"""
    model, node = _select_node()
    _select_result(node({"messages": [HumanMessage(content="改一下收货地址")], "context": {}}))
    assert "售后工单" in model.seen_system
    assert "退款申请" in model.seen_system
    assert "退货" in model.seen_system


def test_select_json_tool_list_uses_short_meta_description():
    """JSON Select 的允许工具列表只用 ToolMeta 短描述 + schema 参数契约。"""
    model, node = _select_node()
    _select_result(node({"messages": [HumanMessage(content="改一下收货地址")], "context": {}}))
    assert "- update_address: 修改本人订单关联的收货地址。" in model.seen_system
    assert "- create_service_ticket: 为本人订单创建售后工单。" in model.seen_system
    assert "参数：order_id(必填)，address(必填)" in model.seen_system
    assert "参数：order_id、address（必填）。" not in model.seen_system
    assert "需退款走 refund_apply" not in model.seen_system


def test_select_bindtools_system_states_ticket_versus_refund_boundary():
    """真模型 Select 的系统提示与 JSON 模板规则 6 等价：工单跟进 ≠ 资金退回。"""
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

    assert model.bind_tools.call_args == call([tool], tool_choice="any")
    system = bound_model.ainvoke.await_args.args[0][0].content
    assert "create_service_ticket" in system
    assert "refund_apply" in system
    assert "核心诉求是售后处理，选 create_service_ticket，不要因「退货」改选 refund_apply" in system
    assert "只有用户明确说出退款金额或「退钱」「退款」时，才选 refund_apply" in system


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

    assert model.bind_tools.call_args == call([tool], tool_choice="any")
    assert bound_model.ainvoke.await_count == 1
    assert output["context"]["pending_tool"]["tool"] == "update_phone"


def _real_model_node(model) -> tuple[Callable[[dict], dict], MagicMock]:
    tool = MagicMock()
    tool.name = "update_phone"
    registry = ToolRegistry()
    registry.register(
        ToolMeta(name="update_phone", description="修改联系电话", min_risk_level="mutation"),
        tool,
    )
    return make_select_node(model, "mutation", [tool], registry), tool


def test_select_real_model_rejects_missing_tool_calls():
    bound_model = AsyncMock()
    bound_model.ainvoke.return_value = AIMessage(content="", tool_calls=[])
    model = MagicMock()
    model.bind_tools.return_value = bound_model
    node, _ = _real_model_node(model)

    output = _select_result(node({
        "messages": [HumanMessage(content="把手机号改成13800138000")],
        "user_id": "u1",
        "context": {},
    }))

    assert output["context"]["precheck_reason"] == "select_parse_failed"
    assert output["execution_status"] == "completed"
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
        node = make_select_node(
            model,
            "mutation",
            DEFAULT_TOOL_REGISTRY.for_risk_level("mutation"),
            DEFAULT_TOOL_REGISTRY,
        )
        builder = StateGraph(CustomerServiceState)
        builder.set_node_defaults(error_handler=make_subgraph_error_handler("finalize_failed"))
        builder.add_node(
            "select_llm",
            node,
            retry_policy=RETRY_LLM,
            timeout=make_timeout_policy(get_settings().llm_node_timeout_seconds),
        )
        builder.add_node("finalize_failed", lambda state: None)
        builder.add_edge(START, "select_llm")
        builder.add_edge("select_llm", "finalize_failed")
        builder.add_edge("finalize_failed", END)
        graph = builder.compile()

        node_spec = graph.nodes["select_llm"]
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
