import httpx
import pytest

from ai_service_agent.config import get_settings
from ai_service_agent.graph.router import make_router
from ai_service_agent.testing import ToolCallingFakeModel, route_intent_json_message


@pytest.fixture
def route_intent_settings(monkeypatch):
    monkeypatch.setenv("JEV_ROUTE_INTENT_ENABLED", "true")
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


@pytest.fixture
def jev_route_settings(route_intent_settings, monkeypatch):
    monkeypatch.setenv("JEV_ENABLED", "true")
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


class _JevResponse:
    def __init__(self, data: dict):
        self._data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


class _JevClient:
    def __init__(self, data: dict):
        self.data = data

    def post(self, *args, **kwargs):
        return _JevResponse(self.data)


def _jev_route_payload(route_intent: str, confidence: float) -> dict:
    probabilities = {intent: 0.0 for intent in (
        "faq",
        "smalltalk",
        "order_query",
        "my_orders",
        "shipment_query",
        "account_profile_update",
        "order_mutation",
        "cancel_order_execute",
        "refund_apply_execute",
        "unsupported_action",
        "unknown",
    )}
    probabilities[route_intent] = 1.0
    return {
        "answers": {
            "route_intent": {
                "type": "choice",
                "choice": route_intent,
                "confidence": confidence,
                "probabilities": probabilities,
            },
        }
    }


def test_deepseek_route_intent_path(route_intent_settings):
    model = ToolCallingFakeModel(
        responses=[route_intent_json_message("my_orders", 0.94)]
    )
    node = make_router(model)
    out = node({"messages": [], "user_id": "u1", "context": {}})
    assert out == {
        "route_intent": "my_orders",
        "confidence": 0.94,
        "execution_status": "routing",
    }


def test_deepseek_low_confidence_becomes_unknown(route_intent_settings):
    model = ToolCallingFakeModel(
        responses=[route_intent_json_message("cancel_order_execute", 0.79)]
    )
    node = make_router(model)
    out = node({"messages": [], "user_id": "u1", "context": {}})
    assert out["route_intent"] == "unknown"
    assert out["confidence"] == 0.79


def test_jev_route_intent_is_primary(jev_route_settings, monkeypatch):
    client = _JevClient(_jev_route_payload("cancel_order_execute", 0.94))
    monkeypatch.setattr("ai_service_agent.graph.router.httpx.Client", lambda timeout: client)
    model = ToolCallingFakeModel(
        responses=[route_intent_json_message("refund_apply_execute", 0.99)]
    )
    node = make_router(model)
    out = node({"messages": [], "user_id": "u1", "context": {}})
    assert out["route_intent"] == "cancel_order_execute"
    assert out["confidence"] == 0.94


def test_jev_low_confidence_falls_back_to_deepseek(jev_route_settings, monkeypatch):
    client = _JevClient(_jev_route_payload("cancel_order_execute", 0.79))
    monkeypatch.setattr("ai_service_agent.graph.router.httpx.Client", lambda timeout: client)
    model = ToolCallingFakeModel(
        responses=[route_intent_json_message("cancel_order_execute", 0.95)]
    )
    node = make_router(model)
    out = node({"messages": [], "user_id": "u1", "context": {}})
    assert out["route_intent"] == "cancel_order_execute"
    assert out["confidence"] == 0.95


def test_jev_network_error_falls_back_to_deepseek(jev_route_settings, monkeypatch):
    class _TimeoutClient:
        def post(self, *args, **kwargs):
            raise httpx.ConnectTimeout("timeout")

    monkeypatch.setattr("ai_service_agent.graph.router.httpx.Client", lambda timeout: _TimeoutClient())
    model = ToolCallingFakeModel(
        responses=[route_intent_json_message("faq", 0.91)]
    )
    node = make_router(model)
    out = node({"messages": [], "user_id": "u1", "context": {}})
    assert out["route_intent"] == "faq"
    assert out["confidence"] == 0.91


def test_jev_invalid_probability_sum_falls_back_to_deepseek(jev_route_settings, monkeypatch):
    payload = _jev_route_payload("cancel_order_execute", 0.95)
    payload["answers"]["route_intent"]["probabilities"]["cancel_order_execute"] = 0.80
    client = _JevClient(payload)
    monkeypatch.setattr("ai_service_agent.graph.router.httpx.Client", lambda timeout: client)
    model = ToolCallingFakeModel(
        responses=[route_intent_json_message("cancel_order_execute", 0.95)]
    )
    node = make_router(model)
    out = node({"messages": [], "user_id": "u1", "context": {}})
    assert out["route_intent"] == "cancel_order_execute"
    assert out["confidence"] == 0.95


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload["answers"]["route_intent"].update({"type": "noul"}),
        lambda payload: payload["answers"]["route_intent"].pop("probabilities"),
        lambda payload: payload["answers"]["route_intent"]["probabilities"].pop("cancel_order_execute"),
        lambda payload: payload["answers"]["route_intent"].update({"confidence": 1.2}),
    ],
)
def test_jev_invalid_choice_shape_falls_back_to_deepseek(
    jev_route_settings,
    monkeypatch,
    mutate,
):
    payload = _jev_route_payload("cancel_order_execute", 0.95)
    mutate(payload)
    client = _JevClient(payload)
    monkeypatch.setattr("ai_service_agent.graph.router.httpx.Client", lambda timeout: client)
    model = ToolCallingFakeModel(
        responses=[route_intent_json_message("cancel_order_execute", 0.95)]
    )
    node = make_router(model)
    out = node({"messages": [], "user_id": "u1", "context": {}})
    assert out["route_intent"] == "cancel_order_execute"
    assert out["confidence"] == 0.95


def test_feature_off_keeps_legacy_router(monkeypatch):
    monkeypatch.setenv("JEV_ROUTE_INTENT_ENABLED", "false")
    get_settings.cache_clear()
    try:
        from ai_service_agent.testing import router_json_message

        model = ToolCallingFakeModel(
            responses=[router_json_message("faq", "read", 0.9)]
        )
        node = make_router(model)
        out = node({"messages": [], "user_id": "u1", "context": {}})
        assert out["capability"] == "faq"
        assert out["risk_level"] == "read"
    finally:
        get_settings.cache_clear()
