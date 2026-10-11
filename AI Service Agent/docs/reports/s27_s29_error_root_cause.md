# S27/S29 error root cause (2026-10-11)

## Observability

`functional_scenario_eval` now records SSE `error.code` / `detail` in `problems` and `actual.errors`.
Node error emission includes underlying exception in `error.data.detail` (user-facing `message` unchanged).

## Repro findings

| Config | Result |
|--------|--------|
| SiliconFlow URL + `ark-` API key (current `.env`) | `OpenAIAuthenticationError` 401 `Token is invalid.` (~200–500ms) |
| ARK URL + same `ark-` key | `OpenAIRateLimitError` 429 `AccountQuotaExceeded` (quota reset 2026-10-18) |

## Root cause

Not HighRisk routing. First chat turn fails model auth/quota before `approval_required`:

1. `.env` points `ARK_BASE_URL` at SiliconFlow while `ARK_API_KEY` is a Volcengine `ark-` token → 401.
2. ARK account itself is over monthly quota → 429 until 2026-10-18.

## Fix required (credentials)

Put a valid **SiliconFlow `sk-` key** in `ARK_API_KEY` (keep SiliconFlow base URL and model ids), **or** wait for ARK quota reset / upgrade and point URL+model back to ARK.

Harness now fails fast via `preflight_model_auth()` when SiliconFlow URL is paired with an `ark-` key.

## Regression after credential fix

```text
uv run --no-sync python scripts/functional_scenario_eval.py --only S27 S29
```

Expect first-turn `approval_required`, then approve/reject pass.
