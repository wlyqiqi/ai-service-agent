"""Evaluate Chatwoot Account Webhook readiness for seat-gated handoff.

Seam: Chatwoot Application API `GET /api/v1/accounts/{id}/webhooks` payload
(see docs/ops/chatwoot-widget-agentbot-setup.md §4.1 / issue #82).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

REQUIRED_SUBSCRIPTION = "conversation_status_changed"
ACCOUNT_WEBHOOK_SUFFIX = "/webhooks/chatwoot"


@dataclass(frozen=True)
class SeatGatedWebhookCheck:
    ok: bool
    matched_webhook_id: int | None
    url_ok: bool
    subscription_ok: bool
    secret_ok: bool
    reasons: tuple[str, ...]


def expected_account_webhook_url(runtime_base: str) -> str:
    return runtime_base.rstrip("/") + ACCOUNT_WEBHOOK_SUFFIX


def normalize_webhook_list(response: Any) -> list[dict]:
    """Accept bare list or Chatwoot `{payload: [...]}` / `{payload: {webhooks: [...]}}`."""
    if isinstance(response, list):
        return [w for w in response if isinstance(w, dict)]
    if not isinstance(response, dict):
        return []
    payload = response.get("payload", response)
    if isinstance(payload, list):
        return [w for w in payload if isinstance(w, dict)]
    if isinstance(payload, dict):
        nested = payload.get("webhooks")
        if isinstance(nested, list):
            return [w for w in nested if isinstance(w, dict)]
    return []


def _url_matches(configured: Any, expected: str) -> bool:
    if not isinstance(configured, str) or not configured.strip():
        return False
    return configured.rstrip("/") == expected.rstrip("/")


def _has_status_subscription(subscriptions: Any) -> bool:
    if not isinstance(subscriptions, list):
        return False
    return REQUIRED_SUBSCRIPTION in subscriptions


def evaluate_seat_gated_webhook_readiness(
    response: Any,
    *,
    runtime_base: str,
    expected_secret: str | None,
) -> SeatGatedWebhookCheck:
    """Return pass/fail for seat-gated Account Webhook configuration."""
    expected_url = expected_account_webhook_url(runtime_base)
    webhooks = normalize_webhook_list(response)
    candidates = [w for w in webhooks if _url_matches(w.get("url"), expected_url)]

    if not candidates:
        return SeatGatedWebhookCheck(
            ok=False,
            matched_webhook_id=None,
            url_ok=False,
            subscription_ok=False,
            secret_ok=False,
            reasons=(
                f"no Account Webhook url equals {expected_url} "
                f"(AgentBot outgoing_url /webhooks/agent_bot is a different channel)",
            ),
        )

    # Prefer a fully ready webhook when several share the URL.
    best: dict | None = None
    best_score = -1
    for w in candidates:
        sub_ok = _has_status_subscription(w.get("subscriptions"))
        secret = w.get("secret")
        sec_ok = (
            isinstance(expected_secret, str)
            and bool(expected_secret)
            and isinstance(secret, str)
            and secret == expected_secret
        )
        score = int(sub_ok) + int(sec_ok)
        if score > best_score:
            best_score = score
            best = w

    if best is None:  # pragma: no cover — candidates non-empty above
        return SeatGatedWebhookCheck(
            ok=False,
            matched_webhook_id=None,
            url_ok=False,
            subscription_ok=False,
            secret_ok=False,
            reasons=("no candidate webhook after url filter",),
        )
    url_ok = True
    subscription_ok = _has_status_subscription(best.get("subscriptions"))
    secret = best.get("secret")
    if not isinstance(expected_secret, str) or not expected_secret:
        secret_ok = False
        secret_reason = "Runtime CHATWOOT_WEBHOOK_SECRET is empty"
    elif not isinstance(secret, str) or not secret:
        secret_ok = False
        secret_reason = "webhook secret missing from GET response; cannot confirm match"
    elif secret != expected_secret:
        secret_ok = False
        secret_reason = "webhook secret does not match Runtime CHATWOOT_WEBHOOK_SECRET"
    else:
        secret_ok = True
        secret_reason = ""

    reasons: list[str] = []
    if not subscription_ok:
        reasons.append(
            f"subscriptions must include {REQUIRED_SUBSCRIPTION} "
            f"(got {best.get('subscriptions')!r})"
        )
    if not secret_ok:
        reasons.append(secret_reason)

    wid = best.get("id")
    matched_id = int(wid) if isinstance(wid, int) else None
    return SeatGatedWebhookCheck(
        ok=subscription_ok and secret_ok and url_ok,
        matched_webhook_id=matched_id,
        url_ok=url_ok,
        subscription_ok=subscription_ok,
        secret_ok=secret_ok,
        reasons=tuple(reasons),
    )
