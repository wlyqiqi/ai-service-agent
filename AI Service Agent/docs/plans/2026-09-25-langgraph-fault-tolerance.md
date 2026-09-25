# LangGraph Fault Tolerance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace scattered graph failure handling with LangGraph official `RetryPolicy` and `error_handler`, while preserving high-risk approval semantics and bounding Router fallback latency.

**Architecture:** Add one fault-tolerance module for transient-error classification and graph-specific failure handlers. Split the composite JEV→DeepSeek router so only DeepSeek is retried. Give ReadGraph, MutationGraph, and HighRiskGraph their own `error_handler` and `finalize_failed` nodes, because LangGraph `set_node_defaults` is not inherited by independently compiled subgraphs.

**Tech Stack:** Python 3.14, LangGraph >= 1.2.10, LangChain >= 1.3.14, Pydantic, httpx, openai SDK, pytest.

**Spec:** `docs/specs/architecture.md` §5 and the reviewed fault-tolerance design recorded in this plan.

## Global Constraints

- Use Context7 before changing LangGraph-facing APIs. Preferred library ID: `/websites/langchain_oss_python_langgraph`.
- Confirmed LangGraph facts for this plan: `set_node_defaults` is not inherited by subgraphs; `error_handler` may return `Command(update=..., goto=...)`; interrupts bypass retry/error handlers; node timeouts are async-only.
- Do **not** add `TimeoutPolicy` in this plan. Existing Router, select, guard, policy, and finalizer functions are synchronous, and LangGraph rejects sync-node timeouts. Add a separate async-conversion plan before introducing timeouts.
- `router_llm`-style composite routing must not use node-level retry. JEV is never retried as part of a whole-chain retry.
- DeepSeek fallback retry uses `RetryPolicy(max_attempts=2)` only.
- LLM retry uses `RetryPolicy(max_attempts=3)` and only transient errors.
- Mutation and HighRisk tool execution must not be retried.
- Business failures, safety rejections, approval decisions, and handoff interrupts keep their existing semantics.
- Error events must not expose secrets or verbose stack traces.
- Offline tests must run with `JEV_ROUTE_INTENT_ENABLED=false` and `JEV_ENABLED=false` unless a test explicitly enables JEV.

---

## File Structure

- Create `src/ai_service_agent/graph/fault_tolerance.py`: central transient-error policy, retry policies, root handler, subgraph handler factory.
- Modify `src/ai_service_agent/graph/router.py`: split JEV and DeepSeek routing.
- Modify `src/ai_service_agent/graph/builder.py`: use split router nodes and root error handler.
- Modify `src/ai_service_agent/graph/agents/read_agent.py`: ReadGraph error handler and failed finalizer.
- Modify `src/ai_service_agent/graph/workflows/mutation_agent.py`: MutationGraph error handler, failed finalizer, tool-error boundary.
- Modify `src/ai_service_agent/graph/workflows/high_risk_agent.py`: HighRiskGraph error handler, failed finalizer, tool-error boundary.
- Modify `src/ai_service_agent/graph/workflows/select.py`: stop converting model invocation exceptions into parse failures.
- Modify `src/ai_service_agent/graph/workflows/copy.py`: add deterministic user-facing runtime failure copy.
- Modify `pyproject.toml`: declare direct use of `openai` exception types.
- Tests: `tests/test_fault_tolerance.py`, `tests/test_router_fault_tolerance.py`, `tests/test_read_agent.py`, `tests/test_mutation_graph.py`, `tests/test_high_risk_graph.py`, `tests/test_router.py`, `tests/test_select.py`.

---

### Task 1: Confirm Official APIs and Declare openai Dependency

**Files:**
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: LangGraph `RetryPolicy`, `error_handler`, `NodeError`, `Command`.
- Produces: dependency declaration needed by `fault_tolerance.py` in Task 2.

- [ ] **Step 1: Query Context7 for the exact APIs**

Use Context7 library ID `/websites/langchain_oss_python_langgraph` and query these concepts separately:

1. `RetryPolicy` and `retry_on` behavior.
2. `error_handler` return values and `Command(update=..., goto=...)`.
3. `set_node_defaults` subgraph inheritance limitations.
4. `ToolNode(handle_tool_errors=...)` behavior.

Do not proceed if the installed signatures differ.

- [ ] **Step 2: Confirm installed signatures**

Run:

```bash
$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync python -c "import inspect; from langgraph.types import RetryPolicy; from langgraph.graph import StateGraph; from langgraph.prebuilt import ToolNode; print(inspect.signature(RetryPolicy)); print(inspect.signature(StateGraph.set_node_defaults)); print(inspect.signature(ToolNode))"
```

Expected: `RetryPolicy`, `set_node_defaults`, and `ToolNode` signatures are available and include the fields used by this plan.

- [ ] **Step 3: Add the direct openai dependency**

`openai` is already installed through `langchain-openai`, but this plan imports its exception classes directly. Declare it explicitly:

```bash
$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv add "openai>=3.13,<4"
```

- [ ] **Step 4: Verify dependency resolution**

Run:

```bash
$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync python -c "import openai; print(openai.__version__)"
```

Expected: prints `3.13.0` or a compatible later version below `4.0.0`.

- [ ] **Step 5: Commit dependency declaration**

```bash
git add pyproject.toml uv.lock
git commit -m "build: declare openai dependency for fault classification"
```

---

### Task 2: Add Central Fault Tolerance Policies

**Files:**
- Create: `src/ai_service_agent/graph/fault_tolerance.py`
- Modify: `src/ai_service_agent/graph/workflows/copy.py:1`
- Test: `tests/test_fault_tolerance.py`

**Interfaces:**
- Produces:
  - `RETRY_LLM: RetryPolicy`
  - `RETRY_DEEPSEEK_ROUTER: RetryPolicy`
  - `is_transient_error(error: BaseException) -> bool`
  - `root_error_handler(state: dict, error: NodeError) -> dict`
  - `make_subgraph_error_handler(final_node: str) -> Callable[[dict, NodeError], Command]`
  - `RUNTIME_ERROR_TEXT: str` imported from `copy.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_fault_tolerance.py`:

```python
import httpx
import openai
import pytest
from langgraph.errors import NodeError

from ai_service_agent.graph.fault_tolerance import (
    RETRY_DEEPSEEK_ROUTER,
    RETRY_LLM,
    is_transient_error,
    make_subgraph_error_handler,
    root_error_handler,
)
from ai_service_agent.graph.workflows.copy import RUNTIME_ERROR_TEXT


def test_only_transient_provider_errors_are_retried():
    request = httpx.Request("POST", "https://ark.example.invalid")
    assert is_transient_error(httpx.ConnectError("connect failed"))
    assert is_transient_error(httpx.ReadTimeout("read timed out"))
    assert is_transient_error(TimeoutError("timed out"))
    assert is_transient_error(openai.APIConnectionError(request=request))
    assert not is_transient_error(ValueError("bad input"))
    assert not is_transient_error(TypeError("bad type"))


def test_retry_policies_have_bounded_attempts():
    assert RETRY_LLM.max_attempts == 3
    assert RETRY_DEEPSEEK_ROUTER.max_attempts == 2


def test_root_error_handler_marks_failed():
    error = NodeError(node="router_jev", error=RuntimeError("provider unavailable"))
    output = root_error_handler({}, error)
    assert output == {"execution_status": "failed"}


def test_subgraph_error_handler_routes_to_local_finalizer():
    error = NodeError(node="tools", error=RuntimeError("database unavailable"))
    handler = make_subgraph_error_handler("finalize_failed")
    command = handler({}, error)
    assert command.goto == "finalize_failed"
    assert command.update == {"execution_status": "failed"}


def test_runtime_error_text_is_customer_safe():
    assert "Traceback" not in RUNTIME_ERROR_TEXT
    assert "转人工" in RUNTIME_ERROR_TEXT
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
uv run --no-sync pytest tests/test_fault_tolerance.py -q
```

Expected: FAIL because `ai_service_agent.graph.fault_tolerance` does not exist.

- [ ] **Step 3: Add deterministic runtime failure copy**

In `src/ai_service_agent/graph/workflows/copy.py`, add this public constant after the imports:

```python
RUNTIME_ERROR_TEXT = (
    "抱歉，系统刚才遇到技术问题，暂时无法完成该操作。"
    "请稍后再试，或输入“转人工”联系人工客服。"
)
```

- [ ] **Step 4: Implement fault tolerance module**

Create `src/ai_service_agent/graph/fault_tolerance.py`:

```python
from collections.abc import Callable

import httpx
import openai
from langgraph.config import get_stream_writer
from langgraph.errors import NodeError
from langgraph.types import Command, RetryPolicy


TRANSIENT_HTTP_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


def _http_status(error: Exception) -> int | None:
    if isinstance(error, openai.APIStatusError):
        return getattr(error, "status_code", None)
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code
    return None


def is_transient_error(error: BaseException) -> bool:
    """Return True only for errors that are safe to retry at the LLM boundary."""
    if isinstance(error, (TimeoutError, ConnectionError, httpx.TransportError)):
        return True
    if isinstance(error, (openai.APIConnectionError, openai.RateLimitError)):
        return True
    status = _http_status(error)
    return status is not None and status in TRANSIENT_HTTP_STATUSES


RETRY_LLM = RetryPolicy(
    max_attempts=3,
    initial_interval=0.5,
    backoff_factor=2.0,
    max_interval=4.0,
    jitter=True,
    retry_on=is_transient_error,
)

RETRY_DEEPSEEK_ROUTER = RetryPolicy(
    max_attempts=2,
    initial_interval=0.5,
    backoff_factor=2.0,
    max_interval=2.0,
    jitter=True,
    retry_on=is_transient_error,
)


def _emit_node_error(error: NodeError) -> None:
    try:
        get_stream_writer()({
            "type": "error",
            "data": {
                "code": type(error.error).__name__,
                "message": str(error.error)[:200],
            },
        })
    except RuntimeError:
        pass


def root_error_handler(state: dict, error: NodeError) -> dict:
    _emit_node_error(error)
    return {"execution_status": "failed"}


def make_subgraph_error_handler(final_node: str) -> Callable[[dict, NodeError], Command]:
    def handler(state: dict, error: NodeError) -> Command:
        _emit_node_error(error)
        return Command(
            update={"execution_status": "failed"},
            goto=final_node,
        )

    return handler
```

- [ ] **Step 5: Run tests**

Run:

```bash
uv run --no-sync pytest tests/test_fault_tolerance.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/ai_service_agent/graph/fault_tolerance.py src/ai_service_agent/graph/workflows/copy.py tests/test_fault_tolerance.py
git commit -m "feat: add langgraph fault tolerance policies"
```

---

### Task 3: Split JEV and DeepSeek Router Retry Boundaries

**Files:**
- Modify: `src/ai_service_agent/graph/router.py:166-225`
- Modify: `src/ai_service_agent/graph/builder.py:95-140`
- Test: `tests/test_router_fault_tolerance.py`
- Modify: `tests/test_router.py`

**Interfaces:**
- Consumes: `make_deepseek_router(model, route_intent_enabled: bool)`, `make_jev_router()`, `RETRY_DEEPSEEK_ROUTER`.
- Produces: Root Graph nodes `router_jev` and `router_deepseek`; `router_jev` uses no retry, `router_deepseek` uses two attempts.

- [ ] **Step 1: Write failing JEV retry-boundary test**

Create `tests/test_router_fault_tolerance.py`:

```python
import time

import httpx
from langchain_core.messages import AIMessage, HumanMessage

from ai_service_agent.config import get_settings
from ai_service_agent.graph.builder import build_graph
from ai_service_agent.graph import router as router_mod
from ai_service_agent.testing import ToolCallingFakeModel, route_intent_json_message
from ai_service_agent.tools.kb import kb_search


class FlakyDeepSeekModel(ToolCallingFakeModel):
    def __init__(self):
        super().__init__(responses=[])
        self.router_calls = 0

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        system = next((m.content for m in messages if getattr(m, "type", "") == "system"), "")
        if "Route Intent" in system or "路由" in system:
            self.router_calls += 1
            if self.router_calls == 1:
                raise ConnectionError("deepseek transient failure")
            return route_intent_json_message("faq", "read", 0.9)
        return AIMessage(content="这是 FAQ 的离线回复。")


def test_jev_error_retries_only_deepseek(monkeypatch):
    jev_calls = []

    def failing_jev(client, state, settings):
        jev_calls.append("jev")
        raise httpx.ConnectError("jev unavailable")

    monkeypatch.setattr(router_mod, "_classify_route_intent_with_jev", failing_jev)
    monkeypatch.setenv("JEV_ROUTE_INTENT_ENABLED", "true")
    monkeypatch.setenv("JEV_ENABLED", "true")
    monkeypatch.setenv("JEV_TIMEOUT_SECONDS", "0.1")
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    get_settings.cache_clear()

    try:
        model = FlakyDeepSeekModel()
        graph = build_graph(model, tools=[kb_search])
        output = graph.invoke({
            "messages": [HumanMessage(content="商品怎么使用？")],
            "user_id": "u1",
            "session_id": "router-fault-1",
            "context": {},
        }, {"configurable": {"thread_id": "router-fault-1"}})
    finally:
        get_settings.cache_clear()

    assert jev_calls == ["jev"]
    assert model.router_calls == 2
    assert output["execution_status"] == "completed"
    assert "FAQ" in output["messages"][-1].content
```

- [ ] **Step 2: Run test to verify failure**

Run:

```bash
uv run --no-sync pytest tests/test_router_fault_tolerance.py::test_jev_error_retries_only_deepseek -q
```

Expected: FAIL because the current graph has one composite `router_llm` node and no `router_jev`/`router_deepseek` split.

- [ ] **Step 3: Replace composite router factories**

In `src/ai_service_agent/graph/router.py`, remove `make_router` and `make_route_intent_router`, then add:

```python
def make_deepseek_router(model, *, route_intent_enabled: bool):
    system_prompt = ROUTE_INTENT_SYSTEM if route_intent_enabled else ROUTER_SYSTEM
    agent = create_agent(_router_model(model), tools=[], system_prompt=system_prompt)

    if not route_intent_enabled:
        def deepseek_route(state: dict) -> dict:
            result = agent.invoke({"messages": state["messages"]})
            payload = _parse_router_content(result["messages"][-1].content)
            parsed = RouterSchema.model_validate(payload)
            confidence = float(parsed.confidence)
            threshold = get_settings().router_conf_threshold
            return {
                "capability": parsed.capability if confidence >= threshold else "unknown",
                "risk_level": parsed.risk_level,
                "confidence": confidence,
                "execution_status": "routing",
            }

        return deepseek_route

    def deepseek_route(state: dict) -> dict:
        result = agent.invoke({"messages": state["messages"]})
        payload = _parse_route_intent_content(result["messages"][-1].content)
        parsed = RouteIntentSchema.model_validate(payload)
        intent = parsed.route_intent
        confidence = float(parsed.confidence)
        if intent not in ROUTE_POLICY or not route_intent_is_confident(intent, confidence):
            intent = "unknown"
        return _route_intent_result(intent, confidence, "deepseek", True)

    return deepseek_route


def make_jev_router():
    settings = get_settings()

    def route(state: dict) -> dict:
        try:
            with httpx.Client(timeout=settings.jev_timeout_seconds) as jev_client:
                candidate = _classify_route_intent_with_jev(jev_client, state, settings)
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            return _route_intent_result("fallback_required", 0.0, "jev", True)
        if candidate is None:
            return _route_intent_result("fallback_required", 0.0, "jev", True)
        if route_intent_is_confident(candidate["route_intent"], candidate["confidence"]):
            return candidate
        return _route_intent_result("unknown", candidate["confidence"], "jev", False)

    return route
```

- [ ] **Step 4: Configure split Root Graph nodes**

In `src/ai_service_agent/graph/builder.py`, replace the `router_llm` node and its edges with:

```python
settings = get_settings()
use_jev = (
    settings.route_intent_enabled
    and settings.jev_enabled
    and bool(settings.jev_api_key)
    and not settings.fake_model
)

if use_jev:
    builder.add_node("router_jev", make_jev_router())
    builder.add_node(
        "router_deepseek",
        make_deepseek_router(model, route_intent_enabled=True),
        retry_policy=RETRY_DEEPSEEK_ROUTER,
    )
    builder.add_edge(START, "router_jev")
    builder.add_edge(START, "guard")
    builder.add_conditional_edges(
        "router_jev",
        lambda state: "router_deepseek" if state.get("routing_fallback") else "policy_gate",
        {"router_deepseek": "router_deepseek", "policy_gate": "policy_gate"},
    )
    builder.add_edge("router_deepseek", "policy_gate")
else:
    builder.add_node(
        "router_deepseek",
        make_deepseek_router(model, route_intent_enabled=settings.route_intent_enabled),
        retry_policy=RETRY_DEEPSEEK_ROUTER,
    )
    builder.add_edge(START, "router_deepseek")
    builder.add_edge(START, "guard")
    builder.add_edge("router_deepseek", "policy_gate")
```

Import `RETRY_DEEPSEEK_ROUTER`, `make_jev_router`, and `make_deepseek_router`; remove the old `make_router` import.

- [ ] **Step 5: Update direct router tests**

In `tests/test_router.py`, replace `make_router(model)` with:

```python
make_deepseek_router(model, route_intent_enabled=False)
```

Import `make_deepseek_router` instead of `make_router`.

- [ ] **Step 6: Run router tests**

Run:

```bash
uv run --no-sync pytest tests/test_router_fault_tolerance.py tests/test_router.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/ai_service_agent/graph/router.py src/ai_service_agent/graph/builder.py tests/test_router_fault_tolerance.py tests/test_router.py
git commit -m "refactor: bound router fallback retry to deepseek"
```

---

### Task 4: Add ReadGraph Local Failure Terminal

**Files:**
- Modify: `src/ai_service_agent/graph/agents/read_agent.py:23-45`
- Test: `tests/test_read_agent.py`

**Interfaces:**
- Consumes: `root_error_handler` is not used here; `make_subgraph_error_handler("finalize_failed")`, `RETRY_LLM`, `RUNTIME_ERROR_TEXT`.
- Produces: ReadGraph terminal node `finalize_failed`.

- [ ] **Step 1: Write failing test**

Create `tests/test_read_agent.py`:

```python
from unittest.mock import MagicMock

from langchain_core.messages import HumanMessage

from ai_service_agent.graph.agents import read_agent as read_agent_mod
from ai_service_agent.graph.agents.read_agent import make_read_graph
from ai_service_agent.graph.workflows.copy import RUNTIME_ERROR_TEXT
from ai_service_agent.testing import ToolCallingFakeModel
from ai_service_agent.tools.kb import kb_search


def test_read_graph_failure_uses_local_finalize_failed(monkeypatch):
    failing_agent = MagicMock()
    failing_agent.invoke.side_effect = RuntimeError("model unavailable")
    monkeypatch.setattr(read_agent_mod, "create_agent", lambda *args, **kwargs: failing_agent)

    graph = make_read_graph(ToolCallingFakeModel(responses=[]), [kb_search])
    output = graph.invoke(
        {
            "messages": [HumanMessage(content="查订单 ORD-1")],
            "user_id": "u1",
            "session_id": "read-failure-1",
            "context": {},
        },
        {"configurable": {"thread_id": "read-failure-1"}},
    )

    assert output["execution_status"] == "failed"
    assert output["messages"][-1].content == RUNTIME_ERROR_TEXT
```

- [ ] **Step 2: Run test to verify failure**

Run:

```bash
uv run --no-sync pytest tests/test_read_agent.py::test_read_graph_failure_uses_local_finalize_failed -q
```

Expected: FAIL because ReadGraph has no `finalize_failed` node or local error handler.

- [ ] **Step 3: Configure ReadGraph error handler**

Modify `make_read_graph`:

```python
from langgraph.types import Command

from ai_service_agent.graph.fault_tolerance import RETRY_LLM, make_subgraph_error_handler
from ai_service_agent.graph.workflows.copy import RUNTIME_ERROR_TEXT


def make_read_graph(model, tools: list, tool_hooks=None):
    middleware = [AllowedToolsMiddleware(), ModelCallLimitMiddleware(run_limit=READ_MODEL_CALL_LIMIT, exit_behavior="end")]
    if tool_hooks:
        middleware.append(ToolHookMiddleware(tool_hooks))

    agent = create_agent(
        model,
        tools=tools,
        system_prompt=READ_SYSTEM_PROMPT,
        state_schema=ReadAgentState,
        middleware=middleware,
    )
    tools_node = ToolNode(tools)

    def finalize(state: dict) -> dict:
        return {"execution_status": "completed"}

    def finalize_failed(state: dict) -> dict:
        return {"messages": [AIMessage(content=RUNTIME_ERROR_TEXT)], "execution_status": "failed"}

    builder = StateGraph(CustomerServiceState).set_node_defaults(
        error_handler=make_subgraph_error_handler("finalize_failed"),
    )
    builder.add_node("agent_llm", agent, retry_policy=RETRY_LLM)
    builder.add_node("tools", tools_node)
    builder.add_node("finalize", finalize)
    builder.add_node("finalize_failed", finalize_failed)
    builder.add_edge(START, "agent_llm")
    builder.add_conditional_edges(
        "agent_llm", tools_condition, {"tools": "tools", "__end__": "finalize"}
    )
    builder.add_edge("tools", "agent_llm")
    builder.add_edge("finalize", END)
    builder.add_edge("finalize_failed", END)
    return builder.compile()
```

Add `AIMessage` import from `langchain_core.messages` if missing.

- [ ] **Step 4: Run test**

Run:

```bash
uv run --no-sync pytest tests/test_read_agent.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/ai_service_agent/graph/agents/read_agent.py tests/test_read_agent.py
git commit -m "feat: add read graph local failure terminal"
```

---

### Task 5: Add MutationGraph Failure Terminal and Tool Error Boundary

**Files:**
- Modify: `src/ai_service_agent/graph/workflows/mutation_agent.py:41-140`
- Test: `tests/test_mutation_graph.py`

**Interfaces:**
- Consumes: `make_subgraph_error_handler("finalize_failed")`, `RETRY_LLM`, `RUNTIME_ERROR_TEXT`.
- Produces: MutationGraph terminal node `finalize_failed`; `tools` raises unexpected errors instead of fabricating success.

- [ ] **Step 1: Write failing tool-exception test**

Add to `tests/test_mutation_graph.py`:

```python
from unittest.mock import MagicMock

from ai_service_agent.graph.workflows.copy import RUNTIME_ERROR_TEXT
from ai_service_agent.tools import write as write_mod


def test_mutation_tool_exception_uses_local_finalize_failed(seed, monkeypatch):
    module, db_path = seed
    _use_db(monkeypatch, db_path)
    monkeypatch.setattr(
        write_mod.update_address,
        "invoke",
        MagicMock(side_effect=RuntimeError("database unavailable")),
    )

    graph = build_graph(_mutation_model(), tools=[kb_search], checkpointer=MemorySaver())
    _invoke(graph, "改一下收货地址", "mutation-failure-1")
    output = _invoke(graph, "确认", "mutation-failure-1", resume={"action": "confirm"})

    assert output["execution_status"] == "failed"
    assert output["messages"][-1].content == RUNTIME_ERROR_TEXT
    assert "已更新完成" not in output["messages"][-1].content
```

- [ ] **Step 2: Run test to verify failure**

Run:

```bash
uv run --no-sync pytest tests/test_mutation_graph.py::test_mutation_tool_exception_uses_local_finalize_failed -q
```

Expected: FAIL because the tool exception is not routed to a local `finalize_failed`.

- [ ] **Step 3: Configure MutationGraph error handler**

Modify `make_mutation_graph`:

```python
from ai_service_agent.graph.fault_tolerance import RETRY_LLM, make_subgraph_error_handler
from ai_service_agent.graph.workflows.copy import RUNTIME_ERROR_TEXT
```

Inside the factory:

```python
def finalize_failed(state: dict) -> dict:
    return {"messages": [AIMessage(content=RUNTIME_ERROR_TEXT)], "execution_status": "failed"}
```

Change graph construction:

```python
builder = StateGraph(CustomerServiceState).set_node_defaults(
    error_handler=make_subgraph_error_handler("finalize_failed"),
)
builder.add_node("select_llm", make_select_node(model, "mutation", tools, registry), retry_policy=RETRY_LLM)
builder.add_node("finalize_failed", finalize_failed)
builder.add_node(
    "tools",
    ToolNode(
        tools,
        handle_tool_errors=False,
        wrap_tool_call=boundary_sync,
        awrap_tool_call=boundary_async,
    ),
)
builder.add_edge("finalize_failed", END)
```

Leave all existing conditional edges and business finalizers unchanged.

- [ ] **Step 4: Run mutation tests**

Run:

```bash
uv run --no-sync pytest tests/test_mutation_graph.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/ai_service_agent/graph/workflows/mutation_agent.py tests/test_mutation_graph.py
git commit -m "feat: add mutation graph failure terminal"
```

---

### Task 6: Add HighRiskGraph Failure Terminal and Preserve Approval Semantics

**Files:**
- Modify: `src/ai_service_agent/graph/workflows/high_risk_agent.py:48-251`
- Test: `tests/test_high_risk_graph.py`

**Interfaces:**
- Consumes: `make_subgraph_error_handler("finalize_failed")`, `RETRY_LLM`, `RUNTIME_ERROR_TEXT`.
- Produces: HighRiskGraph terminal node `finalize_failed`; approval, rejection, and expiration paths remain unchanged.

- [ ] **Step 1: Write failing tool-exception test**

Add to `tests/test_high_risk_graph.py`:

```python
from unittest.mock import MagicMock

from ai_service_agent.graph.workflows.copy import RUNTIME_ERROR_TEXT
from ai_service_agent.tools import write as write_mod


def test_high_risk_tool_exception_uses_local_finalize_failed(monkeypatch, tmp_path):
    _, db_path = _load_refund_seed(tmp_path, "highrisk-tool-failure")
    _use_cancel_db(monkeypatch, db_path)
    conn = sqlite3.connect(db_path)
    try:
        owner, order_id = conn.execute(
            "SELECT customer_id, order_id FROM orders WHERE status='paid' LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    monkeypatch.setattr(
        write_mod.refund_apply,
        "invoke",
        MagicMock(side_effect=RuntimeError("database unavailable")),
    )
    model = ToolCallingFakeModel(responses=[
        router_json_message("order", "high_risk", 0.9),
        select_json_message("refund_apply", {"order_id": order_id, "amount": 1.0}),
    ])
    graph = build_graph(model, tools=[kb_search], checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "highrisk-failure-1"}}
    graph.invoke({
        "messages": [HumanMessage(content="申请退款")],
        "user_id": owner,
        "session_id": "highrisk-failure-1",
        "context": {},
    }, config)
    output = graph.invoke(Command(resume={"action": "approve"}), config)

    assert output["execution_status"] == "failed"
    assert output["messages"][-1].content == RUNTIME_ERROR_TEXT
    assert "已提交成功" not in output["messages"][-1].content
```

- [ ] **Step 2: Run test to verify failure**

Run:

```bash
uv run --no-sync pytest tests/test_high_risk_graph.py::test_high_risk_tool_exception_uses_local_finalize_failed -q
```

Expected: FAIL because HighRiskGraph has no local `finalize_failed`.

- [ ] **Step 3: Configure HighRiskGraph error handler**

Modify `make_high_risk_graph`:

```python
from ai_service_agent.graph.fault_tolerance import RETRY_LLM, make_subgraph_error_handler
from ai_service_agent.graph.workflows.copy import RUNTIME_ERROR_TEXT
```

Inside the factory:

```python
def finalize_failed(state: dict) -> dict:
    return {"messages": [AIMessage(content=RUNTIME_ERROR_TEXT)], "execution_status": "failed"}
```

Change graph construction:

```python
builder = StateGraph(CustomerServiceState).set_node_defaults(
    error_handler=make_subgraph_error_handler("finalize_failed"),
)
builder.add_node("select_llm", make_select_node(model, "high_risk", tools, registry), retry_policy=RETRY_LLM)
builder.add_node("finalize_failed", finalize_failed)
builder.add_node(
    "tools",
    ToolNode(
        tools,
        handle_tool_errors=False,
        wrap_tool_call=boundary_sync,
        awrap_tool_call=boundary_async,
    ),
)
builder.add_edge("finalize_failed", END)
```

Leave approval interruption, recheck, execution, and all approval finalizers unchanged.

- [ ] **Step 4: Run high-risk tests**

Run:

```bash
uv run --no-sync pytest tests/test_high_risk_graph.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Verify interrupt paths are unchanged**

Run:

```bash
uv run --no-sync pytest tests/test_high_risk_graph.py::test_high_risk_reaches_approval_interrupt tests/test_high_risk_graph.py::test_high_risk_approve_executes_tool tests/test_high_risk_graph.py::test_high_risk_reject_no_tool tests/test_high_risk_graph.py::test_high_risk_expired_no_tool -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/ai_service_agent/graph/workflows/high_risk_agent.py tests/test_high_risk_graph.py
git commit -m "feat: add high risk graph failure terminal"
```

---

### Task 7: Correct select_llm Exception Boundary

**Files:**
- Modify: `src/ai_service_agent/graph/workflows/select.py:176-195`
- Modify: `tests/test_select.py:150-169`

**Interfaces:**
- Consumes: `RetryPolicy` attached by MutationGraph and HighRiskGraph.
- Produces: provider exceptions propagate to LangGraph; missing/invalid `tool_calls` remain a deterministic parse rejection.

- [ ] **Step 1: Update the invocation-failure test**

In `tests/test_select.py`, replace `test_select_real_model_rejects_invocation_failure` with:

```python
def test_select_real_model_provider_failure_propagates_for_retry():
    bound_model = MagicMock()
    bound_model.invoke.side_effect = RuntimeError("provider unavailable")
    model = MagicMock()
    model.bind_tools.return_value = bound_model
    node, _ = _real_model_node(model)

    with pytest.raises(RuntimeError, match="provider unavailable"):
        node({
            "messages": [HumanMessage(content="把手机号改成13800138000")],
            "user_id": "u1",
            "context": {},
        })
```

Add `import pytest` at the top if missing.

- [ ] **Step 2: Run test to verify failure**

Run:

```bash
uv run --no-sync pytest tests/test_select.py::test_select_real_model_provider_failure_propagates_for_retry -q
```

Expected: FAIL because the node currently catches the exception and returns `select_parse_failed`.

- [ ] **Step 3: Remove the broad invocation catch**

Replace the real-model block:

```python
try:
    result = bound.invoke(messages)
    if not result.tool_calls:
        return _reject(state, "select_parse_failed")
    tool_call = result.tool_calls[0]
    tool_name = tool_call["name"]
    args = dict(tool_call.get("args") or {})
except Exception:
    return _reject(state, "select_parse_failed")
```

with:

```python
result = bound.invoke(messages)
if not result.tool_calls:
    return _reject(state, "select_parse_failed")
tool_call = result.tool_calls[0]
tool_name = tool_call["name"]
args = dict(tool_call.get("args") or {})
```

Do not catch missing tool calls or schema validation errors; those remain deterministic fail-closed behavior.

- [ ] **Step 4: Run select tests**

Run:

```bash
uv run --no-sync pytest tests/test_select.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/ai_service_agent/graph/workflows/select.py tests/test_select.py
git commit -m "fix: allow select provider errors to use langgraph retry"
```

---

### Task 8: Regression and Documentation

**Files:**
- Create: `docs/decisions/2026-09-25-langgraph-fault-tolerance.md`

**Interfaces:**
- Consumes: Tasks 1-7.
- Produces: completed fault-tolerance phase 1 and follow-up scope for async timeouts.

- [ ] **Step 1: Run targeted regression**

Run:

```bash
$env:JEV_ROUTE_INTENT_ENABLED="false"; $env:JEV_ENABLED="false"; uv run --no-sync pytest tests/test_fault_tolerance.py tests/test_router_fault_tolerance.py tests/test_router.py tests/test_read_agent.py tests/test_mutation_graph.py tests/test_high_risk_graph.py tests/test_select.py -q
```

Expected: all targeted tests pass.

- [ ] **Step 2: Run full offline regression**

Run:

```bash
$env:JEV_ROUTE_INTENT_ENABLED="false"; $env:JEV_ENABLED="false"; uv run --no-sync pytest -q
```

Expected: 473+ passed, 0 failed.

- [ ] **Step 3: Run interrupt-focused regression**

Run:

```bash
uv run --no-sync pytest tests/test_mutation_graph.py::test_mutation_reaches_confirmation_interrupt tests/test_mutation_graph.py::test_mutation_confirm_executes_tool tests/test_high_risk_graph.py::test_high_risk_reaches_approval_interrupt tests/test_high_risk_graph.py::test_high_risk_approve_executes_tool tests/test_high_risk_graph.py::test_high_risk_reject_no_tool tests/test_high_risk_graph.py::test_high_risk_expired_no_tool -q
```

Expected: all tests pass.

- [ ] **Step 4: Record the architecture decision**

Create `docs/decisions/2026-09-25-langgraph-fault-tolerance.md` with:

```markdown
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
```

- [ ] **Step 5: Commit**

```bash
git add docs/decisions/2026-09-25-langgraph-fault-tolerance.md
git commit -m "docs: record langgraph fault tolerance decision"
```
