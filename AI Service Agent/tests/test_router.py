from langchain_core.messages import AIMessage

from ai_service_agent.graph.router import make_router
from ai_service_agent.testing import ToolCallingFakeModel


def test_router_parses_structured_output():
    model = ToolCallingFakeModel(
        responses=[AIMessage(content='{"capability": "order", "risk_level": "read", "confidence": 0.9}')]
    )
    node = make_router(model)
    out = node({"messages": [], "user_id": "u1", "session_id": "s1", "context": {}})
    assert out["capability"] == "order"
    assert out["risk_level"] == "read"
    assert out["confidence"] == 0.9


def test_router_low_confidence_goes_unknown():
    model = ToolCallingFakeModel(
        responses=[AIMessage(content='{"capability": "order", "risk_level": "read", "confidence": 0.3}')]
    )
    node = make_router(model)
    out = node({"messages": [], "user_id": "u1", "session_id": "s1", "context": {}})
    assert out["capability"] == "unknown"
