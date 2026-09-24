# Harness Tool Hook MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为工具执行增加最小 Harness pre/post hook 扩展点（日志/观测 MVP），通过 LangGraph `ToolNode` 原生 `awrap_tool_call` 挂载，不改变现有 Graph / ToolRegistry / SSE 行为。

**Architecture:** 新建 `harness/tool_hooks.py` 定义 `ToolHook` 协议与 `make_tool_hook_wrapper` 适配器；三个子图的 `ToolNode` 通过可选 `tool_hooks` 参数接入 wrapper；`harness/graph.py` 默认注入 `LoggingToolHook`。Hook 只读上下文、只记录日志，异常 fail-open。

**Tech Stack:** Python 3.14 / LangGraph 1.2.10（`ToolNode(awrap_tool_call=...)`）/ langchain-core 1.5.3 / pytest。零新增依赖。

**Spec:** `docs/specs/architecture.md` §8.1（Harness 层职责与 hook 扩展点）；`docs/specs/event-spec.md`（tool_call / tool_result 事件协议）；`AGENTS.md` §21。

## Global Constraints

- **挂在循环上，不写进循环里**：hook 只通过 `ToolNode(awrap_tool_call=...)` 挂载，禁止在 agent / 工具内部写横切逻辑（architecture.md §8.1 / AGENTS.md §21）。
- **State 只读**：hook 不得修改 `request.state`、不得产生 State 更新、不得写会话消息。
- **本 MVP 只观测**：pre-hook 不实现阻断；阻断能力留给后续任务。
- **fail-open**：任何 hook 异常只记日志，不阻断工具执行，不吞掉工具自身异常。
- **SSE 事件协议不变**：`tool_call` / `tool_result` 事件的结构与顺序不得变化，hook 不发事件（event-spec.md）。
- **观测数据不写入 LangGraph State / 会话消息**。
- 零新增依赖；Python 3.14 兼容。
- 每个任务只跑定向测试；全量回归 + `langgraph validate` 只在最后执行。
- Git 提交显式 `git add -- <files>`，禁止 `git add .` / `git add -A`。

## File Structure

| 文件 | 职责 |
|---|---|
| `src/ai_service_agent/harness/tool_hooks.py`（新建） | hook 协议、上下文/结果数据类、`LoggingToolHook`、`make_tool_hook_wrapper` 适配器 |
| `src/ai_service_agent/graph/agents/read_agent.py`（修改） | `make_read_graph` 接受 `tool_hooks`，传给 `ToolNode` |
| `src/ai_service_agent/graph/workflows/mutation_agent.py`（修改） | `make_mutation_graph` 接受 `tool_hooks`，传给 `ToolNode` |
| `src/ai_service_agent/graph/workflows/high_risk_agent.py`（修改） | `make_high_risk_graph` 接受 `tool_hooks`，传给 `ToolNode` |
| `src/ai_service_agent/graph/builder.py`（修改） | `build_graph` 接受 `tool_hooks` 并下发到三个子图 |
| `src/ai_service_agent/harness/graph.py`（修改） | `make_ai_graph` 默认注入 `LoggingToolHook` |
| `tests/test_tool_hooks.py`（新建） | 全部新测试 |

## 已验证的 API 事实（2026-09-22，LangGraph 1.2.10，`.venv/Lib/site-packages/langgraph/prebuilt/tool_node.py`）

- `ToolCallRequest` 是 dataclass：`tool_call`（dict，含 `name`/`args`/`id`）、`tool`、`state`（图 state，dict）、`runtime`（`ToolRuntime`）。`state` 与 `user_id` 真实可用（ReadAgentState / CustomerServiceState 均含 `user_id`）。
- `request.runtime.config` 是当前执行的 `RunnableConfig`，`["configurable"]["thread_id"]` 真实可用。
- `ToolNode.__init__` 支持 `awrap_tool_call: AsyncToolCallWrapper | None`，签名为 `async def wrapper(request, execute) -> ToolMessage | Command`；`execute` 是 `async def execute(request) -> ToolMessage | Command`。
- `ToolCallRequest` **未**从 `langgraph.prebuilt` 顶层导出（仅在 `tool_node` 模块内）。为避免私有导入，本计划对 request 使用鸭子类型，不 import `ToolCallRequest`。

---

### Task 1: Tool hook 核心模块

**Files:**
- Create: `src/ai_service_agent/harness/tool_hooks.py`
- Test: `tests/test_tool_hooks.py`

**Interfaces:**
- Produces: `ToolHookContext(tool_name, tool_call_id, args, user_id, thread_id)`、`ToolHookOutcome(ok, result_summary, error, duration_ms)`、`ToolHook` Protocol（`on_before(ctx)` / `on_after(ctx, outcome)`）、`LoggingToolHook`、`build_hook_context(request) -> ToolHookContext`、`make_tool_hook_wrapper(hooks) -> async wrapper | None`（空列表返回 `None`）。后续任务直接消费这些名字。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_tool_hooks.py`：

```python
import asyncio

import pytest
from langchain_core.messages import ToolMessage

from ai_service_agent.harness.tool_hooks import (
    ToolHookContext,
    build_hook_context,
    make_tool_hook_wrapper,
)


class FakeRuntime:
    def __init__(self, config):
        self.config = config


class FakeRequest:
    def __init__(self, tool_call, state, runtime):
        self.tool_call = tool_call
        self.state = state
        self.runtime = runtime


def _request(**overrides):
    tool_call = {
        "name": overrides.get("tool_name", "kb_search"),
        "args": {"text": "hi"},
        "id": "call-1",
        "type": "tool_call",
    }
    state = overrides.get("state", {"user_id": "u1"})
    runtime = FakeRuntime(config={"configurable": {"thread_id": "t1"}})
    return FakeRequest(tool_call=tool_call, state=state, runtime=runtime)


class Recorder:
    def __init__(self):
        self.events = []

    def on_before(self, context):
        self.events.append(("before", context))

    def on_after(self, context, outcome):
        self.events.append(("after", context, outcome))


class BoomHook:
    """所有回调都抛异常，用于验证 fail-open。"""

    def on_before(self, context):
        raise RuntimeError("hook boom")

    def on_after(self, context, outcome):
        raise RuntimeError("hook boom")


async def _execute_ok(request):
    return ToolMessage(content="ok result", name=request.tool_call["name"], tool_call_id=request.tool_call["id"])


def test_build_context_extracts_fields():
    ctx = build_hook_context(_request())
    assert ctx == ToolHookContext(
        tool_name="kb_search",
        tool_call_id="call-1",
        args={"text": "hi"},
        user_id="u1",
        thread_id="t1",
    )


def test_build_context_tolerates_non_dict_state_and_missing_config():
    ctx = build_hook_context(_request(state=None))
    assert ctx.user_id == ""
    assert ctx.thread_id == ""


def test_wrapper_reports_success_outcome():
    recorder = Recorder()
    wrapper = make_tool_hook_wrapper([recorder])
    result = asyncio.run(wrapper(_request(), _execute_ok))
    assert isinstance(result, ToolMessage)
    assert recorder.events[0][0] == "before"
    assert recorder.events[0][1].tool_name == "kb_search"
    _, _, outcome = recorder.events[1]
    assert outcome.ok is True
    assert outcome.result_summary == "ok result"
    assert outcome.error is None
    assert outcome.duration_ms >= 0


def test_wrapper_reports_error_and_reraises():
    recorder = Recorder()
    wrapper = make_tool_hook_wrapper([recorder])

    async def execute(request):
        raise RuntimeError("tool boom")

    with pytest.raises(RuntimeError, match="tool boom"):
        asyncio.run(wrapper(_request(), execute))
    _, _, outcome = recorder.events[-1]
    assert outcome.ok is False
    assert outcome.error == "tool boom"
    assert outcome.duration_ms >= 0


def test_wrapper_fail_open_on_hook_errors():
    recorder = Recorder()
    wrapper = make_tool_hook_wrapper([BoomHook(), recorder])
    result = asyncio.run(wrapper(_request(), _execute_ok))
    assert result.content == "ok result"
    assert recorder.events[0][0] == "before"
    assert recorder.events[1][0] == "after"


def test_empty_hooks_return_none():
    assert make_tool_hook_wrapper([]) is None
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run --no-sync pytest tests/test_tool_hooks.py -q`
Expected: FAIL / ERROR（`ModuleNotFoundError: ai_service_agent.harness.tool_hooks`）

- [ ] **Step 3: 最小实现**

创建 `src/ai_service_agent/harness/tool_hooks.py`：

```python
"""Harness tool hooks（architecture.md §8.1：挂在循环上，不写进循环里）。

MVP 只做日志/观测：State 只读、不发 SSE 事件、hook 异常 fail-open。
通过 ToolNode 原生 awrap_tool_call 挂载（LangGraph 1.2.10 实测签名：
async def wrapper(request, execute) -> ToolMessage | Command）。
"""

import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol

from langchain_core.messages import ToolMessage
from langgraph.types import Command

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolHookContext:
    tool_name: str
    tool_call_id: str
    args: dict[str, Any]
    user_id: str
    thread_id: str


@dataclass(frozen=True)
class ToolHookOutcome:
    ok: bool
    result_summary: str | None
    error: str | None
    duration_ms: float


class ToolHook(Protocol):
    def on_before(self, context: ToolHookContext) -> None: ...

    def on_after(self, context: ToolHookContext, outcome: ToolHookOutcome) -> None: ...


class LoggingToolHook:
    """默认观测 hook：结构化日志（stdlib logging，观测体系后续升级 structlog）。"""

    def on_before(self, context: ToolHookContext) -> None:
        logger.info(
            "tool.call",
            extra={
                "tool_name": context.tool_name,
                "tool_call_id": context.tool_call_id,
                "user_id": context.user_id,
                "thread_id": context.thread_id,
            },
        )

    def on_after(self, context: ToolHookContext, outcome: ToolHookOutcome) -> None:
        payload = {
            "tool_name": context.tool_name,
            "tool_call_id": context.tool_call_id,
            "user_id": context.user_id,
            "thread_id": context.thread_id,
            "duration_ms": round(outcome.duration_ms, 3),
        }
        if outcome.ok:
            logger.info("tool.result", extra={**payload, "summary": (outcome.result_summary or "")[:120]})
        else:
            logger.warning("tool.result.error", extra={**payload, "error": outcome.error})


def build_hook_context(request: Any) -> ToolHookContext:
    """从 ToolCallRequest 提取只读上下文（鸭子类型，不 import 私有类）。"""
    tool_call = request.tool_call
    state = request.state if isinstance(request.state, dict) else {}
    runtime = getattr(request, "runtime", None)
    config = getattr(runtime, "config", None) or {}
    configurable = config.get("configurable") or {}
    return ToolHookContext(
        tool_name=tool_call["name"],
        tool_call_id=tool_call["id"],
        args=dict(tool_call.get("args") or {}),
        user_id=str(state.get("user_id") or ""),
        thread_id=str(configurable.get("thread_id") or ""),
    )


def _is_ok(result: Any) -> bool:
    if isinstance(result, ToolMessage):
        return result.status != "error"
    return True


def _summarize(result: Any) -> str | None:
    if isinstance(result, ToolMessage):
        text = result.content if isinstance(result.content, str) else str(result.content)
        return text[:200]
    if isinstance(result, Command):
        return "command"
    if result is None:
        return None
    return str(result)[:200]


def make_tool_hook_wrapper(hooks):
    """生成 ToolNode awrap_tool_call 兼容包装器；hooks 为空返回 None（零开销直通）。"""
    hook_list = list(hooks)
    if not hook_list:
        return None

    async def wrapper(request: Any, execute) -> Any:
        context = build_hook_context(request)
        for hook in hook_list:
            try:
                hook.on_before(context)
            except Exception:
                logger.exception("tool hook on_before failed; fail-open")
        start = time.perf_counter()
        try:
            result = await execute(request)
        except Exception as exc:
            duration_ms = (time.perf_counter() - start) * 1000
            outcome = ToolHookOutcome(ok=False, result_summary=None, error=str(exc), duration_ms=duration_ms)
            for hook in hook_list:
                try:
                    hook.on_after(context, outcome)
                except Exception:
                    logger.exception("tool hook on_after failed; fail-open")
            raise
        duration_ms = (time.perf_counter() - start) * 1000
        outcome = ToolHookOutcome(
            ok=_is_ok(result), result_summary=_summarize(result), error=None, duration_ms=duration_ms
        )
        for hook in hook_list:
            try:
                hook.on_after(context, outcome)
            except Exception:
                logger.exception("tool hook on_after failed; fail-open")
        return result

    return wrapper
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run --no-sync pytest tests/test_tool_hooks.py -q`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add -- src/ai_service_agent/harness/tool_hooks.py tests/test_tool_hooks.py
git commit -m "feat: add harness tool hook core module"
```

### Task 2: 接线三个子图与 builder

**Files:**
- Modify: `src/ai_service_agent/graph/agents/read_agent.py:15`（`make_read_graph` 签名与 `ToolNode` 构造，原 21 行）
- Modify: `src/ai_service_agent/graph/workflows/mutation_agent.py:48`（`make_mutation_graph` 签名）与 `:98`（`ToolNode` 构造）
- Modify: `src/ai_service_agent/graph/workflows/high_risk_agent.py:166`（`make_high_risk_graph` 签名）与 `:201`（`ToolNode` 构造）
- Modify: `src/ai_service_agent/graph/builder.py:53`（`build_graph` 签名与三个子图构造）
- Test: `tests/test_tool_hooks.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `make_tool_hook_wrapper`。
- Produces: `make_read_graph(model, tools, tool_hooks=None)`、`make_mutation_graph(model, tools, registry, tool_hooks=None)`、`make_high_risk_graph(model, tools, registry, tool_hooks=None)`、`build_graph(model, tools, checkpointer=None, registry=None, tool_hooks=None)`。`tool_hooks=None` 时行为与现状完全一致。

- [ ] **Step 1: 写失败测试（追加到 `tests/test_tool_hooks.py`）**

在文件末尾追加：

```python
import importlib.util
import sqlite3
import tempfile
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from ai_service_agent.graph.agents.read_agent import make_read_graph
from ai_service_agent.graph.builder import build_graph
from ai_service_agent.testing import ToolCallingFakeModel, router_json_message, select_json_message
from ai_service_agent.tools.kb import kb_search


ROOT = Path(__file__).resolve().parents[1]


@tool
def hook_echo_tool(text: str) -> str:
    """测试用回声工具（离线确定性，不触外部资源）。"""
    return f"echo:{text}"


def test_read_graph_runs_tool_hooks():
    recorder = Recorder()
    model = ToolCallingFakeModel(responses=[
        AIMessage(content="", tool_calls=[
            {"name": "hook_echo_tool", "args": {"text": "hello"}, "id": "call-1", "type": "tool_call"},
        ]),
        AIMessage(content="查到了。"),
    ])
    graph = make_read_graph(model, tools=[hook_echo_tool], tool_hooks=[recorder])
    out = graph.invoke(
        {"messages": [], "user_id": "u1"},
        config={"configurable": {"thread_id": "t-hook-read"}},
    )
    assert out["execution_status"] == "completed"
    assert recorder.events[0][0] == "before"
    ctx = recorder.events[0][1]
    assert ctx.tool_name == "hook_echo_tool"
    assert ctx.args == {"text": "hello"}
    assert ctx.user_id == "u1"
    assert ctx.thread_id == "t-hook-read"
    assert recorder.events[1][0] == "after"
    assert "echo:hello" in recorder.events[1][2].result_summary
    assert recorder.events[1][2].ok is True


def _load_seed_module():
    spec = importlib.util.spec_from_file_location(
        "seed_mock_db_hook", ROOT / "scripts" / "seed_mock_db.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


import pytest


@pytest.fixture(scope="module")
def hook_seed():
    module = _load_seed_module()
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "hook.db"
        module.build_database(str(db_path))
        yield module, db_path


def _patch_hook_db(monkeypatch, db_path):
    from ai_service_agent.tools import orders as orders_mod
    from ai_service_agent.tools import write as write_mod

    class _Settings:
        mock_ecommerce_db_path = str(db_path)

    monkeypatch.setattr(write_mod, "get_settings", lambda: _Settings())
    monkeypatch.setattr(orders_mod, "get_settings", lambda: _Settings())


def test_mutation_graph_runs_tool_hooks(hook_seed, monkeypatch):
    _, db_path = hook_seed
    _patch_hook_db(monkeypatch, db_path)
    recorder = Recorder()
    model = ToolCallingFakeModel(responses=[
        router_json_message("order", "mutation", 0.9),
        select_json_message("update_address", {"address": "北京市朝阳区"}),
    ])
    graph = build_graph(model, tools=[kb_search], checkpointer=MemorySaver(), tool_hooks=[recorder])
    config = {"configurable": {"thread_id": "t-hook-m"}}
    graph.invoke(
        {"messages": [HumanMessage(content="改一下收货地址")], "user_id": "u1", "session_id": "t-hook-m", "context": {}},
        config,
    )
    out = graph.invoke(Command(resume={"action": "confirm"}), config)
    assert out["execution_status"] == "completed"
    afters = [event for event in recorder.events if event[0] == "after"]
    assert afters
    assert afters[-1][1].tool_name == "update_address"
    assert afters[-1][1].user_id == "u1"
    assert afters[-1][2].ok is True


def _load_refund_seed(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "seed_mock_db_hook_refund", ROOT / "scripts" / "seed_mock_db.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    db_path = tmp_path / "hook-refund.db"
    module.build_database(str(db_path))
    return module, db_path


def test_high_risk_graph_runs_tool_hooks(monkeypatch, tmp_path):
    _, db_path = _load_refund_seed(tmp_path)
    _patch_hook_db(monkeypatch, db_path)
    conn = sqlite3.connect(db_path)
    try:
        owner, order_id = conn.execute(
            "SELECT customer_id, order_id FROM orders WHERE status='paid' LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    recorder = Recorder()
    model = ToolCallingFakeModel(responses=[
        router_json_message("order", "high_risk", 0.9),
        select_json_message("refund_apply", {"order_id": order_id, "amount": 1.0}),
    ])
    graph = build_graph(model, tools=[kb_search], checkpointer=MemorySaver(), tool_hooks=[recorder])
    config = {"configurable": {"thread_id": "t-hook-hr"}}
    graph.invoke(
        {"messages": [HumanMessage(content="申请退款")], "user_id": owner, "session_id": "t-hook-hr", "context": {}},
        config,
    )
    out = graph.invoke(Command(resume={"action": "approve"}), config)
    assert out["execution_status"] == "completed"
    afters = [event for event in recorder.events if event[0] == "after"]
    assert afters
    assert afters[-1][1].tool_name == "refund_apply"
    assert afters[-1][2].ok is True
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run --no-sync pytest tests/test_tool_hooks.py -q`
Expected: FAIL（`make_read_graph() got an unexpected keyword argument 'tool_hooks'` / `build_graph() got an unexpected keyword argument 'tool_hooks'`）

- [ ] **Step 3: 最小实现**

`read_agent.py`：

```python
from ai_service_agent.harness.tool_hooks import make_tool_hook_wrapper


def make_read_graph(model, tools: list, tool_hooks=None):
    agent = create_agent(model, tools=tools, system_prompt=READ_SYSTEM_PROMPT, state_schema=ReadAgentState)
    tools_node = ToolNode(tools, awrap_tool_call=make_tool_hook_wrapper(tool_hooks) if tool_hooks else None)
```

（其余函数体不变；import 放在文件已有 import 块之后。）

`mutation_agent.py`：

```python
from ai_service_agent.harness.tool_hooks import make_tool_hook_wrapper


def make_mutation_graph(model, tools: list, registry: ToolRegistry, tool_hooks=None):
    ...
    builder.add_node("tools", ToolNode(tools, awrap_tool_call=make_tool_hook_wrapper(tool_hooks) if tool_hooks else None))
```

（只改函数签名一行与 `ToolNode` 一行；其余逻辑不动。）

`high_risk_agent.py`：

```python
from ai_service_agent.harness.tool_hooks import make_tool_hook_wrapper


def make_high_risk_graph(model, tools: list, registry: ToolRegistry, tool_hooks=None):
    ...
    builder.add_node("tools", ToolNode(tools, awrap_tool_call=make_tool_hook_wrapper(tool_hooks) if tool_hooks else None))
```

（只改函数签名一行与 `ToolNode` 一行；其余逻辑不动。）

`builder.py`：

```python
def build_graph(model, tools: list, checkpointer=None, registry=None, tool_hooks=None):
    reg = registry if registry is not None else DEFAULT_TOOL_REGISTRY
    router = make_router(model)
    guard = make_guard_node()
    read_graph = make_read_graph(model, tools, tool_hooks=tool_hooks)
    fallback = make_fallback_graph(model)
    mutation_graph = make_mutation_graph(model, reg.for_risk_level("mutation"), reg, tool_hooks=tool_hooks)
    high_risk_graph = make_high_risk_graph(model, reg.for_risk_level("high_risk"), reg, tool_hooks=tool_hooks)
```

（只改函数签名一行与三个子图构造行；其余不动。）

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run --no-sync pytest tests/test_tool_hooks.py -q`
Expected: 10 passed

- [ ] **Step 5: 跑三个子图现有定向测试（确认零回归）**

Run: `uv run --no-sync pytest tests/test_read_agent.py tests/test_mutation_graph.py tests/test_high_risk_graph.py -q`
Expected: 全部 passed

- [ ] **Step 6: Commit**

```bash
git add -- src/ai_service_agent/graph/agents/read_agent.py src/ai_service_agent/graph/workflows/mutation_agent.py src/ai_service_agent/graph/workflows/high_risk_agent.py src/ai_service_agent/graph/builder.py tests/test_tool_hooks.py
git commit -m "feat: wire tool hooks into read/mutation/high-risk graphs"
```

### Task 3: 默认 LoggingToolHook 接入 + SSE 协议兼容

**Files:**
- Modify: `src/ai_service_agent/harness/graph.py:15`（`make_ai_graph` 默认 hooks）
- Test: `tests/test_tool_hooks.py`（追加）

**Interfaces:**
- Consumes: Task 2 的 `build_graph(..., tool_hooks=...)`、Task 1 的 `LoggingToolHook`。
- Produces: `make_ai_graph(model, checkpointer, registry=None)` 行为不变，但运行链路默认带 `LoggingToolHook`（服务端 `/chat` 与 `/handoffs/{id}/resolve` 自动获得日志观测）。

- [ ] **Step 1: 写失败测试（追加到 `tests/test_tool_hooks.py`）**

```python
from ai_service_agent.harness.streaming import event_stream


def _patch_kb_client(monkeypatch):
    from ai_service_agent.tools import kb as kb_module

    class _FakeLightRag:
        def query_context(self, query):
            return {
                "context": "炸篮 4.5L",
                "references": [{"file_path": "JoyAir_A1智能空气炸锅.md", "reference_id": "1"}],
            }

    monkeypatch.setattr(kb_module._lightrag, "get_lightrag_client", lambda: _FakeLightRag())


def test_make_ai_graph_default_logging_hook_wired(monkeypatch):
    import ai_service_agent.harness.graph as graph_mod

    _patch_kb_client(monkeypatch)
    recorded = []

    class SpyHook:
        def on_before(self, context):
            recorded.append(("before", context))

        def on_after(self, context, outcome):
            recorded.append(("after", context, outcome))

    monkeypatch.setattr(graph_mod, "LoggingToolHook", SpyHook)
    model = ToolCallingFakeModel(responses=[
        router_json_message("faq", "read", 0.9),
        AIMessage(content="", tool_calls=[
            {"name": "kb_search", "args": {"text": "退款"}, "id": "call-1", "type": "tool_call"},
        ]),
        AIMessage(content="查到了。"),
    ])
    graph = graph_mod.make_ai_graph(model, MemorySaver())
    out = graph.invoke(
        {"messages": [HumanMessage(content="保修多久")], "user_id": "u1", "session_id": "s1", "context": {}},
        {"configurable": {"thread_id": "t-hook-default"}},
    )
    assert out["execution_status"] == "completed"
    assert recorded
    assert recorded[0][0] == "before"
    assert recorded[0][1].tool_name == "kb_search"
    assert recorded[-1][0] == "after"
    assert recorded[-1][2].ok is True


def test_sse_tool_events_unchanged_with_hooks(monkeypatch):
    import asyncio

    _patch_kb_client(monkeypatch)
    recorder = Recorder()
    model = ToolCallingFakeModel(responses=[
        router_json_message("faq", "read", 0.9),
        AIMessage(content="", tool_calls=[
            {"name": "kb_search", "args": {"text": "退款"}, "id": "call-1", "type": "tool_call"},
        ]),
        AIMessage(content="查到了。"),
    ])
    graph = build_graph(model, tools=[kb_search], checkpointer=MemorySaver(), tool_hooks=[recorder])

    async def _acollect(gen):
        return [event async for event in gen]

    events = asyncio.run(_acollect(event_stream(
        graph,
        config={"configurable": {"thread_id": "t-sse-hook"}},
        messages=[HumanMessage(content="保修多久")],
        extra_state={"user_id": "u1"},
    )))
    tool_calls = [event for event in events if event["type"] == "tool_call"]
    tool_results = [event for event in events if event["type"] == "tool_result"]
    assert len(tool_calls) == 1
    assert tool_calls[0]["data"]["name"] == "kb_search"
    assert tool_calls[0]["data"]["args"] == {"text": "退款"}
    assert len(tool_results) == 1
    assert tool_results[0]["data"]["name"] == "kb_search"
    assert tool_results[0]["data"]["ok"] is True
    assert events[-1]["type"] == "done"
    assert recorder.events and recorder.events[0][0] == "before"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run --no-sync pytest tests/test_tool_hooks.py::test_make_ai_graph_default_logging_hook_wired tests/test_tool_hooks.py::test_sse_tool_events_unchanged_with_hooks -q`
Expected: `test_make_ai_graph_default_logging_hook_wired` FAIL（`recorded` 为空，默认 hook 未接线）；`test_sse_tool_events_unchanged_with_hooks` PASS（SSE 兼容是守护性测试）

- [ ] **Step 3: 最小实现**

`src/ai_service_agent/harness/graph.py`：

```python
from ai_service_agent.graph.builder import build_graph
from ai_service_agent.harness.tool_hooks import LoggingToolHook
from ai_service_agent.tools import DEFAULT_TOOL_REGISTRY


def make_ai_graph(model, checkpointer, registry=None):
    reg = registry if registry is not None else DEFAULT_TOOL_REGISTRY
    tools = reg.for_risk_level("read")
    return build_graph(
        model, tools=tools, checkpointer=checkpointer, registry=reg,
        tool_hooks=[LoggingToolHook()],
    )
```

（docstring 保留原文，追加一行：默认注入 LoggingToolHook（architecture.md §8.1 harness hook 扩展点 MVP）。）

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run --no-sync pytest tests/test_tool_hooks.py -q`
Expected: 12 passed

- [ ] **Step 5: 跑 harness 定向测试**

Run: `uv run --no-sync pytest tests/test_harness_graph.py tests/test_streaming.py tests/test_server.py -q`
Expected: 全部 passed

- [ ] **Step 6: Commit**

```bash
git add -- src/ai_service_agent/harness/graph.py tests/test_tool_hooks.py
git commit -m "feat: enable default logging tool hook in harness graph"
```

### Task 4: 全量回归与最终验证（无新提交）

**Files:**
- 无新增/修改（验证任务）

**Interfaces:**
- Consumes: Task 1–3 全部产出。

- [ ] **Step 1: 全量测试**

Run: `$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest -q --basetemp="D:\AI_Project\AI Service Agent\.pytest-tmp"`
Expected: 全部 passed（基线 359 + 新增 12）

- [ ] **Step 2: langgraph 校验**

Run: `langgraph validate`
Expected: 校验通过

- [ ] **Step 3: 收尾检查**

Run: `git status --short`
Expected: 工作区无未提交的计划内改动（保留项目原有未跟踪文件 `docs/superpowers/plans/2026-09-21-refund-apply.md` 与 `scripts/real_readgraph_smoke.py`，不在本计划范围）。

## 验收清单（对照 spec）

- [x] 挂在循环上：hook 仅经 `ToolNode(awrap_tool_call=...)` 挂载，三图零循环内改动（Task 2）。
- [x] pre/post hook 均实现，pre 拿上下文、post 拿结果/错误/耗时（Task 1）。
- [x] State 只读：wrapper 不修改 request/state，不产生 State 更新（Task 1 实现约束）。
- [x] fail-open：hook 异常只记日志，工具照常执行（Task 1 测试）。
- [x] Read / Mutation / HighRisk 三图同一 wrapper（Task 2 三条集成测试）。
- [x] SSE `tool_call` / `tool_result` 协议不变（Task 3 守护测试）。
- [x] 观测数据只进日志，不进 State / 会话消息（Task 1 实现约束）。
- [x] 零新增依赖（stdlib + 现有 langgraph/langchain-core）。
- [x] 全量回归 + `langgraph validate` 收尾（Task 4）。
