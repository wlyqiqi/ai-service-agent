import asyncio

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from langchain_core.messages import HumanMessage

from ai_service_agent.graph.builder import build_graph
from ai_service_agent.harness.streaming import event_stream
from ai_service_agent.testing import ToolCallingFakeModel, router_json_message, select_json_message
from ai_service_agent.tools.kb import kb_search


def _mutation_model():
    return ToolCallingFakeModel(responses=[
        router_json_message("order", "mutation", 0.9),
        select_json_message("update_address", {"address": "北京市朝阳区"}),
    ])


def _invoke(graph, text, thread_id, resume=None, user_id="u1"):
    kwargs = {"configurable": {"thread_id": thread_id}}
    if resume is not None:
        return graph.invoke(Command(resume=resume), kwargs)
    return graph.invoke(
        {"messages": [HumanMessage(content=text)], "user_id": user_id, "session_id": thread_id, "context": {}},
        kwargs,
    )


def test_mutation_reaches_confirmation_interrupt():
    graph = build_graph(_mutation_model(), tools=[kb_search], checkpointer=MemorySaver())
    out = _invoke(graph, "改一下收货地址", "m1")
    values = [it.value for it in out.get("__interrupt__", [])]
    assert any(v.get("type") == "confirmation_required" for v in values)


def test_mutation_confirm_executes_tool():
    graph = build_graph(_mutation_model(), tools=[kb_search], checkpointer=MemorySaver())
    _invoke(graph, "改一下收货地址", "m2")
    out = _invoke(graph, "确认", "m2", resume={"action": "confirm"})
    assert out["execution_status"] == "completed"
    tool_msgs = [m for m in out["messages"] if getattr(m, "type", "") == "tool"]
    assert tool_msgs and getattr(tool_msgs[-1], "name", "") == "update_address"


def test_mutation_cancel_does_not_execute_tool():
    graph = build_graph(_mutation_model(), tools=[kb_search], checkpointer=MemorySaver())
    _invoke(graph, "改一下收货地址", "m3")
    out = _invoke(graph, "取消", "m3", resume={"action": "cancel"})
    assert out["execution_status"] == "completed"
    assert [m for m in out["messages"] if getattr(m, "type", "") == "tool"] == []
    assert "取消" in out["messages"][-1].content


def test_mutation_unknown_tool_rejected_no_interrupt():
    model = ToolCallingFakeModel(responses=[
        router_json_message("order", "mutation", 0.9),
        select_json_message("order_set_status", {"order_id": "10086"}),
    ])
    graph = build_graph(model, tools=[kb_search], checkpointer=MemorySaver())
    out = _invoke(graph, "把订单状态改成已发货", "m4")
    assert not out.get("__interrupt__")
    assert out["execution_status"] == "completed"
    assert [m for m in out["messages"] if getattr(m, "type", "") == "tool"] == []


def test_mutation_precheck_rejects_unauthenticated():
    model = ToolCallingFakeModel(responses=[
        router_json_message("order", "mutation", 0.9),
        select_json_message("update_address", {"address": "新地址"}),
    ])
    graph = build_graph(model, tools=[kb_search], checkpointer=MemorySaver())
    out = graph.invoke(
        {"messages": [HumanMessage(content="我没登录，改一下地址")], "user_id": "", "session_id": "m5", "context": {}},
        {"configurable": {"thread_id": "m5"}},
    )
    assert not out.get("__interrupt__")
    assert out["execution_status"] == "completed"


async def _acollect(gen):
    return [e async for e in gen]


def test_mutation_reject_streams_safety_rejected():
    model = ToolCallingFakeModel(responses=[
        router_json_message("order", "mutation", 0.9),
        select_json_message("order_set_status", {"order_id": "10086"}),
    ])
    graph = build_graph(model, tools=[kb_search], checkpointer=MemorySaver())
    events = asyncio.run(_acollect(event_stream(
        graph, {"configurable": {"thread_id": "m6"}}, [HumanMessage(content="改状态")]
    )))
    types = [e["type"] for e in events]
    assert "safety_rejected" in types
    assert types[-1] == "done"


def test_mutation_select_unknown_field_rejected_invalid_args():
    """真实模型漂移样例（{"action": ...}）：未知字段被 select 拒绝为 invalid_args，不产生 interrupt。"""
    model = ToolCallingFakeModel(responses=[
        router_json_message("order", "mutation", 0.9),
        select_json_message("update_address", {"action": "update_shipping_address"}),
    ])
    graph = build_graph(model, tools=[kb_search], checkpointer=MemorySaver())
    out = _invoke(graph, "改一下收货地址", "m7")
    assert not out.get("__interrupt__")
    assert out["execution_status"] == "completed"
    assert out["context"].get("precheck_reason") == "invalid_args"
    assert [m for m in out["messages"] if getattr(m, "type", "") == "tool"] == []


def test_mutation_0089_delete_orders_unknown_tool():
    """D5 验证（P4 Task 3）：CTRL-0089 删除订单——scripted select 输出不存在工具 ->
    precheck_reason=unknown_tool、无 interrupt（registry 无删除类工具）。"""
    model = ToolCallingFakeModel(responses=[
        router_json_message("order", "mutation", 0.9),
        select_json_message("delete_order_history", {"order_id": "ORD-10086"}),
    ])
    graph = build_graph(model, tools=[kb_search], checkpointer=MemorySaver())
    out = _invoke(graph, "把我在你们这的所有订单记录删掉", "t3-0089")
    assert not out.get("__interrupt__")
    assert out["execution_status"] == "completed"
    assert out["context"].get("precheck_reason") == "unknown_tool"
    assert [m for m in out["messages"] if getattr(m, "type", "") == "tool"] == []
def test_mutation_multi_turn_clarify_then_confirm_executes():
    """跨轮端到端：缺参"我要改个地址"澄清 -> 用户补地址 -> confirmation interrupt -> confirm 执行一次。

    对应真机冒烟 Mutation 场景与 mutation 写图链路（select_llm -> clarify -> confirm_pause -> recheck -> execute）。
    共享 MemorySaver 保持跨轮 checkpoint 状态（context/pending_tool 随 thread_id 延续）。
    """
    saver = MemorySaver()

    def _build(model):
        return build_graph(model, tools=[kb_search], checkpointer=saver)

    def _turn(graph, text, thread_id):
        return graph.invoke(
            {"messages": [HumanMessage(content=text)], "user_id": "u1", "session_id": thread_id},
            {"configurable": {"thread_id": thread_id}},
        )

    clarify_model = ToolCallingFakeModel(responses=[
        router_json_message("account", "mutation", 0.9),
        select_json_message("update_address", {}),
    ])
    complete_model = ToolCallingFakeModel(responses=[
        router_json_message("account", "mutation", 0.9),
        select_json_message("update_address", {"address": "北京市朝阳区"}),
    ])
    tid = "mt-clarify-1"

    # 轮1：缺参 -> 澄清（普通轮，非 interrupt）
    t1 = _turn(_build(clarify_model), "我要改个地址", tid)
    assert t1["execution_status"] == "clarifying"
    assert not t1.get("__interrupt__")
    assert t1["context"]["clarify_missing"] == ["address"]
    assert "请提供：收货地址。" in t1["messages"][-1].content

    # 轮2：补地址 -> 完整 select -> 确认中断（参数已进 summary，本轮不执行工具）
    t2 = _turn(_build(complete_model), "新地址是北京市朝阳区", tid)
    values = [it.value for it in t2.get("__interrupt__", [])]
    confirm = next((v for v in values if v.get("type") == "confirmation_required"), None)
    assert confirm is not None
    assert "update_address" in confirm["summary"]
    assert "北京市朝阳区" in confirm["summary"]

    # 轮3：confirm -> recheck -> execute -> 工具只执行一次
    t3 = _build(complete_model).invoke(Command(resume={"action": "confirm"}), {"configurable": {"thread_id": tid}})
    assert t3["execution_status"] == "completed"
    tool_msgs = [m for m in t3["messages"] if getattr(m, "type", "") == "tool"]
    assert len(tool_msgs) == 1 and getattr(tool_msgs[0], "name", "") == "update_address"
    assert "操作已完成" in t3["messages"][-1].content
