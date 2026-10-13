# OrderRepository Decoupling (2026-10-10)

## Context

- HighRiskGraph, MutationGraph, safety nodes, and write tools previously depended directly on SQLite and the mock order database.
- Switching to another order source would require changing business logic.
- High-risk approval needed stronger TOCTOU protection.

## Decision

- Introduce `OrderRepository` as the only order domain boundary.
- Expose `OrderSnapshot` for facts: order status, ownership, amounts, invoice/refund totals, signed time, product summary, and version/rowid.
- Inject the repository through LangGraph `context_schema=GraphContext` and `Runtime`.
- Keep SQLite knowledge inside `SQLiteOrderRepository`; graph nodes and tools must not open database connections directly.
- Build high-risk write tools with `make_cancel_order_tool(order_repository)` and `make_refund_apply_tool(order_repository)`.
- Require an explicit `order_id` for `cancel_order` / `refund_apply`; do not auto-select an order even when only one candidate exists.
- After approval, recheck the current snapshot and reject if the order is missing, owned by someone else, not in an allowed status, or the version/rowid changed.

## Consequences

- Graph, select, and safety logic are database-agnostic.
- Postgres, HTTP APIs, or ERP adapters can be added without changing graph business logic.
- TOCTOU failures are converted to deterministic `safety_rejected`; only runtime failures become `error`.
- The eval harness can isolate mutation/high-risk state per case by restoring a clean SQLite snapshot.
- Production high-risk tools are built via `make_tool_registry(order_repository)` → `make_cancel_order_tool` / `make_refund_apply_tool` (no module-level SQL `@tool` for cancel/refund). `DEFAULT_TOOL_REGISTRY` holds read+mutation only.
- `REFUND_WINDOW_DAYS` is owned by `domain/orders.py` and shared with safety precheck.
