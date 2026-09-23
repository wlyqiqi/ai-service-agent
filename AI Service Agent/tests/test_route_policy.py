from ai_service_agent.graph.route_policy import (
    ROUTE_POLICY,
    resolve_route_policy,
    route_intent_is_confident,
)
from ai_service_agent.tools import DEFAULT_TOOL_REGISTRY


def test_route_policy_maps_query_intents_to_read_graph():
    assert ROUTE_POLICY["order_query"].target_graph == "read_graph"
    assert ROUTE_POLICY["order_query"].execution_class == "read_only"
    assert ROUTE_POLICY["order_query"].allowed_tools == ("order_lookup",)

    assert ROUTE_POLICY["my_orders"].target_graph == "read_graph"
    assert ROUTE_POLICY["my_orders"].allowed_tools == ("my_orders",)

    assert ROUTE_POLICY["shipment_query"].target_graph == "read_graph"
    assert ROUTE_POLICY["shipment_query"].allowed_tools == ("shipment_query",)


def test_route_policy_maps_write_intents_and_boundaries():
    assert ROUTE_POLICY["account_profile_update"].allowed_tools == (
        "update_address",
        "update_phone",
    )
    assert ROUTE_POLICY["order_mutation"].allowed_tools == (
        "update_order_note",
        "create_invoice",
        "create_service_ticket",
    )
    assert ROUTE_POLICY["cancel_order_execute"].allowed_tools == ("cancel_order",)
    assert ROUTE_POLICY["cancel_order_execute"].requires_approval is True
    assert ROUTE_POLICY["refund_apply_execute"].allowed_tools == ("refund_apply",)
    assert ROUTE_POLICY["refund_apply_execute"].requires_approval is True


def test_unknown_and_unsupported_routes_fall_back():
    for intent in ("unsupported_action", "unknown", "not_a_real_intent"):
        policy = resolve_route_policy(intent)
        assert policy.target_graph == "fallback"
        assert policy.execution_class == "read_only"
        assert policy.allowed_tools == ()
        assert policy.requires_approval is False


def test_execution_class_confidence_thresholds():
    assert route_intent_is_confident("faq", 0.60) is True
    assert route_intent_is_confident("faq", 0.59) is False
    assert route_intent_is_confident("account_profile_update", 0.70) is True
    assert route_intent_is_confident("account_profile_update", 0.69) is False
    assert route_intent_is_confident("cancel_order_execute", 0.80) is True
    assert route_intent_is_confident("cancel_order_execute", 0.79) is False


def test_registry_tools_for_names_preserves_registry_order():
    tools = DEFAULT_TOOL_REGISTRY.tools_for_names(["shipment_query", "kb_search"])
    assert [tool.name for tool in tools] == ["kb_search", "shipment_query"]
