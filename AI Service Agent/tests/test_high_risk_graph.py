import sqlite3

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from langchain_core.messages import HumanMessage

from ai_service_agent.graph.builder import build_graph
from ai_service_agent.testing import ToolCallingFakeModel, router_json_message, select_json_message
from ai_service_agent.tools.kb import kb_search
from langgraph.graph import END, START, StateGraph

from ai_service_agent.graph.state import CustomerServiceState
from ai_service_agent.graph.workflows.safety import _apply_business_rules, _check, make_precheck_fetch_node, make_recheck_node, make_safety_check_node
from ai_service_agent.tools import DEFAULT_TOOL_REGISTRY


def _hr_model():
    return ToolCallingFakeModel(responses=[
        router_json_message("order", "high_risk", 0.9),
        select_json_message("refund_apply", {"order_id": "10086"}),
    ])


def _invoke(graph, text, thread_id, resume=None):
    kwargs = {"configurable": {"thread_id": thread_id}}
    if resume is not None:
        return graph.invoke(Command(resume=resume), kwargs)
    return graph.invoke(
        {"messages": [HumanMessage(content=text)], "user_id": "u1", "session_id": thread_id, "context": {}},
        kwargs,
    )


def test_high_risk_reaches_approval_interrupt():
    graph = build_graph(_hr_model(), tools=[kb_search], checkpointer=MemorySaver())
    out = _invoke(graph, "申请退款", "h1")
    values = [it.value for it in out.get("__interrupt__", [])]
    assert any(v.get("type") == "approval_required" and v.get("approval_id") and v.get("expires_at") for v in values)


def test_high_risk_approve_executes_tool():
    graph = build_graph(_hr_model(), tools=[kb_search], checkpointer=MemorySaver())
    _invoke(graph, "申请退款", "h2")
    out = _invoke(graph, "审批通过", "h2", resume={"action": "approve"})
    assert out["execution_status"] == "completed"
    tool_msgs = [m for m in out["messages"] if getattr(m, "type", "") == "tool"]
    assert tool_msgs and getattr(tool_msgs[-1], "name", "") == "refund_apply"


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
    out = graph.invoke({
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
    out = graph.invoke({
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
    out = node({"context": {"pending_tool": {"tool": "refund_apply", "args": {"order_id": "ORD-10086"}}}})
    d = out["context"]["precheck_data"]
    assert d["status"] == "signed" and d["user_id"] == "u1" and d["order_id"] == "ORD-10086"
    assert "precheck_reason" not in out["context"]
    out2 = node({"context": {"pending_tool": {"tool": "update_address", "args": {"address": "x"}}}})
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
    out = graph.invoke(
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
    idempotency = [
        dict(row)
        for row in conn.execute("SELECT * FROM idempotency ORDER BY tool_name, request_id")
    ]
    return {"orders": orders, "idempotency": idempotency}


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


def _invoke_cancel(graph, owner, thread_id):
    return graph.invoke(
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

    out = graph.invoke(
        Command(resume={"action": "approve"}),
        {"configurable": {"thread_id": thread_id}},
    )
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

    out = graph.invoke(
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

    out = graph.invoke(
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
