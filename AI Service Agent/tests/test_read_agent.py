from langchain_core.messages import AIMessage

from ai_service_agent.graph.agents.read_agent import make_read_graph
from ai_service_agent.testing import ToolCallingFakeModel
from ai_service_agent.tools.kb import kb_search


def test_read_graph_answers_and_completes():
    model = ToolCallingFakeModel(responses=[AIMessage(content="查询结果：订单已发货。")])
    graph = make_read_graph(model, tools=[kb_search])
    out = graph.invoke({"messages": [], "user_id": "u1", "session_id": "s1", "context": {}})
    assert out["execution_status"] == "completed"
    assert "发货" in out["messages"][-1].content
