import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage

from ai_service_agent.config import get_settings
from ai_service_agent.graph.router import (
    _JEV_ROUTE_INTENT_QUESTION,
    _jev_state,
    make_deepseek_router,
    make_jev_router,
)
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


def _current_user_state(content: str = "帮我取消订单") -> dict:
    return {
        "messages": [HumanMessage(content=content)],
        "user_id": "u1",
        "context": {},
    }


@pytest.mark.asyncio
async def test_deepseek_route_intent_path(route_intent_settings):
    model = ToolCallingFakeModel(
        responses=[route_intent_json_message("my_orders", 0.94)]
    )
    node = make_deepseek_router(model, route_intent_enabled=True)
    out = await node(_current_user_state())
    assert out["route_intent"] == "my_orders"
    assert out["confidence"] == 0.94
    assert out["routing_backend"] == "deepseek"
    assert out["routing_fallback"] is True


@pytest.mark.asyncio
async def test_deepseek_low_confidence_becomes_unknown(route_intent_settings):
    model = ToolCallingFakeModel(
        responses=[route_intent_json_message("cancel_order_execute", 0.79)]
    )
    node = make_deepseek_router(model, route_intent_enabled=True)
    out = await node(_current_user_state())
    assert out["route_intent"] == "unknown"
    assert out["confidence"] == 0.79
    assert out["routing_backend"] == "deepseek"
    assert out["routing_fallback"] is True


@pytest.mark.asyncio
async def test_jev_route_intent_is_primary(jev_route_settings, monkeypatch):
    client = _JevClient(_jev_route_payload("cancel_order_execute", 0.94))
    monkeypatch.setattr("ai_service_agent.graph.router.httpx.Client", lambda timeout: client)
    model = ToolCallingFakeModel(
        responses=[route_intent_json_message("refund_apply_execute", 0.99)]
    )
    node = make_jev_router()
    out = await node(_current_user_state())
    assert out["route_intent"] == "cancel_order_execute"
    assert out["confidence"] == 0.94
    assert out["routing_backend"] == "jev"
    assert out["routing_fallback"] is False


def test_jev_state_separates_current_message_and_recent_context():
    state = {
        "messages": [
            HumanMessage(content="我想改手机号"),
            AIMessage(content="请提供新的手机号"),
            HumanMessage(content="18600001111"),
            AIMessage(content="已修改"),
            HumanMessage(content="看看我的订单"),
            AIMessage(content="请提供订单号"),
            HumanMessage(content="那帮我取消"),
        ]
    }

    assert _jev_state(state) == {
        "current_user_message": "那帮我取消",
        "recent_context": [
            {"role": "human", "content": "看看我的订单"},
            {"role": "ai", "content": "请提供订单号"},
        ],
    }


@pytest.mark.asyncio
async def test_jev_without_current_user_turn_returns_unknown_without_call(
    jev_route_settings,
    monkeypatch,
):
    created_clients = []

    class _UnexpectedJevClient:
        def __init__(self, data):
            created_clients.append(self)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def post(self, *args, **kwargs):
            raise AssertionError("JEV must not be called without a current user turn")

    monkeypatch.setattr(
        "ai_service_agent.graph.router.httpx.Client",
        lambda timeout: _UnexpectedJevClient(_jev_route_payload("faq", 1.0)),
    )
    node = make_jev_router()
    state = {
        "messages": [
            HumanMessage(content="帮我查订单"),
            AIMessage(content="订单已查询"),
        ]
    }

    out = await node(state)

    assert out == {
        "route_intent": "unknown",
        "confidence": 0.0,
        "execution_status": "routing",
        "routing_backend": "none",
        "routing_fallback": False,
        "route_control": "policy",
    }
    assert created_clients == []


def test_jev_route_instructions_pin_current_message_priority():
    question = _JEV_ROUTE_INTENT_QUESTION["route_intent"]
    instructions = question["instructions"]

    assert "current_user_message 是唯一主判断对象" in instructions
    assert "recent_context 仅用于解析当前消息中的指代和省略" in instructions
    assert "不得把 recent_context 中已经完成的历史意图当作当前意图" in instructions
    assert "即使结合 recent_context 仍无法确定意图" in question["criteria"]["unknown"]


def test_jev_faq_criteria_covers_product_catalog_and_price():
    criteria = _JEV_ROUTE_INTENT_QUESTION["route_intent"]["criteria"]

    assert "产品清单" in criteria["faq"]
    assert "产品目录" in criteria["faq"]
    assert "商品价格" in criteria["faq"]
    assert "报价" in criteria["faq"]


@pytest.mark.asyncio
async def test_jev_low_confidence_returns_unknown_without_deepseek(jev_route_settings, monkeypatch):
    client = _JevClient(_jev_route_payload("cancel_order_execute", 0.79))
    monkeypatch.setattr("ai_service_agent.graph.router.httpx.Client", lambda timeout: client)
    model = ToolCallingFakeModel(
        responses=[route_intent_json_message("cancel_order_execute", 0.95)]
    )
    node = make_jev_router()
    out = await node(_current_user_state())
    assert out["route_intent"] == "unknown"
    assert out["confidence"] == 0.79
    assert out["routing_backend"] == "jev"
    assert out["routing_fallback"] is False


@pytest.mark.asyncio
async def test_jev_network_error_falls_back_to_deepseek(jev_route_settings, monkeypatch):
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
    node = make_jev_router()
    out = await node(_current_user_state())
    assert out == {
        "execution_status": "routing",
        "routing_backend": "jev",
        "routing_fallback": True,
        "route_control": "deepseek_fallback",
    }
    assert out["routing_fallback"] is True


@pytest.mark.asyncio
async def test_jev_invalid_probability_sum_falls_back_to_deepseek(jev_route_settings, monkeypatch):
    payload = _jev_route_payload("cancel_order_execute", 0.95)
    payload["answers"]["route_intent"]["probabilities"]["cancel_order_execute"] = 0.80
    client = _JevClient(payload)
    monkeypatch.setattr("ai_service_agent.graph.router.httpx.Client", lambda timeout: client)
    model = ToolCallingFakeModel(
        responses=[route_intent_json_message("cancel_order_execute", 0.95)]
    )
    node = make_jev_router()
    out = await node(_current_user_state())
    assert out == {
        "execution_status": "routing",
        "routing_backend": "jev",
        "routing_fallback": True,
        "route_control": "deepseek_fallback",
    }
    assert out["routing_fallback"] is True


@pytest.mark.asyncio
async def test_jev_non_dict_answers_falls_back_to_deepseek(jev_route_settings, monkeypatch):
    payload = _jev_route_payload("cancel_order_execute", 0.95)
    payload["answers"] = []
    client = _JevClient(payload)
    monkeypatch.setattr("ai_service_agent.graph.router.httpx.Client", lambda timeout: client)
    model = ToolCallingFakeModel(
        responses=[route_intent_json_message("cancel_order_execute", 0.95)]
    )
    node = make_jev_router()
    out = await node(_current_user_state())
    assert out == {
        "execution_status": "routing",
        "routing_backend": "jev",
        "routing_fallback": True,
        "route_control": "deepseek_fallback",
    }
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
@pytest.mark.asyncio
async def test_jev_invalid_choice_shape_falls_back_to_deepseek(
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
    node = make_jev_router()
    out = await node(_current_user_state())
    assert out == {
        "execution_status": "routing",
        "routing_backend": "jev",
        "routing_fallback": True,
        "route_control": "deepseek_fallback",
    }
    assert out["routing_fallback"] is True


@pytest.mark.asyncio
async def test_jev_client_is_created_and_closed_for_each_call(jev_route_settings, monkeypatch):
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
    node = make_jev_router()
    state = _current_user_state()
    await node(state)
    await node(state)

    assert len(created_clients) == 2
    assert all(client.closed for client in created_clients)


@pytest.mark.asyncio
async def test_feature_off_keeps_legacy_router(monkeypatch):
    monkeypatch.setenv("JEV_ROUTE_INTENT_ENABLED", "false")
    get_settings.cache_clear()
    try:
        from ai_service_agent.testing import router_json_message

        model = ToolCallingFakeModel(
            responses=[router_json_message("faq", "read", 0.9)]
        )
        node = make_deepseek_router(model, route_intent_enabled=False)
        out = await node({"messages": [], "user_id": "u1", "context": {}})
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
    assert "没有对应工具" in criteria["unsupported_action"]
    assert "不归 unknown" in criteria["unsupported_action"]
    assert "不在支持范围内" not in criteria["unknown"]
    assert "unsupported_action" in criteria["unknown"]
