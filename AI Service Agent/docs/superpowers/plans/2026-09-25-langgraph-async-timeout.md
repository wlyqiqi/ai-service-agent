# LangGraph Async Timeout Migration Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert the five LLM/router boundary nodes to async, add bounded per-node `TimeoutPolicy`, and verify timeout/retry/error behavior plus production-oriented concurrency.

**Architecture:** Use LangGraph official async-only node timeouts at the model boundary while keeping write tools out of timeout scope. Convert sync blocking calls to native async calls, wrap `create_agent` nodes explicitly with async functions, and preserve JEV-to-DeepSeek fallback semantics. Use per-node timeout configuration because `set_node_defaults(timeout=...)` also applies timeout to LangGraph implicit sync error-handler nodes and fails compile in the installed runtime.

**Tech Stack:** Python 3.14, LangGraph 1.2.11, langchain-core/langchain-openai, httpx, FastAPI, pytest, pytest-asyncio.

**Spec:** `docs/specs/architecture.md` §5 and the scope confirmation recorded in this plan.

## Global Constraints

- Node timeouts are async-only in LangGraph; sync nodes with `timeout=` are rejected at compile time.
- Use `TimeoutPolicy` from `langgraph.types` and `NodeTimeoutError` from `langgraph.errors`.
- Do not use `set_node_defaults(timeout=...)`; configure timeout per node.
- Keep JEV as no-retry primary; JEV timeout falls back to DeepSeek.
- Keep DeepSeek router retry at `RETRY_DEEPSEEK_ROUTER(max_attempts=2)`; do not rerun the whole JEV+DeepSeek chain.
- Keep write tools outside timeout scope in this migration.
- Async nodes must not perform blocking SQLite or HTTP calls directly.
- Offline regression uses `JEV_ROUTE_INTENT_ENABLED=false` and `JEV_ENABLED=false` unless a test enables JEV.
- Real-model tests are targeted smoke tests, not full regression.
- Error handlers must annotate the injected parameter as `error: NodeError`; LangGraph 1.2.11 does not inject an untyped second parameter.

## Confirmed Facts

- There are currently no node-level `TimeoutPolicy` usages in project source.
- Existing `timeout=` values are JEV HTTP request timeouts and SQLite busy timeouts.
- `router_jev`, `router_deepseek`, and `select_llm` are sync callables.
- ReadGraph and Fallback register `create_agent` runnables directly.
- All 11 registered tools are sync functions.
- `kb_search` uses sync `httpx.Client`; order/mutation/high-risk tools use SQLite.
- `cancel_order` and `refund_apply` are local SQLite writes today, not external API calls.
- `TimeoutPolicy(run_timeout=...)` raises `NodeTimeoutError`; retry policy decides whether to retry.
- `set_node_defaults(timeout=...)` can fail compile because it applies timeout to the implicit sync error-handler node.

## Intended Defaults

- `JEV_NODE_TIMEOUT_SECONDS=3.0`
- `ROUTER_NODE_TIMEOUT_SECONDS=6.0`
- `LLM_NODE_TIMEOUT_SECONDS=12.0`

---

### Task 1: Central Timeout Policy and Error Handler Compatibility

**Files:**
- Modify: `src/ai_service_agent/config.py`
- Modify: `src/ai_service_agent/graph/fault_tolerance.py`
- Modify: `tests/test_fault_tolerance.py`

**Interfaces:**
- Produces: `make_timeout_policy(run_timeout: float) -> TimeoutPolicy`; settings `jev_node_timeout_seconds`, `router_node_timeout_seconds`, and `llm_node_timeout_seconds`; `NodeTimeoutError` classified as transient.

- [ ] **Step 1: Add failing tests**

Add to `tests/test_fault_tolerance.py`:

```python
import pytest
from langgraph.errors import NodeTimeoutError
from langgraph.types import Command

from ai_service_agent.graph.fault_tolerance import make_timeout_policy


def test_node_timeout_is_transient():
    error = NodeTimeoutError(node="router_deepseek", elapsed=0.05, kind="run", run_timeout=0.05)
    assert is_transient_error(error)


def test_make_timeout_policy_uses_hard_run_timeout():
    policy = make_timeout_policy(0.05)
    assert policy.run_timeout == 0.05
    assert policy.idle_timeout is None


def test_root_error_handler_marks_failed():
    error = NodeError(node="router_deepseek", error=RuntimeError("provider unavailable"))
    command = root_error_handler({}, error)
    assert command.update == {"execution_status": "failed"}
    assert command.goto == "final"
```

Also add graph-level runtime tests for `root_error_handler` and `make_subgraph_error_handler`; a failing async node must produce final `execution_status="failed"`.

- [ ] **Step 2: Run tests**

Run:

```powershell
$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest tests/test_fault_tolerance.py -q
```

Expected: FAIL because the helper/settings do not exist and runtime error handlers are not invoked.

- [ ] **Step 3: Add settings**

In `src/ai_service_agent/config.py`, add:

```python
    jev_node_timeout_seconds: float = Field(default=3.0, validation_alias="JEV_NODE_TIMEOUT_SECONDS")
    router_node_timeout_seconds: float = Field(default=6.0, validation_alias="ROUTER_NODE_TIMEOUT_SECONDS")
    llm_node_timeout_seconds: float = Field(default=12.0, validation_alias="LLM_NODE_TIMEOUT_SECONDS")
```

- [ ] **Step 4: Implement policy and handler fixes**

In `fault_tolerance.py`:

```python
from langgraph.errors import NodeError, NodeTimeoutError
from langgraph.types import Command, RetryPolicy, TimeoutPolicy


def make_timeout_policy(run_timeout: float) -> TimeoutPolicy:
    return TimeoutPolicy(run_timeout=run_timeout)


def root_error_handler(state: dict, error: NodeError) -> Command:
    _emit_node_error(error)
    return Command(update={"execution_status": "failed"}, goto="final")


def make_subgraph_error_handler(final_node: str):
    def handler(state: dict, error: NodeError) -> Command:
        _emit_node_error(error)
        return Command(update={"execution_status": "failed"}, goto=final_node)

    return handler
```

Add this first branch to `is_transient_error`:

```python
    if isinstance(error, NodeTimeoutError):
        return True
```

- [ ] **Step 5: Verify and commit**

Run the Task 1 test command again; expected PASS. Then:

```powershell
git add src/ai_service_agent/config.py src/ai_service_agent/graph/fault_tolerance.py tests/test_fault_tolerance.py
git commit -m "feat: add bounded async timeout policy"
```

---

### Task 2: Async Router Boundary with Bounded Timeout

**Files:**
- Modify: `src/ai_service_agent/graph/router.py`
- Modify: `src/ai_service_agent/graph/builder.py`
- Modify: `tests/test_router_fault_tolerance.py`

**Interfaces:**
- Produces: async `router_jev` and `router_deepseek`; JEV timeout handler returning `route_control="deepseek_fallback"`; DeepSeek timeout retried by existing policy.

- [ ] **Step 1: Add failing async timeout tests**

Add a `SlowDeepSeekModel` whose async `_agenerate` detects the router system prompt, increments `router_calls`, sleeps `delay_seconds`, then returns `route_intent_json_message("faq", 0.9)`.

Add two tests:

1. `test_jev_node_timeout_falls_back_without_chain_retry`
   - Enable JEV, set `JEV_NODE_TIMEOUT_SECONDS=0.05`, `JEV_TIMEOUT_SECONDS=0.10`, `ROUTER_NODE_TIMEOUT_SECONDS=2.0`.
   - Monkeypatch `_classify_route_intent_with_jev` with an async helper that raises `httpx.ConnectError`.
   - Invoke with `graph.ainvoke`.
   - Assert `routing_fallback is True`, `execution_status == "completed"`, DeepSeek calls `== 1`, and elapsed `< 1.0`.

2. `test_deepseek_node_timeout_retries_only_router`
   - Disable JEV, set `ROUTER_NODE_TIMEOUT_SECONDS=0.05`, and make the fake router sleep `0.10`.
   - Invoke the graph.
   - Assert `execution_status == "failed"`, `router_calls == 2`, and elapsed `< 1.0`.

- [ ] **Step 2: Run tests**

Run:

```powershell
$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest tests/test_router_fault_tolerance.py -q
```

Expected: FAIL because router nodes are still sync and have no timeout.

- [ ] **Step 3: Convert JEV to async**

Change `_classify_route_intent_with_jev` to accept `httpx.AsyncClient` and use `await client.post(...)` / `await response.json()`.

Change `make_jev_router`:

```python
async def route(state: dict) -> dict:
    try:
        async with httpx.AsyncClient(timeout=settings.jev_timeout_seconds) as jev_client:
            candidate = await _classify_route_intent_with_jev(jev_client, state, settings)
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        return _jev_fallback_result()
    # Keep existing candidate validation and route_intent confidence logic unchanged.
```

Add:

```python
from langgraph.errors import NodeError, NodeTimeoutError
from langgraph.types import Command
from ai_service_agent.graph.fault_tolerance import root_error_handler


def router_jev_error_handler(state: dict, error: NodeError) -> dict | Command:
    if isinstance(error.error, NodeTimeoutError):
        return _jev_fallback_result()
    return root_error_handler(state, error)
```

- [ ] **Step 4: Convert DeepSeek to async**

Wrap both route-intent-on and route-intent-off variants:

```python
async def deepseek_route(state: dict, config: RunnableConfig) -> dict:
    result = await agent.ainvoke({"messages": state["messages"]}, config=config)
```

Import `RunnableConfig` from `langchain_core.runnables`. Keep parsing and confidence logic unchanged.

- [ ] **Step 5: Register timeout in builder**

Import `make_timeout_policy` and `router_jev_error_handler`. Register `router_jev` with:

```python
timeout=make_timeout_policy(settings.jev_node_timeout_seconds),
error_handler=router_jev_error_handler,
```

Register both `router_deepseek` paths with:

```python
retry_policy=RETRY_DEEPSEEK_ROUTER,
timeout=make_timeout_policy(settings.router_node_timeout_seconds),
```

- [ ] **Step 6: Verify and commit**

Run:

```powershell
$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest tests/test_router_fault_tolerance.py tests/test_router.py tests/test_route_intent_router.py -q
```

Expected: PASS. Commit:

```powershell
git add src/ai_service_agent/graph/router.py src/ai_service_agent/graph/builder.py tests/test_router_fault_tolerance.py
git commit -m "feat: add async router timeout boundaries"
```

---

### Task 3: Async Select Nodes with Bounded Timeout

**Files:**
- Modify: `src/ai_service_agent/graph/workflows/select.py`
- Modify: `src/ai_service_agent/graph/workflows/mutation_agent.py`
- Modify: `src/ai_service_agent/graph/workflows/high_risk_agent.py`
- Modify: `tests/test_select.py`

**Interfaces:**
- Produces: async `select_llm` in MutationGraph and HighRiskGraph.

- [ ] **Step 1: Add failing select timeout test**

Create `SlowSelectFakeModel(ToolCallingFakeModel)` with async `_agenerate` that sleeps `delay_seconds`, then returns `select_json_message("update_address", {"order_id": "ORD-1", "address": "北京"})`.

Add `test_select_node_timeout_is_bounded`:

- Set `LLM_NODE_TIMEOUT_SECONDS=0.05`.
- Build `make_select_node(model, "mutation", [], DEFAULT_TOOL_REGISTRY)`.
- Call `await node(state, None)`.
- Assert `execution_status == "failed"` and elapsed `< 0.5`.

- [ ] **Step 2: Run test**

```powershell
$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest tests/test_select.py -q
```

Expected: FAIL because select is sync and unbounded.

- [ ] **Step 3: Convert select**

Change:

```python
def select(state: dict) -> dict:
```

to:

```python
async def select(state: dict, config: RunnableConfig) -> dict:
```

Add:

```python
import asyncio
from langchain_core.runnables import RunnableConfig
```

Replace both model invocations with:

```python
await bound.ainvoke(messages, config=config)
```

Replace cancellation and refund candidate queries with:

```python
await asyncio.to_thread(find_own_cancellable_paid_orders, state.get("user_id", ""))
```

```python
await asyncio.to_thread(find_own_refundable_orders, state.get("user_id", ""))
```

- [ ] **Step 4: Register timeout**

In both write graphs, import:

```python
from ai_service_agent.config import get_settings
from ai_service_agent.graph.fault_tolerance import RETRY_LLM, make_timeout_policy, make_subgraph_error_handler
```

Register:

```python
retry_policy=RETRY_LLM,
timeout=make_timeout_policy(get_settings().llm_node_timeout_seconds),
```

- [ ] **Step 5: Verify and commit**

Run:

```powershell
$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest tests/test_select.py tests/test_mutation_graph.py tests/test_high_risk_graph.py -q
```

Expected: PASS. Commit:

```powershell
git add src/ai_service_agent/graph/workflows/select.py src/ai_service_agent/graph/workflows/mutation_agent.py src/ai_service_agent/graph/workflows/high_risk_agent.py tests/test_select.py
git commit -m "feat: add async select timeout"
```

---

### Task 4: Async ReadGraph and Fallback LLM Nodes

**Files:**
- Modify: `src/ai_service_agent/graph/agents/read_agent.py`
- Modify: `src/ai_service_agent/graph/workflows/fallback.py`
- Modify: `tests/test_read_agent.py`
- Modify: `tests/test_fallback.py`

**Interfaces:**
- Produces: async `agent_llm` and `explain_llm`.

- [ ] **Step 1: Convert ReadGraph**

Wrap the `create_agent` runnable:

```python
async def agent_llm(state: dict, config: RunnableConfig):
    return await agent.ainvoke(state, config=config)
```

Register:

```python
retry_policy=RETRY_LLM,
timeout=make_timeout_policy(get_settings().llm_node_timeout_seconds),
```

- [ ] **Step 2: Convert Fallback**

Rename the `create_agent` result to `agent` and wrap:

```python
async def explain_llm(state: dict, config: RunnableConfig):
    return await agent.ainvoke(state, config=config)
```

Register the same `RETRY_LLM` and timeout policy.

- [ ] **Step 3: Add compile assertions**

In both graph tests:

```python
node = graph.nodes["agent_llm"]
assert asyncio.iscoroutinefunction(node.node)
assert isinstance(node.timeout, TimeoutPolicy)
```

Use `"explain_llm"` for Fallback.

- [ ] **Step 4: Verify and commit**

Run:

```powershell
$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest tests/test_read_agent.py tests/test_fallback.py tests/test_builder.py -q
```

Expected: PASS. Commit:

```powershell
git add src/ai_service_agent/graph/agents/read_agent.py src/ai_service_agent/graph/workflows/fallback.py tests/test_read_agent.py tests/test_fallback.py
git commit -m "feat: add async read and fallback timeouts"
```

---

### Task 5: Full Graph Compile-Time Layer 1 Verification

**Files:**
- Modify: `tests/test_builder.py`

**Interfaces:**
- Produces: compile-time proof that every timeout target is async and registered.

- [ ] **Step 1: Add compile assertions**

Build the root graph with `make_fake_model()` and assert:

```python
for name in ("router_jev", "router_deepseek"):
    if name not in graph.nodes:
        continue
    node = graph.nodes[name]
    assert asyncio.iscoroutinefunction(node.node)
    assert isinstance(node.timeout, TimeoutPolicy)
```

Build ReadGraph, FallbackGraph, MutationGraph, and HighRiskGraph with the fake model and default registry. Assert `agent_llm`, `explain_llm`, and both `select_llm` nodes are async and have `TimeoutPolicy`.

- [ ] **Step 2: Run compile layer**

```powershell
$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest tests/test_builder.py -q
```

Expected: PASS. Any remaining sync timeout target must make compile fail.

- [ ] **Step 3: Commit**

```powershell
git add tests/test_builder.py
git commit -m "test: verify async timeout compile safety"
```

---

### Task 6: Real-Model Runtime Timeout Layer 2 Verification

**Files:**
- Create: `tests/test_async_timeout_real.py`

**Interfaces:**
- Produces: targeted real-model proof that a slow first attempt times out and a real second attempt recovers.

- [ ] **Step 1: Add real-model timeout tests**

Create `SlowRealModel(ChatOpenAI)`:

- Read ARK model/API/base URL from `get_settings()`, never print secrets.
- Accept `boundary` and `delay_seconds`.
- In async `_agenerate_with_cache` and `_astream`, call `_maybe_delay(messages)`.
- `_maybe_delay` appends a call when the system prompt contains the boundary text and sleeps only on the first boundary call.

Add two tests skipped unless `E2E_MODEL=real`:

1. `test_real_router_timeout_retries_and_recovers`
   - Enable JEV but monkeypatch `_classify_route_intent_with_jev` to raise `httpx.ConnectError`.
   - Set `JEV_NODE_TIMEOUT_SECONDS=0.05`, `ROUTER_NODE_TIMEOUT_SECONDS=0.05`.
   - Use boundary text `"客服意图路由器"` and delay `0.10`.
   - Invoke root graph with a FAQ message.
   - Assert two router calls, `execution_status == "completed"`, and elapsed `< 5.0`.

2. `test_real_select_timeout_retries_and_recovers`
   - Set `LLM_NODE_TIMEOUT_SECONDS=0.05`.
   - Use boundary text `"客服操作选择器"` and delay `0.10`.
   - Invoke MutationGraph with `"订单10086的收货地址改成北京市朝阳区"`.
   - Assert two select calls and `pending_tool.tool == "update_address"`.

- [ ] **Step 2: Run offline-safe check**

```powershell
$env:E2E_MODEL="fake"; $env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest tests/test_async_timeout_real.py -q
```

Expected: 2 skipped.

- [ ] **Step 3: Run real-model targeted check**

```powershell
$env:E2E_MODEL="real"; $env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest tests/test_async_timeout_real.py -vv
```

Expected: 2 passed.

- [ ] **Step 4: Commit**

```powershell
git add tests/test_async_timeout_real.py
git commit -m "test: verify real model async timeout recovery"
```

---

### Task 7: Offline Concurrency and Event-Loop Responsiveness

**Files:**
- Create: `tests/test_async_concurrency.py`

**Interfaces:**
- Produces: automated proof that async graph invocations scale concurrently without event-loop serialization.

- [ ] **Step 1: Add concurrency tests**

Create `ConcurrentFakeModel(ToolCallingFakeModel)` with async `_agenerate` that returns router JSON for router prompts and `AIMessage(content="离线并发回复")` otherwise.

Add `test_root_graph_supports_100_concurrent_users`:

```python
results = await asyncio.gather(*(invoke_one(index) for index in range(100)))
```

Use unique `user_id`, `session_id`, and `thread_id`. Assert 100 results, all `execution_status == "completed"`, and elapsed `< 10.0`.

Add `test_async_timeout_does_not_block_event_loop`:

- Start a heartbeat task that sleeps 10ms ten times and asserts loop lag `< 0.5`.
- Run 20 graph invocations concurrently with `asyncio.gather`.
- Await the heartbeat and assert 20 results.

- [ ] **Step 2: Run concurrency tests**

```powershell
$env:JEV_ROUTE_INTENT_ENABLED="false"; $env:JEV_ENABLED="false"; $env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest tests/test_async_concurrency.py -vv
```

Expected: PASS. Do not serialize graph execution if the CI machine is slow; lower the count to 50 with an environment override if needed.

- [ ] **Step 3: Commit**

```powershell
git add tests/test_async_concurrency.py
git commit -m "test: verify async graph concurrency"
```

---

### Task 8: Documentation, Review, and Final Regression

**Files:**
- Create: `docs/decisions/2026-09-25-async-timeout.md`
- Modify: `docs/specs/architecture.md`

**Interfaces:**
- Produces: operational defaults and production boundary documentation.

- [ ] **Step 1: Record architecture decision**

Create `docs/decisions/2026-09-25-async-timeout.md` with these sections:

```markdown
# Async LLM Boundary Timeout

## Decision

Convert Router, Select, ReadGraph, and Fallback LLM boundary nodes to async and attach LangGraph per-node `TimeoutPolicy`.

## Defaults

- JEV node: 3.0 seconds
- DeepSeek router node: 6.0 seconds
- Select / ReadGraph / Fallback LLM node: 12.0 seconds

## Boundaries

- JEV timeout falls back to DeepSeek; JEV itself is not retried.
- DeepSeek timeout is retried at most once by `RETRY_DEEPSEEK_ROUTER`.
- Select / ReadGraph / Fallback timeout is retried only when `is_transient_error` accepts it.
- Tool execution is not given a timeout in this migration.
- SQLite write tools remain outside timeout scope until side-effect compensation is explicit.

## Compile Constraint

LangGraph node timeouts are async-only. Use per-node timeout because `set_node_defaults(timeout=...)` can also apply to the implicit sync error-handler node and fail compile.
```

- [ ] **Step 2: Update architecture spec**

In `docs/specs/architecture.md` §5, add:

```markdown
### Async Node Timeout

LLM and router boundary nodes are async and use LangGraph `TimeoutPolicy(run_timeout=...)`. Timeouts are configured per node; do not use `set_node_defaults(timeout=...)`. `NodeTimeoutError` is transient and is controlled by the node retry policy. Tool execution, especially mutation and high-risk writes, must not receive a timeout until side-effect compensation is explicitly defined.
```

- [ ] **Step 3: Run targeted review**

Review the implementation diff and verify:

1. All timeout targets are async.
2. No `set_node_defaults(timeout=...)` exists.
3. JEV still falls back; DeepSeek retries only itself.
4. No write tool has timeout.
5. Error handlers annotate `NodeError`.
6. Async nodes do not directly call blocking SQLite or HTTP helpers.

- [ ] **Step 4: Run full offline regression**

```powershell
$env:JEV_ROUTE_INTENT_ENABLED="false"; $env:JEV_ENABLED="false"; $env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest -q --basetemp="D:\AI_Project\AI Service Agent\.pytest-tmp"
```

Expected: all tests pass.

- [ ] **Step 5: Run targeted real-model smoke**

```powershell
$env:E2E_MODEL="real"; $env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest tests/test_async_timeout_real.py tests/test_e2e_real.py::TestRealFaultToleranceE2E -vv
```

Expected: targeted real-model timeout and fault tests pass.

- [ ] **Step 6: Commit documentation**

```powershell
git add docs/decisions/2026-09-25-async-timeout.md docs/specs/architecture.md
git commit -m "docs: record async timeout boundaries"
```
