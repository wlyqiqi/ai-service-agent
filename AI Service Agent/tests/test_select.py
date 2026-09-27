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
from ai_service_agent.graph.workflows import select as select_module
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


class ClarificationRecordingSelectModel(ToolCallingFakeModel):
    """记录最后一次用户提示，验证澄清草稿进入 fake JSON 输入。"""

    def _record_last_user(self, messages):
        object.__setattr__(self, "seen_last_user", next(
            (m.content for m in messages if getattr(m, "type", "") == "human"), ""
        ))

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self._record_last_user(messages)
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        self._record_last_user(messages)
        return await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)


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


def test_select_clarification_prompt_does_not_expose_untrusted_draft():
    state = {
        "messages": [HumanMessage(content="放在小区保安那里")],
        "route_intent": "order_mutation",
        "context": {
            "pending_clarification": {
                "route_intent": "order_mutation",
                "tool": "update_order_note",
                "args": {"order_id": "ORD-1"},
                "missing": ["note"],
            }
        },
    }

    model = ClarificationRecordingSelectModel(responses=[
        select_json_message("update_order_note", {
            "order_id": "ORD-1",
            "note": "放在小区保安那里",
        }),
    ])
    node = make_select_node(
        model, "mutation", DEFAULT_TOOL_REGISTRY.for_risk_level("mutation"), DEFAULT_TOOL_REGISTRY
    )
    _select_result(node(state))
    assert "update_order_note" not in model.seen_last_user
    assert "ORD-1" not in model.seen_last_user
    assert "继续处理未完成操作" in model.seen_last_user
    assert "放在小区保安那里" in model.seen_last_user

    bound_model = AsyncMock()

    async def record_real_model_input(messages, **kwargs):
        object.__setattr__(bound_model, "seen_last_user", next(
            (message.content for message in messages if getattr(message, "type", "") == "human"), ""
        ))
        return AIMessage(
            content="",
            tool_calls=[{
                "name": "update_order_note",
                "args": {"order_id": "ORD-1", "note": "放在小区保安那里"},
                "id": "call_1",
            }],
        )

    bound_model.ainvoke.side_effect = record_real_model_input
    real_model = MagicMock()
    real_model.bind_tools.return_value = bound_model
    real_node = make_select_node(
        real_model, "mutation", DEFAULT_TOOL_REGISTRY.for_risk_level("mutation"), DEFAULT_TOOL_REGISTRY
    )
    _select_result(real_node(state))
    assert real_model.bind_tools.call_args.args[0]
    assert "update_order_note" not in bound_model.seen_last_user
    assert "ORD-1" not in bound_model.seen_last_user
    assert "继续处理未完成操作" in bound_model.seen_last_user
    assert "放在小区保安那里" in bound_model.seen_last_user


def _clarification_node(response: dict) -> tuple[ClarificationRecordingSelectModel, Callable[[dict], object]]:
    model = ClarificationRecordingSelectModel(responses=[
        select_json_message(response["tool"], response["args"]),
    ])
    node = make_select_node(
        model, "mutation", DEFAULT_TOOL_REGISTRY.for_risk_level("mutation"), DEFAULT_TOOL_REGISTRY
    )
    return model, node


def test_select_merges_matching_pending_clarification_args():
    pending = {
        "route_intent": "order_mutation",
        "tool": "update_order_note",
        "args": {"order_id": "ORD-1"},
        "missing": ["note"],
    }
    _, node = _clarification_node({"tool": "update_order_note", "args": {"note": "放小区保安那里"}})

    output = _select_result(node({
        "messages": [HumanMessage(content="放小区保安那里")],
        "route_intent": "order_mutation",
        "context": {"clarify_count": 1, "pending_clarification": pending},
    }))

    assert output["context"]["pending_tool"]["tool"] == "update_order_note"
    pending_tool_args = output["context"]["pending_tool"]["args"]
    assert pending_tool_args == {
        "order_id": "ORD-1",
        "note": "放小区保安那里",
        "request_id": pending_tool_args["request_id"],
    }
    assert "pending_clarification" not in output["context"]
    assert output["context"]["clarify_count"] == 0


def test_select_matching_clarification_progress_resets_count():
    pending = {
        "route_intent": "order_mutation",
        "tool": "update_address",
        "args": {},
        "missing": ["order_id", "address"],
    }
    _, node = _clarification_node({"tool": "update_address", "args": {"order_id": "ORD-10086"}})

    output = _select_result(node({
        "messages": [HumanMessage(content="订单号是 ORD-10086")],
        "route_intent": "order_mutation",
        "context": {"clarify_count": 1, "pending_clarification": pending},
    }))

    assert output["context"]["clarify_count"] == 0
    assert output["context"]["pending_clarification"]["args"]["order_id"] == "ORD-10086"
    assert output["context"]["pending_clarification"]["missing"] == ["address"]


def test_select_pending_clarification_tool_mismatch_does_not_inherit():
    pending = {
        "route_intent": "order_mutation",
        "tool": "update_order_note",
        "args": {"order_id": "ORD-1"},
        "missing": ["note"],
    }
    _, node = _clarification_node({"tool": "update_address", "args": {"address": "朝阳区"}})

    output = _select_result(node({
        "messages": [HumanMessage(content="改成朝阳区")],
        "route_intent": "order_mutation",
        "context": {"pending_clarification": pending},
    }))

    assert output["context"]["pending_clarification"] == {
        "route_intent": "order_mutation",
        "tool": "update_address",
        "args": {"address": "朝阳区"},
        "missing": ["order_id"],
    }


def test_select_matching_clarification_latest_value_overrides_old_value():
    pending = {
        "route_intent": "order_mutation",
        "tool": "update_address",
        "args": {"address": "旧地址"},
        "missing": ["order_id"],
    }
    _, node = _clarification_node({"tool": "update_address", "args": {"address": "新地址"}})

    output = _select_result(node({
        "messages": [HumanMessage(content="新地址")],
        "route_intent": "order_mutation",
        "context": {"pending_clarification": pending},
    }))

    assert output["context"]["pending_clarification"]["args"]["address"] == "新地址"
    assert output["context"]["pending_clarification"]["missing"] == ["order_id"]


def test_select_cancellation_clarification_preserves_schema_args(monkeypatch):
    candidates = [
        {"order_id": "ORD-1", "total_amount": 100},
        {"order_id": "ORD-2", "total_amount": 200},
    ]
    monkeypatch.setattr(
        select_module, "find_own_cancellable_paid_orders", lambda user_id: candidates
    )
    _, node = _clarification_node({"tool": "cancel_order", "args": {"reason": "不想要了"}})

    output = _select_result(node({
        "messages": [HumanMessage(content="取消吧")],
        "route_intent": "order_mutation",
        "user_id": "u1",
        "context": {},
    }))

    assert output["context"]["pending_clarification"]["tool"] == "cancel_order"
    assert output["context"]["pending_clarification"]["args"]["reason"] == "不想要了"
    assert output["context"]["cancellation_candidates"] == candidates


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
