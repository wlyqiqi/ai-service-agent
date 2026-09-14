# MVP 第一公里 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 从空壳仓库到一条可运行的 MVP 闭环：会话创建、conversation_mode 两态入口判断、Router/Policy/ReadGraph/Fallback、SSE 事件流、handoff 最小接管闭环（interrupt → 外层建 Record + 切 mode → resolve 回切）。

**Architecture:** FastAPI 只做 Transport；Conversation Runtime 入口层用轻量 `conversation_status`（SQLite 表）判断 `conversation_mode`，`human` 不触图；`ai` 进入 LangGraph 总图（Router → Policy → ReadGraph / Fallback → Final Response）；子图经 `interrupt(handoff_request)` 退出，外层统一建 Handoff Record + 原子切 mode；SSE 输出归一化事件。

**Tech Stack:** Python 3.14；LangGraph 1.2.10（`StateGraph` / `interrupt` / `Command` / `AsyncSqliteSaver`）；`langchain.agents.create_agent`（**禁止** `langgraph.prebuilt.create_react_agent`）；FastAPI + uvicorn + sse-starlette；pydantic-settings；pytest + httpx（TestClient）；开发期离线 `FakeMessagesListChatModel`。

**Spec:** `docs/specs/mvp-first-mile.md`（范围/数据流/验收）；权威架构 `docs/specs/handoff-spec.md`、`docs/specs/architecture.md`、`docs/specs/graph-spec.md`、`docs/specs/state-spec.md`、`docs/specs/event-spec.md`、`docs/specs/safety-spec.md`。

## Global Constraints

- Python 3.14；依赖统一在 `pyproject.toml` 声明，禁止直接 import 未登记包（`AGENTS.md` §19）。
- LangGraph API 以 `docs/project-audit.md` B 节实测清单为准；**禁止**旧教程假设（`create_react_agent` 已弃用）。
- 业务编排留在 LangGraph；FastAPI 只做 Transport（`AGENTS.md` §4）。例外：`conversation_mode` 轻量判断在入口层（`handoff-spec.md` §2）。
- `conversation_id == session_id`；`thread_id` 为 checkpoint 标识，一对一映射但独立字段（`handoff-spec.md` §5.1）。
- `conversation_mode` 两态（`ai`/`human`），`POST /sessions` 显式写 `ai`；判断不触 checkpointer（`handoff-spec.md` §1.1、§2）。
- `ai → human` 必须与创建 Handoff Record **同一事务**；发送 AI 回复前实时复检 `mode==ai`，否则丢弃（`handoff-spec.md` §1.2、§1.4）。
- 子图不自行创建 Handoff Record / 不自行切 mode，统一 `interrupt({"type":"handoff_request","reason","summary"})`（`graph-spec.md` §6.5）。
- `execution_status` 9 值；抛 `handoff_request` 后置 `waiting_handoff`，不得置 `completed`/`failed`（`state-spec.md` §1）。
- 事件走 `event-spec.md` 归一化协议：`{ "type": str, "data": object }`；每 turn 必发 `done`，`error` 后仍发 `done`。
- 人工车道 `conversation_message` 仅展示用途，与 `state.messages` 真相源**未决**（`handoff-spec.md` §5.2），不宣称 canonical。
- handoff 触发必须是确定性规则（显式关键词），LLM 无权临时决定转人工（`AGENTS.md` §18b）。

---

## Task 0: 环境与依赖

**Files:**
- Modify: `pyproject.toml`
- Delete: `agent.py`（弃用 `create_react_agent` 骨架，功能被 `graph/router.py` + `graph/agents/read_agent.py` 取代）
- Create: `src/ai_service_agent/__init__.py`（已存在，保持空）、`tests/conftest.py`

**Interfaces:**
- Produces: `pyproject.toml` 含依赖：`fastapi`、`uvicorn`、`sse-starlette`、`pydantic-settings`；dev：`pytest`、`httpx`。

- [ ] **Step 1: 安装依赖**

Run: `uv add fastapi uvicorn sse-starlette pydantic-settings && uv add --dev pytest httpx`
Expected: `pyproject.toml` 出现新依赖，`uv run python -c "import fastapi, sse_starlette, pydantic_settings; print('ok')"` 输出 `ok`。

- [ ] **Step 2: 删除弃用骨架并确认目录**

Run: `Remove-Item agent.py`（或 `git rm agent.py`），确认 `src/ai_service_agent/` 下仍只有 `__init__.py`。
Expected: `agent.py` 消失；`git status` 显示 `agent.py` 为 deleted。

- [ ] **Step 3: 写 smoke 测试**

`tests/conftest.py`:
```python
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
```

`tests/test_smoke.py`:
```python
def test_smoke():
    import ai_service_agent  # noqa: F401
    assert True
```

- [ ] **Step 4: 跑 smoke 测试**

Run: `uv run pytest tests/test_smoke.py -v`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock agent.py tests/
git commit -m "chore: 添加 MVP 依赖并移除弃用 agent 骨架"
```

## Task 1: 配置、数据库基础设施、conversation_status

**Files:**
- Create: `src/ai_service_agent/config.py`
- Create: `src/ai_service_agent/harness/__init__.py`
- Create: `src/ai_service_agent/harness/session.py`
- Test: `tests/test_config.py`、`tests/test_session.py`

**Interfaces:**
- Consumes: Task 0（pydantic-settings 可用）。
- Produces:
  - `config.py::Settings`（pydantic BaseSettings）：`db_path: str = "data/ai_service.db"`、`fake_model: bool = False`、`model_name: str`、`router_conf_threshold: float = 0.6`、`handoff_keywords: list[str]`；`get_settings() -> Settings`（模块级单例）。
  - `harness/session.py::ConversationStatusStore`：
    - `create_session(user_id: str) -> dict` 返回 `{"conversation_id": str, "thread_id": str, "mode": "ai"}`
    - `get_mode(conversation_id: str) -> str`（`"ai"` / `"human"`；不存在返回 `"ai"`，fail-open 见 `handoff-spec.md` §1.1）
    - `set_mode(conversation_id: str, mode: str) -> None`
    - `get_thread_id(conversation_id: str) -> str | None`

- [ ] **Step 1: 写失败测试（config）**

`tests/test_config.py`:
```python
from ai_service_agent.config import Settings

def test_settings_defaults():
    s = Settings(_env_file=None)
    assert s.db_path == "data/ai_service.db"
    assert s.fake_model is False
    assert s.router_conf_threshold == 0.6
    assert "转人工" in s.handoff_keywords
```

- [ ] **Step 2: 写失败测试（session，TDD 红）**

`tests/test_session.py`:
```python
import sqlite3
from ai_service_agent.harness.session import ConversationStatusStore

def _store(tmp_path):
    conn = sqlite3.connect(tmp_path / "test.db")
    return ConversationStatusStore(conn), conn

def test_create_session_writes_ai_mode(tmp_path):
    store, conn = _store(tmp_path)
    info = store.create_session("u1")
    assert info["mode"] == "ai"
    assert store.get_mode(info["conversation_id"]) == "ai"
    assert store.get_thread_id(info["conversation_id"]) == info["thread_id"]
    conn.close()

def test_mode_is_two_state(tmp_path):
    store, conn = _store(tmp_path)
    info = store.create_session("u1")
    store.set_mode(info["conversation_id"], "human")
    assert store.get_mode(info["conversation_id"]) == "human"
    conn.close()

def test_missing_session_fail_open_ai(tmp_path):
    store, conn = _store(tmp_path)
    assert store.get_mode("nope") == "ai"
    conn.close()
```

- [ ] **Step 3: 跑测试确认失败**

Run: `uv run pytest tests/test_config.py tests/test_session.py -v`
Expected: FAIL（`ModuleNotFoundError` / 类未定义）。

- [ ] **Step 4: 实现 config.py**

`src/ai_service_agent/config.py`:
```python
from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    db_path: str = "data/ai_service.db"
    fake_model: bool = False
    model_name: str = "openai-compatible"
    model_endpoint: str | None = None
    model_api_key: str | None = None
    router_conf_threshold: float = 0.6
    handoff_keywords: list[str] = ["转人工", "人工客服", "找真人", "人工"]


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

- [ ] **Step 5: 实现 harness/session.py**

`src/ai_service_agent/harness/session.py`:
```python
import sqlite3
import uuid


SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation_status (
    conversation_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'ai',
    user_id TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
"""


class ConversationStatusStore:
    """轻量 conversation_status：conversation_id -> mode（两态）。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.conn.execute(SCHEMA)
        self.conn.commit()

    def create_session(self, user_id: str) -> dict:
        conversation_id = str(uuid.uuid4())
        thread_id = str(uuid.uuid4())
        self.conn.execute(
            "INSERT INTO conversation_status (conversation_id, thread_id, mode, user_id) VALUES (?, ?, 'ai', ?)",
            (conversation_id, thread_id, user_id),
        )
        self.conn.commit()
        return {"conversation_id": conversation_id, "thread_id": thread_id, "mode": "ai"}

    def get_mode(self, conversation_id: str) -> str:
        row = self.conn.execute(
            "SELECT mode FROM conversation_status WHERE conversation_id = ?", (conversation_id,)
        ).fetchone()
        # 两态判断，fail-open：缺失/意外值按 ai 放行（handoff-spec.md §1.1）
        return "human" if row and row[0] == "human" else "ai"

    def set_mode(self, conversation_id: str, mode: str) -> None:
        self.conn.execute(
            "UPDATE conversation_status SET mode = ? WHERE conversation_id = ?", (mode, conversation_id)
        )
        self.conn.commit()

    def get_thread_id(self, conversation_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT thread_id FROM conversation_status WHERE conversation_id = ?", (conversation_id,)
        ).fetchone()
        return row[0] if row else None
```

- [ ] **Step 6: 跑测试确认通过**

Run: `uv run pytest tests/test_config.py tests/test_session.py -v`
Expected: PASS。

- [ ] **Step 7: Commit**

```bash
git add src/ai_service_agent/config.py src/ai_service_agent/harness/ tests/test_config.py tests/test_session.py
git commit -m "feat: 配置 + conversation_status 轻量存储（两态判断）"
```

## Task 2: State 定义 + 工具注册表 + stub 工具

**Files:**
- Create: `src/ai_service_agent/graph/__init__.py`、`src/ai_service_agent/graph/state.py`
- Create: `src/ai_service_agent/tools/__init__.py`、`src/ai_service_agent/tools/registry.py`、`src/ai_service_agent/tools/kb.py`、`src/ai_service_agent/tools/orders.py`
- Test: `tests/test_state.py`、`tests/test_tools.py`

**Interfaces:**
- Consumes: 无（纯模型 + 工具层）。
- Produces:
  - `graph/state.py::CustomerServiceState`（TypedDict）：`messages`（`Annotated[list, add_messages]`）、`user_id`、`session_id`、`capability`、`risk_level`（`Literal["read","mutation","high_risk"]`）、`confidence`、`context`、`execution_status`（9 值 Literal）、`handoff: bool`。
  - `tools/registry.py::ToolRegistry`：
    - `register(meta: ToolMeta, fn)`、`for_risk_level(level: str) -> list`（构建期按 `min_risk_level` 过滤注入）
    - `ToolMeta` dataclass：`name`、`description`、`min_risk_level`、`idempotent: bool = False`
  - `tools/kb.py::kb_search(text: str) -> str`（stub，返回固定结果）
  - `tools/orders.py::order_lookup(order_id: str) -> dict`（stub，返回固定订单）

- [ ] **Step 1: 写失败测试（state + tools）**

`tests/test_state.py`:
```python
from ai_service_agent.graph.state import CustomerServiceState, EXECUTION_STATUSES

def test_execution_status_has_waiting_handoff():
    assert "waiting_handoff" in EXECUTION_STATUSES
    assert len(EXECUTION_STATUSES) == 9

def test_state_requires_session_id():
    s: CustomerServiceState = {"messages": [], "user_id": "u1", "session_id": "s1"}
    assert s["session_id"] == "s1"
```

`tests/test_tools.py`:
```python
from ai_service_agent.tools.registry import ToolRegistry, ToolMeta
from ai_service_agent.tools.kb import kb_search
from ai_service_agent.tools.orders import order_lookup

def test_registry_injects_by_risk_level():
    reg = ToolRegistry()
    reg.register(ToolMeta("kb_search", "检索", "read"), kb_search)
    reg.register(ToolMeta("order_lookup", "查订单", "read"), order_lookup)
    assert {f.__name__ for f in reg.for_risk_level("read")} == {"kb_search", "order_lookup"}
    # MVP 只有 read 工具；后续写工具在 higher risk 才注入
    assert {f.__name__ for f in reg.for_risk_level("high_risk")} == {"kb_search", "order_lookup"}

def test_stub_tools_return_values():
    assert "结果" in kb_search("退款")
    assert order_lookup("ORD-1")["order_id"] == "ORD-1"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_state.py tests/test_tools.py -v`
Expected: FAIL（模块未定义）。

- [ ] **Step 3: 实现 state.py**

`src/ai_service_agent/graph/state.py`:
```python
from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

EXECUTION_STATUSES = (
    "idle", "routing", "clarifying", "running",
    "waiting_confirmation", "waiting_approval",
    "waiting_handoff", "completed", "failed",
)


class CustomerServiceState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    user_id: str
    session_id: str
    capability: str
    risk_level: Literal["read", "mutation", "high_risk"]
    confidence: float
    context: dict
    execution_status: Literal[
        "idle", "routing", "clarifying", "running",
        "waiting_confirmation", "waiting_approval",
        "waiting_handoff", "completed", "failed",
    ]
    handoff: bool
```

- [ ] **Step 4: 实现 tools 层**

`src/ai_service_agent/tools/registry.py`:
```python
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class ToolMeta:
    name: str
    description: str
    min_risk_level: str
    idempotent: bool = False


@dataclass
class ToolRegistry:
    _tools: dict[str, tuple[ToolMeta, Callable]] = field(default_factory=dict)

    def register(self, meta: ToolMeta, fn: Callable) -> None:
        self._tools[meta.name] = (meta, fn)

    def for_risk_level(self, level: str) -> list:
        """构建期注入：只给该风险等级及以下可用的工具。"""
        order = {"read": 0, "mutation": 1, "high_risk": 2}
        return [fn for meta, fn in self._tools.values() if order[meta.min_risk_level] <= order[level]]
```

`src/ai_service_agent/tools/kb.py`:
```python
def kb_search(text: str) -> str:
    return f"知识库检索结果：与「{text}」相关的内容（stub）。"
```

`src/ai_service_agent/tools/orders.py`:
```python
def order_lookup(order_id: str) -> dict:
    return {"order_id": order_id, "status": "已发货", "amount": 99.0}
```

> 注意：若 `FakeMessagesListChatModel` / `create_agent` 工具绑定需 `@tool` 装饰器，改用 `from langchain_core.tools import tool` 装饰同名函数，`for_risk_level` 返回 `list[BaseTool]`（后续任务消费工具名列表）。实现时以 `docs/project-audit.md` B 节实测为准。

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run pytest tests/test_state.py tests/test_tools.py -v`
Expected: PASS（若 `for_risk_level` 断言失败，按 Step 4 备注调整返回类型为 `list[BaseTool]` 并在测试中断言 `t.name`）。

- [ ] **Step 6: Commit**

```bash
git add src/ai_service_agent/graph/ src/ai_service_agent/tools/ tests/test_state.py tests/test_tools.py
git commit -m "feat: State 定义（execution_status 9 值）+ 工具注册表 + stub 只读工具"
```

## Task 3: Router 节点（create_agent + 结构化输出）

**Files:**
- Create: `src/ai_service_agent/graph/router.py`
- Test: `tests/test_router.py`

**Interfaces:**
- Consumes: Task 2 的 `CustomerServiceState`、`config.get_settings()`。
- Produces:
  - `router.py::RouterSchema`（pydantic BaseModel）：`capability: str`、`risk_level: Literal["read","mutation","high_risk"]`、`confidence: float`
  - `router.py::make_router(model) -> Callable[[dict], dict]`：返回图节点函数，输入 `state`，输出 `{"capability","risk_level","confidence","execution_status":"routing"→"running"}` 中的分类字段；低置信度 → `capability="unknown"`（走 Fallback）。

- [ ] **Step 1: 写失败测试**

`tests/test_router.py`:
```python
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from ai_service_agent.graph.router import make_router

def test_router_parses_structured_output():
    model = FakeMessagesListChatModel(
        responses=[AIMessage(content='{"capability": "order", "risk_level": "read", "confidence": 0.9}')]
    )
    node = make_router(model)
    out = node({"messages": [], "user_id": "u1", "session_id": "s1", "context": {}})
    assert out["capability"] == "order"
    assert out["risk_level"] == "read"
    assert out["confidence"] == 0.9

def test_router_low_confidence_goes_unknown():
    model = FakeMessagesListChatModel(
        responses=[AIMessage(content='{"capability": "order", "risk_level": "read", "confidence": 0.3}')]
    )
    node = make_router(model)
    out = node({"messages": [], "user_id": "u1", "session_id": "s1", "context": {}})
    assert out["capability"] == "unknown"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_router.py -v`
Expected: FAIL（`make_router` 未定义）。

- [ ] **Step 3: 实现 router.py**

`src/ai_service_agent/graph/router.py`:
```python
import json
from typing import Literal

from langchain.agents import create_agent
from pydantic import BaseModel

from ai_service_agent.config import get_settings


class RouterSchema(BaseModel):
    capability: str
    risk_level: Literal["read", "mutation", "high_risk"]
    confidence: float


ROUTER_SYSTEM = (
    "你是客服意图路由器。只输出 JSON："
    '{"capability": "faq|order|account|smalltalk|unknown", '
    '"risk_level": "read|mutation|high_risk", "confidence": 0-1}。'
    "不要输出其它内容。"
)


def make_router(model) -> Callable[[dict], dict]:
    """生产用 create_agent + response_format；fake model 下解析其 JSON 文本。"""
    agent = create_agent(
        model,
        tools=[],
        system_prompt=ROUTER_SYSTEM,
        response_format=RouterSchema,
    )

    def route(state: dict) -> dict:
        result = agent.invoke(state["messages"])
        text = result["messages"][-1].content
        payload = json.loads(text) if isinstance(text, str) else text
        conf = float(payload.get("confidence", 0.0))
        threshold = get_settings().router_conf_threshold
        return {
            "capability": payload.get("capability", "unknown") if conf >= threshold else "unknown",
            "risk_level": payload.get("risk_level", "read"),
            "confidence": conf,
            "execution_status": "routing",
        }

    return route
```

> 兼容说明：`create_agent.invoke` 返回带 `messages` 的 dict（`AgentOutput`）。若实测 `response_format` 已把输出解析为 pydantic 对象，则 `payload` 直接取 `result["messages"][-1].content` 的字段；以 `docs/project-audit.md` B 节实测为准调整解析分支。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_router.py -v`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add src/ai_service_agent/graph/router.py tests/test_router.py
git commit -m "feat: Router 节点（create_agent + 结构化输出 + 低置信度→unknown）"
```

## Task 4: ReadGraph + Fallback（含 check_handoff → interrupt）

**Files:**
- Create: `src/ai_service_agent/graph/agents/__init__.py`、`src/ai_service_agent/graph/agents/read_agent.py`
- Create: `src/ai_service_agent/graph/workflows/__init__.py`、`src/ai_service_agent/graph/workflows/fallback.py`
- Test: `tests/test_read_agent.py`、`tests/test_fallback.py`

**Interfaces:**
- Consumes: Task 2（`ToolRegistry` / stub 工具）、Task 3（无直接依赖）。
- Produces:
  - `agents/read_agent.py::make_read_graph(model, tools: list) -> CompiledStateGraph`：ReAct 子图，输出写回 `messages`，`execution_status="running"`→`"completed"`。
  - `workflows/fallback.py::make_fallback_graph(model) -> CompiledStateGraph`：兜底解释 + `check_handoff` 节点。
  - `workflows/fallback.py::check_handoff(state) -> dict`：命中显式关键词 → 置 `handoff=True`、`execution_status="waiting_handoff"`、`interrupt({"type":"handoff_request","reason","summary"})`。

- [ ] **Step 1: 写失败测试（ReadGraph）**

`tests/test_read_agent.py`:
```python
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from ai_service_agent.graph.agents.read_agent import make_read_graph
from ai_service_agent.tools.kb import kb_search

def test_read_graph_answers_and_completes():
    model = FakeMessagesListChatModel(responses=[AIMessage(content="查询结果：订单已发货。")])
    graph = make_read_graph(model, tools=[kb_search])
    out = graph.invoke({"messages": [], "user_id": "u1", "session_id": "s1", "context": {}})
    assert out["execution_status"] == "completed"
    assert "发货" in out["messages"][-1].content
```

- [ ] **Step 2: 写失败测试（check_handoff）**

`tests/test_fallback.py`:
```python
import pytest
from langchain_core.messages import HumanMessage
from langgraph.types import interrupt

from ai_service_agent.graph.workflows.fallback import check_handoff

def test_check_handoff_ignores_normal_input():
    out = check_handoff({"messages": [], "context": {}, "user_id": "u1", "session_id": "s1"})
    assert out is None or out.get("handoff") is False

def test_check_handoff_detects_keyword(monkeypatch):
    # 命中关键词时必须抛出 interrupt；monkeypatch 捕获信号内容
    captured = {}

    def fake_interrupt(value):
        captured["value"] = value
        return value

    monkeypatch.setattr("langgraph.types.interrupt", fake_interrupt)
    check_handoff({"messages": [HumanMessage(content="我要转人工")], "context": {}, "user_id": "u1", "session_id": "s1"})
    assert captured["value"]["type"] == "handoff_request"
```

- [ ] **Step 3: 跑测试确认失败**

Run: `uv run pytest tests/test_read_agent.py tests/test_fallback.py -v`
Expected: FAIL（模块未定义）。

- [ ] **Step 4: 实现 read_agent.py**

`src/ai_service_agent/graph/agents/read_agent.py`:
```python
from langchain.agents import create_agent
from langgraph.graph import END, START, StateGraph

from ai_service_agent.graph.state import CustomerServiceState


def make_read_graph(model, tools: list):
    agent = create_agent(model, tools=tools, system_prompt="你是客服助手，仅回答知识库与订单查询相关的问题。")

    def run(state: dict) -> dict:
        result = agent.invoke(state["messages"])
        return {"messages": [result["messages"][-1]], "execution_status": "completed"}

    builder = StateGraph(CustomerServiceState)
    builder.add_node("read_agent", run)
    builder.add_edge(START, "read_agent")
    builder.add_edge("read_agent", END)
    return builder.compile()
```

- [ ] **Step 5: 实现 fallback.py**

`src/ai_service_agent/graph/workflows/fallback.py`:
```python
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ai_service_agent.config import get_settings
from ai_service_agent.graph.state import CustomerServiceState


def check_handoff(state: dict) -> dict:
    """确定性规则：显式转人工关键词命中才置位；LLM 无权临时决定。"""
    last_content = state["messages"][-1].content if state.get("messages") else ""
    if any(kw in last_content for kw in get_settings().handoff_keywords):
        interrupt({"type": "handoff_request", "reason": "用户显式请求转人工", "summary": last_content[:80]})
        return {"handoff": True, "execution_status": "waiting_handoff"}
    return {"handoff": False, "execution_status": "completed"}


def make_fallback_graph(model):
    def explain(state: dict) -> dict:
        return {"messages": [{"role": "assistant", "content": "我暂时无法处理，已为你转接人工客服。"}]}

    builder = StateGraph(CustomerServiceState)
    builder.add_node("fallback_explain", explain)
    builder.add_node("check_handoff", check_handoff)
    builder.add_edge(START, "fallback_explain")
    builder.add_edge("fallback_explain", "check_handoff")
    builder.add_edge("check_handoff", END)
    return builder.compile()
```

- [ ] **Step 6: 跑测试确认通过**

Run: `uv run pytest tests/test_read_agent.py tests/test_fallback.py -v`
Expected: PASS（若 `interrupt` 在假实现下未触发，按 `graph-spec.md` §6.5 语义调整 check_handoff 为「先置位再 interrupt」并在测试断言 `execution_status`）。

- [ ] **Step 7: Commit**

```bash
git add src/ai_service_agent/graph/agents/ src/ai_service_agent/graph/workflows/ tests/test_read_agent.py tests/test_fallback.py
git commit -m "feat: ReadGraph + Fallback/check_handoff（interrupt handoff_request）"
```

## Task 5: 总图 builder（入口条件边 + Policy + 统一出口）

**Files:**
- Create: `src/ai_service_agent/graph/builder.py`
- Test: `tests/test_builder.py`

**Interfaces:**
- Consumes: Task 3 `make_router`、Task 4 `make_read_graph` / `make_fallback_graph`、Task 2 state。
- Produces:
  - `builder.py::POLICY: dict[tuple[str, str], str]`：确定性映射，`("order","read")→"read_graph"`、`("faq","read")→"read_graph"`、`("account","read")→"read_graph"`、`("smalltalk","read")→"read_graph"`、其余（含 mutation/high_risk/unknown）→ `"fallback"`。
  - `builder.py::build_graph(model, tools, checkpointer=None) -> CompiledStateGraph`：总图节点 `router`、`read_graph`、`fallback`；条件边按 `capability × risk_level` 路由；`Final Response` 节点把 `execution_status="completed"` 收尾。

- [ ] **Step 1: 写失败测试**

`tests/test_builder.py`:
```python
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage

from ai_service_agent.graph.builder import POLICY, build_graph
from ai_service_agent.tools.kb import kb_search

def test_policy_maps_read_path():
    assert POLICY[("order", "read")] == "read_graph"
    assert POLICY[("unknown", "read")] == "fallback"
    assert POLICY[("order", "high_risk")] == "fallback"  # MVP 未纳入审批

def test_build_graph_read_path():
    model = FakeMessagesListChatModel(responses=[AIMessage(content='{"capability":"order","risk_level":"read","confidence":0.9}'), AIMessage(content="订单已发货。")])
    graph = build_graph(model, tools=[kb_search])
    out = graph.invoke({"messages": [HumanMessage(content="查订单 ORD-1")], "user_id": "u1", "session_id": "s1", "context": {}})
    assert out["execution_status"] == "completed"
    assert "发货" in out["messages"][-1].content
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_builder.py -v`
Expected: FAIL（`build_graph` 未定义）。

- [ ] **Step 3: 实现 builder.py**

`src/ai_service_agent/graph/builder.py`:
```python
from langgraph.graph import END, START, StateGraph

from ai_service_agent.graph.agents.read_agent import make_read_graph
from ai_service_agent.graph.router import make_router
from ai_service_agent.graph.state import CustomerServiceState
from ai_service_agent.graph.workflows.fallback import make_fallback_graph


POLICY = {
    ("faq", "read"): "read_graph",
    ("order", "read"): "read_graph",
    ("account", "read"): "read_graph",
    ("smalltalk", "read"): "read_graph",
    # 其余组合（mutation / high_risk / unknown）MVP 走 fallback
}


def _route_target(state: dict) -> str:
    return POLICY.get((state.get("capability", "unknown"), state.get("risk_level", "read")), "fallback")


def build_graph(model, tools: list, checkpointer=None):
    router = make_router(model)
    read_graph = make_read_graph(model, tools)
    fallback = make_fallback_graph(model)

    def finalize(state: dict) -> dict:
        return {"execution_status": "completed"}

    builder = StateGraph(CustomerServiceState)
    builder.add_node("router", router)
    builder.add_node("read_graph", read_graph)
    builder.add_node("fallback", fallback)
    builder.add_node("final", finalize)
    builder.add_edge(START, "router")
    builder.add_conditional_edges("router", _route_target, {"read_graph": "read_graph", "fallback": "fallback"})
    builder.add_edge("read_graph", "final")
    builder.add_edge("fallback", "final")
    builder.add_edge("final", END)
    return builder.compile(checkpointer=checkpointer)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_builder.py -v`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add src/ai_service_agent/graph/builder.py tests/test_builder.py
git commit -m "feat: 总图 builder（Policy 映射 + 统一出口）"
```

## Task 6: SSE 事件流（streaming）+ 错误归一化（errors）

**Files:**
- Create: `src/ai_service_agent/harness/streaming.py`、`src/ai_service_agent/harness/errors.py`
- Test: `tests/test_streaming.py`、`tests/test_errors.py`

**Interfaces:**
- Consumes: Task 5 `build_graph`、Task 2 state。
- Produces:
  - `streaming.py::event_stream(graph, config: dict, on_interrupt: Callable | None = None) -> AsyncGenerator[dict, None]`：yield `{"type": str, "data": dict}`；`token`（增量文本）、`tool_call` / `tool_result`（基础检测）、`done`（必有，最后一个）、`error`（归一化）。
  - `errors.py::normalize_error(exc: Exception) -> dict`：返回 `{"type": "error", "data": {"code", "message"}}`。

- [ ] **Step 1: 写失败测试**

`tests/test_errors.py`:
```python
from ai_service_agent.harness.errors import normalize_error

def test_normalize_error_shape():
    e = ValueError("bad")
    out = normalize_error(e)
    assert out["type"] == "error"
    assert out["data"]["code"] == "ValueError"
    assert out["data"]["message"] == "bad"
```

`tests/test_streaming.py`:
```python
import asyncio

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage

from ai_service_agent.graph.builder import build_graph
from ai_service_agent.harness.streaming import event_stream
from ai_service_agent.tools.kb import kb_search


def _collect(gen):
    return asyncio.run(_acollect(gen))


async def _acollect(gen):
    return [e async for e in gen]


def test_stream_emits_token_and_done():
    model = FakeMessagesListChatModel(responses=[
        AIMessage(content='{"capability":"faq","risk_level":"read","confidence":0.9}'),
        AIMessage(content="FAQ 回答。"),
    ])
    graph = build_graph(model, tools=[kb_search])
    events = _collect(event_stream(graph, config={"thread_id": "t1"}, message=HumanMessage(content="hi")))
    types = [e["type"] for e in events]
    assert types[-1] == "done"
    assert "token" in types


def test_stream_emits_error_then_done():
    class Boom(Exception):
        pass

    def bad_model(*args, **kwargs):
        raise Boom("boom")

    graph = bad_model
    events = _collect(event_stream(graph, config={"thread_id": "t1"}, message=HumanMessage(content="hi")))
    types = [e["type"] for e in events]
    assert types[-1] == "done"
    assert "error" in types
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_streaming.py tests/test_errors.py -v`
Expected: FAIL（模块未定义）。

- [ ] **Step 3: 实现 errors.py**

`src/ai_service_agent/harness/errors.py`:
```python
def normalize_error(exc: Exception) -> dict:
    return {"type": "error", "data": {"code": type(exc).__name__, "message": str(exc)}}
```

- [ ] **Step 4: 实现 streaming.py**

`src/ai_service_agent/harness/streaming.py`:
```python
from typing import AsyncGenerator, Callable

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage

from ai_service_agent.harness.errors import normalize_error


async def event_stream(graph, config: dict, message: HumanMessage, on_interrupt: Callable | None = None) -> AsyncGenerator[dict, None]:
    try:
        async for mode, payload in graph.astream(
            {"messages": [message]}, config=config, stream_mode=["messages", "updates"]
        ):
            if mode == "messages":
                chunk, _meta = payload
                if isinstance(chunk, AIMessageChunk) and chunk.content:
                    yield {"type": "token", "data": {"text": chunk.content}}
            elif mode == "updates":
                for node_name, update in payload.items():
                    interrupts = update.get("__interrupt__")
                    if interrupts and on_interrupt is not None:
                        for it in interrupts:
                            await on_interrupt(it.value)
                    for msg in update.get("messages", []):
                        if isinstance(msg, AIMessage) and msg.tool_calls:
                            for tc in msg.tool_calls:
                                yield {"type": "tool_call", "data": {"name": tc["name"], "args": tc["args"]}}
                        elif isinstance(msg, ToolMessage):
                            yield {"type": "tool_result", "data": {"name": msg.name or "tool", "ok": True, "summary": msg.content[:80]}}
        final = _last_assistant_text(graph, config)
        yield {"type": "done", "data": {"final": final}}
    except Exception as exc:
        yield normalize_error(exc)
        yield {"type": "done", "data": {"final": None}}


def _last_assistant_text(graph, config: dict) -> str | None:
    try:
        state = graph.get_state(config)
        for m in reversed(state.values.get("messages", [])):
            if isinstance(m, AIMessage) and m.content:
                return m.content if isinstance(m.content, str) else str(m.content)
    except Exception:
        return None
    return None
```

> 兼容说明：`graph.astream` 双模式返回 `(mode, payload)` 二元组（`stream_mode` 传 list）。若实测 `interrupt` 出现在 `updates` 的 `{"__interrupt__": (Interrupt,)}` 中，按上述取值；`Interrupt.value` 即 `{"type":"handoff_request",...}`。以 `docs/project-audit.md` B 节为准。

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run pytest tests/test_streaming.py tests/test_errors.py -v`
Expected: PASS（若 token 事件在 fake model 下不产生 chunk，改断言为 `done` 在最后 + `error` 场景正确；token 为真实模型流式特性）。

- [ ] **Step 6: Commit**

```bash
git add src/ai_service_agent/harness/streaming.py src/ai_service_agent/harness/errors.py tests/test_streaming.py tests/test_errors.py
git commit -m "feat: SSE 事件流（token/tool/error/done）+ 错误归一化"
```

## Task 7: Handoff 服务（建 Record + 同事务切 mode + resolve）

**Files:**
- Create: `src/ai_service_agent/harness/handoff.py`
- Test: `tests/test_handoff.py`

**Interfaces:**
- Consumes: Task 1 `ConversationStatusStore`、Task 6 `event_stream` 的 `on_interrupt` 回调位。
- Produces:
  - `handoff.py::HandoffService`：
    - `create_handoff(conversation_id: str, reason: str, summary: str) -> dict`：INSERT `handoff_record`（status=`active`）+ UPDATE `conversation_status.mode='human'` **同一事务**；返回 record。
    - `resolve_handoff(handoff_id: str) -> dict`：record.status=`resolved` + `mode='ai'`（同一事务）；返回 `{"handoff_id", "mode": "ai"}`。
    - `make_interrupt_callback(graph_config: dict) -> Callable`：包装 `on_interrupt`，当 `value["type"]=="handoff_request"` 时 `create_handoff` 并产出 `handoff_created` 事件（供 streaming 汇入）。

- [ ] **Step 1: 写失败测试**

`tests/test_handoff.py`:
```python
import sqlite3

from ai_service_agent.harness.handoff import HandoffService
from ai_service_agent.harness.session import ConversationStatusStore


def _svc(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    status = ConversationStatusStore(conn)
    return HandoffService(conn, status), status, conn


def test_create_handoff_switches_mode_same_transaction(tmp_path):
    svc, status, conn = _svc(tmp_path)
    info = status.create_session("u1")
    rec = svc.create_handoff(info["conversation_id"], "r", "s")
    assert rec["status"] == "active"
    assert status.get_mode(info["conversation_id"]) == "human"
    conn.close()


def test_resolve_handoff_switches_back(tmp_path):
    svc, status, conn = _svc(tmp_path)
    info = status.create_session("u1")
    rec = svc.create_handoff(info["conversation_id"], "r", "s")
    out = svc.resolve_handoff(rec["handoff_id"])
    assert out["mode"] == "ai"
    assert status.get_mode(info["conversation_id"]) == "ai"
    conn.close()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_handoff.py -v`
Expected: FAIL（`HandoffService` 未定义）。

- [ ] **Step 3: 实现 handoff.py**

`src/ai_service_agent/harness/handoff.py`:
```python
import sqlite3
import uuid
from typing import Callable

from ai_service_agent.harness.session import ConversationStatusStore


SCHEMA = """
CREATE TABLE IF NOT EXISTS handoff_record (
    handoff_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    reason TEXT,
    summary TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT DEFAULT (datetime('now')),
    resolved_at TEXT
);
"""


class HandoffService:
    def __init__(self, conn: sqlite3.Connection, status: ConversationStatusStore):
        self.conn = conn
        self.status = status
        self.conn.execute(SCHEMA)
        self.conn.commit()

    def create_handoff(self, conversation_id: str, reason: str, summary: str) -> dict:
        """建 Record + 切 mode=human：同一事务（handoff-spec.md §1.2）。"""
        handoff_id = str(uuid.uuid4())
        with self.conn:  # sqlite 单连接事务块
            self.conn.execute(
                "INSERT INTO handoff_record (handoff_id, conversation_id, reason, summary) VALUES (?, ?, ?, ?)",
                (handoff_id, conversation_id, reason, summary),
            )
            self.status.set_mode(conversation_id, "human")
        return {"handoff_id": handoff_id, "conversation_id": conversation_id, "status": "active"}

    def resolve_handoff(self, handoff_id: str) -> dict:
        row = self.conn.execute(
            "SELECT conversation_id FROM handoff_record WHERE handoff_id = ?", (handoff_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"handoff not found: {handoff_id}")
        with self.conn:
            self.conn.execute(
                "UPDATE handoff_record SET status='resolved', resolved_at=datetime('now') WHERE handoff_id=?",
                (handoff_id,),
            )
            self.status.set_mode(row[0], "ai")
        return {"handoff_id": handoff_id, "mode": "ai"}

    def make_interrupt_callback(self) -> Callable:
        async def on_interrupt(value) -> dict | None:
            if isinstance(value, dict) and value.get("type") == "handoff_request":
                conv_id = value.get("conversation_id") or value.get("session_id")
                if conv_id is None:
                    return None
                rec = self.create_handoff(
                    conv_id,
                    value.get("reason", "unknown"),
                    value.get("summary", ""),
                )
                return {"type": "handoff_created", "data": rec}
            return None

        return on_interrupt
```

> 注意：`interrupt` 载荷由子图抛出（`graph-spec.md` §6.5 只有 `type/reason/summary`），`conversation_id` 由外层在 `config` 中绑定：Task 8 中 `event_stream` 调用前把 `session_id` 传入 `on_interrupt` 闭包。若按此实现，`make_interrupt_callback(conversation_id: str)` 签名接收会话 id（测试中传 `info["conversation_id"]`），`on_interrupt` 直接用它建 Record。

- [ ] **Step 4: 按备注修正后跑测试**

将 `make_interrupt_callback` 改为 `make_interrupt_callback(conversation_id: str) -> Callable`（闭包捕获会话 id），更新测试：
```python
def test_callback_creates_handoff(tmp_path):
    svc, status, conn = _svc(tmp_path)
    info = status.create_session("u1")
    cb = svc.make_interrupt_callback(info["conversation_id"])
    import asyncio
    ev = asyncio.run(cb({"type": "handoff_request", "reason": "r", "summary": "s"}))
    assert ev["type"] == "handoff_created"
    assert status.get_mode(info["conversation_id"]) == "human"
    conn.close()
```

Run: `uv run pytest tests/test_handoff.py -v`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add src/ai_service_agent/harness/handoff.py tests/test_handoff.py
git commit -m "feat: Handoff 服务（建 Record + 同事务切 mode + resolve + handoff_created 事件）"
```

## Task 8: FastAPI server（sessions / chat / 人工车道 / 发送前校验 / resolve）

**Files:**
- Create: `src/ai_service_agent/harness/server.py`
- Modify: `src/ai_service_agent/harness/streaming.py`（`on_interrupt` 回调返回值汇入事件流）
- Test: `tests/test_server.py`

**Interfaces:**
- Consumes: Task 1 `ConversationStatusStore`、Task 5 `build_graph`、Task 6 `event_stream` / `normalize_error`、Task 7 `HandoffService`。
- Produces:
  - `server.py::create_app(db_path: str, model=None) -> FastAPI`：
    - `POST /sessions` 请求体 `{user_id}` → `{session_id, mode:"ai"}`（`thread_id` 内部持有）
    - `POST /chat` 请求体 `{session_id, message}` → SSE 事件流（`text/event-stream`）
    - `POST /handoffs/{handoff_id}/resolve` → `{handoff_id, mode:"ai"}`
  - `conversation_message` 表：人工车道写入（仅展示用途，真相源未决）。

- [ ] **Step 1: 写失败测试**

`tests/test_server.py`:
```python
from fastapi.testclient import TestClient

from ai_service_agent.harness.server import create_app


def test_sessions_creates_ai_mode(tmp_path):
    client = TestClient(create_app(str(tmp_path / "s.db"), model="fake"))
    r = client.post("/sessions", json={"user_id": "u1"})
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "ai"
    assert body["session_id"]


def test_chat_unknown_session_errors(tmp_path):
    client = TestClient(create_app(str(tmp_path / "s.db"), model="fake"))
    with client.stream("POST", "/chat", json={"session_id": "nope", "message": "hi"}) as r:
        text = "".join(r.iter_text())
    assert "error" in text


def test_human_lane_after_handoff(tmp_path):
    from ai_service_agent.harness.handoff import HandoffService
    import sqlite3
    from ai_service_agent.harness.session import ConversationStatusStore

    conn = sqlite3.connect(tmp_path / "s.db")
    # 直接构造：先建会话再手动切 human 模拟人工接管
    store = ConversationStatusStore(conn)
    info = store.create_session("u1")
    store.set_mode(info["conversation_id"], "human")
    conn.close()

    client = TestClient(create_app(str(tmp_path / "s.db"), model="fake"))
    with client.stream("POST", "/chat", json={"session_id": info["conversation_id"], "message": "在吗"}) as r:
        text = "".join(r.iter_text())
    assert "人工" in text  # 不触图，返回人工车道占位
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_server.py -v`
Expected: FAIL（`create_app` 未定义）。

- [ ] **Step 3: 修正 streaming.py 的 on_interrupt 返回值汇入**

把 `src/ai_service_agent/harness/streaming.py` 中：
```python
for it in interrupts:
    await on_interrupt(it.value)
```
改为：
```python
for it in interrupts:
    extra = await on_interrupt(it.value)
    if extra:
        yield extra
```

- [ ] **Step 4: 实现 server.py**

`src/ai_service_agent/harness/server.py`:
```python
import sqlite3

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ai_service_agent.graph.builder import build_graph
from ai_service_agent.harness.handoff import HandoffService
from ai_service_agent.harness.session import ConversationStatusStore
from ai_service_agent.harness.streaming import event_stream
from langchain_core.messages import HumanMessage


MESSAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation_message (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);
"""


class CreateSessionBody(BaseModel):
    user_id: str


class ChatBody(BaseModel):
    session_id: str
    message: str


def create_app(db_path: str, model=None):
    conn = sqlite3.connect(db_path)
    conn.execute(MESSAGE_SCHEMA)
    conn.commit()
    status = ConversationStatusStore(conn)
    handoff = HandoffService(conn, status)
    graph = build_graph(model, tools=[])

    app = FastAPI()

    @app.post("/sessions")
    def create_session(body: CreateSessionBody):
        info = status.create_session(body.user_id)
        return {"session_id": info["conversation_id"], "mode": info["mode"]}

    @app.post("/chat")
    def chat(body: ChatBody):
        conv_id = body.session_id
        thread_id = status.get_thread_id(conv_id)
        if thread_id is None:
            raise HTTPException(status_code=404, detail="session not found")

        if status.get_mode(conv_id) == "human":
            # 人工车道：写展示记录，不触图（handoff-spec.md §1.1）
            with conn:
                conn.execute(
                    "INSERT INTO conversation_message (conversation_id, role, content) VALUES (?, 'user', ?)",
                    (conv_id, body.message),
                )
                conn.execute(
                    "INSERT INTO conversation_message (conversation_id, role, content) VALUES (?, 'assistant', ?)",
                    (conv_id, "已转人工，坐席将尽快回复。"),
                )

            def human_stream():
                yield {"type": "done", "data": {"final": "已转人工，坐席将尽快回复。"}}

            return StreamingResponse(
                _sse(human_stream()),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        async def ai_stream():
            cb = handoff.make_interrupt_callback(conv_id)
            async for ev in event_stream(graph, {"thread_id": thread_id}, HumanMessage(content=body.message), on_interrupt=cb):
                if ev["type"] == "done" and status.get_mode(conv_id) != "ai":
                    # 发送前实时复检：已被切走则丢弃（handoff-spec.md §1.4）
                    yield {"type": "error", "data": {"code": "ModeChanged", "message": "AI 回复已丢弃（会话已转人工）"}}
                    yield {"type": "done", "data": {"final": None}}
                    return
                yield ev

        return StreamingResponse(
            _sse(ai_stream()),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/handoffs/{handoff_id}/resolve")
    def resolve(handoff_id: str):
        try:
            return handoff.resolve_handoff(handoff_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="handoff not found")

    return app


def _sse(events):
    import json

    async def gen():
        async for ev in _wrap(events):
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"

    return gen()


async def _wrap(events):
    if hasattr(events, "__aiter__"):
        async for e in events:
            yield e
    else:
        for e in events:
            yield e
```

> 兼容说明：`human_stream` 是同步生成器，`ai_stream` 是异步生成器；`_sse` 统一把事件编码为 `data: {json}\n\n`（事件 payload 为 dict，`StreamingResponse` 内 `json.dumps` 编码）。若 `event_stream` 已异步，则 `ai_stream` 直接 `async for`；`_wrap` 负责两种生成器。

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run pytest tests/test_server.py -v`
Expected: PASS。

- [ ] **Step 6: Commit**

```bash
git add src/ai_service_agent/harness/server.py src/ai_service_agent/harness/streaming.py tests/test_server.py
git commit -m "feat: FastAPI server（sessions/chat SSE/人工车道/发送前校验/handoffs resolve）"
```

## Task 9: CLI 入口 + 端到端验证

**Files:**
- Create: `src/ai_service_agent/main.py`
- Modify: `pyproject.toml`（`[project.scripts]` → `ai-service-agent = "ai_service_agent.main:main"`）
- Test: `tests/test_e2e.py`（端到端闭环）

**Interfaces:**
- Consumes: Task 8 `create_app`、`config.get_settings()`。
- Produces: `main.py::main()`（uvicorn 启动）、`pyproject.toml` script 修正。

- [ ] **Step 1: 写端到端测试**

`tests/test_e2e.py`:
```python
from fastapi.testclient import TestClient

from ai_service_agent.harness.server import create_app
from ai_service_agent.harness.session import ConversationStatusStore
import sqlite3


def test_full_handoff_loop(tmp_path):
    db = str(tmp_path / "e2e.db")
    conn = sqlite3.connect(db)
    store = ConversationStatusStore(conn)
    info = store.create_session("u1")
    conn.close()

    client = TestClient(create_app(db, model="fake"))
    # 模拟子图抛 handoff_request 由外层接管（验收 #4/#6）
    from ai_service_agent.harness.handoff import HandoffService
    conn2 = sqlite3.connect(db)
    handoff = HandoffService(conn2, ConversationStatusStore(conn2))
    rec = handoff.create_handoff(info["conversation_id"], "reason", "summary")
    conn2.close()

    conn3 = sqlite3.connect(db)
    mode = ConversationStatusStore(conn3).get_mode(info["conversation_id"])
    conn3.close()
    assert mode == "human"

    r = client.post(f"/handoffs/{rec['handoff_id']}/resolve")
    assert r.json()["mode"] == "ai"
```

- [ ] **Step 2: 实现 main.py**

`src/ai_service_agent/main.py`:
```python
import uvicorn

from ai_service_agent.config import get_settings
from ai_service_agent.harness.server import create_app


def main() -> None:
    settings = get_settings()
    app = create_app(settings.db_path, model=None if not settings.fake_model else "fake")
    uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: 修正 pyproject scripts 并跑全部测试**

`pyproject.toml`：
```toml
[project.scripts]
ai-service-agent = "ai_service_agent.main:main"
```

Run: `uv run pytest -v`
Expected: 全绿。

- [ ] **Step 4: 手动端到端验证（uvicorn + curl）**

Run:
```bash
uv run ai-service-agent
```
另开终端：
```bash
curl -s -X POST http://127.0.0.1:8000/sessions -H "Content-Type: application/json" -d '{"user_id":"u1"}'
curl -N -X POST http://127.0.0.1:8000/chat -H "Content-Type: application/json" -d '{"session_id":"<id>","message":"hi"}'
```
Expected: sessions 返回 `{"session_id":..., "mode":"ai"}`；chat 返回 SSE 流（含 `token` / `done`）。

- [ ] **Step 5: Commit**

```bash
git add src/ai_service_agent/main.py pyproject.toml tests/test_e2e.py
git commit -m "feat: CLI 入口 + 端到端闭环验证"
```
