"""Verify Chatwoot Account Webhook is ready for seat-gated handoff (#82).

Calls GET /api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/webhooks and checks:
  - url == {RUNTIME_BASE}/webhooks/chatwoot
  - subscriptions include conversation_status_changed
  - secret matches Runtime CHATWOOT_WEBHOOK_SECRET

Usage (PowerShell):
  $env:UV_CACHE_DIR="<repo>\\.uv-cache"; $env:UV_LINK_MODE="copy"
  $env:RUNTIME_BASE="http://172.99.46.165:8000"
  uv run --no-sync python scripts/check_seat_gated_webhooks.py

Exit 0 when ready; exit 1 on misconfig or API failure.
"""

from __future__ import annotations

import argparse
import os
import sys

import httpx

from ai_service_agent.config import get_settings
from ai_service_agent.ops.seat_gated_webhook_check import (
    evaluate_seat_gated_webhook_readiness,
    expected_account_webhook_url,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runtime-base",
        default=os.environ.get("RUNTIME_BASE", "").strip() or None,
        help="Runtime public base URL (or env RUNTIME_BASE)",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    missing = [
        name
        for name, val in (
            ("CHATWOOT_BASE_URL", settings.chatwoot_base_url),
            ("CHATWOOT_ACCOUNT_ID", settings.chatwoot_account_id),
            ("CHATWOOT_API_TOKEN", settings.chatwoot_api_token),
            ("CHATWOOT_WEBHOOK_SECRET", settings.chatwoot_webhook_secret),
        )
        if not val
    ]
    if missing:
        print("FAIL: missing settings: " + ", ".join(missing), file=sys.stderr)
        return 1
    if not args.runtime_base:
        print(
            "FAIL: set RUNTIME_BASE or pass --runtime-base "
            "(e.g. http://172.99.46.165:8000)",
            file=sys.stderr,
        )
        return 1

    base = settings.chatwoot_base_url.rstrip("/")
    account_id = settings.chatwoot_account_id
    url = f"{base}/api/v1/accounts/{account_id}/webhooks"
    expected = expected_account_webhook_url(args.runtime_base)
    print(f"GET {url}")
    print(f"expect url={expected}")

    try:
        with httpx.Client(trust_env=False, timeout=30.0) as client:
            r = client.get(
                url,
                headers={"api_access_token": settings.chatwoot_api_token},
            )
            r.raise_for_status()
            body = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        print(f"FAIL: Chatwoot webhooks GET error: {exc}", file=sys.stderr)
        return 1

    result = evaluate_seat_gated_webhook_readiness(
        body,
        runtime_base=args.runtime_base,
        expected_secret=settings.chatwoot_webhook_secret,
    )
    print(
        "checks: "
        f"url_ok={result.url_ok} "
        f"subscription_ok={result.subscription_ok} "
        f"secret_ok={result.secret_ok} "
        f"webhook_id={result.matched_webhook_id}"
    )
    if result.ok:
        print("PASS: Account Webhook ready for seat-gated handoff")
        return 0
    for reason in result.reasons:
        print(f"FAIL: {reason}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
