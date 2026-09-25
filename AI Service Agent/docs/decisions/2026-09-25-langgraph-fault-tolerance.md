# LangGraph Fault Tolerance Decision

## Decision

Use LangGraph `RetryPolicy` and graph-specific `error_handler` instead of ad hoc node-level try/except. Root Graph, ReadGraph, MutationGraph, and HighRiskGraph each own their failure policy because `set_node_defaults` is not inherited by subgraphs.

## Retry Boundaries

- JEV routing is not retried.
- DeepSeek fallback has a dedicated node with at most two attempts.
- General LLM nodes have at most three attempts.
- Mutation and HighRisk tool execution is never automatically retried.
- Interrupt, approval, and handoff semantics bypass fault handlers.

## Failure Terminals

Each subgraph routes unexpected internal failures to its own `finalize_failed`. The handler emits one normalized `error` event, marks `execution_status=failed`, and returns deterministic customer-safe copy.

## Timeout Follow-up

`TimeoutPolicy` is intentionally deferred. LangGraph node timeouts are async-only, while the current Router and select nodes are synchronous. A follow-up plan must convert external I/O nodes to async before adding timeouts.
