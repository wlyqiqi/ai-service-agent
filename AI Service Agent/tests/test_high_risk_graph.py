import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from langchain_core.messages import HumanMessage

from ai_service_agent.graph.builder import build_graph
from ai_service_agent.harness.streaming import event_stream
from conftest import ainvoke_graph
from ai_service_agent.testing import ToolCallingFakeModel, router_json_message, select_json_message
from ai_service_agent.tools.kb import kb_search
from langgraph.graph import END, START, StateGraph

from ai_service_agent.graph.state import CustomerServiceState
from ai_service_agent.graph.workflows import high_risk_agent as high_risk_agent_mod
from ai_service_agent.graph.workflows import safety as safety_mod
from ai_service_agent.graph.workflows.safety import _apply_business_rules, _check, make_precheck_fetch_node, make_recheck_node, make_safety_check_node
from ai_service_agent.tools import DEFAULT_TOOL_REGISTRY
from ai_service_agent.tools import write as write_mod
from ai_service_agent.graph.workflows.copy import RUNTIME_ERROR_TEXT


def _hr_model():
    return ToolCallingFakeModel(responses=[
        router_json_message("order", "high_risk", 0.9),
        select_json_message("refund_apply", {"order_id": "10086"}),
    ])


def _invoke(graph, text, thread_id, resume=None):
    kwargs = {"configurable": {"thread_id": thread_id}}
    if resume is not None:
        return ainvoke_graph(graph, Command(resume=resume), kwargs)
    return ainvoke_graph(graph,
        {"messages": [HumanMessage(content=text)], "user_id": "u1", "session_id": thread_id, "context": {}},
        kwargs,
    )


def test_high_risk_reaches_approval_interrupt():
    graph = build_graph(_hr_model(), tools=[kb_search], checkpointer=MemorySaver())
    out = _invoke(graph, "申请退款", "h1")
    values = [it.value for it in out.get("__interrupt__", [])]
    assert any(v.get("type") == "approval_required" and v.get("approval_id") and v.get("expires_at") for v in values)


def test_high_risk_approve_executes_tool(monkeypatch, tmp_path):
    _, db_path = _load_refund_seed(tmp_path, "approve-executes")
    _use_cancel_db(monkeypatch, db_path)
    conn = sqlite3.connect(db_path)
    try:
        owner, order_id = conn.execute(
            "SELECT customer_id, order_id FROM orders WHERE status='paid' LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    model = ToolCallingFakeModel(responses=[
        router_json_message("order", "high_risk", 0.9),
        select_json_message("refund_apply", {"order_id": order_id, "amount": 1.0}),
    ])
    graph = build_graph(model, tools=[kb_search], checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "h2"}}
    ainvoke_graph(graph,
        {
            "messages": [HumanMessage(content="申请退款")],
            "user_id": owner,
            "session_id": "h2",
            "context": {},
        },
        config,
    )
    out = ainvoke_graph(graph, Command(resume={"action": "approve"}), config)
    assert out["execution_status"] == "completed"
    tool_msgs = [m for m in out["messages"] if getattr(m, "type", "") == "tool"]
    assert tool_msgs and getattr(tool_msgs[-1], "name", "") == "refund_apply"


def test_high_risk_tool_exception_uses_local_finalize_failed(monkeypatch, tmp_path):
    _, db_path = _load_refund_seed(tmp_path, "highrisk-tool-failure")
    _use_cancel_db(monkeypatch, db_path)
    conn = sqlite3.connect(db_path)
    try:
        owner, order_id = conn.execute(
            "SELECT customer_id, order_id FROM orders WHERE status='paid' LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    monkeypatch.setattr(
        type(write_mod.refund_apply),
        "invoke",
        MagicMock(side_effect=RuntimeError("database unavailable")),
    )
    model = ToolCallingFakeModel(responses=[
        router_json_message("order", "high_risk", 0.9),
        select_json_message("refund_apply", {"order_id": order_id, "amount": 1.0}),
    ])
    graph = build_graph(model, tools=[kb_search], checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "highrisk-failure-1"}}
    ainvoke_graph(graph, {
        "messages": [HumanMessage(content="申请退款")],
        "user_id": owner,
        "session_id": "highrisk-failure-1",
        "context": {},
    }, config)
    output = ainvoke_graph(graph, Command(resume={"action": "approve"}), config)

    assert output["execution_status"] == "failed"
    assert output["messages"][-1].content == RUNTIME_ERROR_TEXT
    assert "已提交成功" not in output["messages"][-1].content


def test_high_risk_reject_no_tool():
    graph = build_graph(_hr_model(), tools=[kb_search], checkpointer=MemorySaver())
    _invoke(graph, "申请退款", "h3")
    out = _invoke(graph, "拒绝", "h3", resume={"action": "reject", "note": "资料不足"})
    assert out["execution_status"] == "completed"
    assert [m for m in out["messages"] if getattr(m, "type", "") == "tool"] == []
    assert "未通过" in out["messages"][-1].content


def test_high_risk_expired_no_tool():
    graph = build_graph(_hr_model(), tools=[kb_search], checkpointer=MemorySaver())
    _invoke(graph, "申请退款", "h4")
    out = _invoke(graph, "过期", "h4", resume={"result": "expired"})
    assert out["execution_status"] == "completed"
    assert [m for m in out["messages"] if getattr(m, "type", "") == "tool"] == []
    assert "过期" in out["messages"][-1].content


def test_high_risk_unknown_tool_rejected():
    model = ToolCallingFakeModel(responses=[
        router_json_message("order", "high_risk", 0.9),
        select_json_message("order_set_status", {"order_id": "10086"}),
    ])
    graph = build_graph(model, tools=[kb_search], checkpointer=MemorySaver())
    out = _invoke(graph, "改状态", "h5")
    assert not out.get("__interrupt__")
    assert out["execution_status"] == "completed"


# ---- D2（P4）必填标识符非空：预检层兜底（select 之外来源的 pending_tool；safety-spec §1.2）----

def _safety_mini_graph(node):
    b = StateGraph(CustomerServiceState)
    b.add_node("n", node)
    b.add_edge(START, "n")
    b.add_edge("n", END)
    return b.compile()


def test_check_rule_required_identifier_nonempty():
    """纯函数 _check：空必填标识符 -> invalid_args；非空放行；request_id 豁免。"""
    assert _check({"tool": "refund_apply", "args": {"request_id": "r1", "order_id": ""}}, "high_risk", DEFAULT_TOOL_REGISTRY, "u1") == "invalid_args"
    assert _check({"tool": "refund_apply", "args": {"request_id": "r1", "order_id": "10086"}}, "high_risk", DEFAULT_TOOL_REGISTRY, "u1") is None
    assert _check({"tool": "refund_apply", "args": {"request_id": "", "order_id": "10086"}}, "high_risk", DEFAULT_TOOL_REGISTRY, "u1") is None


def test_safety_check_node_empty_required_backstop():
    """safety_check 节点（图上下文）：空必填标识符 -> invalid_args 拒绝，无 interrupt。"""
    graph = _safety_mini_graph(make_safety_check_node("high_risk", DEFAULT_TOOL_REGISTRY))
    out = ainvoke_graph(graph, {
        "messages": [],
        "user_id": "u1",
        "session_id": "d2-h1",
        "context": {"pending_tool": {"tool": "refund_apply", "args": {"request_id": "r1", "order_id": ""}, "request_id": "r1"}},
    })
    assert out["context"]["precheck_reason"] == "invalid_args"
    assert not out.get("__interrupt__")
    assert out["execution_status"] == "completed"


def test_recheck_node_empty_required_backstop():
    """recheck（resume 路径）同样兜底：空必填标识符 -> recheck_reason=invalid_args。"""
    graph = _safety_mini_graph(make_recheck_node("high_risk", DEFAULT_TOOL_REGISTRY))
    out = ainvoke_graph(graph, {
        "messages": [],
        "user_id": "u1",
        "session_id": "d2-h2",
        "context": {"pending_tool": {"tool": "refund_apply", "args": {"request_id": "r1", "order_id": ""}, "request_id": "r1"}},
    })
    assert out["context"]["recheck_reason"] == "invalid_args"
    assert out["execution_status"] == "completed"


# ---- D3（P4 Task 3）：订单状态 stub + precheck_fetch + 业务规则（safety-spec §1.2）----

def _hr_graph(model):
    return build_graph(model, tools=[kb_search], checkpointer=MemorySaver())


def test_precheck_fetch_writes_facts_only():
    """precheck_fetch：写 context[precheck_data] 仅事实、无裁决；无 order_id 不写。"""
    node = make_precheck_fetch_node()
    out = asyncio.run(node({"context": {"pending_tool": {"tool": "refund_apply", "args": {"order_id": "ORD-10086"}}}}))
    d = out["context"]["precheck_data"]
    assert d["status"] == "signed" and d["user_id"] == "u1" and d["order_id"] == "ORD-10086"
    assert "precheck_reason" not in out["context"]
    out2 = asyncio.run(node({"context": {"pending_tool": {"tool": "update_address", "args": {"address": "x"}}}}))
    assert "precheck_data" not in out2.get("context", {})  # 无 order_id -> 空更新


def test_high_risk_0086_refund_window_expired():
    """0086：refund_apply + ORD-10086（签收 10 天前，超 7 天窗口）-> 预检拒绝，不进入审批。"""
    model = ToolCallingFakeModel(responses=[
        router_json_message("order", "high_risk", 0.9),
        select_json_message("refund_apply", {"order_id": "ORD-10086"}),
    ])
    out = _invoke(_hr_graph(model), "我签收已经 10 天了，就要无理由退货", "t3-0086")
    assert not out.get("__interrupt__")
    assert out["execution_status"] == "completed"
    assert out["context"].get("precheck_reason") == "refund_window_expired"
    assert [m for m in out["messages"] if getattr(m, "type", "") == "tool"] == []


def test_high_risk_0090_cancel_already_cancelled():
    """0090：cancel_order + ORD-CANCEL-1（已取消）-> 预检拒绝。"""
    model = ToolCallingFakeModel(responses=[
        router_json_message("order", "high_risk", 0.9),
        select_json_message("cancel_order", {"order_id": "ORD-CANCEL-1"}),
    ])
    out = _invoke(_hr_graph(model), "这个订单已经取消了，再帮我取消一次", "t3-0090")
    assert not out.get("__interrupt__")
    assert out["execution_status"] == "completed"
    assert out["context"].get("precheck_reason") == "order_already_cancelled"
    assert [m for m in out["messages"] if getattr(m, "type", "") == "tool"] == []


def test_precheck_cancel_order_already_cancelled():
    """预检业务规则（D3）：cancel_order + 已取消订单状态 -> order_already_cancelled。"""
    from ai_service_agent.graph.workflows.safety import _apply_business_rules
    reason = _apply_business_rules(
        {"tool": "cancel_order", "args": {"order_id": "ORD-CANCEL-1"}},
        {"order_id": "ORD-CANCEL-1", "status": "cancelled"},
    )
    assert reason == "order_already_cancelled"


def test_cancel_precheck_status_machine():
    """cancel_order 预检：仅 paid 可进入审批；未找到、已取消、其他状态均拒绝。"""
    tool = {"tool": "cancel_order", "args": {"order_id": "ORD-X", "request_id": "r"}}
    assert _apply_business_rules(tool, {"order_id": "ORD-X", "status": "not_found"}, "u1") == "order_not_found"
    assert _apply_business_rules(tool, {"order_id": "ORD-X", "status": "paid"}, "u1") is None
    assert _apply_business_rules(tool, {"order_id": "ORD-X", "status": "cancelled"}, "u1") == "order_already_cancelled"
    assert _apply_business_rules(tool, {"order_id": "ORD-X", "status": "shipped"}, "u1") == "cancel_not_allowed"
    assert _apply_business_rules(
        tool, {"order_id": "ORD-X", "user_id": "u2", "status": "paid"}, "u1"
    ) == "data_boundary"


def test_cancel_precheck_distinguishes_db_unavailable(monkeypatch):
    monkeypatch.setattr(
        safety_mod,
        "lookup_order_state_with_availability",
        lambda order_id: (None, False),
    )
    out = ainvoke_graph(
        _safety_mini_graph(make_precheck_fetch_node()),
        {
            "messages": [],
            "user_id": "u1",
            "session_id": "cancel-datasource",
            "context": {
                "pending_tool": {
                    "tool": "cancel_order",
                    "args": {"order_id": "ORD-X", "request_id": "r"},
                }
            }
        },
        {"configurable": {"thread_id": "cancel-datasource"}},
    )
    assert out["context"]["precheck_data"] == {
        "order_id": "ORD-X",
        "status": "data_source_unavailable",
    }
    assert _apply_business_rules(
        out["context"]["pending_tool"],
        out["context"]["precheck_data"],
        "u1",
    ) == "data_source_unavailable"


def _load_refund_seed(tmp_path, label):
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        f"seed_mock_db_refund_precheck_{label}", root / "scripts" / "seed_mock_db.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    db_path = tmp_path / f"refund-precheck-{label}.db"
    module.build_database(str(db_path))
    return module, db_path


def test_refund_facts_include_occupied_refund_total(monkeypatch, tmp_path):
    """precheck_fetch 事实层包含 refunding/refunded 已占用退款金额。"""
    _, db_path = _load_refund_seed(tmp_path, "occupied-facts")
    _use_cancel_db(monkeypatch, db_path)
    from ai_service_agent.tools.orders import lookup_order_state_with_availability

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT o.order_id, COALESCE(SUM(r.amount), 0)
            FROM orders o LEFT JOIN refunds r
              ON r.order_id = o.order_id AND r.status IN ('refunding', 'refunded')
            WHERE o.status IN ('refunding', 'refunded')
            GROUP BY o.order_id
            """
        ).fetchall()
    finally:
        conn.close()

    assert rows
    for order_id, occupied in rows:
        facts, available = lookup_order_state_with_availability(order_id)
        assert available is True
        assert facts["occupied_refund_amount"] == occupied


def test_refund_precheck_rejects_wrong_status(monkeypatch, tmp_path):
    """refund_apply on shipped order -> refund_not_allowed (not allowed statuses)."""
    _, db_path = _load_refund_seed(tmp_path, "wrong-status")
    _use_cancel_db(monkeypatch, db_path)

    conn = sqlite3.connect(db_path)
    try:
        owner, order_id = conn.execute(
            "SELECT customer_id, order_id FROM orders WHERE status='shipped' LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    model = ToolCallingFakeModel(responses=[
        router_json_message("order", "high_risk", 0.9),
        select_json_message("refund_apply", {"order_id": order_id, "amount": 1.0}),
    ])
    graph = build_graph(model, tools=[], checkpointer=MemorySaver())
    thread_id = "refund-precheck-wrong-status"
    out = ainvoke_graph(graph,
        {
            "messages": [HumanMessage(content="申请退款")],
            "user_id": owner,
            "session_id": thread_id,
            "context": {},
        },
        {"configurable": {"thread_id": thread_id}},
    )

    assert [it for it in out.get("__interrupt__", []) if it.value.get("type") == "approval_required"] == []
    assert out["execution_status"] == "completed"
    assert out["context"]["precheck_reason"] == "refund_not_allowed"
    assert [m for m in out["messages"] if getattr(m, "type", "") == "tool"] == []


def test_refund_precheck_rejects_over_amount(monkeypatch, tmp_path):
    """refund_apply amount > total - occupied -> refund_amount_exceeds."""
    _, db_path = _load_refund_seed(tmp_path, "over-amount")
    _use_cancel_db(monkeypatch, db_path)

    conn = sqlite3.connect(db_path)
    try:
        owner, order_id, total_amount = conn.execute(
            "SELECT customer_id, order_id, total_amount FROM orders WHERE status='paid' LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    model = ToolCallingFakeModel(responses=[
        router_json_message("order", "high_risk", 0.9),
        select_json_message(
            "refund_apply", {"order_id": order_id, "amount": float(total_amount) + 99999.0}
        ),
    ])
    graph = build_graph(model, tools=[], checkpointer=MemorySaver())
    thread_id = "refund-precheck-over-amount"
    out = ainvoke_graph(graph,
        {
            "messages": [HumanMessage(content="申请退款")],
            "user_id": owner,
            "session_id": thread_id,
            "context": {},
        },
        {"configurable": {"thread_id": thread_id}},
    )

    assert [it for it in out.get("__interrupt__", []) if it.value.get("type") == "approval_required"] == []
    assert out["execution_status"] == "completed"
    assert out["context"]["precheck_reason"] == "refund_amount_exceeds"
    assert [m for m in out["messages"] if getattr(m, "type", "") == "tool"] == []


def test_refund_precheck_rejects_completed_order(monkeypatch, tmp_path):
    """refund_apply on a completed order is rejected before approval."""
    _, db_path = _load_refund_seed(tmp_path, "completed-allowed")
    _use_cancel_db(monkeypatch, db_path)

    conn = sqlite3.connect(db_path)
    try:
        owner, order_id = conn.execute(
            "SELECT customer_id, order_id FROM orders"
            " WHERE status='completed' ORDER BY signed_at DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    model = ToolCallingFakeModel(responses=[
        router_json_message("order", "high_risk", 0.9),
        select_json_message("refund_apply", {"order_id": order_id, "amount": 1.0}),
    ])
    graph = build_graph(model, tools=[], checkpointer=MemorySaver())
    thread_id = "refund-precheck-completed"
    out = ainvoke_graph(graph,
        {
            "messages": [HumanMessage(content="申请退款")],
            "user_id": owner,
            "session_id": thread_id,
            "context": {},
        },
        {"configurable": {"thread_id": thread_id}},
    )

    assert out["context"].get("precheck_reason") == "refund_not_allowed"
    assert [it for it in out.get("__interrupt__", []) if it.value.get("type") == "approval_required"] == []


def test_refund_order_approval_summary_contains_amount(monkeypatch, tmp_path):
    """Approval card shows refund amount and order facts."""
    _, db_path = _load_refund_seed(tmp_path, "approval-summary")
    _use_cancel_db(monkeypatch, db_path)

    conn = sqlite3.connect(db_path)
    try:
        owner, order_id, total_amount = conn.execute(
            "SELECT customer_id, order_id, total_amount FROM orders WHERE status='paid' LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    model = ToolCallingFakeModel(responses=[
        router_json_message("order", "high_risk", 0.9),
        select_json_message("refund_apply", {"order_id": order_id, "amount": 25.5}),
    ])
    graph = build_graph(model, tools=[], checkpointer=MemorySaver())
    thread_id = "refund-approval-summary"
    out = ainvoke_graph(graph,
        {
            "messages": [HumanMessage(content="申请退款")],
            "user_id": owner,
            "session_id": thread_id,
            "context": {},
        },
        {"configurable": {"thread_id": thread_id}},
    )

    approvals = [
        it.value for it in out.get("__interrupt__", [])
        if it.value.get("type") == "approval_required"
    ]
    assert len(approvals) == 1
    summary = approvals[0]["summary"]
    assert "退款申请" in summary
    assert order_id in summary
    assert "退款金额 25.5" in summary
    assert f"订单总额 {total_amount}" in summary
    assert "已占用退款" in summary
    assert "申请人" in summary


def test_refund_missing_order_id_auto_resolves_single(monkeypatch, tmp_path):
    """Single refundable order auto-selects and reaches refund approval."""
    _, db_path = _load_refund_seed(tmp_path, "single-refundable")
    _use_cancel_db(monkeypatch, db_path)

    conn = sqlite3.connect(db_path)
    try:
        owner, order_id = conn.execute(
            """
            SELECT customer_id, order_id FROM orders o
            WHERE o.status IN ('paid', 'completed')
              AND (SELECT COUNT(*) FROM orders c
                   WHERE c.customer_id=o.customer_id
                     AND c.status IN ('paid', 'completed')) = 1
            LIMIT 1
            """
        ).fetchone()
    finally:
        conn.close()

    model = ToolCallingFakeModel(responses=[
        router_json_message("order", "high_risk", 0.9),
        select_json_message("refund_apply", {"amount": 10.0}),
    ])
    graph = build_graph(model, tools=[], checkpointer=MemorySaver())
    thread_id = "refund-single-auto-resolve"
    out = ainvoke_graph(graph,
        {
            "messages": [HumanMessage(content="申请退款")],
            "user_id": owner,
            "session_id": thread_id,
            "context": {},
        },
        {"configurable": {"thread_id": thread_id}},
    )

    assert out.get("__interrupt__")
    assert order_id in out["__interrupt__"][0].value["summary"]


def test_refund_missing_order_id_clarifies_multiple(monkeypatch, tmp_path):
    """Multiple refundable orders ask the user to choose one."""
    _, db_path = _load_refund_seed(tmp_path, "multiple-refundable")
    _use_cancel_db(monkeypatch, db_path)

    conn = sqlite3.connect(db_path)
    try:
        owner, order_ids = conn.execute(
            """
            SELECT customer_id, GROUP_CONCAT(order_id) FROM orders
            WHERE status IN ('paid', 'completed')
            GROUP BY customer_id
            HAVING COUNT(*) >= 2
            LIMIT 1
            """
        ).fetchone()
    finally:
        conn.close()

    model = ToolCallingFakeModel(responses=[
        router_json_message("order", "high_risk", 0.9),
        select_json_message("refund_apply", {}),
    ])
    graph = build_graph(model, tools=[], checkpointer=MemorySaver())
    thread_id = "refund-multiple-clarify"
    out = ainvoke_graph(graph,
        {
            "messages": [HumanMessage(content="申请退款")],
            "user_id": owner,
            "session_id": thread_id,
            "context": {},
        },
        {"configurable": {"thread_id": thread_id}},
    )

    assert out["context"]["clarify_missing"] == ["order_id"]
    assert {item["order_id"] for item in out["context"]["cancellation_candidates"]} <= set(order_ids.split(","))
    assert "多笔符合条件的订单" in out["messages"][-1].content


def test_high_risk_refund_unknown_order_still_approval():
    """规则不误伤：订单不在种子库 -> 无事实 -> 正常进入审批。"""
    model = ToolCallingFakeModel(responses=[
        router_json_message("order", "high_risk", 0.9),
        select_json_message("refund_apply", {"order_id": "ORD-UNKNOWN"}),
    ])
    out = _invoke(_hr_graph(model), "申请退款", "t3-pos")
    values = [it.value for it in out.get("__interrupt__", [])]
    assert any(v.get("type") == "approval_required" for v in values)


def test_cancel_order_approval_summary_contains_order_facts(monkeypatch, tmp_path):
    import importlib.util
    import sqlite3
    from pathlib import Path

    from ai_service_agent.tools import orders as orders_mod

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "seed_mock_db_cancel_summary", root / "scripts" / "seed_mock_db.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    db_path = tmp_path / "cancel-summary.db"
    module.build_database(str(db_path))

    class _Settings:
        mock_ecommerce_db_path = str(db_path)

    monkeypatch.setattr(orders_mod, "get_settings", lambda: _Settings())
    conn = sqlite3.connect(db_path)
    try:
        owner, order_id, amount = conn.execute(
            "SELECT customer_id, order_id, total_amount FROM orders WHERE status='paid' LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    model = ToolCallingFakeModel(responses=[
        router_json_message("order", "high_risk", 0.9),
        select_json_message("cancel_order", {"order_id": order_id}),
    ])
    graph = build_graph(model, tools=[], checkpointer=MemorySaver())
    thread_id = "cancel-summary"
    out = ainvoke_graph(graph,
        {
            "messages": [HumanMessage(content="取消这个订单")],
            "user_id": owner,
            "session_id": thread_id,
            "context": {},
        },
        {"configurable": {"thread_id": thread_id}},
    )
    summary = next(
        it.value["summary"] for it in out.get("__interrupt__", [])
        if it.value.get("type") == "approval_required"
    )
    assert summary == (
        f"取消订单 {order_id}｜"
        f"金额 {amount}｜"
        f"商品：{orders_mod.order_items_summary(order_id)}｜"
        f"申请人 {owner}｜"
        f"状态 已付款"
    )


def test_cancel_approval_fails_closed_without_valid_paid_facts(monkeypatch, tmp_path):
    import importlib.util
    import sqlite3
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "seed_mock_db_cancel_fail_closed", root / "scripts" / "seed_mock_db.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    db_path = tmp_path / "cancel-fail-closed.db"
    module.build_database(str(db_path))
    _use_cancel_db(monkeypatch, db_path)

    conn = sqlite3.connect(db_path)
    try:
        owner, order_id = conn.execute(
            "SELECT customer_id, order_id FROM orders WHERE status='paid' LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    def missing_precheck_fetch():
        return lambda state: {}

    def passing_safety_check(tier, registry):
        return lambda state: {
            "context": {**state.get("context", {}), "precheck": "pass"}
        }

    monkeypatch.setattr(high_risk_agent_mod, "make_precheck_fetch_node", missing_precheck_fetch)
    monkeypatch.setattr(high_risk_agent_mod, "make_safety_check_node", passing_safety_check)

    model = ToolCallingFakeModel(responses=[
        router_json_message("order", "high_risk", 0.9),
        select_json_message("cancel_order", {"order_id": order_id}),
    ])
    graph = build_graph(model, tools=[], checkpointer=MemorySaver())
    thread_id = "cancel-fail-closed"
    out = ainvoke_graph(graph,
        {
            "messages": [HumanMessage(content="取消这个订单")],
            "user_id": owner,
            "session_id": thread_id,
            "context": {},
        },
        {"configurable": {"thread_id": thread_id}},
    )

    approvals = [
        it for it in out.get("__interrupt__", [])
        if it.value.get("type") == "approval_required"
    ]
    assert approvals == []
    assert out["context"]["precheck_reason"] == "cancel_order_facts_unavailable"
    assert "未能通过安全校验" in out["messages"][-1].content


def _use_cancel_db(monkeypatch, db_path):
    from ai_service_agent.tools import orders as orders_mod
    from ai_service_agent.tools import write as write_mod

    class _Settings:
        mock_ecommerce_db_path = str(db_path)

    monkeypatch.setattr(orders_mod, "get_settings", lambda: _Settings())
    monkeypatch.setattr(write_mod, "get_settings", lambda: _Settings())


def _db_snapshot(conn):
    conn.row_factory = sqlite3.Row
    orders = [dict(row) for row in conn.execute("SELECT * FROM orders ORDER BY order_id")]
    refunds = [dict(row) for row in conn.execute("SELECT * FROM refunds ORDER BY refund_id")]
    idempotency = [
        dict(row)
        for row in conn.execute("SELECT * FROM idempotency ORDER BY tool_name, request_id")
    ]
    return {"orders": orders, "refunds": refunds, "idempotency": idempotency}


def _load_cancel_seed(tmp_path, label):
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        f"seed_mock_db_cancel_graph_{label}", root / "scripts" / "seed_mock_db.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    db_path = tmp_path / f"cancel-graph-{label}.db"
    module.build_database(str(db_path))
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        paid_order = conn.execute(
            "SELECT * FROM orders WHERE status='paid' ORDER BY order_id LIMIT 1"
        ).fetchone()
        before = _db_snapshot(conn)
    finally:
        conn.close()
    return module, db_path, paid_order["customer_id"], paid_order["order_id"], before


def _cancel_graph(order_id, request_id):
    model = ToolCallingFakeModel(responses=[
        router_json_message("order", "high_risk", 0.9),
        select_json_message("cancel_order", {"order_id": order_id, "request_id": request_id}),
    ])
    return build_graph(model, tools=[], checkpointer=MemorySaver())


def _refund_graph(order_id, request_id, amount=1.0):
    model = ToolCallingFakeModel(responses=[
        router_json_message("order", "high_risk", 0.9),
        select_json_message(
            "refund_apply", {"order_id": order_id, "amount": amount, "request_id": request_id}
        ),
    ])
    return build_graph(model, tools=[], checkpointer=MemorySaver())


def _invoke_refund(graph, owner, thread_id):
    return ainvoke_graph(graph,
        {
            "messages": [HumanMessage(content="申请退款")],
            "user_id": owner,
            "session_id": thread_id,
            "context": {},
        },
        {"configurable": {"thread_id": thread_id}},
    )


def _approval_required(value):
    return value.get("type") == "approval_required"


def _invoke_cancel(graph, owner, thread_id):
    return ainvoke_graph(graph,
        {
            "messages": [HumanMessage(content="取消这个订单")],
            "user_id": owner,
            "session_id": thread_id,
            "context": {},
        },
        {"configurable": {"thread_id": thread_id}},
    )


def test_cancel_order_approved_writes_db_once(monkeypatch, tmp_path):
    from ai_service_agent.tools.write import cancel_order

    _, db_path, owner, order_id, before = _load_cancel_seed(tmp_path, "approved")
    _use_cancel_db(monkeypatch, db_path)
    graph = _cancel_graph(order_id, "cancel-approved")
    thread_id = "cancel-approved"
    request_id = "cancel-approved"

    out = _invoke_cancel(graph, owner, thread_id)
    approval_values = [
        it.value for it in out.get("__interrupt__", [])
        if it.value.get("type") == "approval_required"
    ]
    assert len(approval_values) == 1
    assert approval_values[0]["approval_id"]

    events = asyncio.run(_collect_events(event_stream(
        graph,
        {"configurable": {"thread_id": thread_id}},
        messages=[HumanMessage(content="审批通过")],
        resume_payload={"action": "approve"},
    )))
    tool_results = [event for event in events if event["type"] == "tool_result"]
    assert [(result["data"]["name"], result["data"]["ok"]) for result in tool_results] == [
        ("cancel_order", True)
    ]

    out = graph.get_state({"configurable": {"thread_id": thread_id}}).values
    assert out["execution_status"] == "completed"
    tool_msgs = [m for m in out["messages"] if getattr(m, "type", "") == "tool"]
    assert [getattr(m, "name", "") for m in tool_msgs] == ["cancel_order"]

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        order = conn.execute(
            "SELECT * FROM orders WHERE order_id=?", (order_id,)
        ).fetchone()
        cancel_rows = conn.execute(
            "SELECT * FROM idempotency WHERE tool_name='cancel_order'"
        ).fetchall()
    finally:
        conn.close()

    expected_cancelled_at = order["cancelled_at"]
    assert order["status"] == "cancelled"
    assert expected_cancelled_at
    assert len(cancel_rows) == 1
    replay = cancel_order.invoke(
        {"order_id": order_id, "request_id": request_id, "user_id": owner}
    )
    assert replay == {
        "ok": True,
        "replayed": True,
        "result": {
            "field": "cancel_order",
            "order_id": order_id,
            "status": "cancelled",
            "cancelled_at": expected_cancelled_at,
            "reason": None,
        },
    }

    conn = sqlite3.connect(db_path)
    try:
        after = _db_snapshot(conn)
    finally:
        conn.close()
    assert len(after["idempotency"]) == len(before["idempotency"]) + 1
    assert after["orders"] == [
        {**row, "status": "cancelled", "cancelled_at": expected_cancelled_at}
        if row["order_id"] == order_id else row
        for row in before["orders"]
    ]


def test_refund_order_approved_writes_db_once(monkeypatch, tmp_path):
    from ai_service_agent.tools.write import refund_apply

    _, db_path, owner, order_id, before = _load_cancel_seed(tmp_path, "refund-approved")
    _use_cancel_db(monkeypatch, db_path)
    graph = _refund_graph(order_id, "refund-approved")
    thread_id = "refund-approved"
    request_id = "refund-approved"

    out = _invoke_refund(graph, owner, thread_id)
    approvals = [
        it.value for it in out.get("__interrupt__", []) if _approval_required(it.value)
    ]
    assert len(approvals) == 1
    assert approvals[0]["approval_id"]

    events = asyncio.run(_collect_events(event_stream(
        graph,
        {"configurable": {"thread_id": thread_id}},
        messages=[HumanMessage(content="审批通过")],
        resume_payload={"action": "approve"},
    )))
    tool_results = [event for event in events if event["type"] == "tool_result"]
    assert [(result["data"]["name"], result["data"]["ok"]) for result in tool_results] == [
        ("refund_apply", True)
    ]

    out = graph.get_state({"configurable": {"thread_id": thread_id}}).values
    assert out["execution_status"] == "completed"
    tool_msgs = [m for m in out["messages"] if getattr(m, "type", "") == "tool"]
    assert [getattr(m, "name", "") for m in tool_msgs] == ["refund_apply"]

    conn = sqlite3.connect(db_path)
    try:
        status = conn.execute(
            "SELECT status FROM orders WHERE order_id=?", (order_id,)
        ).fetchone()[0]
        refund = conn.execute(
            "SELECT amount, status FROM refunds WHERE order_id=?", (order_id,)
        ).fetchone()
        idem = conn.execute(
            "SELECT tool_name, request_id FROM idempotency WHERE tool_name='refund_apply'"
        ).fetchall()
    finally:
        conn.close()
    assert status == "refunding"
    assert refund == (1.0, "refunding")
    assert idem == [("refund_apply", request_id)]

    replay = refund_apply.invoke(
        {"order_id": order_id, "amount": 1.0, "request_id": request_id, "user_id": owner}
    )
    assert replay["replayed"] is True
    conn = sqlite3.connect(db_path)
    try:
        after = _db_snapshot(conn)
    finally:
        conn.close()
    assert len(after["refunds"]) == len(before["refunds"]) + 1
    assert len(after["idempotency"]) == len(before["idempotency"]) + 1
    assert next(row for row in after["orders"] if row["order_id"] == order_id)["status"] == "refunding"


def test_signed_refund_approval_path_writes_db_once(monkeypatch, tmp_path):
    _, db_path, owner, order_id, before = _load_cancel_seed(tmp_path, "signed-refund-approved")
    _use_cancel_db(monkeypatch, db_path)
    signed_at = datetime.now(timezone.utc) - timedelta(days=2)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE orders SET status='signed', signed_at=? WHERE order_id=?",
            (signed_at.isoformat(), order_id),
        )
        conn.commit()
    finally:
        conn.close()

    graph = _refund_graph(order_id, "signed-refund-approved")
    thread_id = "signed-refund-approved"
    request_id = "signed-refund-approved"

    out = _invoke_refund(graph, owner, thread_id)
    approvals = [
        it.value for it in out.get("__interrupt__", []) if _approval_required(it.value)
    ]
    assert len(approvals) == 1
    assert "已签收" in approvals[0]["summary"]

    events = asyncio.run(_collect_events(event_stream(
        graph,
        {"configurable": {"thread_id": thread_id}},
        messages=[HumanMessage(content="审批通过")],
        resume_payload={"action": "approve"},
    )))
    tool_results = [event for event in events if event["type"] == "tool_result"]
    assert [(result["data"]["name"], result["data"]["ok"]) for result in tool_results] == [
        ("refund_apply", True)
    ]

    state = graph.get_state({"configurable": {"thread_id": thread_id}}).values
    assert state["execution_status"] == "completed"
    assert state["context"]["approval_summary"].endswith("已签收")
    tool_msgs = [m for m in state["messages"] if getattr(m, "type", "") == "tool"]
    assert [getattr(m, "name", "") for m in tool_msgs] == ["refund_apply"]

    conn = sqlite3.connect(db_path)
    try:
        status = conn.execute(
            "SELECT status FROM orders WHERE order_id=?", (order_id,)
        ).fetchone()[0]
        refund = conn.execute(
            "SELECT amount, status FROM refunds WHERE order_id=?", (order_id,)
        ).fetchone()
        idem = conn.execute(
            "SELECT tool_name, request_id FROM idempotency WHERE tool_name='refund_apply'"
        ).fetchall()
    finally:
        conn.close()
    assert status == "refunding"
    assert refund == (1.0, "refunding")
    assert idem == [("refund_apply", request_id)]

    conn = sqlite3.connect(db_path)
    try:
        after = _db_snapshot(conn)
    finally:
        conn.close()
    assert len(after["refunds"]) == len(before["refunds"]) + 1
    assert len(after["idempotency"]) == len(before["idempotency"]) + 1


def test_refund_order_rejected_does_not_write_db(monkeypatch, tmp_path):
    _, db_path, owner, order_id, before = _load_cancel_seed(tmp_path, "refund-rejected")
    _use_cancel_db(monkeypatch, db_path)
    graph = _refund_graph(order_id, "refund-rejected")
    thread_id = "refund-rejected"

    out = _invoke_refund(graph, owner, thread_id)
    approvals = [it.value for it in out.get("__interrupt__", []) if _approval_required(it.value)]
    assert len(approvals) == 1

    out = ainvoke_graph(graph,
        Command(resume={"action": "reject", "note": "资料不足"}),
        {"configurable": {"thread_id": thread_id}},
    )
    assert out["execution_status"] == "completed"
    assert [m for m in out["messages"] if getattr(m, "type", "") == "tool"] == []
    assert "未通过" in out["messages"][-1].content

    conn = sqlite3.connect(db_path)
    try:
        after = _db_snapshot(conn)
    finally:
        conn.close()
    assert after == before


def test_refund_rejected_when_completed(monkeypatch, tmp_path):
    from ai_service_agent.graph.workflows.safety import _apply_business_rules

    pending = {"tool": "refund_apply", "args": {"order_id": "ORD-X", "amount": 10.0}}
    facts = {"order_id": "ORD-X", "user_id": "u1", "status": "completed",
             "total_amount": 100.0, "occupied_refund_amount": 0.0}
    assert _apply_business_rules(pending, facts, "u1") == "refund_not_allowed"


def test_refund_allowed_when_signed_within_window():
    from datetime import datetime, timedelta, timezone
    from ai_service_agent.graph.workflows.safety import _apply_business_rules

    pending = {"tool": "refund_apply", "args": {"order_id": "ORD-X", "amount": 10.0}}
    facts = {"order_id": "ORD-X", "user_id": "u1", "status": "signed",
             "signed_at": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
             "total_amount": 100.0, "occupied_refund_amount": 0.0}
    assert _apply_business_rules(pending, facts, "u1") is None


def test_transaction_prechecks_reject_signed_address_and_refunded_refunding_invoice():
    address_pending = {"tool": "update_address", "args": {"order_id": "ORD-X"}}
    signed_facts = {"order_id": "ORD-X", "status": "signed"}
    assert _apply_business_rules(address_pending, signed_facts, "u1") == "address_not_editable"

    invoice_pending = {"tool": "create_invoice", "args": {"order_id": "ORD-X"}}
    assert _apply_business_rules(
        invoice_pending, {"order_id": "ORD-X", "status": "refunded"}, "u1"
    ) == "invoice_not_allowed"
    assert _apply_business_rules(
        invoice_pending, {"order_id": "ORD-X", "status": "refunding"}, "u1"
    ) == "invoice_not_allowed"


def test_refund_order_expired_does_not_write_db(monkeypatch, tmp_path):
    _, db_path, owner, order_id, before = _load_cancel_seed(tmp_path, "refund-expired")
    _use_cancel_db(monkeypatch, db_path)
    graph = _refund_graph(order_id, "refund-expired")
    thread_id = "refund-expired"

    out = _invoke_refund(graph, owner, thread_id)
    approvals = [it.value for it in out.get("__interrupt__", []) if _approval_required(it.value)]
    assert len(approvals) == 1
    assert approvals[0]["expires_at"]

    out = ainvoke_graph(graph,
        Command(resume={"result": "expired"}),
        {"configurable": {"thread_id": thread_id}},
    )
    assert out["execution_status"] == "completed"
    assert [m for m in out["messages"] if getattr(m, "type", "") == "tool"] == []
    assert "过期" in out["messages"][-1].content

    conn = sqlite3.connect(db_path)
    try:
        after = _db_snapshot(conn)
    finally:
        conn.close()
    assert after == before


def test_refund_order_replay_binds_to_requester(monkeypatch, tmp_path):
    from ai_service_agent.tools.write import refund_apply

    _, db_path, owner, order_id, _ = _load_cancel_seed(
        tmp_path, "refund-replay-boundary"
    )
    _use_cancel_db(monkeypatch, db_path)
    conn = sqlite3.connect(db_path)
    try:
        different_order = conn.execute(
            "SELECT order_id FROM orders WHERE customer_id=? AND order_id<>?",
            (owner, order_id),
        ).fetchone()[0]
    finally:
        conn.close()

    first = refund_apply.invoke(
        {"order_id": order_id, "request_id": "refund-replay-boundary", "user_id": owner}
    )
    assert first["replayed"] is False
    replay = refund_apply.invoke(
        {
            "order_id": different_order,
            "request_id": "refund-replay-boundary",
            "user_id": owner,
        }
    )
    assert replay == {"error": "data_boundary", "order_id": different_order}


def test_refund_order_post_approval_drift_reports_failure(monkeypatch, tmp_path):
    _, db_path, owner, order_id, before = _load_cancel_seed(tmp_path, "refund-drift")
    _use_cancel_db(monkeypatch, db_path)
    graph = _refund_graph(order_id, "refund-drift")
    thread_id = "refund-drift"

    out = _invoke_refund(graph, owner, thread_id)
    approvals = [it for it in out.get("__interrupt__", []) if _approval_required(it.value)]
    assert len(approvals) == 1

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE orders SET status='shipped' WHERE order_id=?", (order_id,))
        conn.commit()
    finally:
        conn.close()

    events = asyncio.run(_collect_events(event_stream(
        graph,
        {"configurable": {"thread_id": thread_id}},
        messages=[HumanMessage(content="审批通过")],
        resume_payload={"action": "approve"},
    )))
    tool_results = [event for event in events if event["type"] == "tool_result"]
    assert len(tool_results) == 1
    assert tool_results[0]["data"]["name"] == "refund_apply"
    assert tool_results[0]["data"]["ok"] is False
    assert "refund_not_allowed" in tool_results[0]["data"]["summary"]
    assert events[-1]["type"] == "done"
    assert "遇到问题" in events[-1]["data"]["final"]

    state = graph.get_state({"configurable": {"thread_id": thread_id}})
    assert state.values["execution_status"] == "failed"
    assert "处理编号" in state.values["messages"][-1].content

    conn = sqlite3.connect(db_path)
    try:
        after = _db_snapshot(conn)
        status = conn.execute(
            "SELECT status FROM orders WHERE order_id=?", (order_id,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert status == "shipped"
    assert len(after["refunds"]) == len(before["refunds"])
    assert len(after["idempotency"]) == len(before["idempotency"])


async def _collect_events(gen):
    return [event async for event in gen]


def test_cancel_order_post_approval_drift_reports_failure(monkeypatch, tmp_path):
    _, db_path, owner, order_id, before = _load_cancel_seed(tmp_path, "drift")
    _use_cancel_db(monkeypatch, db_path)
    graph = _cancel_graph(order_id, "cancel-drift")
    thread_id = "cancel-drift"

    out = _invoke_cancel(graph, owner, thread_id)
    approvals = [
        it for it in out.get("__interrupt__", [])
        if it.value.get("type") == "approval_required"
    ]
    assert len(approvals) == 1

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE orders SET status='shipped', cancelled_at=NULL WHERE order_id=?",
            (order_id,),
        )
        conn.commit()
    finally:
        conn.close()

    events = asyncio.run(_collect_events(event_stream(
        graph,
        {"configurable": {"thread_id": thread_id}},
        messages=[HumanMessage(content="审批通过")],
        resume_payload={"action": "approve"},
    )))
    tool_results = [event for event in events if event["type"] == "tool_result"]
    assert len(tool_results) == 1
    assert tool_results[0]["data"]["name"] == "cancel_order"
    assert tool_results[0]["data"]["ok"] is False
    assert "cancel_not_allowed" in tool_results[0]["data"]["summary"]
    assert events[-1]["type"] == "done"
    assert "遇到问题" in events[-1]["data"]["final"]
    assert "操作已完成" not in events[-1]["data"]["final"]

    state = graph.get_state({"configurable": {"thread_id": thread_id}})
    assert state.values["execution_status"] == "failed"
    tool_msgs = [m for m in state.values["messages"] if getattr(m, "type", "") == "tool"]
    assert [getattr(m, "name", "") for m in tool_msgs] == ["cancel_order"]
    assert "遇到问题" in state.values["messages"][-1].content
    assert "处理编号" in state.values["messages"][-1].content
    assert "操作已完成" not in state.values["messages"][-1].content

    conn = sqlite3.connect(db_path)
    try:
        after = _db_snapshot(conn)
        status = conn.execute(
            "SELECT status FROM orders WHERE order_id=?", (order_id,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert status == "shipped"
    assert len(after["idempotency"]) == len(before["idempotency"])


def test_cancel_order_rejected_does_not_write_db(monkeypatch, tmp_path):
    _, db_path, owner, order_id, before = _load_cancel_seed(tmp_path, "rejected")
    _use_cancel_db(monkeypatch, db_path)
    graph = _cancel_graph(order_id, "cancel-rejected")
    thread_id = "cancel-rejected"

    out = _invoke_cancel(graph, owner, thread_id)
    approvals = [
        it.value for it in out.get("__interrupt__", [])
        if it.value.get("type") == "approval_required"
    ]
    assert len(approvals) == 1
    assert approvals[0]["approval_id"]

    out = ainvoke_graph(graph,
        Command(resume={"action": "reject", "note": "资料不足"}),
        {"configurable": {"thread_id": thread_id}},
    )
    assert out["execution_status"] == "completed"
    assert [m for m in out["messages"] if getattr(m, "type", "") == "tool"] == []
    assert "未通过" in out["messages"][-1].content

    conn = sqlite3.connect(db_path)
    try:
        after = _db_snapshot(conn)
    finally:
        conn.close()
    assert after == before


def test_cancel_order_expired_does_not_write_db(monkeypatch, tmp_path):
    _, db_path, owner, order_id, before = _load_cancel_seed(tmp_path, "expired")
    _use_cancel_db(monkeypatch, db_path)
    graph = _cancel_graph(order_id, "cancel-expired")
    thread_id = "cancel-expired"

    out = _invoke_cancel(graph, owner, thread_id)
    approvals = [
        it.value for it in out.get("__interrupt__", [])
        if it.value.get("type") == "approval_required"
    ]
    assert len(approvals) == 1
    assert approvals[0]["approval_id"]
    assert approvals[0]["expires_at"]

    out = ainvoke_graph(graph,
        Command(resume={"result": "expired"}),
        {"configurable": {"thread_id": thread_id}},
    )
    assert out["execution_status"] == "completed"
    assert [m for m in out["messages"] if getattr(m, "type", "") == "tool"] == []
    assert "过期" in out["messages"][-1].content

    conn = sqlite3.connect(db_path)
    try:
        after = _db_snapshot(conn)
    finally:
        conn.close()
    assert after == before
