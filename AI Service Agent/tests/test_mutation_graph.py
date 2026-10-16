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
from ai_service_agent.tools import DEFAULT_TOOL_REGISTRY, make_tool_registry
from ai_service_agent.tools.kb import kb_search
from ai_service_agent.tools import orders as orders_mod
from ai_service_agent.tools import write as write_mod
from ai_service_agent.domain.orders import SQLiteOrderRepository
from ai_service_agent.graph import builder as builder_mod


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
    monkeypatch.setattr(builder_mod, "SQLiteOrderRepository", lambda path: SQLiteOrderRepository(str(db_path)))


def _mutation_model():
    return ToolCallingFakeModel(responses=[
        router_json_message("account_profile_update", 0.9),
        select_json_message("update_address", {"order_id": "ORD-PAID-1", "address": "北京市朝阳区"}),
    ])


def _confirm_address(order_id: str = "ORD-PAID-1", address: str = "北京市朝阳区") -> dict:
    """Form Resume for update_address: Order Picker order_id plus the address field."""
    return {"action": "confirm", "order_id": order_id, "address": address}


def _invoke(graph, text, thread_id, resume=None, user_id="u1"):
    kwargs = {"configurable": {"thread_id": thread_id}}
    if resume is not None:
        return ainvoke_graph(graph, Command(resume=resume), kwargs)
    return ainvoke_graph(graph,
        {"messages": [HumanMessage(content=text)], "user_id": user_id, "session_id": thread_id, "context": {}},
        kwargs,
    )


def test_mutation_reaches_form_interrupt(seed, monkeypatch):
    _use_db(monkeypatch, seed[1])
    graph = build_graph(_mutation_model(), tools=[kb_search], checkpointer=MemorySaver())
    out = _invoke(graph, "改一下收货地址", "m1")
    pending = out.get("__interrupt__", [])
    assert pending
    assert pending[0].value["tool"] == "update_address"
    assert any(item["order_id"] == "ORD-PAID-1" for item in pending[0].value["order_candidates"])
    assert pending[0].response_schema["properties"]["action"]["enum"] == ["confirm", "cancel"]


def test_mutation_confirm_executes_tool(seed, monkeypatch):
    _use_db(monkeypatch, seed[1])
    graph = build_graph(_mutation_model(), tools=[kb_search], checkpointer=MemorySaver())
    _invoke(graph, "改一下收货地址", "m2")
    out = _invoke(graph, "确认", "m2", resume=_confirm_address())
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
    output = _invoke(graph, "确认", "mutation-failure-1", resume=_confirm_address())

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
    output = _invoke(graph, "确认", "mutation-tool-error-1", resume=_confirm_address())

    assert output["execution_status"] == "failed"
    last = output["messages"][-1].content
    assert "已更新完成" not in last and "已办理完成" not in last
    assert "未成功" in last


def test_mutation_cancel_does_not_execute_tool(seed, monkeypatch):
    _use_db(monkeypatch, seed[1])
    graph = build_graph(_mutation_model(), tools=[kb_search], checkpointer=MemorySaver())
    _invoke(graph, "改一下收货地址", "m3")
    out = _invoke(graph, "取消", "m3", resume={"action": "cancel"})
    assert out["execution_status"] == "completed"
    assert [m for m in out["messages"] if getattr(m, "type", "") == "tool"] == []
    assert "取消" in out["messages"][-1].content


def test_mutation_unknown_tool_rejected_no_interrupt():
    model = ToolCallingFakeModel(responses=[
        router_json_message("order_mutation", 0.9),
        select_json_message("order_set_status", {"order_id": "10086"}),
    ])
    graph = build_graph(model, tools=[kb_search], checkpointer=MemorySaver())
    out = _invoke(graph, "把订单状态改成已发货", "m4")
    assert not out.get("__interrupt__")
    assert out["execution_status"] == "completed"
    assert [m for m in out["messages"] if getattr(m, "type", "") == "tool"] == []


def test_mutation_precheck_rejects_unauthenticated():
    model = ToolCallingFakeModel(responses=[
        router_json_message("order_mutation", 0.9),
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
        router_json_message("order_mutation", 0.9),
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
        router_json_message("account_profile_update", 0.9),
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
        router_json_message("order_mutation", 0.9),
        select_json_message("delete_order_history", {"order_id": "ORD-10086"}),
    ])
    graph = build_graph(model, tools=[kb_search], checkpointer=MemorySaver())
    out = _invoke(graph, "把我在你们这的所有订单记录删掉", "t3-0089")
    assert not out.get("__interrupt__")
    assert out["execution_status"] == "completed"
    assert out["context"].get("precheck_reason") == "unknown_tool"
    assert [m for m in out["messages"] if getattr(m, "type", "") == "tool"] == []
def test_mutation_incomplete_args_open_form_interrupt(seed, monkeypatch):
    """T7: tool 已知但业务字段不全 → Form Interrupt，不再走聊天 clarify → confirm_pause。"""
    _use_db(monkeypatch, seed[1])
    model = ToolCallingFakeModel(responses=[
        router_json_message("account_profile_update", 0.9),
        select_json_message("update_address", {}),
    ])
    graph = build_graph(model, tools=[kb_search], checkpointer=MemorySaver())
    out = _invoke(graph, "我要改个地址", "mt-form-incomplete-1")
    pending = out.get("__interrupt__", [])
    assert pending
    assert pending[0].value["tool"] == "update_address"
    assert "clarify_missing" not in out.get("context", {})
    assert out["execution_status"] != "clarifying"
    # Subgraph interrupt does not merge waiting_confirmation into the parent checkpoint.
    assert out["execution_status"] == "routing"
    assert out["execution_status"] != "waiting_confirmation"
    assert any(item["order_id"] == "ORD-PAID-1" for item in pending[0].value["order_candidates"])


def test_mutation_incomplete_form_resume_confirm_executes(seed, monkeypatch):
    """T7 happy path: 缺参直接 Form Interrupt → Form Resume(Confirm) 写库一次。"""
    _use_db(monkeypatch, seed[1])
    model = ToolCallingFakeModel(responses=[
        router_json_message("account_profile_update", 0.9),
        select_json_message("update_address", {}),
    ])
    graph = build_graph(model, tools=[kb_search], checkpointer=MemorySaver())
    tid = "mt-form-incomplete-2"
    t1 = _invoke(graph, "我要改个地址", tid)
    assert t1.get("__interrupt__")
    assert t1["execution_status"] != "clarifying"
    t2 = _invoke(graph, "确认", tid, resume=_confirm_address())
    assert t2["execution_status"] == "completed"
    tool_msgs = [m for m in t2["messages"] if getattr(m, "type", "") == "tool"]
    assert len(tool_msgs) == 1 and getattr(tool_msgs[0], "name", "") == "update_address"
    assert "收货地址已修改成功" in t2["messages"][-1].content


def _cancel_model(payload: dict):
    return ToolCallingFakeModel(responses=[
        router_json_message("cancel_order_execute", 0.9),
        select_json_message("cancel_order", payload),
    ])


def test_cancel_order_without_id_clarifies_even_with_single_paid_order(monkeypatch, seed):
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
    value = out["__interrupt__"][0].value
    assert value.get("tool") == "cancel_order"
    assert value.get("type") != "approval_required"
    assert {item["order_id"] for item in value["order_candidates"]} == {order_id}
    assert not (value.get("prefills") or {}).get("order_id")


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
    assert out.get("__interrupt__")
    value = out["__interrupt__"][0].value
    assert value.get("tool") == "cancel_order"
    assert len(value["order_candidates"]) >= 2

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
    assert out["context"].get("cancellation_candidates", []) == []
    assert "订单号" in out["messages"][-1].content


def test_stale_cancel_candidates_do_not_affect_other_tool_clarify(monkeypatch, seed):
    _, db_path = seed
    _use_db(monkeypatch, db_path)
    model = ToolCallingFakeModel(responses=[
        router_json_message("refund_apply_execute", 0.9),
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
    assert first["context"].get("cancellation_candidates", []) == []
    assert "订单号" in first["messages"][-1].content

    high_risk_reg = make_tool_registry(MagicMock())
    select_node = make_select_node(
        ToolCallingFakeModel(responses=[select_json_message("refund_apply", {})]),
        "high_risk",
        high_risk_reg.for_risk_level("high_risk"),
        high_risk_reg,
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


def test_mutation_graph_declares_graph_context_schema():
    """MutationGraph must declare context_schema=GraphContext (same as HighRiskGraph)."""
    from ai_service_agent.graph.runtime_context import GraphContext
    from ai_service_agent.graph.workflows.mutation_agent import make_mutation_graph

    graph = make_mutation_graph(
        ToolCallingFakeModel(responses=[]),
        DEFAULT_TOOL_REGISTRY.for_risk_level("mutation"),
        DEFAULT_TOOL_REGISTRY,
    )
    assert graph.context_schema is GraphContext


def test_event_stream_prefers_runtime_order_repository(seed, monkeypatch):
    """Runtime GraphContext.order_repository must win over build-time closure."""
    from ai_service_agent.graph.runtime_context import GraphContext
    from ai_service_agent.harness.streaming import event_stream

    class RuntimeOnlyRepo(SQLiteOrderRepository):
        def list_order_picker_candidates(self, user_id: str, tool: str) -> list[dict]:
            return [
                {"order_id": "ORD-FROM-RUNTIME", "status": "paid", "product_summary": "runtime"},
            ]

    _use_db(monkeypatch, seed[1])
    closure_repo = SQLiteOrderRepository(str(seed[1]))
    runtime_repo = RuntimeOnlyRepo(str(seed[1]))
    graph = build_graph(
        _mutation_model(),
        tools=[kb_search],
        checkpointer=MemorySaver(),
        order_repository=closure_repo,
    )
    events = asyncio.run(
        _acollect_events(
            event_stream(
                graph,
                {"configurable": {"thread_id": "runtime-ctx-1"}},
                [HumanMessage(content="改一下收货地址")],
                extra_state={"user_id": "u1", "session_id": "runtime-ctx-1"},
                context=GraphContext(order_repository=runtime_repo, user_id="u1"),
            )
        )
    )
    form = next(event for event in events if event["type"] == "form_required")
    candidates = [item["order_id"] for item in form["data"]["order_candidates"]]
    assert candidates == ["ORD-FROM-RUNTIME"]
    assert "ORD-PAID-1" not in candidates


async def _acollect_events(gen):
    return [event async for event in gen]
