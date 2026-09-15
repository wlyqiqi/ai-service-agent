
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Interrupt
from langchain_core.messages import HumanMessage

from ai_service_agent.harness.graph import make_ai_graph
from ai_service_agent.harness.resume import resolve_resume_payload
from ai_service_agent.testing import make_fake_model


def test_make_ai_graph_compiles_and_runs():
    graph = make_ai_graph(make_fake_model(), MemorySaver())
    out = graph.invoke({"messages": [HumanMessage(content="hi")], "user_id": "u1", "session_id": "s1", "context": {}}, {"configurable": {"thread_id": "t1"}})
    assert out["execution_status"] == "completed"


def test_resolve_resume_payload_handoff_request():
    it = Interrupt(value={"type": "handoff_request", "reason": "r", "summary": "s"}, id="i1")
    payload = resolve_resume_payload(it.value, latest_user_text="我要转人工")
    assert payload == {"action": "handoff_resumed", "note": "我要转人工"}


def test_resolve_resume_payload_no_interrupt_returns_none():
    assert resolve_resume_payload(None) is None
    assert resolve_resume_payload({"type": "unknown"}) is None

