"""Clarify 机制（P4 D1）：select 参数完备性触发，普通轮非 interrupt。"""

import asyncio

from langgraph.checkpoint.memory import MemorySaver
from langchain_core.messages import HumanMessage

from ai_service_agent.config import get_settings
from ai_service_agent.graph.builder import build_graph
from ai_service_agent.graph.workflows.clarify import CLARIFY_FALLBACK_TEXT, clarify_text
from ai_service_agent.graph.workflows.select import make_select_node
from ai_service_agent.harness.streaming import event_stream
from ai_service_agent.testing import ToolCallingFakeModel, router_json_message, select_json_message
from ai_service_agent.tools import DEFAULT_TOOL_REGISTRY
from ai_service_agent.tools.kb import kb_search


def _graph(model):
    return build_graph(model, tools=[kb_search], checkpointer=MemorySaver())


def _invoke(graph, text, thread_id, user_id="u1", context=None):
    return graph.invoke(
        {"messages": [HumanMessage(content=text)], "user_id": user_id, "session_id": thread_id, "context": context or {}},
        {"configurable": {"thread_id": thread_id}},
    )


def _highrisk_model(args: dict):
    """refund_apply 属 high_risk 档，须走 high_risk 图（mutation 图里它会 unknown_tool）。"""
    return ToolCallingFakeModel(responses=[
        router_json_message("order", "high_risk", 0.9),
        select_json_message("refund_apply", args),
    ])


def test_clarify_text_maps_labels():
    assert clarify_text(["order_id"]) == "请提供：订单号。"
    assert clarify_text(["order_id", "amount"]) == "请提供：订单号、金额。"
    assert "order_no" in clarify_text(["order_no"])  # 未映射字段回落原字段名


def test_config_clarify_max_turns_default():
    assert get_settings().clarify_max_turns == 2


def test_select_incomplete_sets_clarify_missing():
    """select 直测：account_update 全空 -> clarify_missing，不落 pending_tool、不动 count。"""
    model = ToolCallingFakeModel(responses=[select_json_message("account_update", {})])
    node = make_select_node(model, "mutation", DEFAULT_TOOL_REGISTRY.for_risk_level("mutation"), DEFAULT_TOOL_REGISTRY)
    out = node({"messages": [HumanMessage(content="改一下收货地址")], "context": {"clarify_count": 1}})
    assert set(out["context"]["clarify_missing"]) == {"phone", "email", "address"}
    assert "pending_tool" not in out["context"]
    assert out["context"]["clarify_count"] == 1  # select 不清零，由 clarify 节点/完整 select 管理


def test_select_complete_resets_clarify_count():
    model = ToolCallingFakeModel(responses=[select_json_message("account_update", {"address": "北京市朝阳区"})])
    node = make_select_node(model, "mutation", DEFAULT_TOOL_REGISTRY.for_risk_level("mutation"), DEFAULT_TOOL_REGISTRY)
    out = node({"messages": [HumanMessage(content="改地址")], "context": {"clarify_count": 1}})
    assert out["context"]["clarify_count"] == 0  # 完整 select 清零（D1）
    assert out["context"]["pending_tool"]["tool"] == "account_update"
    assert "clarify_missing" not in out["context"]


def test_highrisk_missing_required_clarifies():
    out = _invoke(_graph(_highrisk_model({})), "退了吧", "c1")
    assert not out.get("__interrupt__")
    assert out["execution_status"] == "clarifying"
    assert out["context"]["clarify_missing"] == ["order_id"]
    assert "订单号" in out["messages"][-1].content


def test_highrisk_empty_required_clarifies():
    """pydantic 接受 order_id=''，须显式判空走 clarify。"""
    out = _invoke(_graph(_highrisk_model({"order_id": ""})), "退了吧", "c2")
    assert out["execution_status"] == "clarifying"
    assert out["context"]["clarify_missing"] == ["order_id"]


def test_mutation_all_optional_empty_clarifies():
    model = ToolCallingFakeModel(responses=[
        router_json_message("account", "mutation", 0.9),
        select_json_message("account_update", {}),
    ])
    out = _invoke(_graph(model), "改一下收货地址", "c3")
    assert out["execution_status"] == "clarifying"
    assert set(out["context"]["clarify_missing"]) == {"phone", "email", "address"}


def test_clarify_over_limit_falls_back():
    out = _invoke(_graph(_highrisk_model({})), "退了吧", "c4", context={"clarify_count": 2})
    assert out["execution_status"] == "completed"
    assert CLARIFY_FALLBACK_TEXT in out["messages"][-1].content


async def _acollect(gen):
    return [e async for e in gen]


def test_clarify_streams_event_and_done():
    events = asyncio.run(_acollect(event_stream(
        _graph(_highrisk_model({})), {"configurable": {"thread_id": "c6"}}, [HumanMessage(content="退了吧")]
    )))
    types = [e["type"] for e in events]
    assert "clarification_required" in types
    assert types[-1] == "done"
