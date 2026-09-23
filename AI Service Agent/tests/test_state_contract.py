from ai_service_agent.graph.state import CustomerServiceState


def test_customer_service_state_declares_route_intent_fields():
    hints = CustomerServiceState.__annotations__
    assert "route_intent" in hints
    assert "target_graph" in hints
    assert "execution_class" in hints
    assert "allowed_tools" in hints
    assert "requires_approval" in hints
