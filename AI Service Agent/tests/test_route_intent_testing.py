import json

from ai_service_agent.testing import route_intent_json_message


def test_route_intent_json_message_contract():
    message = route_intent_json_message("order_query", 0.92)
    assert json.loads(message.content) == {
        "route_intent": "order_query",
        "confidence": 0.92,
    }
