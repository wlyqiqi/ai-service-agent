"""Contract: production high-risk tools delegate to OrderRepository (not write.py SQL)."""

from unittest.mock import MagicMock

from langgraph.checkpoint.memory import MemorySaver

from ai_service_agent.harness.graph import make_ai_graph
from ai_service_agent.testing import ToolCallingFakeModel
from ai_service_agent.tools import DEFAULT_TOOL_REGISTRY, make_tool_registry


def test_default_registry_has_no_high_risk_tools():
    assert DEFAULT_TOOL_REGISTRY.meta_for("cancel_order") is None
    assert DEFAULT_TOOL_REGISTRY.meta_for("refund_apply") is None


def test_make_ai_graph_builds_with_repository_backed_registry():
    repo = MagicMock()
    repo.cancel_order.return_value = {
        "ok": True,
        "replayed": False,
        "result": {"field": "cancel_order", "order_id": "ORD-1", "status": "cancelled"},
    }
    graph = make_ai_graph(
        ToolCallingFakeModel(responses=[]),
        MemorySaver(),
        order_repository=repo,
    )
    assert graph is not None
    # Same factory path make_ai_graph uses when registry is omitted.
    reg = make_tool_registry(repo)
    cancel = next(t for t in reg.for_risk_level("high_risk") if t.name == "cancel_order")
    cancel.invoke({"order_id": "ORD-1", "request_id": "rq-1", "user_id": "u1"})
    repo.cancel_order.assert_called_once_with("ORD-1", "u1", None, "rq-1")
