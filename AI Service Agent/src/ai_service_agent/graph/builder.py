from langgraph.graph import END, START, StateGraph

from ai_service_agent.config import make_model
from ai_service_agent.graph.agents.read_agent import make_read_graph
from ai_service_agent.graph.router import make_router
from ai_service_agent.graph.state import CustomerServiceState
from ai_service_agent.graph.workflows.fallback import make_fallback_graph


# Policy 纯确定性映射（graph-spec.md §5）：read 组合走 ReadGraph，其余（mutation /
# high_risk / unknown / 未映射）走 Fallback。显式登记测试覆盖的组合，其余由默认值兜底。
POLICY = {
    ("faq", "read"): "read_graph",
    ("order", "read"): "read_graph",
    ("account", "read"): "read_graph",
    ("smalltalk", "read"): "read_graph",
    ("unknown", "read"): "fallback",
    ("order", "high_risk"): "fallback",  # MVP 未纳入审批
}


def _route_target(state: dict) -> str:
    return POLICY.get((state.get("capability", "unknown"), state.get("risk_level", "read")), "fallback")


def build_graph(model, tools: list, checkpointer=None):
    """总图：Router -> Policy 条件边 -> ReadGraph / Fallback -> Final Response（统一出口）。"""
    router = make_router(model)
    read_graph = make_read_graph(model, tools)
    fallback = make_fallback_graph(model)

    def finalize(state: dict) -> dict:
        return {"execution_status": "completed"}

    builder = StateGraph(CustomerServiceState)
    builder.add_node("router", router)
    builder.add_node("read_graph", read_graph)
    builder.add_node("fallback", fallback)
    builder.add_node("final", finalize)
    builder.add_edge(START, "router")
    builder.add_conditional_edges(
        "router", _route_target, {"read_graph": "read_graph", "fallback": "fallback"}
    )
    builder.add_edge("read_graph", "final")
    builder.add_edge("fallback", "final")
    builder.add_edge("final", END)
    return builder.compile(checkpointer=checkpointer)


# langgraph platform / CLI 入口（langgraph.json -> builder.py:app）
app = build_graph(make_model(), tools=[])
