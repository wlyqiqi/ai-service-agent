from langchain_core.messages import AIMessage, HumanMessage

from ai_service_agent.graph.builder import POLICY, build_graph
from ai_service_agent.testing import ToolCallingFakeModel
from ai_service_agent.tools.kb import kb_search


def test_policy_maps_read_path():
    assert POLICY[("order", "read")] == "read_graph"
    assert POLICY[("unknown", "read")] == "fallback"
    assert POLICY[("order", "high_risk")] == "fallback"  # MVP 未纳入审批


def test_build_graph_read_path():
    model = ToolCallingFakeModel(
        responses=[
            AIMessage(content='{"capability":"order","risk_level":"read","confidence":0.9}'),
            AIMessage(content="订单已发货。"),
        ]
    )
    graph = build_graph(model, tools=[kb_search])
    out = graph.invoke({"messages": [HumanMessage(content="查订单 ORD-1")], "user_id": "u1", "session_id": "s1", "context": {}})
    assert out["execution_status"] == "completed"
    assert "发货" in out["messages"][-1].content


def test_build_graph_fallback_path():
    model = ToolCallingFakeModel(
        responses=[AIMessage(content='{"capability":"order","risk_level":"read","confidence":0.3}')]
    )
    graph = build_graph(model, tools=[kb_search])
    out = graph.invoke({"messages": [HumanMessage(content="随便聊聊")], "user_id": "u1", "session_id": "s1", "context": {}})
    assert out["execution_status"] == "completed"
