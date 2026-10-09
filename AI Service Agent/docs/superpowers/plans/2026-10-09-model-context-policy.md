# Model Context Policy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add node-specific, budgeted model input assembly for Router, Fallback, ReadGraph, MutationGraph, and HighRiskGraph without changing persisted thread history.

**Architecture:** Each node gets a `ContextPolicy` and a pure context builder. `create_agent` nodes use LangChain's official `AgentMiddleware.wrap_model_call` / `awrap_model_call`; direct model calls invoke the same pure builder in the node. The middleware counts system prompt and tool schemas as fixed cost, then uses `trim_messages` only for the dynamic messages with valid turn/tool boundaries.

**Tech Stack:** Python 3.14, LangGraph 1.2.x, LangChain 1.3.x, `langchain.agents.middleware.AgentMiddleware`, `langchain_core.messages.utils.trim_messages`, `count_tokens_approximately`, pytest.

**Spec:** Approved model-context discussion, 2026-10-09: Checkpoint keeps full history; model requests use per-node policies; Router=800 total tokens; Fallback=800; ReadGraph=1600–2000; Mutation/HighRisk select=1000–1200; exclude historical tool calls/results and reasoning; preserve current-turn ReadGraph tool chains.

## Global Constraints

- Do not modify checkpointed thread history; middleware may only override `ModelRequest`.
- Use official LangChain middleware and utilities; do not create a new context framework.
- Implement both `wrap_model_call` and `awrap_model_call` because project graph nodes are async.
- Always use `request.override(...)` rather than mutating `ModelRequest`.
- Include `request.system_message` and `request.tools` in the total input token budget.
- `trim_messages` must use `start_on="human"`, `end_on=("human", "tool")`, and `allow_partial=False`.
- Never drop the current user message from Router, Fallback, or Select contexts.
- Router/JEV/DeepSeek must share one route intent rule source and output only `route_intent` and `confidence`.
- Store prompts in `src/ai_service_agent/prompts.py`; do not hardcode prompt text in graph code.
- Run tests with `uv run --no-sync --cache-dir .uv-cache pytest ...`.

---

## File Structure

- Create: `src/ai_service_agent/graph/context/__init__.py`
- Create: `src/ai_service_agent/graph/context/builders.py`
  - Pure helpers for visible turns, reasoning stripping, Router/Fallback/Read/Select context building.
- Create: `src/ai_service_agent/graph/context/policy.py`
  - `ContextPolicy` dataclass and node policies built from `Settings`.
- Create: `src/ai_service_agent/graph/context/middleware.py`
  - Official `AgentMiddleware` adapter.
- Modify: `src/ai_service_agent/config.py`
  - Add per-node total token budgets.
- Modify: `src/ai_service_agent/prompts.py`
  - Move shared Router intent criteria here and render both JEV and DeepSeek prompts from it.
- Modify: `src/ai_service_agent/graph/router.py`
  - Use shared Router context builder for JEV and DeepSeek.
- Modify: `src/ai_service_agent/graph/agents/read_agent.py`
  - Attach ReadGraph middleware.
- Modify: `src/ai_service_agent/graph/workflows/fallback.py`
  - Attach Fallback middleware.
- Modify: `src/ai_service_agent/graph/workflows/select.py`
  - Use Select context builder for MutationGraph and HighRiskGraph.
- Test: `tests/test_context_policy.py`
- Test: `tests/test_context_budget_middleware.py`
- Test: `tests/test_router.py`
- Test: `tests/test_route_intent_router.py`
- Test: `tests/test_fallback.py`
- Test: `tests/test_select.py`
- Test: `tests/test_read_agent.py`

---

### Task 1: Context Builders and Visible Turn Selection

**Files:**

- Create: `src/ai_service_agent/graph/context/__init__.py`
- Create: `src/ai_service_agent/graph/context/builders.py`
- Test: `tests/test_context_policy.py`

**Interfaces:**

- Produces:
  - `visible_text(message) -> str`
  - `last_human_index(messages) -> int`
  - `build_recent_visible_context(messages, previous_completed_turns) -> list[BaseMessage]`
  - `build_read_context(messages, previous_completed_turns) -> list[BaseMessage]`
  - `strip_reasoning(text) -> str`

- [ ] **Step 1: Write failing tests for visible turn selection**

Create `tests/test_context_policy.py`:

```python
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from ai_service_agent.graph.context.builders import (
    build_read_context,
    build_recent_visible_context,
    strip_reasoning,
)


def test_strip_reasoning_removes_think_block():
    assert strip_reasoning("<think>secret</think>visible") == "visible"


def test_recent_visible_context_keeps_previous_turn_and_current_user():
    messages = [
        HumanMessage(content="我要修改地址"),
        AIMessage(content="请提供订单号和新地址"),
        HumanMessage(content="ORD-2026-10099"),
    ]

    actual = build_recent_visible_context(messages, previous_completed_turns=1)

    assert [message.content for message in actual] == [
        "我要修改地址",
        "请提供订单号和新地址",
        "ORD-2026-10099",
    ]


def test_recent_visible_context_excludes_tool_messages_and_tool_calls():
    messages = [
        HumanMessage(content="查询订单"),
        AIMessage(
            content="",
            tool_calls=[{"name": "order_lookup", "args": {}, "id": "call-1", "type": "tool_call"}],
        ),
        ToolMessage(content='{"order_id": "ORD-1"}', tool_call_id="call-1"),
        AIMessage(content="订单已发货"),
        HumanMessage(content="帮我取消"),
    ]

    actual = build_recent_visible_context(messages, previous_completed_turns=1)

    assert [(type(message).__name__, message.content) for message in actual] == [
        ("HumanMessage", "查询订单"),
        ("AIMessage", "订单已发货"),
        ("HumanMessage", "帮我取消"),
    ]


def test_read_context_preserves_current_turn_tool_chain():
    messages = [
        HumanMessage(content="上一轮"),
        AIMessage(content="上一轮回复"),
        HumanMessage(content="查询知识库"),
        AIMessage(
            content="",
            tool_calls=[{"name": "kb_search", "args": {}, "id": "call-read", "type": "tool_call"}],
        ),
        ToolMessage(content="knowledge base evidence", tool_call_id="call-read"),
    ]

    actual = build_read_context(messages, previous_completed_turns=1)

    assert actual[0].content == "上一轮"
    assert actual[1].content == "上一轮回复"
    assert actual[2].content == "查询知识库"
    assert actual[3].tool_calls[0]["id"] == "call-read"
    assert actual[4].content == "knowledge base evidence"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run --no-sync --cache-dir .uv-cache pytest tests/test_context_policy.py -v
```

Expected: FAIL with `ModuleNotFoundError: ai_service_agent.graph.context`.

- [ ] **Step 3: Implement pure context builders**

Create `src/ai_service_agent/graph/context/builders.py`:

```python
import re
from collections.abc import Sequence

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from ai_service_agent.graph.messages import message_text

_THINK_PATTERN = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)


def strip_reasoning(text: str) -> str:
    return _THINK_PATTERN.sub("", text).strip()


def visible_text(message: BaseMessage) -> str:
    return strip_reasoning(message_text(message))


def _is_human(message: BaseMessage) -> bool:
    return isinstance(message, HumanMessage)


def _is_final_ai(message: BaseMessage) -> bool:
    return isinstance(message, AIMessage) and not message.tool_calls


def last_human_index(messages: Sequence[BaseMessage]) -> int:
    for index in range(len(messages) - 1, -1, -1):
        if _is_human(messages[index]):
            return index
    return -1


def _completed_visible_turns(messages: Sequence[BaseMessage]) -> list[list[BaseMessage]]:
    turns: list[list[BaseMessage]] = []
    current: list[BaseMessage] = []

    for message in messages:
        if _is_human(message):
            if current:
                turns.append(current)
            current = [message]
        elif current:
            current.append(message)

    if current:
        turns.append(current)
    return turns


def _visible_turn_messages(turn: Sequence[BaseMessage]) -> list[BaseMessage]:
    final_ai = next((message for message in reversed(turn) if _is_final_ai(message)), None)
    visible = [HumanMessage(content=visible_text(turn[0]))]
    if final_ai is not None:
        visible.append(AIMessage(content=visible_text(final_ai)))
    return visible


def build_recent_visible_context(
    messages: Sequence[BaseMessage],
    *,
    previous_completed_turns: int,
) -> list[BaseMessage]:
    current_index = last_human_index(messages)
    if current_index < 0:
        return []

    prior_turns = _completed_visible_turns(messages[:current_index])
    selected_turns = prior_turns[-previous_completed_turns:]
    selected = [
        message
        for turn in selected_turns
        for message in _visible_turn_messages(turn)
    ]
    current = messages[current_index:]
    selected.extend(
        message
        for message in current
        if _is_human(message) or _is_final_ai(message)
    )
    return selected


def build_read_context(
    messages: Sequence[BaseMessage],
    *,
    previous_completed_turns: int,
) -> list[BaseMessage]:
    current_index = last_human_index(messages)
    if current_index < 0:
        return list(messages)

    prior_visible = build_recent_visible_context(
        messages[:current_index],
        previous_completed_turns=previous_completed_turns,
    )
    return [*prior_visible, *messages[current_index:]]
```

Create `src/ai_service_agent/graph/context/__init__.py`:

```python
from ai_service_agent.graph.context.builders import (
    build_read_context,
    build_recent_visible_context,
    strip_reasoning,
    visible_text,
)

__all__ = [
    "build_read_context",
    "build_recent_visible_context",
    "strip_reasoning",
    "visible_text",
]
```

- [ ] **Step 4: Run context tests**

Run:

```bash
uv run --no-sync --cache-dir .uv-cache pytest tests/test_context_policy.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/ai_service_agent/graph/context tests/test_context_policy.py
git commit -m "feat(context): add node context builders"
```

---

### Task 2: Context Policy and Official Middleware Adapter

**Files:**

- Create: `src/ai_service_agent/graph/context/policy.py`
- Create: `src/ai_service_agent/graph/context/middleware.py`
- Modify: `src/ai_service_agent/config.py`
- Test: `tests/test_context_budget_middleware.py`

**Interfaces:**

- Consumes: `build_recent_visible_context`, `build_read_context`.
- Produces:
  - `ContextPolicy(name, max_total_tokens, build_messages)`
  - `make_router_policy(settings)`
  - `make_fallback_policy(settings)`
  - `make_read_policy(settings)`
  - `make_select_policy(settings)`
  - `ContextBudgetMiddleware(policy)`

- [ ] **Step 1: Add budget settings**

Modify `src/ai_service_agent/config.py` inside `Settings`, near the other LLM settings:

```python
    router_total_input_max_tokens: int = Field(
        default=800, ge=1, validation_alias="ROUTER_TOTAL_INPUT_MAX_TOKENS"
    )
    fallback_total_input_max_tokens: int = Field(
        default=800, ge=1, validation_alias="FALLBACK_TOTAL_INPUT_MAX_TOKENS"
    )
    read_total_input_max_tokens: int = Field(
        default=2000, ge=1, validation_alias="READ_TOTAL_INPUT_MAX_TOKENS"
    )
    select_total_input_max_tokens: int = Field(
        default=1200, ge=1, validation_alias="SELECT_TOTAL_INPUT_MAX_TOKENS"
    )
```

- [ ] **Step 2: Write failing middleware tests**

Create `tests/test_context_budget_middleware.py`:

```python
from langchain.agents.middleware import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel

from ai_service_agent.graph.context.middleware import ContextBudgetMiddleware
from ai_service_agent.graph.context.policy import make_read_policy
from ai_service_agent.config import get_settings


def _make_request(messages):
    return ModelRequest(
        model=FakeMessagesListChatModel(responses=[]),
        messages=messages,
        system_message=SystemMessage(content="system"),
        tools=[],
        state={},
    )


def test_middleware_excludes_prior_tool_chain_but_keeps_current_turn():
    messages = [
        HumanMessage(content="old question"),
        AIMessage(content="old answer"),
        HumanMessage(content="search now"),
        AIMessage(
            content="",
            tool_calls=[{"name": "kb_search", "args": {}, "id": "call-1", "type": "tool_call"}],
        ),
        ToolMessage(content="evidence", tool_call_id="call-1"),
    ]
    request = _make_request(messages)
    middleware = ContextBudgetMiddleware(make_read_policy(get_settings()))

    prepared = middleware._prepare(request)

    assert [message.content for message in prepared.messages] == [
        "old question",
        "old answer",
        "search now",
        "",
        "evidence",
    ]


def test_middleware_counts_system_prompt_against_total_budget():
    messages = [
        HumanMessage(content="one" * 100),
        AIMessage(content="two" * 100),
        HumanMessage(content="three" * 100),
    ]
    request = _make_request(messages)

    class SmallPolicy:
        name = "test"
        max_total_tokens = 10

        def build_messages(self, messages):
            return messages

    middleware = ContextBudgetMiddleware(SmallPolicy())
    prepared = middleware._prepare(request)

    assert len(prepared.messages) < len(messages)
```

- [ ] **Step 3: Implement policy and middleware**

Create `src/ai_service_agent/graph/context/policy.py`:

```python
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Any

from langchain_core.messages import BaseMessage

from ai_service_agent.config import Settings
from ai_service_agent.graph.context.builders import (
    build_read_context,
    build_recent_visible_context,
)


@dataclass(frozen=True)
class ContextPolicy:
    name: str
    max_total_tokens: int
    build_messages: Callable[[Sequence[BaseMessage]], list[BaseMessage]]


def make_router_policy(settings: Settings) -> ContextPolicy:
    return ContextPolicy(
        name="router",
        max_total_tokens=settings.router_total_input_max_tokens,
        build_messages=partial(build_recent_visible_context, previous_completed_turns=1),
    )


def make_fallback_policy(settings: Settings) -> ContextPolicy:
    return ContextPolicy(
        name="fallback",
        max_total_tokens=settings.fallback_total_input_max_tokens,
        build_messages=partial(build_recent_visible_context, previous_completed_turns=2),
    )


def make_read_policy(settings: Settings) -> ContextPolicy:
    return ContextPolicy(
        name="read",
        max_total_tokens=settings.read_total_input_max_tokens,
        build_messages=partial(build_read_context, previous_completed_turns=2),
    )


def make_select_policy(settings: Settings) -> ContextPolicy:
    return ContextPolicy(
        name="select",
        max_total_tokens=settings.select_total_input_max_tokens,
        build_messages=partial(build_recent_visible_context, previous_completed_turns=1),
    )
```

Create `src/ai_service_agent/graph/context/middleware.py`:

```python
import logging

from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langchain_core.messages import SystemMessage
from langchain_core.messages.utils import (
    count_tokens_approximately,
    trim_messages,
)

from ai_service_agent.graph.context.policy import ContextPolicy

logger = logging.getLogger(__name__)


class ContextBudgetMiddleware(AgentMiddleware):
    def __init__(self, policy: ContextPolicy):
        self.policy = policy

    def _prepare(self, request: ModelRequest) -> ModelRequest:
        fixed_messages = [request.system_message] if isinstance(request.system_message, SystemMessage) else []
        fixed_tokens = count_tokens_approximately(fixed_messages, tools=request.tools)
        dynamic_budget = max(1, self.policy.max_total_tokens - fixed_tokens)
        messages = self.policy.build_messages(request.messages)
        selected = trim_messages(
            messages,
            strategy="last",
            max_tokens=dynamic_budget,
            token_counter=count_tokens_approximately,
            start_on="human",
            end_on=("human", "tool"),
            allow_partial=False,
        )
        logger.info(
            "context_policy_applied",
            extra={
                "policy": self.policy.name,
                "raw_messages": len(request.messages),
                "selected_messages": len(selected),
                "fixed_tokens": fixed_tokens,
                "dynamic_budget": dynamic_budget,
                "selected_tokens_estimated": count_tokens_approximately(selected),
            },
        )
        return request.override(messages=selected)

    def wrap_model_call(self, request: ModelRequest, handler):
        return handler(self._prepare(request))

    async def awrap_model_call(self, request: ModelRequest, handler):
        return await handler(self._prepare(request))
```

Update `src/ai_service_agent/graph/context/__init__.py`:

```python
from ai_service_agent.graph.context.builders import (
    build_read_context,
    build_recent_visible_context,
    strip_reasoning,
    visible_text,
)
from ai_service_agent.graph.context.middleware import ContextBudgetMiddleware
from ai_service_agent.graph.context.policy import (
    ContextPolicy,
    make_fallback_policy,
    make_read_policy,
    make_router_policy,
    make_select_policy,
)

__all__ = [
    "ContextBudgetMiddleware",
    "ContextPolicy",
    "build_read_context",
    "build_recent_visible_context",
    "make_fallback_policy",
    "make_read_policy",
    "make_router_policy",
    "make_select_policy",
    "strip_reasoning",
    "visible_text",
]
```

- [ ] **Step 4: Run middleware tests**

Run:

```bash
uv run --no-sync --cache-dir .uv-cache pytest tests/test_context_budget_middleware.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/ai_service_agent/config.py src/ai_service_agent/graph/context tests/test_context_budget_middleware.py
git commit -m "feat(context): add official context budget middleware"
```

---

### Task 3: Apply Fallback Policy

**Files:**

- Modify: `src/ai_service_agent/graph/workflows/fallback.py`
- Test: `tests/test_fallback.py`

**Interfaces:**

- Consumes: `ContextBudgetMiddleware`, `make_fallback_policy`.

- [ ] **Step 1: Add failing fallback context test**

Add to `tests/test_fallback.py`:

```python
def test_fallback_explain_llm_receives_policy_context(monkeypatch):
    captured = {}

    class CaptureModel:
        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages, config=None):
            captured["messages"] = messages
            from langchain_core.messages import AIMessage
            return AIMessage(content="兜底回复")

    from langchain_core.messages import AIMessage, HumanMessage
    from ai_service_agent.graph.workflows.fallback import make_fallback_graph

    graph = make_fallback_graph(CaptureModel())
    state = {
        "messages": [
            HumanMessage(content="第一轮"),
            AIMessage(content="第一轮回复"),
            HumanMessage(content="第二轮"),
            AIMessage(content="第二轮回复"),
            HumanMessage(content="随便说说"),
        ],
        "context": {},
        "user_id": "u1",
        "session_id": "s1",
        "handoff": False,
    }
    import asyncio
    asyncio.run(graph.ainvoke(state))

    human_texts = [message.content for message in captured["messages"] if message.type == "human"]
    assert human_texts[-1] == "随便说说"
    assert len(human_texts) <= 3
```

- [ ] **Step 2: Run test to verify current behavior**

Run:

```bash
uv run --no-sync --cache-dir .uv-cache pytest tests/test_fallback.py::test_fallback_explain_llm_receives_policy_context -v
```

Expected: FAIL because full history is passed.

- [ ] **Step 3: Attach middleware to fallback agent**

Modify imports in `src/ai_service_agent/graph/workflows/fallback.py`:

```python
from ai_service_agent.graph.context import ContextBudgetMiddleware, make_fallback_policy
```

Modify `make_fallback_graph`:

```python
    agent = create_agent(
        model,
        tools=[],
        system_prompt=FALLBACK_SYSTEM_PROMPT,
        middleware=[ContextBudgetMiddleware(make_fallback_policy(get_settings()))],
    )
```

- [ ] **Step 4: Run fallback tests**

Run:

```bash
uv run --no-sync --cache-dir .uv-cache pytest tests/test_fallback.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/ai_service_agent/graph/workflows/fallback.py tests/test_fallback.py
git commit -m "feat(fallback): apply context budget policy"
```

---

### Task 4: Unify Router Context and Rules

**Files:**

- Modify: `src/ai_service_agent/prompts.py`
- Modify: `src/ai_service_agent/graph/router.py`
- Test: `tests/test_route_intent_router.py`
- Test: `tests/test_router.py`

**Interfaces:**

- Consumes: `build_recent_visible_context`.
- Produces:
  - `ROUTE_INTENT_CRITERIA: dict[str, str]`
  - `ROUTE_INTENT_QUESTION: dict[str, Any]`
  - `build_router_messages(state) -> list[BaseMessage]`
  - `build_jev_state(state) -> dict`

- [ ] **Step 1: Add failing shared-context tests**

Add to `tests/test_route_intent_router.py`:

```python
def test_jev_and_deepseek_share_recent_turn_context():
    from langchain_core.messages import AIMessage, HumanMessage
    from ai_service_agent.graph.router import _jev_state, build_router_messages

    state = {
        "messages": [
            HumanMessage(content="我要修改地址"),
            AIMessage(content="请提供订单号和新地址"),
            HumanMessage(content="ORD-2026-10099"),
        ]
    }

    jev_state = _jev_state(state)
    messages = build_router_messages(state)

    assert jev_state == {
        "current_user_message": "ORD-2026-10099",
        "recent_context": [
            {"role": "human", "content": "我要修改地址"},
            {"role": "ai", "content": "请提供订单号和新地址"},
        ],
    }
    assert [message.content for message in messages] == [
        "我要修改地址",
        "请提供订单号和新地址",
        "ORD-2026-10099",
    ]
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run --no-sync --cache-dir .uv-cache pytest tests/test_route_intent_router.py::test_jev_and_deepseek_share_recent_turn_context -v
```

Expected: FAIL because `build_router_messages` does not exist.

- [ ] **Step 3: Move shared Router criteria into prompts**

In `src/ai_service_agent/prompts.py`, define `ROUTE_INTENT_CRITERIA` using the exact intent descriptions currently in `_JEV_ROUTE_INTENT_QUESTION`. Render `ROUTE_INTENT_SYSTEM` from this dictionary. Export:

```python
ROUTE_INTENT_CRITERIA: dict[str, str] = { ... }
ROUTE_INTENT_QUESTION = {
    "route_intent": {
        "type": "choice",
        "instructions": ROUTE_INTENT_INSTRUCTIONS,
        "criteria": ROUTE_INTENT_CRITERIA,
    }
}
```

Keep all existing JEV semantics: current message has priority; recent context only resolves references and missing information; missing parameters do not change intent; unsupported action is distinct from unknown.

- [ ] **Step 4: Use shared builder in Router**

In `src/ai_service_agent/graph/router.py`, import:

```python
from ai_service_agent.graph.context.builders import build_recent_visible_context
from ai_service_agent.prompts import ROUTE_INTENT_QUESTION, ROUTE_INTENT_SYSTEM
```

Add:

```python
def build_router_messages(state: dict) -> list:
    return build_recent_visible_context(
        state.get("messages", []),
        previous_completed_turns=1,
    )
```

Rewrite `_jev_state()` to consume this builder and return:

```python
{
    "current_user_message": current_text,
    "recent_context": recent_context,
}
```

Use `ROUTE_INTENT_QUESTION` in `_classify_route_intent_with_jev()`.

For DeepSeek real models, prepend the shared system prompt:

```python
structured_model = model.with_structured_output(RouteIntentSchema, method="function_calling")

async def deepseek_route(state, config=None):
    messages = [SystemMessage(content=ROUTE_INTENT_SYSTEM), *build_router_messages(state)]
    parsed = await structured_model.ainvoke(messages, config=config)
```

For the fake-model branch, continue using `create_agent(..., system_prompt=ROUTE_INTENT_SYSTEM)` but pass:

```python
await agent.ainvoke({"messages": build_router_messages(state)}, config=config)
```

- [ ] **Step 5: Run Router tests**

Run:

```bash
uv run --no-sync --cache-dir .uv-cache pytest tests/test_route_intent_router.py tests/test_router.py tests/test_route_intent_eval.py -v
```

Expected: PASS after updating stale assertions to the new shared contract.

- [ ] **Step 6: Commit**

```bash
git add src/ai_service_agent/prompts.py src/ai_service_agent/graph/router.py tests/test_route_intent_router.py tests/test_router.py tests/test_route_intent_eval.py
git commit -m "feat(router): unify JEV and DeepSeek context policy"
```

---

### Task 5: Apply Select Policy to Mutation and HighRisk Graphs

**Files:**

- Modify: `src/ai_service_agent/graph/workflows/select.py`
- Test: `tests/test_select.py`
- Test: `tests/test_high_risk_graph.py`

**Interfaces:**

- Consumes: `build_recent_visible_context`, `trim_messages`, `count_tokens_approximately`.
- Produces: `build_select_messages(state, selected_tools, system_message) -> list[BaseMessage]`.

- [ ] **Step 1: Add failing clarification-context test**

Add to `tests/test_select.py`:

```python
async def test_select_uses_previous_turn_for_clarification():
    captured = {}

    class CaptureToolModel:
        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages, config=None):
            captured["messages"] = messages
            from langchain_core.messages import AIMessage
            return AIMessage(
                content="",
                tool_calls=[{"name": "update_address", "args": {}, "id": "call-1", "type": "tool_call"}],
            )

    from langchain_core.messages import AIMessage, HumanMessage
    from ai_service_agent.graph.workflows.select import make_select_node
    from ai_service_agent.tools.registry import ToolRegistry

    node = make_select_node(CaptureToolModel(), "mutation", tools=[], registry=ToolRegistry())
    state = {
        "messages": [
            HumanMessage(content="我要修改地址"),
            AIMessage(content="请提供订单号和新地址"),
            HumanMessage(content="ORD-2026-10099"),
        ],
        "allowed_tools": ["update_address"],
        "user_id": "u1",
        "context": {},
    }

    await node(state)
    human_texts = [
        message.content
        for message in captured["messages"]
        if getattr(message, "type", "") == "human"
    ]
    assert human_texts == ["我要修改地址", "ORD-2026-10099"]
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run --no-sync --cache-dir .uv-cache pytest tests/test_select.py::test_select_uses_previous_turn_for_clarification -v
```

Expected: FAIL because only the latest human message is passed.

- [ ] **Step 3: Build budgeted Select messages**

In `src/ai_service_agent/graph/workflows/select.py`, import:

```python
from ai_service_agent.config import get_settings
from ai_service_agent.graph.context.builders import build_recent_visible_context
from ai_service_agent.graph.context.policy import make_select_policy
from langchain_core.messages.utils import count_tokens_approximately, trim_messages
```

Add:

```python
def _budgeted_select_messages(
    state: dict,
    selected_tools: list,
    system_message: SystemMessage,
) -> list:
    settings = get_settings()
    policy = make_select_policy(settings)
    fixed_tokens = count_tokens_approximately(
        [system_message],
        tools=selected_tools,
    )
    dynamic_budget = max(1, policy.max_total_tokens - fixed_tokens)
    dynamic_messages = build_recent_visible_context(
        state.get("messages", []),
        previous_completed_turns=1,
    )
    selected = trim_messages(
        dynamic_messages,
        strategy="last",
        max_tokens=dynamic_budget,
        token_counter=count_tokens_approximately,
        start_on="human",
        end_on=("human", "tool"),
        allow_partial=False,
    )
    return [system_message, *selected]
```

Replace both message constructions in `select()` with:

```python
messages = _budgeted_select_messages(
    state,
    selected_tools,
    SystemMessage(content=SELECT_SYSTEM_TEMPLATE.format(
        tool_list=tool_list,
        examples=_build_examples(selected_tools, registry),
    )),
)
```

and for the bind-tools path:

```python
messages = _budgeted_select_messages(
    state,
    selected_tools,
    SystemMessage(content=SELECT_BINDTOOLS_SYSTEM),
)
```

- [ ] **Step 4: Run Select and HighRisk tests**

Run:

```bash
uv run --no-sync --cache-dir .uv-cache pytest tests/test_select.py tests/test_high_risk_graph.py tests/test_mutation_graph.py -v
```

Expected: PASS after updating assertions that assumed only one human message.

- [ ] **Step 5: Commit**

```bash
git add src/ai_service_agent/graph/workflows/select.py tests/test_select.py tests/test_high_risk_graph.py tests/test_mutation_graph.py
git commit -m "feat(select): use clarification-aware context policy"
```

---

### Task 6: Apply ReadGraph Policy

**Files:**

- Modify: `src/ai_service_agent/graph/agents/read_agent.py`
- Test: `tests/test_read_agent.py`

**Interfaces:**

- Consumes: `ContextBudgetMiddleware`, `make_read_policy`.

- [ ] **Step 1: Add failing current-turn preservation test**

Add to `tests/test_read_agent.py`:

```python
def test_read_agent_keeps_current_tool_chain_and_trims_old_history(monkeypatch):
    captured = {}

    class CaptureModel(ToolCallingFakeModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            captured["messages"] = list(messages)
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    old_messages = [
        HumanMessage(content="old question"),
        AIMessage(content="old answer"),
    ]
    current_messages = [
        HumanMessage(content="search knowledge base"),
        AIMessage(
            content="",
            tool_calls=[{"name": "kb_search", "args": {}, "id": "call-read", "type": "tool_call"}],
        ),
        ToolMessage(content="knowledge evidence", tool_call_id="call-read"),
    ]
    model = CaptureModel(responses=[AIMessage(content="answer with evidence")])
    graph = make_read_graph(model, tools=[kb_search])
    from conftest import ainvoke_graph

    out = ainvoke_graph(
        graph,
        {"messages": old_messages + current_messages, "user_id": "u1", "session_id": "s1", "context": {}},
    )

    contents = [message.content for message in captured["messages"]]
    assert "old question" in contents
    assert "old answer" in contents
    assert "search knowledge base" in contents
    assert "knowledge evidence" in contents
```

Add missing imports if needed:

```python
from langchain_core.messages import ToolMessage
```

- [ ] **Step 2: Attach middleware**

Modify `make_read_graph` in `src/ai_service_agent/graph/agents/read_agent.py`:

```python
from ai_service_agent.graph.context import ContextBudgetMiddleware, make_read_policy
```

Change middleware construction:

```python
    middleware = [
        ContextBudgetMiddleware(make_read_policy(get_settings())),
        AllowedToolsMiddleware(),
        ModelCallLimitMiddleware(
            run_limit=READ_MODEL_CALL_LIMIT,
            exit_behavior="end",
        ),
    ]
```

- [ ] **Step 3: Run ReadGraph tests**

Run:

```bash
uv run --no-sync --cache-dir .uv-cache pytest tests/test_read_agent.py -v
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add src/ai_service_agent/graph/agents/read_agent.py tests/test_read_agent.py
git commit -m "feat(read): apply context policy while preserving tool chain"
```

---

### Task 7: Regression and Real Model Smoke

**Files:**

- No production file changes expected.

**Interfaces:**

- Consumes: all tasks above.

- [ ] **Step 1: Run focused regression**

Run:

```bash
uv run --no-sync --cache-dir .uv-cache pytest tests/test_context_policy.py tests/test_context_budget_middleware.py tests/test_route_intent_router.py tests/test_router.py tests/test_fallback.py tests/test_select.py tests/test_read_agent.py tests/test_high_risk_graph.py tests/test_mutation_graph.py -v
```

Expected: PASS.

- [ ] **Step 2: Run full offline regression**

Run:

```bash
uv run --no-sync --cache-dir .uv-cache pytest -q
```

Expected: PASS with no new failures.

- [ ] **Step 3: Run real-model smoke if credentials and services are available**

Start the service:

```bash
uv run --no-sync python -m ai_service_agent.main
```

Verify these cases through the user frontend:

1. Router clarification: `我要修改地址`, then `ORD-2026-10099` routes to MutationGraph and selects address update.
2. Fallback input: an unknown short message produces one concise fallback response.
3. ReadGraph: a KB query receives one answer and does not carry historical KB evidence into a later turn.
4. LangSmith trace shows `context_policy_applied`, reduced `raw_messages`, and policy-specific token counts.

- [ ] **Step 4: Record results**

Append a section to `docs/real-model-smoke.md`:

```markdown
## 15. Model Context Policy Smoke (2026-10-09)

- Router total input budget: 800 tokens.
- Fallback total input budget: 800 tokens.
- ReadGraph total input budget: configured value from `READ_TOTAL_INPUT_MAX_TOKENS`.
- Select total input budget: configured value from `SELECT_TOTAL_INPUT_MAX_TOKENS`.
- Observed: raw messages, selected messages, estimated tokens, and LLM latency per policy.
- Result: PASS/FAIL with details.
```

- [ ] **Step 5: Final commit**

```bash
git add docs/real-model-smoke.md
git commit -m "docs(context): record model context policy smoke"
```

---

## Self-Review

- Router, Fallback, ReadGraph, MutationGraph, and HighRiskGraph all have explicit context tasks.
- Official components are used: `AgentMiddleware`, `ModelRequest.override`, `trim_messages`, and `count_tokens_approximately`; direct calls use pure builders rather than an invented framework.
- System prompt and tool schemas are counted before dynamic trimming.
- `trim_messages` uses valid message boundaries and does not allow partial truncation.
- No task changes checkpoint history.
- Real-model validation is included after offline regression.
