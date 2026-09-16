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


def _build(model, saver):
    return build_graph(model, tools=[kb_search], checkpointer=saver)


def _turn(graph, text, thread_id, user_id="u1"):
    # 续接轮不传 context：context 由 checkpoint 保留（真实 /chat 入口同语义），仅追加新消息
    return graph.invoke(
        {"messages": [HumanMessage(content=text)], "user_id": user_id, "session_id": thread_id},
        {"configurable": {"thread_id": thread_id}},
    )


def test_multi_turn_clarify_then_complete_enters_highrisk():
    """0076 跨 turn 链路：首轮"退了吧"澄清 -> 用户补订单号 -> 下轮以完整参数进入 HighRisk 审批闸。

    Ruling（2026-09-16，ledger）：子图 interrupt 时父图不落子图 context/execution_status 写
    （LangGraph 语义，mutation 确认闸同；harness 不依赖父图这些字段）。故断言可观测契约：
    approval_required interrupt 携带完整 args（ORD-UNKNOWN），且本轮不再澄清、工具未执行。
    clarify_count 清零发生在子图内，父图视图在 resume 完成后才同步。
    """
    saver = MemorySaver()
    t1 = _turn(_build(_highrisk_model({}), saver), "退了吧", "c7")
    assert t1["execution_status"] == "clarifying"
    assert t1["context"]["clarify_count"] == 1
    assert not t1.get("__interrupt__")

    # 第二轮补参：ORD-UNKNOWN 不在种子库 -> 无事实 -> 预检通过 -> approval_required（同 t3-pos）
    t2 = _turn(_build(_highrisk_model({"order_id": "ORD-UNKNOWN"}), saver), "订单号是 ORD-UNKNOWN", "c7")
    interrupts = [it.value for it in t2.get("__interrupt__", [])]
    approval = next((v for v in interrupts if v.get("type") == "approval_required"), None)
    assert approval is not None                  # 第二轮正常进入 HighRisk 审批闸
    assert "ORD-UNKNOWN" in approval["summary"]  # 完整 select 已被消费（参数进入审批摘要）
    assert not any(getattr(m, "type", "") == "tool" for m in t2["messages"])  # 审批前工具不执行


def test_multi_turn_clarify_over_limit_falls_back():
    """0076 跨 turn：连续 3 轮不补齐 -> 超过 clarify_max_turns=2 -> 确定性 fallback，绝不默认执行。"""
    saver = MemorySaver()
    r1 = _turn(_build(_highrisk_model({}), saver), "退了吧", "c8")
    assert r1["execution_status"] == "clarifying" and r1["context"]["clarify_count"] == 1
    r2 = _turn(_build(_highrisk_model({}), saver), "还是没有订单号", "c8")
    assert r2["execution_status"] == "clarifying" and r2["context"]["clarify_count"] == 2
    r3 = _turn(_build(_highrisk_model({}), saver), "那就这样吧", "c8")
    assert r3["execution_status"] == "completed"
    assert CLARIFY_FALLBACK_TEXT in r3["messages"][-1].content
    assert not r3.get("__interrupt__")


def _tier_model(tool_name, risk):
    return ToolCallingFakeModel(responses=[
        router_json_message("order", risk, 0.9),
        select_json_message(tool_name, {}),
    ])


def test_highrisk_read_tool_selected_tier_clarifies():
    """B2b：high_risk 图内 fake select 只读工具 order_lookup -> tier_mismatch 澄清（普通轮），
    不进审批闸、无 interrupt。镜像真机冒烟 §7 Run 2 的漂移路径。"""
    out = _invoke(_graph(_tier_model("order_lookup", "high_risk")), "退了吧", "b2-h1")
    assert out["execution_status"] == "clarifying"
    assert not out.get("__interrupt__")
    assert out["context"].get("tier_clarify") == "order_lookup"
    assert "pending_tool" not in out["context"]
    assert "请明确" in out["messages"][-1].content


def test_mutation_read_tool_selected_tier_clarifies():
    """B2b：mutation 图内 fake select 只读工具 kb_search -> tier_mismatch 澄清。"""
    out = _invoke(_graph(_tier_model("kb_search", "mutation")), "先查一下再改", "b2-m1")
    assert out["execution_status"] == "clarifying"
    assert out["context"].get("tier_clarify") == "kb_search"
    assert not out.get("__interrupt__")


def test_tier_clarify_streams_event_with_reason():
    """B2b：事件契约——clarification_required 带 reason=tier_mismatch，末事件 done。"""
    events = asyncio.run(_acollect(event_stream(
        _graph(_tier_model("order_lookup", "high_risk")),
        {"configurable": {"thread_id": "b2-s1"}},
        [HumanMessage(content="退了吧")],
    )))
    types = [e["type"] for e in events]
    assert "clarification_required" in types
    clar = next(e for e in events if e["type"] == "clarification_required")
    assert clar["data"].get("reason") == "tier_mismatch"
    assert clar["data"]["missing"] == []
    assert types[-1] == "done"
