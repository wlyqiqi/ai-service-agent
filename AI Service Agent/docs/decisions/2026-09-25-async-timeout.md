# Async LLM Boundary Timeout

## Decision

Convert the Router, Select, ReadGraph, and Fallback LLM boundary nodes to async and attach LangGraph per-node `TimeoutPolicy` at registration time.

## Defaults

- JEV Router: 3.0 seconds (`JEV_NODE_TIMEOUT_SECONDS`).
- DeepSeek Router: 6.0 seconds (`ROUTER_NODE_TIMEOUT_SECONDS`).
- Select, ReadGraph, and Fallback LLM nodes: 12.0 seconds (`LLM_NODE_TIMEOUT_SECONDS`).

## Boundaries

- A JEV timeout falls back to DeepSeek and is not retried.
- A DeepSeek Router timeout is retried at most once by `RETRY_DEEPSEEK_ROUTER`; the retry applies only to the DeepSeek Router node.
- The Select timeout covers the complete Select attempt, including read-only candidate database lookups performed inside that node.
- The ReadGraph timeout covers the complete ReAct attempt, including nested read-only tool work driven by that attempt.
- Fallback timeout applies only to the Fallback LLM-boundary node.
- Mutation and high-risk write tool execution remains outside timeout scope until side-effect compensation is explicit.

## Compile Constraint

LangGraph node timeouts are async-only. Configure timeout on each async node; do not use `set_node_defaults(timeout=...)`, because graph-wide defaults can also affect implicit sync nodes and fail graph compilation.
