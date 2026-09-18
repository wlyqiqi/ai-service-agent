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

def test_fallback_non_handoff_routes_to_explain():
    graph = make_fallback_graph(ToolCallingFakeModel(responses=[]))
    out = graph.invoke({"messages": [HumanMessage(content="随便说说")], "context": {}, "user_id": "u1", "session_id": "s1", "handoff": False})
    assert out["execution_status"] == "completed"
    assert out["messages"][-1].content == "我暂时无法处理，请换个说法或联系人工客服。"


def test_fallback_handoff_path_generates_no_in_graph_text():
    graph = make_fallback_graph(ToolCallingFakeModel(responses=[]))
    out = graph.invoke({"messages": [HumanMessage(content="我要转人工")], "context": {}, "user_id": "u1", "session_id": "s1", "handoff": False})
    assert out["execution_status"] == "waiting_handoff"
    assert [m for m in out.get("messages", []) if getattr(m, "type", "") == "ai"] == []


def test_check_handoff_reuses_shared_detector():
    out = check_handoff(
        {"messages": [HumanMessage(content="叫个真人来")], "context": {}, "user_id": "u1", "session_id": "s1"}
    )
    assert isinstance(out, Command)
    assert out.update["handoff"] is True
    assert out.goto == "pause"


def test_check_handoff_no_false_positive_on_topic():
    out = check_handoff(
        {"messages": [HumanMessage(content="人工客服培训需要多久")], "context": {}, "user_id": "u1", "session_id": "s1"}
    )
    assert out == {"handoff": False, "execution_status": "completed"}
