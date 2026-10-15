"""Ops seam: evaluate Chatwoot Account Webhook readiness for seat-gated handoff (#82)."""

from ai_service_agent.ops.seat_gated_webhook_check import (
    evaluate_seat_gated_webhook_readiness,
)


RUNTIME = "http://172.99.46.165:8000"
SECRET = "runtime-secret"


def _hook(**overrides):
    base = {
        "id": 1,
        "url": f"{RUNTIME}/webhooks/chatwoot",
        "subscriptions": ["message_created", "conversation_status_changed"],
        "secret": SECRET,
        "account_id": 3,
    }
    base.update(overrides)
    return base


def test_ready_when_url_status_event_and_secret_match():
    result = evaluate_seat_gated_webhook_readiness(
        [_hook()],
        runtime_base=RUNTIME,
        expected_secret=SECRET,
    )
    assert result.ok is True
    assert result.matched_webhook_id == 1
    assert result.url_ok is True
    assert result.subscription_ok is True
    assert result.secret_ok is True


def test_accepts_payload_wrapper_from_list_api():
    result = evaluate_seat_gated_webhook_readiness(
        {"payload": [_hook()]},
        runtime_base=RUNTIME,
        expected_secret=SECRET,
    )
    assert result.ok is True


def test_fails_when_conversation_status_changed_missing():
    result = evaluate_seat_gated_webhook_readiness(
        [_hook(subscriptions=["message_created"])],
        runtime_base=RUNTIME,
        expected_secret=SECRET,
    )
    assert result.ok is False
    assert result.subscription_ok is False
    assert any("conversation_status_changed" in r for r in result.reasons)


def test_fails_when_url_is_agent_bot_path():
    result = evaluate_seat_gated_webhook_readiness(
        [_hook(url=f"{RUNTIME}/webhooks/agent_bot")],
        runtime_base=RUNTIME,
        expected_secret=SECRET,
    )
    assert result.ok is False
    assert result.url_ok is False


def test_fails_when_secret_mismatches_runtime():
    result = evaluate_seat_gated_webhook_readiness(
        [_hook(secret="other")],
        runtime_base=RUNTIME,
        expected_secret=SECRET,
    )
    assert result.ok is False
    assert result.secret_ok is False


def test_fails_when_no_matching_url_among_webhooks():
    result = evaluate_seat_gated_webhook_readiness(
        [_hook(url="https://example.com/hooks")],
        runtime_base=RUNTIME,
        expected_secret=SECRET,
    )
    assert result.ok is False
    assert result.matched_webhook_id is None


def test_normalizes_trailing_slash_on_runtime_base():
    result = evaluate_seat_gated_webhook_readiness(
        [_hook()],
        runtime_base=RUNTIME + "/",
        expected_secret=SECRET,
    )
    assert result.ok is True
