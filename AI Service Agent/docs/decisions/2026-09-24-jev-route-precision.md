# Decision: JEV Route Precision

## Context

Real E2E testing found that profile-update utterances containing an order id can be confused with `order_mutation`, and a low-frequency run misclassified an address update as `unsupported_action`.

## Decision

1. Keep JEV as the primary Router.
2. Make profile-update semantics explicit in the JEV Choice criteria.
3. Keep DeepSeek only for timeout, HTTP error, or invalid JEV output.
4. Return `unknown` directly when JEV confidence is below the execution-class threshold.
5. Add one narrow deterministic Policy Gate normalization for address/phone/profile-contact update text misclassified as `order_mutation`.
6. Record `routing_backend` and `routing_fallback` as Router observability fields only.

## Non-goals

- No second Router.
- No low-confidence second LLM check.
- No expansion of `order_mutation.allowed_tools`.
- No permission or execution decision inside Router.
