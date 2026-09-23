import pytest

from ai_service_agent.config import get_settings
from ai_service_agent.graph.builder import _guard_aware_target, _policy_gate


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("JEV_ROUTE_INTENT_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_policy_gate_maps_order_query(enabled):
    update = _policy_gate({"route_intent": "order_query"})
    assert update["target_graph"] == "read_graph"
    assert update["execution_class"] == "read_only"
    assert update["allowed_tools"] == ["order_lookup"]
    assert update["requires_approval"] is False


def test_policy_gate_maps_unknown_intent(enabled):
    update = _policy_gate({"route_intent": "update_phone"})
    assert update["target_graph"] == "fallback"
    assert update["execution_class"] == "read_only"
    assert update["allowed_tools"] == []


def test_policy_gate_maps_valid_account_update(enabled):
    update = _policy_gate({"route_intent": "account_profile_update"})
    assert update["target_graph"] == "mutation_graph"
    assert update["execution_class"] == "standard_write"
    assert update["allowed_tools"] == ["update_address", "update_phone"]


def test_policy_gate_maps_cancel_order(enabled):
    update = _policy_gate({"route_intent": "cancel_order_execute"})
    assert update["target_graph"] == "high_risk_graph"
    assert update["execution_class"] == "high_risk"
    assert update["allowed_tools"] == ["cancel_order"]
    assert update["requires_approval"] is True


def test_guard_fail_ignores_route_target(enabled):
    assert _guard_aware_target({"guard_status": "fail", "target_graph": "high_risk_graph"}) == "safety_final"


def test_feature_off_uses_legacy_policy(monkeypatch):
    monkeypatch.setenv("JEV_ROUTE_INTENT_ENABLED", "false")
    get_settings.cache_clear()
    try:
        assert _policy_gate({"route_intent": "order_query"}) == {}
        assert _route_target_legacy() == "fallback"
    finally:
        get_settings.cache_clear()


def _route_target_legacy():
    from ai_service_agent.graph.builder import _route_target
    return _route_target({"capability": "unknown", "risk_level": "read"})
