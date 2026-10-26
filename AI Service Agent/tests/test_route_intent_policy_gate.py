import pytest

from langchain_core.messages import HumanMessage

from ai_service_agent.config import get_settings
from ai_service_agent.graph.builder import _guard_aware_target, _policy_gate


@pytest.fixture
def enabled(monkeypatch):
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


def test_policy_gate_ignores_unknown_inputs():
    assert _policy_gate({"route_intent": "update_phone"})["target_graph"] == "fallback"


def test_policy_gate_corrects_address_mutation(enabled):
    state = {
        "route_intent": "order_mutation",
        "messages": [HumanMessage(content="订单 ORD-2026-10015 的收货地址改成北京市朝阳区测试路 99 号")],
    }
    update = _policy_gate(state)
    assert update["route_intent"] == "account_profile_update"
    assert update["target_graph"] == "mutation_graph"
    assert update["allowed_tools"] == ["update_address", "update_phone"]


@pytest.mark.parametrize(
    "text",
    [
        "我要改收货地址",
        "改下收货地址",
        "改收货地址",
        "帮我改一下收货地址",
    ],
)
def test_policy_gate_corrects_colloquial_address_mutation(enabled, text):
    """ADR 0004: colloquial 改/改下 must normalize so allowed_tools include update_address."""
    update = _policy_gate(
        {"route_intent": "order_mutation", "messages": [HumanMessage(content=text)]}
    )
    assert update["route_intent"] == "account_profile_update"
    assert update["allowed_tools"] == ["update_address", "update_phone"]


def test_policy_gate_corrects_phone_mutation(enabled):
    state = {
        "route_intent": "order_mutation",
        "messages": [HumanMessage(content="订单 ORD-2026-10015 的联系电话换成 13900002222")],
    }
    update = _policy_gate(state)
    assert update["route_intent"] == "account_profile_update"
    assert update["allowed_tools"] == ["update_address", "update_phone"]


def test_policy_gate_does_not_correct_order_note(enabled):
    state = {
        "route_intent": "order_mutation",
        "messages": [HumanMessage(content="给订单 ORD-2026-10015 加个备注：放驿站就行")],
    }
    update = _policy_gate(state)
    assert update["route_intent"] == "order_mutation"
    assert update["allowed_tools"] == ["update_order_note", "create_invoice", "create_service_ticket"]


def test_policy_gate_does_not_rescue_unsupported(enabled):
    state = {
        "route_intent": "unsupported_action",
        "messages": [HumanMessage(content="帮我把订单里的收货地址记录删除")],
    }
    update = _policy_gate(state)
    assert update["route_intent"] == "unsupported_action"
    assert update["target_graph"] == "fallback"
    assert update["allowed_tools"] == []


def test_policy_gate_corrects_dict_message_address(enabled):
    state = {
        "route_intent": "order_mutation",
        "messages": [{"type": "human", "content": "订单 ORD-2026-10015 的收货地址改成北京市朝阳区测试路 99 号"}],
    }
    update = _policy_gate(state)
    assert update["route_intent"] == "account_profile_update"
    assert update["allowed_tools"] == ["update_address", "update_phone"]

def test_policy_gate_empty_messages_no_correction(enabled):
    update = _policy_gate({"route_intent": "order_mutation", "messages": []})
    assert update["route_intent"] == "order_mutation"
    assert update["allowed_tools"] == ["update_order_note", "create_invoice", "create_service_ticket"]

def test_policy_gate_resets_clarify_count_when_route_changes(enabled):
    update = _policy_gate({
        "route_intent": "account_profile_update",
        "target_graph": "high_risk_graph",
        "context": {
            "clarify_count": 2,
            "clarify_route_intent": "refund_apply_execute",
            "clarify_target_graph": "high_risk_graph",
            "fallback_count": 1,
        },
    })
    assert update["target_graph"] == "mutation_graph"
    assert update["context"]["clarify_count"] == 0
    assert update["context"]["fallback_count"] == 1
    assert update["context"]["clarify_route_intent"] == "account_profile_update"
    assert update["context"]["clarify_target_graph"] == "mutation_graph"


def test_policy_gate_resets_clarify_count_when_intent_changes_inside_graph(enabled):
    update = _policy_gate({
        "route_intent": "cancel_order_execute",
        "target_graph": "high_risk_graph",
        "context": {
            "clarify_count": 3,
            "clarify_route_intent": "refund_apply_execute",
            "clarify_target_graph": "high_risk_graph",
        },
    })
    assert update["target_graph"] == "high_risk_graph"
    assert update["context"]["clarify_count"] == 0
    assert update["context"]["clarify_route_intent"] == "cancel_order_execute"


def test_policy_gate_keeps_clarify_count_when_route_unchanged(enabled):
    update = _policy_gate({
        "route_intent": "refund_apply_execute",
        "target_graph": "high_risk_graph",
        "context": {
            "clarify_count": 2,
            "clarify_route_intent": "refund_apply_execute",
            "clarify_target_graph": "high_risk_graph",
        },
    })
    assert "context" not in update


def test_policy_gate_does_not_reset_injected_count_without_previous_route(enabled):
    update = _policy_gate({
        "route_intent": "refund_apply_execute",
        "context": {"clarify_count": 3},
    })
    assert update["context"]["clarify_count"] == 3
    assert update["context"]["clarify_route_intent"] == "refund_apply_execute"
    assert update["context"]["clarify_target_graph"] == "high_risk_graph"


def test_policy_gate_legacy_checkpoint_resets_when_target_graph_changes(enabled):
    update = _policy_gate({
        "route_intent": "account_profile_update",
        "target_graph": "high_risk_graph",
        "context": {"clarify_count": 2, "fallback_count": 1},
    })
    assert update["context"]["clarify_count"] == 0
    assert update["context"]["fallback_count"] == 1
    assert update["context"]["clarify_target_graph"] == "mutation_graph"


def test_policy_gate_uses_current_turn_only(enabled):
    state = {
        "route_intent": "order_mutation",
        "messages": [
            HumanMessage(content="我要修改收货地址"),
            HumanMessage(content="给订单 ORD-2026-10015 加个备注：放驿站就行"),
        ],
    }
    update = _policy_gate(state)
    assert update["route_intent"] == "order_mutation"
