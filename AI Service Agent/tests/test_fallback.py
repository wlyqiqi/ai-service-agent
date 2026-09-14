from langchain_core.messages import HumanMessage
from langgraph.types import Command

from ai_service_agent.graph.workflows.fallback import check_handoff, make_fallback_graph
from ai_service_agent.testing import ToolCallingFakeModel


def test_check_handoff_ignores_normal_input():
    out = check_handoff({"messages": [], "context": {}, "user_id": "u1", "session_id": "s1"})
    assert out == {"handoff": False, "execution_status": "completed"}


def test_check_handoff_detects_keyword():
    out = check_handoff(
        {"messages": [HumanMessage(content="我要转人工")], "context": {}, "user_id": "u1", "session_id": "s1"}
    )
    assert isinstance(out, Command)
    assert out.update["handoff"] is True
    assert out.update["execution_status"] == "waiting_handoff"
    assert out.goto == "pause"


def test_fallback_graph_interrupts_on_handoff():
    model = ToolCallingFakeModel(responses=[])
    graph = make_fallback_graph(model)
    out = graph.invoke(
        {"messages": [HumanMessage(content="我要转人工")], "user_id": "u1", "session_id": "s1", "context": {}, "handoff": False}
    )
    assert out["execution_status"] == "waiting_handoff"
    assert out["handoff"] is True
    interrupt_values = [it.value for it in out.get("__interrupt__", [])]
    assert any(v.get("type") == "handoff_request" for v in interrupt_values)
