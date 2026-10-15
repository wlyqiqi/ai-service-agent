# Live smoke — seat-gated handoff (#82)

Date: 2026-10-15
Runtime: ephemeral `0.0.0.0:8766` via `scripts/live_seat_gated_triangular.py` (current main + #81)
Chatwoot: `http://localhost:3000` (account 3)
Account Webhook: `http://192.168.1.119:8766/webhooks/chatwoot`
Subscriptions: `message_created`, `conversation_status_changed`
Chatwoot env: `SAFE_FETCH_ALLOW_PRIVATE_NETWORK=true` (required; SafeFetch otherwise blocks LAN)

## Automated checks

| Step | Result | Notes |
|---|---|---|
| 0 Webhook GET | PASS | `scripts/check_seat_gated_webhooks.py` — url + status event + secret |
| A Request hang | PASS | `handoff_requested`, mode=`ai`, private note + `handoff-request` |
| B AI still replies | PASS | FAQ after hang still SSE `done` + tokens, mode=`ai` |
| C Take over + Joined Notice | PASS | Chatwoot `toggle_status(open)` → webhook → mode=`human` + Joined Notice |
| D Human lane tip | PASS | Further chat mode=`human`, no `token`, `done.final` human tip |
| E Resolve → ai | PASS | Chatwoot `toggle_status(resolved)` → webhook → mode=`ai` |
| F AI resumes | PASS | FAQ again SSE tokens + `done` |

## Ops notes

- Prior webhook URL `http://host.docker.internal:8766/webhooks/chatwoot` failed delivery until `SAFE_FETCH_ALLOW_PRIVATE_NETWORK=true` and LAN `RUNTIME_BASE`.
- API-created conversations may start `open`; smoke forces `pending` then `open` so `conversation_status_changed` fires (UI Take over from bot queue is pending→open).
