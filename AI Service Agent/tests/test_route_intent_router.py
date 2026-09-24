import httpx
import pytest

from ai_service_agent.config import get_settings
from ai_service_agent.graph.router import _JEV_ROUTE_INTENT_QUESTION, make_router
from ai_service_agent.testing import ToolCallingFakeModel, route_intent_json_message


@pytest.fixture
def route_intent_settings(monkeypatch):
    monkeypatch.setenv("JEV_ROUTE_INTENT_ENABLED", "true")
    monkeypatch.setenv("JEV_ENABLED", "false")
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


@pytest.fixture
def jev_route_settings(route_intent_settings, monkeypatch):
    monkeypatch.setenv("JEV_ENABLED", "true")
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setenv("FAKE_MODEL", "false")
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

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

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
    assert out["route_intent"] == "my_orders"
    assert out["confidence"] == 0.94
    assert out["routing_backend"] == "deepseek"
    assert out["routing_fallback"] is True


def test_deepseek_low_confidence_becomes_unknown(route_intent_settings):
    model = ToolCallingFakeModel(
        responses=[route_intent_json_message("cancel_order_execute", 0.79)]
    )
    node = make_router(model)
    out = node({"messages": [], "user_id": "u1", "context": {}})
    assert out["route_intent"] == "unknown"
    assert out["confidence"] == 0.79
    assert out["routing_backend"] == "deepseek"
    assert out["routing_fallback"] is True


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
    assert out["routing_backend"] == "jev"
    assert out["routing_fallback"] is False


def test_jev_low_confidence_returns_unknown_without_deepseek(jev_route_settings, monkeypatch):
    client = _JevClient(_jev_route_payload("cancel_order_execute", 0.79))
    monkeypatch.setattr("ai_service_agent.graph.router.httpx.Client", lambda timeout: client)
    model = ToolCallingFakeModel(
        responses=[route_intent_json_message("cancel_order_execute", 0.95)]
    )
    node = make_router(model)
    out = node({"messages": [], "user_id": "u1", "context": {}})
    assert out["route_intent"] == "unknown"
    assert out["confidence"] == 0.79
    assert out["routing_backend"] == "jev"
    assert out["routing_fallback"] is False


def test_jev_network_error_falls_back_to_deepseek(jev_route_settings, monkeypatch):
    class _TimeoutClient:
        def post(self, *args, **kwargs):
            raise httpx.ConnectTimeout("timeout")

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    monkeypatch.setattr("ai_service_agent.graph.router.httpx.Client", lambda timeout: _TimeoutClient())
    model = ToolCallingFakeModel(
        responses=[route_intent_json_message("faq", 0.91)]
    )
    node = make_router(model)
    out = node({"messages": [], "user_id": "u1", "context": {}})
    assert out["route_intent"] == "faq"
    assert out["confidence"] == 0.91
    assert out["routing_backend"] == "deepseek"
    assert out["routing_fallback"] is True


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
    assert out["routing_backend"] == "deepseek"
    assert out["routing_fallback"] is True


def test_jev_non_dict_answers_falls_back_to_deepseek(jev_route_settings, monkeypatch):
    payload = _jev_route_payload("cancel_order_execute", 0.95)
    payload["answers"] = []
    client = _JevClient(payload)
    monkeypatch.setattr("ai_service_agent.graph.router.httpx.Client", lambda timeout: client)
    model = ToolCallingFakeModel(
        responses=[route_intent_json_message("cancel_order_execute", 0.95)]
    )
    node = make_router(model)
    out = node({"messages": [], "user_id": "u1", "context": {}})
    assert out["route_intent"] == "cancel_order_execute"
    assert out["confidence"] == 0.95
    assert out["routing_backend"] == "deepseek"
    assert out["routing_fallback"] is True


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload["answers"]["route_intent"].pop("type"),
        lambda payload: payload["answers"]["route_intent"].update({"type": "noul"}),
        lambda payload: payload["answers"]["route_intent"].update({"choice": "tool_name"}),
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
    assert out["routing_backend"] == "deepseek"
    assert out["routing_fallback"] is True


def test_jev_client_is_created_and_closed_for_each_call(jev_route_settings, monkeypatch):
    created_clients = []
    payload = _jev_route_payload("cancel_order_execute", 0.95)

    class _CleanupClient:
        def __init__(self, data):
            self.data = data
            self.closed = False
            created_clients.append(self)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            self.closed = True
            return False

        def post(self, *args, **kwargs):
            return _JevResponse(self.data)

    monkeypatch.setattr(
        "ai_service_agent.graph.router.httpx.Client",
        lambda timeout: _CleanupClient(payload),
    )
    model = ToolCallingFakeModel(
        responses=[route_intent_json_message("cancel_order_execute", 0.95)]
    )
    node = make_router(model)
    state = {"messages": [], "user_id": "u1", "context": {}}
    node(state)
    node(state)

    assert len(created_clients) == 2
    assert all(client.closed for client in created_clients)


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


def test_jev_route_choice_pins_profile_update_boundary():
    criteria = _JEV_ROUTE_INTENT_QUESTION["route_intent"]["criteria"]
    assert "收货地址" in criteria["account_profile_update"]
    assert "联系电话" in criteria["account_profile_update"]
    assert "手机号" in criteria["account_profile_update"]
    assert "即使输入包含订单号" in criteria["account_profile_update"]
    assert criteria["order_mutation"] == "修改订单备注、开发票或创建售后工单"
    assert "取消预约" in criteria["unsupported_action"]
    assert "清空订单历史" in criteria["unsupported_action"]
