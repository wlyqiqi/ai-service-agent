from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from langchain_core.messages import HumanMessage

from ai_service_agent.graph.builder import build_graph
from ai_service_agent.testing import ToolCallingFakeModel, router_json_message, select_json_message
from ai_service_agent.tools.kb import kb_search
from langgraph.graph import END, START, StateGraph

from ai_service_agent.graph.state import CustomerServiceState
from ai_service_agent.graph.workflows.safety import _check, make_recheck_node, make_safety_check_node
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
