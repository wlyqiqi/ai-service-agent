# P1 Checkpointer 持久化与 Handoff 恢复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 MVP 接入 `AsyncSqliteSaver`（按 `thread_id` 持久化）、入口层 resume 调度（harness 检测挂起中断 + `Command(resume=...)` 恢复）、resolve 时服务端主动完成 handoff resume、消息对账（`state.messages` 恒等于 Message Store 快照），并新增并发竞态一致性保障。

**Architecture:** 每次图调用前由 harness `aget_state()` 检测挂起中断：有则 `Command(resume=<唯一 schema>, update={"messages": 水合历史})` 从中断点继续（天然不重走 Router），无则普通输入新起 run。`/chat` 与 `/handoffs/{id}/resolve` 共用同一图构建入口（`make_ai_graph`）；Fallback 重排为 `check_handoff` 前置消除 resume 重放；`conversation_message` 增 `message_id` 列 + 稳定 id 水合，经 `add_messages` 按 id 对账；每线程 asyncio 锁串行化同一 `thread_id` 的图调用，杜绝双重 resume。

**Tech Stack:** Python 3.14；LangGraph 1.2.11（`AsyncSqliteSaver` / `Command(resume=..., update=...)` / `aget_state`）；`langchain.agents.create_agent`（禁止 `create_react_agent`）；FastAPI + sse-starlette；pytest + httpx（`TestClient` / `ASGITransport`）。

**Spec:**
- 决策：`docs/decisions/2026-09-14-checkpointer-resume-handoff.md`（实测依据 1–5 + D1–D6，权威）
- 规格：`docs/specs/graph-spec.md` §1/§3/§6.4 · `handoff-spec.md` §1.3/§5.2 · `state-spec.md` §1 · `event-spec.md` §2 · `approval-spec.md` §3.3 · `architecture.md` §2/§4
- 权威架构基线：`docs/specs/`（README 索引）

## Global Constraints

- 唯一 resume schema（`graph-spec.md` §3 / `approval-spec.md` §3.3）：`Command(resume= { "action": "approve" | "reject" | "confirm" | "cancel" | "handoff_resumed", "note"?: str } | { "result": "expired" })`；`handoff_request` 中断一律 `{"action": "handoff_resumed"}`。
- 图调用 config 一律 `{"configurable": {"thread_id": <thread_id>}}`；裸 `thread_id` 键不生效（D6）。
- Checkpointer：每请求 `AsyncSqliteSaver.from_conn_string(db_path)`（同库独立连接）；`MemorySaver` 仅测试用（`AGENTS.md` §15）。
- **图构建唯一入口**：`/chat` 与 resolve 必须调用同一 `make_ai_graph(model, checkpointer)`（同一 model、同一 tools），禁止两处各自 `build_graph`（用户要求）。
- 入口 resume 调度：`aget_state()` 检测挂起中断 → `resolve_resume_payload()` 构造 payload → `event_stream(..., resume_payload=...)`；resume 时 `done.final` 不回落历史文本（避免重复落库）。
- `state.messages` 对账：user 水合 id = `store-{row_id}`；assistant 复用落库 `message_id`（缺失则 `store-{row_id}`）；图内自产消息（工具 / Router 输出）不入 Store。
- resolve 主动 resume：在 `/handoffs/{id}/resolve` 请求内**同步 await** 完成（D2）；失败降级 = 中断保留，下一用户消息兜底；无挂起中断则跳过。
- 并发：同一 `thread_id` 的图调用经 per-thread asyncio 锁串行化（/chat 与 resolve 共用），杜绝双重 resume。
- 事件协议（`event-spec.md`）：`done` 含 `{final, message_id?}`；`error` 后仍发 `done`；resolve 主动 resume 不产生 SSE。
- Python 3.14；依赖统一在 `pyproject.toml`（本计划**不新增依赖**：`aiosqlite` 已随 langgraph 安装）；禁止 import 未登记包。
- 运行命令前缀（本机）：`$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy";`，一律 `uv run --no-sync ...`。
- git 根在 `D:\AI_Project`，commit 路径带 `AI Service Agent/` 前缀；git 写 `.git` 需 escalation（已批 `git add` / `git commit`）。
- 现有 37 个测试**预期全部保持不变**（实现后全量回归确认；若有行为性破坏，先停下说明再适配）。
- 每个任务结束时跑：该任务新增/涉及测试全绿 + `git commit`。

---

## Task 1: MessageStore `message_id` 列 + 稳定 id 水合（对账机制）

**Files:**
- Modify: `src/ai_service_agent/harness/messages.py`
- Test: `tests/test_messages.py`

**Interfaces:**
- Consumes: 现有 `MessageStore(conn)` / `append` / `list` / `hydrate`（`tests/test_messages.py` 现有 3 个测试保持通过）。
- Produces:
  - `MessageStore.append(conversation_id, role, content, message_id=None) -> int`（`message_id` 可选）
  - `MessageStore.list(conversation_id) -> list[dict]`，行含 `message_id`
  - `MessageStore.hydrate(conversation_id) -> list[BaseMessage]`：user → `HumanMessage(content, id=f"store-{row_id}")`；assistant → `AIMessage(content, id=message_id or f"store-{row_id}")`
  - 旧 schema（无 `message_id` 列）建库后**幂等迁移**：`__init__` 里 `PRAGMA table_info` 检查，缺列则 `ALTER TABLE ADD COLUMN message_id TEXT`

- [x] **Step 1: 写失败测试（追加到 `tests/test_messages.py`）**

```python
def test_append_with_message_id_and_hydrate_reuses(tmp_path):
    store, conn = _store(tmp_path)
    store.append("c1", "user", "u1")
    store.append("c1", "assistant", "a1", message_id="msg-abc")
    rows = store.list("c1")
    assert rows[1]["message_id"] == "msg-abc"
    msgs = store.hydrate("c1")
    assert msgs[0].id == "store-1"
    assert msgs[1].id == "msg-abc"
    assert msgs[1].content == "a1"
    conn.close()


def test_old_schema_is_migrated_with_message_id_column(tmp_path):
    conn = sqlite3.connect(tmp_path / "old.db")
    conn.execute(
        "CREATE TABLE conversation_message (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "conversation_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, "
        "created_at TEXT DEFAULT (datetime('now')))"
    )
    conn.execute("INSERT INTO conversation_message (conversation_id, role, content) VALUES ('c1','user','old')")
    conn.commit()
    store = MessageStore(conn)  # __init__ 应幂等补列
    rows = store.list("c1")
    assert rows[0]["message_id"] is None
    conn.close()
```

- [x] **Step 2: 跑测试确认失败**

Run: `$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest tests/test_messages.py -q`
Expected: FAIL（`list()` 无 `message_id` 键 / `hydrate` 无 id / 旧表缺列报错）。

- [x] **Step 3: 实现（修改 `harness/messages.py`）**

```python
MESSAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation_message (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    message_id TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
"""

class MessageStore:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.conn.execute(MESSAGE_SCHEMA)
        self._ensure_message_id_column()
        self.conn.commit()

    def _ensure_message_id_column(self) -> None:
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(conversation_message)").fetchall()]
        if "message_id" not in cols:
            self.conn.execute("ALTER TABLE conversation_message ADD COLUMN message_id TEXT")

    def append(self, conversation_id: str, role: str, content: str, message_id: str | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO conversation_message (conversation_id, role, content, message_id) VALUES (?, ?, ?, ?)",
            (conversation_id, role, content, message_id),
        )
        self.conn.commit()
        return cur.lastrowid

    def list(self, conversation_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, role, content, created_at, message_id FROM conversation_message "
            "WHERE conversation_id = ? ORDER BY id",
            (conversation_id,),
        ).fetchall()
        return [
            {"id": r[0], "role": r[1], "content": r[2], "created_at": r[3], "message_id": r[4]}
            for r in rows
        ]

    def hydrate(self, conversation_id: str) -> list[BaseMessage]:
        """水合为 langchain messages；稳定 id 使 add_messages 按 id 对账（handoff-spec.md §5.2）。"""
        messages: list[BaseMessage] = []
        for row in self.list(conversation_id):
            msg_id = row["message_id"] or f"store-{row['id']}"
            if row["role"] == "user":
                messages.append(HumanMessage(content=row["content"], id=msg_id))
            elif row["role"] == "assistant":
                messages.append(AIMessage(content=row["content"], id=msg_id))
        return messages
```

- [x] **Step 4: 跑测试确认通过**

Run: `$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest tests/test_messages.py -q`
Expected: PASS（现有 3 + 新增 2）。

- [x] **Step 5: Commit**

```bash
git add "AI Service Agent/src/ai_service_agent/harness/messages.py" "AI Service Agent/tests/test_messages.py"
git commit -m "feat: MessageStore message_id 列 + 稳定 id 水合（对账机制，handoff-spec §5.2）"
```
## Task 2: 共享图构建 `make_ai_graph` + 入口 resume 调度 `resolve_resume_payload`

**Files:**
- Create: `src/ai_service_agent/harness/graph.py`
- Create: `src/ai_service_agent/harness/resume.py`
- Test: `tests/test_harness_graph.py`

**Interfaces:**
- Consumes: `graph.builder.build_graph(model, tools, checkpointer)`；`langgraph.types.Interrupt` / `Command`；`langchain_core.messages.BaseMessage`。
- Produces:
  - `harness/graph.py::AI_GRAPH_TOOLS: list`（MVP 空列表）
  - `harness/graph.py::make_ai_graph(model, checkpointer) -> CompiledStateGraph`（`/chat` 与 resolve **唯一**图构建入口）
  - `harness/resume.py::resolve_resume_payload(interrupt_value, latest_user_text="") -> dict | None`（有中断 → `{"action": "handoff_resumed", "note": <latest_user_text>}`；无/未知 → `None`）

- [x] **Step 1: 写失败测试（`tests/test_harness_graph.py`）**

```python
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Interrupt
from langchain_core.messages import HumanMessage

from ai_service_agent.harness.graph import make_ai_graph
from ai_service_agent.harness.resume import resolve_resume_payload
from ai_service_agent.testing import make_fake_model


def test_make_ai_graph_compiles_and_runs():
    graph = make_ai_graph(make_fake_model(), MemorySaver())
    out = graph.invoke({"messages": [HumanMessage(content="hi")], "user_id": "u1", "session_id": "s1", "context": {}})
    assert out["execution_status"] == "completed"


def test_resolve_resume_payload_handoff_request():
    it = Interrupt(value={"type": "handoff_request", "reason": "r", "summary": "s"}, id="i1")
    payload = resolve_resume_payload(it.value, latest_user_text="我要转人工")
    assert payload == {"action": "handoff_resumed", "note": "我要转人工"}


def test_resolve_resume_payload_no_interrupt_returns_none():
    assert resolve_resume_payload(None) is None
    assert resolve_resume_payload({"type": "unknown"}) is None
```

- [x] **Step 2: 跑测试确认失败**

Run: `$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest tests/test_harness_graph.py -q`
Expected: FAIL（模块不存在）。

- [x] **Step 3: 实现**

```python
# harness/graph.py
from ai_service_agent.graph.builder import build_graph

# MVP：AI 路径暂不注入业务工具；后续 P3/P4 按 capability x risk 从 ToolRegistry 取
AI_GRAPH_TOOLS: list = []


def make_ai_graph(model, checkpointer):
    """/chat 与 /handoffs/{id}/resolve 共用的图构建入口。

    两处必须调用本函数，保证 build_graph 参数（model / tools / checkpointer）
    完全一致，避免后续 Mutation/HighRisk 注入后 resume 行为漂移。
    """
    return build_graph(model, tools=AI_GRAPH_TOOLS, checkpointer=checkpointer)
```

```python
# harness/resume.py
from langchain_core.messages import BaseMessage
from langgraph.types import Command

# 唯一 resume schema 见 graph-spec.md §3 / approval-spec.md §3.3
HANDOFF_RESUME_ACTION = "handoff_resumed"


def resolve_resume_payload(interrupt_value, latest_user_text: str = "") -> dict | None:
    """入口层 resume 调度：由挂起中断构造 resume payload。

    handoff_request -> {"action": "handoff_resumed", "note": <latest_user_text>}；
    无中断 / 未知类型 -> None（调用方按普通输入新起 run）。
    P3/P4 扩展：approval / confirmation 中断在此映射为同一 schema 的 approve/reject/confirm/cancel。
    """
    if not isinstance(interrupt_value, dict):
        return None
    if interrupt_value.get("type") == "handoff_request":
        return {"action": HANDOFF_RESUME_ACTION, "note": latest_user_text}
    return None
```

- [x] **Step 4: 跑测试确认通过**

Run: `$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest tests/test_harness_graph.py -q`
Expected: PASS（3 个新测试）。

- [x] **Step 5: Commit**

```bash
git add "AI Service Agent/src/ai_service_agent/harness/graph.py" "AI Service Agent/src/ai_service_agent/harness/resume.py" "AI Service Agent/tests/test_harness_graph.py"
git commit -m "feat: 共享图构建 make_ai_graph + 入口 resume 调度 resolve_resume_payload（D1）"
```

## Task 3: `event_stream` 支持 `resume_payload` + `done.message_id` + async 兜底读取

**Files:**
- Modify: `src/ai_service_agent/harness/streaming.py`
- Test: `tests/test_streaming.py`

**Interfaces:**
- Consumes: `event_stream(graph, config, messages, on_interrupt=None)`（现有签名，`tests/test_streaming.py` 2 个现有测试**保持调用方式不变**）。
- Produces:
  - `event_stream(graph, config, messages, on_interrupt=None, resume_payload=None)`；`resume_payload` 非 None 时内部以 `Command(resume=resume_payload, update={"messages": messages})` 调用 `graph.astream`，否则 `{"messages": messages}`。
  - `done` 事件：`{"type": "done", "data": {"final": str | None, "message_id": str | None}}`（`message_id` = 本 turn 最后一条 assistant 消息的 langchain id，取自流式 chunk）。
  - resume 时（`resume_payload` 非 None）：`final` 只取本轮流式累积；为空则 `None`，**不回落** `_last_assistant_text`（避免把历史/重放文本当新输出）。
  - `_last_assistant_text` 改为 async `_last_assistant_text_and_id(graph, config) -> (text | None, id | None)`，用 `aget_state`（async saver 下 sync `get_state` 会失败）。

- [x] **Step 1: 写失败测试（追加到 `tests/test_streaming.py`）**

```python
from langgraph.checkpoint.memory import MemorySaver

from ai_service_agent.graph.builder import build_graph
from ai_service_agent.tools.kb import kb_search


def test_done_carries_message_id():
    model = ToolCallingFakeModel(
        responses=[
            AIMessage(content='{"capability":"faq","risk_level":"read","confidence":0.9}'),
            AIMessage(content="带 id 的回复。"),
        ]
    )
    graph = build_graph(model, tools=[kb_search], checkpointer=MemorySaver())
    events = _collect(event_stream(graph, {"configurable": {"thread_id": "t1"}}, [HumanMessage(content="hi")]))
    assert events[-1]["type"] == "done"
    assert events[-1]["data"]["final"] == "带 id 的回复。"
    assert events[-1]["data"]["message_id"]  # 非空 uuid
```

- [x] **Step 2: 跑测试确认失败**

Run: `$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest tests/test_streaming.py -q`
Expected: FAIL（`done.data` 无 `message_id` 键）。

- [x] **Step 3: 实现（修改 `harness/streaming.py`）**

```python
from langgraph.types import Command


async def event_stream(
    graph,
    config: dict,
    messages: list[BaseMessage],
    on_interrupt: Callable | None = None,
    resume_payload: dict | None = None,
) -> AsyncGenerator[dict, None]:
    """graph.astream -> SSE 归一化事件（event-spec.md）。

    token / tool_call / tool_result / error / done；done 必有且最后；
    中断以 updates 顶层 {"__interrupt__": (Interrupt,)} 呈现，由 on_interrupt 接管。
    resume_payload 非 None 时以 Command(resume=..., update={messages}) 恢复挂起中断
    （入口层 resume 调度，graph-spec.md §3）；resume 的 final 只取本轮流式增量，
    为空则 None（不回落历史文本，避免重复落库）。
    done 的 message_id = 本 turn 最后一条 assistant 消息的 langchain id，
    供 Message Store 对账持久化（handoff-spec.md §5.2）。
    """
    invocation = Command(resume=resume_payload, update={"messages": messages}) if resume_payload else {"messages": messages}
    final_text: list[str] = []
    final_message_id: str | None = None
    try:
        async for mode, payload in graph.astream(invocation, config=config, stream_mode=["messages", "updates"]):
            if mode == "messages":
                chunk, _meta = payload
                if isinstance(chunk, (AIMessage, AIMessageChunk)) and chunk.content:
                    text = chunk.content if isinstance(chunk.content, str) else str(chunk.content)
                    final_text.append(text)
                    final_message_id = chunk.id or final_message_id
            elif mode == "updates":
                interrupts = payload.get("__interrupt__")
                if interrupts and on_interrupt is not None:
                    for it in interrupts:
                        extra = await on_interrupt(it.value)
                        if extra:
                            yield extra
                for node_name, update in payload.items():
                    if node_name == "__interrupt__":
                        continue
                    for msg in update.get("messages", []):
                        if isinstance(msg, AIMessage) and msg.tool_calls:
                            for tc in msg.tool_calls:
                                yield {"type": "tool_call", "data": {"name": tc["name"], "args": tc["args"]}}
                        elif isinstance(msg, ToolMessage):
                            yield {
                                "type": "tool_result",
                                "data": {"name": msg.name or "tool", "ok": True, "summary": str(msg.content)[:80]},
                            }
        final = "".join(final_text) or None
        if not final and not resume_payload:
            text, msg_id = await _last_assistant_text_and_id(graph, config)
            final = text
            final_message_id = final_message_id or msg_id
        yield {"type": "done", "data": {"final": final, "message_id": final_message_id}}
    except Exception as exc:
        yield normalize_error(exc)
        yield {"type": "done", "data": {"final": None, "message_id": None}}


async def _last_assistant_text_and_id(graph, config: dict) -> tuple[str | None, str | None]:
    try:
        state = await graph.aget_state(config)
        for m in reversed(state.values.get("messages", [])):
            if isinstance(m, AIMessage) and m.content:
                return (m.content if isinstance(m.content, str) else str(m.content)), m.id
    except Exception:
        pass
    return None, None
```

- [x] **Step 4: 跑测试确认通过**

Run: `$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest tests/test_streaming.py tests/test_harness_graph.py -q`
Expected: PASS（test_streaming 现有 2 + 新增 1；test_harness_graph 3）。

- [x] **Step 5: Commit**

```bash
git add "AI Service Agent/src/ai_service_agent/harness/streaming.py" "AI Service Agent/tests/test_streaming.py"
git commit -m "feat: event_stream 支持 resume_payload + done.message_id（async 兜底读取）"
```
## Task 4: Fallback 重排——`check_handoff` 前置，`explain` 仅非 handoff

**Files:**
- Modify: `src/ai_service_agent/graph/workflows/fallback.py`
- Test: `tests/test_fallback.py`

**Interfaces:**
- Consumes: `check_handoff`（**函数不变**：命中返回 `Command(update={handoff, execution_status: waiting_handoff}, goto="pause")`，未命中返回 `{handoff: False, execution_status: "completed"}`）。
- Produces:
  - `make_fallback_graph(model)` 节点顺序：`START -> check_handoff` → 条件边 `handoff? -> pause | else -> explain`；`explain -> END`、`pause -> END`。
  - `explain` 文案：`"我暂时无法处理，请换个说法或联系人工客服。"`（非 handoff 兜底）；handoff 命中路径**不生成**图内文案（文案由外层在 `handoff_created` 时发 token + 落库，D3）。

- [x] **Step 1: 写失败测试（追加到 `tests/test_fallback.py`）**

```python
def test_fallback_non_handoff_routes_to_explain():
    graph = make_fallback_graph(ToolCallingFakeModel(responses=[]))
    out = graph.invoke({"messages": [HumanMessage(content="随便说说")], "context": {}, "user_id": "u1", "session_id": "s1", "handoff": False})
    assert out["execution_status"] == "completed"
    assert out["messages"][-1].content == "我暂时无法处理，请换个说法或联系人工客服。"


def test_fallback_handoff_path_generates_no_in_graph_text():
    graph = make_fallback_graph(ToolCallingFakeModel(responses=[]))
    out = graph.invoke({"messages": [HumanMessage(content="我要转人工")], "context": {}, "user_id": "u1", "session_id": "s1", "handoff": False})
    assert out["execution_status"] == "waiting_handoff"
    assert [m for m in out.get("messages", []) if getattr(m, "type", "") == "ai"] == []
```

- [x] **Step 2: 跑测试确认失败**

Run: `$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest tests/test_fallback.py -q`
Expected: 新增 2 个 FAIL（旧结构 handoff 路径先 explain、非 handoff 无 explain）。

- [x] **Step 3: 实现（修改 `graph/workflows/fallback.py` 的 `make_fallback_graph`）**

```python
def make_fallback_graph(model):
    """Fallback / Concierge：check_handoff 前置（graph-spec.md §6.4 P1 定案）。

    命中 handoff -> pause（interrupt，图内不生成文案）；未命中 -> explain 兜底回复。
    """
    def explain(state: dict) -> dict:
        return {"messages": [AIMessage(content="我暂时无法处理，请换个说法或联系人工客服。")]}

    builder = StateGraph(CustomerServiceState)
    builder.add_node("check_handoff", check_handoff)
    builder.add_node("explain", explain)
    builder.add_node("pause", pause)
    builder.add_edge(START, "check_handoff")
    builder.add_conditional_edges(
        "check_handoff",
        lambda s: "pause" if s.get("handoff") else "explain",
        {"pause": "pause", "explain": "explain"},
    )
    builder.add_edge("explain", END)
    builder.add_edge("pause", END)
    return builder.compile()
```

- [x] **Step 4: 跑测试确认通过（含既有 3 个）**

Run: `$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest tests/test_fallback.py -q`
Expected: PASS（既有 3 + 新增 2）。

- [x] **Step 5: Commit**

```bash
git add "AI Service Agent/src/ai_service_agent/graph/workflows/fallback.py" "AI Service Agent/tests/test_fallback.py"
git commit -m "feat: Fallback 重排——check_handoff 前置，explain 仅非 handoff（D3，消除 resume 重放）"
```

## Task 5: Server 接线——每请求 saver + 并发锁 + resume 调度 + resolve 主动 resume + handoff 文案

**Files:**
- Modify: `src/ai_service_agent/harness/server.py`
- Modify: `src/ai_service_agent/harness/handoff.py`（`HANDOFF_NOTICE` 常量 + `resolve_handoff` 返回 `conversation_id`）
- Test: `tests/test_server.py`、`tests/test_e2e.py`

**Interfaces:**
- Consumes: Task 1（`hydrate` 稳定 id）、Task 2（`make_ai_graph` / `resolve_resume_payload`）、Task 3（`event_stream` 新签名）、Task 4（重排后 fallback）。
- Produces:
  - `handoff.py::HANDOFF_NOTICE = "已为你转接人工客服，坐席将尽快回复。"`（外层文案，D3）
  - `handoff.py::HandoffService.resolve_handoff(handoff_id) -> dict` 返回值增加 `conversation_id`（现有 `test_handoff.py` 3 个测试保持通过）
  - `server.py`：per-thread asyncio 锁（`_thread_lock(thread_id)`）；`/chat` ai_stream 每请求 `AsyncSqliteSaver` + `aget_state` + `resolve_resume_payload` + 锁内图调用；`/handoffs/{id}/resolve` 改 async，回切 mode 后**同步 await 主动 resume**（有挂起中断才 resume，异常降级仍返回 `mode=ai`）；`handoff_created` 时落库 `HANDOFF_NOTICE` + 发 token。
  - `create_app` 不再在模块级编译无 checkpointer 图（改由每请求构建）；`main.py` 不变。

- [x] **Step 1: 写失败测试**

`tests/test_server.py` 追加（覆盖 resolve 主动 resume 后第一条消息走 Router）：

```python
import asyncio
import sqlite3

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from ai_service_agent.harness.graph import make_ai_graph
from ai_service_agent.testing import make_fake_model


class ResolveProactiveFakeModel(ToolCallingFakeModel):
    """路由器区分转人工；read agent 回显收到的用户消息条数。"""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        from langchain_core.outputs import ChatGeneration, ChatResult
        system = next((m.content for m in messages if getattr(m, "type", "") == "system"), "")
        user = next((m.content for m in reversed(messages) if getattr(m, "type", "") == "human"), "")
        users = [m for m in messages if getattr(m, "type", "") == "human"]
        if "路由器" in system:
            payload = '{"capability": "unknown", "risk_level": "read", "confidence": 0.2}' if "转人工" in user else '{"capability": "faq", "risk_level": "read", "confidence": 0.9}'
        else:
            payload = f"echo-{len(users)}"
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=payload))])


def test_resolve_proactively_resumes_then_next_chat_is_answered(tmp_path):
    db = str(tmp_path / "p.db")
    client = TestClient(create_app(db, model=ResolveProactiveFakeModel(responses=[])))
    session_id = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    with client.stream("POST", "/chat", json={"session_id": session_id, "message": "我要转人工"}) as r:
        text1 = "".join(r.iter_text())
    assert "handoff_created" in text1

    conn = sqlite3.connect(db)
    handoff_id = conn.execute("SELECT handoff_id FROM handoff_record WHERE conversation_id=?", (session_id,)).fetchone()[0]
    thread_id = ConversationStatusStore(conn).get_thread_id(session_id)
    conn.close()

    r = client.post(f"/handoffs/{handoff_id}/resolve")
    assert r.json()["mode"] == "ai"

    # resolve 后 checkpoint 应已无挂起中断（主动 resume 完成）
    async def check_no_interrupt():
        async with AsyncSqliteSaver.from_conn_string(db) as saver:
            graph = make_ai_graph(make_fake_model(), saver)
            st = await graph.aget_state({"configurable": {"thread_id": thread_id}})
            return st.interrupts, st.values.get("execution_status")
    interrupts, status = asyncio.run(check_no_interrupt())
    assert not interrupts
    assert status == "completed"

    # resolve 后第一条消息走正常 Router 并得到回复（echo-2，两条 user）
    with client.stream("POST", "/chat", json={"session_id": session_id, "message": "你好"}) as r:
        text2 = "".join(r.iter_text())
    assert "echo-2" in text2
    assert '"error"' not in text2
```

`tests/test_server.py` 追加（handoff_created 时落库外层文案 + 发 token）：

```python
def test_handoff_turn_persists_outer_notice_and_emits_token(tmp_path):
    from ai_service_agent.harness.handoff import HANDOFF_NOTICE
    client = TestClient(create_app(str(tmp_path / "n.db"), model=KeywordHandoffFakeModel(responses=[])))
    session_id = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    with client.stream("POST", "/chat", json={"session_id": session_id, "message": "我要转人工"}) as r:
        text = "".join(r.iter_text())
    assert '"token"' in text
    assert "handoff_created" in text
    conn = sqlite3.connect(str(tmp_path / "n.db"))
    rows = MessageStore(conn).list(session_id)
    conn.close()
    assert [r["role"] for r in rows] == ["user", "assistant"]
    assert rows[1]["content"] == HANDOFF_NOTICE
```

> 注：`KeywordHandoffFakeModel` 在 `tests/test_e2e.py` 已定义；本任务直接 `from tests.test_e2e import KeywordHandoffFakeModel` 或复制该类到本文件（建议复制，保持测试文件自包含）。

- [x] **Step 2: 跑测试确认失败**

Run: `$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest tests/test_server.py -q`
Expected: 新增 2 个 FAIL（resolve 不主动 resume、handoff 不落文案）。
- [x] **Step 3: 实现**

`harness/handoff.py`：
```python
HANDOFF_NOTICE = "已为你转接人工客服，坐席将尽快回复。"
```
`resolve_handoff` 返回值改为：
```python
return {"handoff_id": handoff_id, "conversation_id": conversation_id, "mode": "ai"}
```

`harness/server.py` 顶部与 `create_app`：
```python
import asyncio

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from ai_service_agent.harness.graph import make_ai_graph
from ai_service_agent.harness.handoff import HANDOFF_NOTICE
from ai_service_agent.harness.resume import resolve_resume_payload

_locks: dict[str, asyncio.Lock] = {}
_locks_guard = asyncio.Lock()


async def _thread_lock(thread_id: str) -> asyncio.Lock:
    """同一 thread_id 的图调用串行化，杜绝双重 resume（用户并发要求）。"""
    async with _locks_guard:
        if thread_id not in _locks:
            _locks[thread_id] = asyncio.Lock()
        return _locks[thread_id]
```
`create_app` 中删除 `graph = build_graph(...)`（每请求构建）。

`/chat` 的 `ai_stream` 改为：
```python
async def ai_stream():
    # 先持久化用户消息（即便本 turn 失败也留痕），再水合历史作为 state.messages 输入
    messages_store.append(conv_id, "user", body.message)
    history = messages_store.hydrate(conv_id)
    cb = handoff.make_interrupt_callback(conv_id)
    lock = await _thread_lock(thread_id)
    async with lock:
        async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
            graph = make_ai_graph(_resolve_model(model), saver)
            cfg = {"configurable": {"thread_id": thread_id}}
            st = await graph.aget_state(cfg)
            resume_payload = resolve_resume_payload(st.interrupts[0].value, body.message) if st.interrupts else None
            async for ev in event_stream(graph, cfg, history, on_interrupt=cb, resume_payload=resume_payload):
                if ev["type"] == "handoff_created":
                    # 外层文案（D3）：落库 + 发 token，图内不生成
                    messages_store.append(conv_id, "assistant", HANDOFF_NOTICE)
                    yield {"type": "token", "data": {"text": HANDOFF_NOTICE}}
                    yield ev
                    continue
                if ev["type"] == "done":
                    mode_now = status.get_mode(conv_id)
                    if mode_now != "ai":
                        # 发送前实时复检：已被切走则丢弃且不持久化（handoff-spec.md §1.4）
                        yield {"type": "error", "data": {"code": "ModeChanged", "message": "AI 回复已丢弃（会话已转人工）"}}
                        yield {"type": "done", "data": {"final": None, "message_id": None}}
                        return
                    final = ev["data"].get("final")
                    if final is not None:
                        messages_store.append(conv_id, "assistant", final, message_id=ev["data"].get("message_id"))
                    yield ev
                    return
                yield ev
```

`/handoffs/{handoff_id}/resolve` 改为 async + 主动 resume：
```python
@app.post("/handoffs/{handoff_id}/resolve")
async def resolve(handoff_id: str):
    try:
        result = handoff.resolve_handoff(handoff_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="handoff not found")
    conv_id = result["conversation_id"]
    thread_id = status.get_thread_id(conv_id)
    lock = await _thread_lock(thread_id)
    async with lock:
        async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
            graph = make_ai_graph(_resolve_model(model), saver)
            cfg = {"configurable": {"thread_id": thread_id}}
            st = await graph.aget_state(cfg)
            if st.interrupts:
                history = messages_store.hydrate(conv_id)
                try:
                    await graph.ainvoke(
                        Command(resume={"action": "handoff_resumed"}, update={"messages": history}),
                        config=cfg,
                    )
                except Exception:
                    # 降级（D2）：中断保留，下一用户消息经入口 resume 调度兜底
                    pass
    return {"handoff_id": handoff_id, "mode": "ai"}
```

- [x] **Step 4: 跑测试确认通过**

Run: `$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest tests/test_server.py tests/test_handoff.py tests/test_e2e.py -q`
Expected: PASS（test_server 既有 5 + 新增 2；test_handoff 4；test_e2e 4）。

- [x] **Step 5: Commit**

```bash
git add "AI Service Agent/src/ai_service_agent/harness/server.py" "AI Service Agent/src/ai_service_agent/harness/handoff.py" "AI Service Agent/tests/test_server.py" "AI Service Agent/tests/test_e2e.py"
git commit -m "feat: server 接线 checkpointer——每请求 saver + 并发锁 + resume 调度 + resolve 主动 resume（D2/D6）"
```

## Task 6: 并发竞态测试——resolve 主动 resume 与 /chat 入口兜底

**Files:**
- Create: `tests/test_concurrency.py`

**Interfaces:**
- Consumes: Task 5 的锁与两条恢复路径。
- Produces: 一条并发测试——同一 `thread_id` 上 resolve 主动 resume 与用户新消息入口 resume 在极短窗口内并发，验证：无异常、无双重 resume、最终状态一致（无挂起中断、`execution_status=completed`、`mode=ai`、文案不重复落库）。

- [x] **Step 1: 写测试（`tests/test_concurrency.py`）**

```python
import asyncio
import sqlite3

import httpx

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from ai_service_agent.harness.graph import make_ai_graph
from ai_service_agent.harness.messages import MessageStore
from ai_service_agent.harness.server import create_app
from ai_service_agent.harness.session import ConversationStatusStore
from ai_service_agent.testing import ToolCallingFakeModel


class RaceKeywordModel(ToolCallingFakeModel):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        user = next((m.content for m in reversed(messages) if getattr(m, "type", "") == "human"), "")
        payload = '{"capability": "unknown", "risk_level": "read", "confidence": 0.2}' if "转人工" in user else '{"capability": "faq", "risk_level": "read", "confidence": 0.9}'
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=payload))])


def test_resolve_and_chat_race_no_double_resume(tmp_path):
    db = str(tmp_path / "race.db")
    app = create_app(db, model=RaceKeywordModel(responses=[]))

    # 先造一个真实挂起中断（/chat 转人工）
    from fastapi.testclient import TestClient
    client = TestClient(app)
    session_id = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    with client.stream("POST", "/chat", json={"session_id": session_id, "message": "我要转人工"}) as r:
        assert "handoff_created" in "".join(r.iter_text())

    conn = sqlite3.connect(db)
    handoff_id = conn.execute("SELECT handoff_id FROM handoff_record WHERE conversation_id=?", (session_id,)).fetchone()[0]
    thread_id = ConversationStatusStore(conn).get_thread_id(session_id)
    conn.close()

    async def race():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            # 同时发起：resolve 主动 resume vs /chat 入口兜底 resume
            resolve_task = c.post(f"/handoffs/{handoff_id}/resolve")
            chat_ctx = c.stream("POST", "/chat", json={"session_id": session_id, "message": "在吗"})
            chat_resp = await chat_ctx.__aenter__()
            resolve_resp = await resolve_task
            chat_text = "".join([chunk async for chunk in chat_resp.aiter_text()])
            await chat_ctx.__aexit__(None, None, None)
            return resolve_resp, chat_text

    resolve_resp, chat_text = asyncio.run(race())
    assert resolve_resp.status_code == 200
    assert resolve_resp.json()["mode"] == "ai"
    assert '"done"' in chat_text          # /chat 正常结束
    assert '"error"' not in chat_text     # 无异常事件

    # 最终一致性：无挂起中断、execution_status=completed、mode=ai
    conn = sqlite3.connect(db)
    assert ConversationStatusStore(conn).get_mode(session_id) == "ai"
    conn.close()

    async def final_state():
        async with AsyncSqliteSaver.from_conn_string(db) as saver:
            graph = make_ai_graph(RaceKeywordModel(responses=[]), saver)
            st = await graph.aget_state({"configurable": {"thread_id": thread_id}})
            return st.interrupts, st.values.get("execution_status")

    interrupts, status = asyncio.run(final_state())
    assert not interrupts
    assert status == "completed"

    # 外层文案只落一次（不因并发双重持久化）
    conn = sqlite3.connect(db)
    rows = MessageStore(conn).list(session_id)
    conn.close()
    notices = [r for r in rows if r["role"] == "assistant" and "转接人工" in r["content"]]
    assert len(notices) == 1
```

- [x] **Step 2: 跑测试**

Run: `$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest tests/test_concurrency.py -q`
Expected: PASS。断言为时序无关的不变量（谁先抢到锁不影响结果）；若出现 `database is locked` 等偶发失败，属 sqlite busy timeout 竞争，先复跑确认，再考虑给 `create_app` 的 sqlite 连接设 `timeout` 参数（在 Task 7 说明）。

- [x] **Step 3: Commit**

```bash
git add "AI Service Agent/tests/test_concurrency.py"
git commit -m "test: resolve 主动 resume 与 /chat 兜底并发竞态一致性（无双重 resume）"
```
## Task 7: 全量回归 + validate + 收尾

**Files:**
- Modify: 无（如回归发现破坏，回到对应 Task 修复）
- Verify: 全量 pytest、`langgraph validate`

- [x] **Step 1: 全量测试**

Run: `$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest -q`
Expected: 全绿（37 个既有 + 新增约 14 个）。

- [x] **Step 2: langgraph validate**

Run: `langgraph validate`
Expected: `Configuration file ... is valid. (1 graph found)`（`builder.py:app` 未变）。

- [x] **Step 3: 复跑并发测试（防偶发 sqlite 锁）**

Run: `$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest tests/test_concurrency.py -q`（连跑 3 次）
Expected: 稳定通过。若 `database is locked` 反复出现：在 `create_app` 的 `sqlite3.connect(db_path, check_same_thread=False, timeout=30)` 增大 busy timeout，并回 Task 5 提交。

- [x] **Step 4: 收尾提交（如有修复）**

```bash
git add -A "AI Service Agent/"
git commit -m "chore: P1 回归修复（如有）"
```

- [x] **Step 5: 更新计划勾选与验收汇报**

- 逐任务勾选本文件 checkbox。
- 汇报：commit 列表、新增测试数、37 个既有测试保持不变的回归结果、并发测试结论、D1–D6 落地映射。

## Self-Review（writing-plans 强制）

**1. Spec 覆盖：**
- D1（harness resume 调度）→ Task 2/3/5；D2（resolve 主动 resume）→ Task 5/6；D3（fallback 重排 + 文案移外层）→ Task 4/5；D4（message_id 对账）→ Task 1/3/5；D5（唯一 schema）→ Task 2 的 `resolve_resume_payload` + `handoff_resumed` 常量；D6（per-request saver + configurable）→ Task 5。
- 用户补充 1（共享图构建）→ Task 2 `make_ai_graph`，Task 5 两端点统一调用。
- 用户补充 2（并发测试）→ Task 6。
- 现有 37 测试保持不变 → Task 7 全量回归。

**2. Placeholder 扫描：** 无 TBD/TODO；每步含完整代码与运行命令。

**3. 类型一致：**
- `event_stream(graph, config, messages, on_interrupt=None, resume_payload=None)` 全计划一致（Task 3 定义、Task 5 调用）。
- `resolve_resume_payload(interrupt_value, latest_user_text="") -> dict | None`（Task 2 定义、Task 5 调用）。
- `make_ai_graph(model, checkpointer)`（Task 2 定义、Task 5/6 调用）。
- `MessageStore.append(..., message_id=None)` / `hydrate()` 稳定 id（Task 1 定义、Task 5 落库/水合）。
- `HandoffService.resolve_handoff` 返回含 `conversation_id`（Task 5 定义、Task 5 resolve 端点消费）。
- `done` 事件 `{"final", "message_id"}` 在 Task 3 定义、Task 5 消费。
