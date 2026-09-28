import asyncio
import importlib.util
import sqlite3
import tempfile
from unittest.mock import MagicMock
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from langchain_core.messages import HumanMessage

from ai_service_agent.graph.builder import build_graph
from ai_service_agent.graph.workflows import clarify as clarify_mod
from ai_service_agent.graph.workflows.copy import RUNTIME_ERROR_TEXT, clarify_missing_text
from ai_service_agent.graph.workflows.select import make_select_node
from ai_service_agent.harness.streaming import event_stream
from conftest import ainvoke_graph
from ai_service_agent.testing import ToolCallingFakeModel, router_json_message, select_json_message
from ai_service_agent.tools import DEFAULT_TOOL_REGISTRY
from ai_service_agent.tools.kb import kb_search
from ai_service_agent.tools import orders as orders_mod
from ai_service_agent.tools import write as write_mod


ROOT = Path(__file__).resolve().parents[1]


def _load_seed_module():
    spec = importlib.util.spec_from_file_location(
        "seed_mock_db_mutation", ROOT / "scripts" / "seed_mock_db.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def seed():
    module = _load_seed_module()
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "mutation.db"
        module.build_database(str(db_path))
        yield module, db_path


def _use_db(monkeypatch, db_path: Path):
    class _Settings:
        mock_ecommerce_db_path = str(db_path)

    monkeypatch.setattr(write_mod, "get_settings", lambda: _Settings())
    monkeypatch.setattr(orders_mod, "get_settings", lambda: _Settings())


def _mutation_model():
    return ToolCallingFakeModel(responses=[
        router_json_message("order", "mutation", 0.9),
        select_json_message("update_address", {"order_id": "ORD-PAID-1", "address": "北京市朝阳区"}),
    ])


def _invoke(graph, text, thread_id, resume=None, user_id="u1", include_context=True):
    kwargs = {"configurable": {"thread_id": thread_id}}
    if resume is not None:
        return ainvoke_graph(graph, Command(resume=resume), kwargs)
    state = {"messages": [HumanMessage(content=text)], "user_id": user_id, "session_id": thread_id}
    if include_context:
        state["context"] = {}
    return ainvoke_graph(graph,
        state,
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


def test_mutation_tool_exception_uses_local_finalize_failed(seed, monkeypatch):
    module, db_path = seed
    _use_db(monkeypatch, db_path)
    monkeypatch.setattr(
        write_mod.update_address,
        "func",
        MagicMock(side_effect=RuntimeError("database unavailable")),
    )

    graph = build_graph(_mutation_model(), tools=[kb_search], checkpointer=MemorySaver())
    _invoke(graph, "改一下收货地址", "mutation-failure-1")
    output = _invoke(graph, "确认", "mutation-failure-1", resume={"action": "confirm"})

    assert output["execution_status"] == "failed"
    assert output["messages"][-1].content == RUNTIME_ERROR_TEXT
    assert "已更新完成" not in output["messages"][-1].content


def test_mutation_tool_error_result_not_rewritten_as_success(seed, monkeypatch):
    """P1-3 回归：工具返回 {"error": ...} 时，finalize_ok 不得输出成功话术。"""
    module, db_path = seed
    _use_db(monkeypatch, db_path)
    monkeypatch.setattr(
        write_mod.update_address,
        "func",
        MagicMock(return_value={"error": "not_found", "order_id": "ORD-PAID-1"}),
    )

    graph = build_graph(_mutation_model(), tools=[kb_search], checkpointer=MemorySaver())
    _invoke(graph, "改一下收货地址", "mutation-tool-error-1")
    output = _invoke(graph, "确认", "mutation-tool-error-1", resume={"action": "confirm"})

    assert output["execution_status"] == "failed"
    last = output["messages"][-1].content
    assert "已更新完成" not in last and "已办理完成" not in last
    assert "未成功" in last


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
            select_json_message("update_address", {"order_id": "ORD-PAID-1", "address": "新地址"}),
    ])
    graph = build_graph(model, tools=[kb_search], checkpointer=MemorySaver())
    out = ainvoke_graph(graph,
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
        return ainvoke_graph(graph,
            {"messages": [HumanMessage(content=text)], "user_id": "u1", "session_id": thread_id},
            {"configurable": {"thread_id": thread_id}},
        )

    clarify_model = ToolCallingFakeModel(responses=[
        router_json_message("account", "mutation", 0.9),
        select_json_message("update_address", {}),
    ])
    complete_model = ToolCallingFakeModel(responses=[
        router_json_message("account", "mutation", 0.9),
        select_json_message("update_address", {"order_id": "ORD-PAID-1", "address": "北京市朝阳区"}),
    ])
    tid = "mt-clarify-1"

    # 轮1：缺参 -> 澄清（普通轮，非 interrupt）
    t1 = _turn(_build(clarify_model), "我要改个地址", tid)
    assert t1["execution_status"] == "clarifying"
    assert not t1.get("__interrupt__")
    assert t1["context"]["clarify_missing"] == ["order_id", "address"]
    assert "收货地址" in t1["messages"][-1].content

    # 轮2：补地址 -> 完整 select -> 确认中断（参数已进 summary，本轮不执行工具）
    t2 = _turn(_build(complete_model), "新地址是北京市朝阳区", tid)
    values = [it.value for it in t2.get("__interrupt__", [])]
    confirm = next((v for v in values if v.get("type") == "confirmation_required"), None)
    assert confirm is not None
    assert "收货地址修改" in confirm["summary"]
    assert "北京市朝阳区" in confirm["summary"]

    # 轮3：confirm -> recheck -> execute -> 工具只执行一次
    t3 = _build(complete_model).invoke(Command(resume={"action": "confirm"}), {"configurable": {"thread_id": tid}})
    assert t3["execution_status"] == "completed"
    tool_msgs = [m for m in t3["messages"] if getattr(m, "type", "") == "tool"]
    assert len(tool_msgs) == 1 and getattr(tool_msgs[0], "name", "") == "update_address"
    assert "收货地址已更新" in t3["messages"][-1].content


def test_update_order_note_multi_turn_clarification_merge_executes_once(monkeypatch, seed):
    """订单号先收集、下一轮补备注后合并，确认只执行一次 update_order_note。"""
    _, db_path = seed
    _use_db(monkeypatch, db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO orders (
                order_id, customer_id, status, ordered_at, signed_at,
                cancelled_at, total_amount, currency, channel, market, note
            )
            SELECT 'ORD-PAID-1', customer_id, status, ordered_at, signed_at,
                   cancelled_at, total_amount, currency, channel, market, note
            FROM orders WHERE order_id='ORD-REFUND-1'
            """
        )
        conn.commit()
        owner = conn.execute(
            "SELECT customer_id FROM orders WHERE order_id='ORD-PAID-1'"
        ).fetchone()[0]
    finally:
        conn.close()

    model = ToolCallingFakeModel(responses=[
        router_json_message("order", "mutation", 0.9),
        select_json_message("update_order_note", {"order_id": "ORD-PAID-1"}),
        router_json_message("order", "mutation", 0.9),
        select_json_message("update_order_note", {"note": "放小区保安那里"}),
    ])
    graph = build_graph(model, tools=[kb_search], checkpointer=MemorySaver())
    thread_id = "update-note-clarification-merge"

    first = _invoke(
        graph, "请帮我修改订单 ORD-PAID-1 的备注", thread_id, user_id=owner, include_context=False
    )
    assert first["execution_status"] == "clarifying"

    second = _invoke(
        graph, "我最近不在家，备注放小区保安那里", thread_id, user_id=owner, include_context=False
    )
    assert second.get("__interrupt__")
    config = {"configurable": {"thread_id": thread_id}}
    interrupted_task = next(
        task for task in graph.get_state(config, subgraphs=True).tasks
        if task.name == "mutation_graph" and task.state is not None
    )
    pending_args = interrupted_task.state.values["context"]["pending_tool"]["args"]
    assert pending_args == {
        "order_id": "ORD-PAID-1",
        "note": "放小区保安那里",
        "request_id": pending_args["request_id"],
    }

    third = _invoke(graph, "确认", thread_id, resume={"action": "confirm"}, user_id=owner)
    assert third["execution_status"] == "completed"
    tool_messages = [m for m in third["messages"] if getattr(m, "type", "") == "tool"]
    assert len([m for m in tool_messages if getattr(m, "name", "") == "update_order_note"]) == 1


def _cancel_model(payload: dict):
    return ToolCallingFakeModel(responses=[
        router_json_message("order", "high_risk", 0.9),
        select_json_message("cancel_order", payload),
    ])


def test_cancel_order_without_id_uses_single_paid_order(monkeypatch, seed):
    _, db_path = seed
    _use_db(monkeypatch, db_path)
    conn = sqlite3.connect(db_path)
    try:
        owner, order_id = conn.execute(
            """
            SELECT o.customer_id, o.order_id FROM orders o
            WHERE o.status='paid'
              AND (SELECT COUNT(*) FROM orders c WHERE c.customer_id=o.customer_id AND c.status='paid') = 1
            LIMIT 1
            """
        ).fetchone()
    finally:
        conn.close()

    graph = build_graph(_cancel_model({}), tools=[], checkpointer=MemorySaver())
    thread_id = "cancel-single"
    out = ainvoke_graph(graph,
        {
            "messages": [HumanMessage(content="取消我的订单")],
            "user_id": owner,
            "session_id": thread_id,
            "context": {},
        },
        {"configurable": {"thread_id": thread_id}},
    )
    assert out.get("__interrupt__")
    interrupt_value = out["__interrupt__"][0].value
    assert order_id in interrupt_value["summary"]


def test_cancel_order_without_id_handles_multiple_and_empty(monkeypatch, seed):
    _, db_path = seed
    _use_db(monkeypatch, db_path)
    graph = build_graph(_cancel_model({}), tools=[], checkpointer=MemorySaver())

    conn = sqlite3.connect(db_path)
    try:
        owner = conn.execute(
            """
            SELECT customer_id FROM orders
            WHERE status='paid'
            GROUP BY customer_id
            HAVING COUNT(*) >= 2
            LIMIT 1
            """
        ).fetchone()[0]
    finally:
        conn.close()

    thread_id = "cancel-multiple"
    out = ainvoke_graph(graph,
        {
            "messages": [HumanMessage(content="取消我的订单")],
            "user_id": owner,
            "session_id": thread_id,
            "context": {},
        },
        {"configurable": {"thread_id": thread_id}},
    )
    assert out["context"]["cancellation_candidates"]
    assert out["context"]["clarify_missing"] == ["order_id"]

    thread_id = "cancel-empty"
    out = ainvoke_graph(graph,
        {
            "messages": [HumanMessage(content="取消我的订单")],
            "user_id": "user-with-no-paid-order",
            "session_id": thread_id,
            "context": {},
        },
        {"configurable": {"thread_id": thread_id}},
    )
    assert out["context"]["cancellation_candidates"] == []
    assert "没有查询到可取消" in out["messages"][-1].content


def test_stale_cancel_candidates_do_not_affect_other_tool_clarify(monkeypatch, seed):
    _, db_path = seed
    _use_db(monkeypatch, db_path)
    model = ToolCallingFakeModel(responses=[
        router_json_message("order", "high_risk", 0.9),
        select_json_message("cancel_order", {}),
    ])
    graph = build_graph(model, tools=[], checkpointer=MemorySaver())
    thread_id = "cancel-stale-candidates"
    config = {"configurable": {"thread_id": thread_id}}

    first = ainvoke_graph(graph,
        {
            "messages": [HumanMessage(content="取消我的订单")],
            "user_id": "user-with-no-paid-order",
            "session_id": thread_id,
            "context": {},
        },
        config,
    )
    assert first["context"]["cancellation_candidates"] == []
    assert "没有查询到可取消" in first["messages"][-1].content

    select_node = make_select_node(
        ToolCallingFakeModel(responses=[select_json_message("refund_apply", {})]),
        "high_risk",
        DEFAULT_TOOL_REGISTRY.for_risk_level("high_risk"),
        DEFAULT_TOOL_REGISTRY,
    )
    selected = asyncio.run(select_node({**first, "messages": [HumanMessage(content="帮我申请退款")]}))
    assert "cancellation_candidates" not in selected["context"]
    assert selected["context"]["clarify_missing"] == ["order_id"]

    clarification_events = []
    monkeypatch.setattr(
        clarify_mod,
        "get_stream_writer",
        lambda: clarification_events.append,
    )
    clarified = clarify_mod.make_clarify_node()(selected)
    assert len(clarification_events) == 1
    clarification = clarification_events[0]
    assert clarification["type"] == "clarification_required"
    assert "candidates" not in clarification["data"]
    assert clarified["messages"][-1].content == clarify_missing_text(selected["context"]["clarify_missing"])


def test_update_address_rejected_after_shipped(seed, monkeypatch):
    """已发货订单不可修改地址（业务规则 address_not_editable）。"""
    from ai_service_agent.graph.workflows.safety import _apply_business_rules

    pending = {"tool": "update_address", "args": {"order_id": "ORD-2026-10031"}}
    facts = {"order_id": "ORD-2026-10031", "user_id": "u0026", "status": "shipped"}
    reason = _apply_business_rules(pending, facts, "u0026")
    assert reason == "address_not_editable"


def test_update_address_allowed_when_paid(seed, monkeypatch):
    from ai_service_agent.graph.workflows.safety import _apply_business_rules

    pending = {"tool": "update_address", "args": {"order_id": "ORD-2026-10031"}}
    facts = {"order_id": "ORD-2026-10031", "user_id": "u0026", "status": "paid"}
    assert _apply_business_rules(pending, facts, "u0026") is None


def test_create_invoice_rejected_when_cancelled(seed, monkeypatch):
    from ai_service_agent.graph.workflows.safety import _apply_business_rules

    pending = {"tool": "create_invoice", "args": {"order_id": "ORD-X", "amount": 10.0}}
    facts = {"order_id": "ORD-X", "user_id": "u1", "status": "cancelled",
             "total_amount": 100.0, "invoiced_amount": 0.0}
    assert _apply_business_rules(pending, facts, "u1") == "invoice_not_allowed"
