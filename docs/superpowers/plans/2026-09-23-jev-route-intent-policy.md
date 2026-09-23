# Jev Route Intent Policy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the two-field Jev Router (`capability` + `risk_level`) with a one-choice `route_intent` Router while keeping deterministic Policy as the authority for graph routing, execution class, allowed tools, and approval.

**Architecture:** Add a feature-flagged `route_intent` Router path. Jev performs one `Choice(route_intent)`; DeepSeek uses the same output contract as fallback. `Policy Gate` maps the intent through a static route-policy table into `target_graph`, `execution_class`, `allowed_tools`, and `requires_approval`. Subgraphs receive `allowed_tools` as the candidate-tool boundary, but the LLM still selects the final tool inside that boundary.

**Tech Stack:** Python 3.14, LangGraph 1.2.10, LangChain agents/middleware, Pydantic, httpx, pytest.

**Spec:** This plan implements the agreed Jev route-intent design recorded in this file. During implementation, Task 9 updates the authoritative specs in `docs/specs/graph-spec.md` and `docs/specs/state-spec.md`.

## Verified TypeSafe Choice Contract

The HTTP endpoint is:

```http
POST https://api.typesafe.ai/v1/systemone
Authorization: Bearer <API_KEY>
Content-Type: application/json
```

The route-intent request must use this exact shape:

```json
{
  "state": {
    "messages": [
      {"role": "human", "content": "帮我取消订单"}
    ]
  },
  "model": "jev-latest",
  "questions": {
    "route_intent": {
      "type": "choice",
      "instructions": "判断用户当前 turn 的主要业务意图。",
      "criteria": {
        "faq": "产品使用、售后政策、商品参数、故障排查等知识问答",
        "smalltalk": "问候、感谢或非业务闲聊",
        "order_query": "根据订单号查询某一笔订单"
      }
    }
  }
}
```

For Choice, the response must contain an `answers` entry under the same question id. That answer must contain:

- `type` equal to `"choice"`;
- `choice`, the selected option;
- `probabilities`, a map containing every option and summing to 1;
- `confidence`, a number from 0 to 1.

Official example response:

```json
{
  "model": "jev-1.13.0",
  "answers": {
    "route_intent": {
      "type": "choice",
      "choice": "cancel_order_execute",
      "probabilities": {
        "cancel_order_execute": 1.0,
        "refund_apply_execute": 0.0
      },
      "confidence": 1.0
    }
  },
  "usage": {"input_tokens": 318, "output_tokens": 34}
}
```

The implementation must reject a missing `type`, missing `probabilities`, or a `choice` absent from `probabilities` as an invalid Jev response and fall back to DeepSeek.

## Global Constraints

- Keep Policy deterministic; no LLM call may decide `target_graph`, execution permissions, approval, or tool authorization.
- Router must not output or select `tool_name`; it may only output `route_intent` and `confidence`.
- The new path is disabled by default with `JEV_ROUTE_INTENT_ENABLED=false`.
- Preserve the existing `capability + risk_level` Router path when the feature flag is off.
- Use execution classes `read_only`, `standard_write`, and `high_risk`; do not introduce `no_risk` or `low_risk`.
- Unsupported business actions must map to `fallback`, not `high_risk_graph`.
- High-risk execution must keep safety precheck and human approval.
- Do not add new third-party dependencies.
- Keep messages as the only accumulated cross-subgraph conversation channel; other state fields are overwritten normally.
- Every task must leave the test suite passing before commit.

---

### Task 1: Route policy contract and registry lookup

**Files:**
- Create: `src/ai_service_agent/graph/route_policy.py`
- Modify: `src/ai_service_agent/tools/registry.py`
- Test: `tests/test_route_policy.py`

**Interfaces:**
- Consumes: `ToolRegistry` and registered tool names.
- Produces: `RouteIntent`, `TargetGraph`, `ExecutionClass`, `RoutePolicy`, `ROUTE_POLICY`, `resolve_route_policy(route_intent: str) -> RoutePolicy`, `route_intent_is_confident(route_intent: str, confidence: float) -> bool`, and `ToolRegistry.tools_for_names(names: Iterable[str]) -> list[BaseTool]`.

- [ ] **Step 1: Write the failing route-policy tests**

Create `tests/test_route_policy.py`:

```python
import pytest

from ai_service_agent.graph.route_policy import (
    ROUTE_POLICY,
    resolve_route_policy,
    route_intent_is_confident,
)
from ai_service_agent.tools import DEFAULT_TOOL_REGISTRY


def test_route_policy_maps_query_intents_to_read_graph():
    assert ROUTE_POLICY["order_query"].target_graph == "read_graph"
    assert ROUTE_POLICY["order_query"].execution_class == "read_only"
    assert ROUTE_POLICY["order_query"].allowed_tools == ("order_lookup",)

    assert ROUTE_POLICY["my_orders"].target_graph == "read_graph"
    assert ROUTE_POLICY["my_orders"].allowed_tools == ("my_orders",)

    assert ROUTE_POLICY["shipment_query"].target_graph == "read_graph"
    assert ROUTE_POLICY["shipment_query"].allowed_tools == ("shipment_query",)


def test_route_policy_maps_write_intents_and_boundaries():
    assert ROUTE_POLICY["account_profile_update"].allowed_tools == (
        "update_address",
        "update_phone",
    )
    assert ROUTE_POLICY["order_mutation"].allowed_tools == (
        "update_order_note",
        "create_invoice",
        "create_service_ticket",
    )
    assert ROUTE_POLICY["cancel_order_execute"].allowed_tools == ("cancel_order",)
    assert ROUTE_POLICY["cancel_order_execute"].requires_approval is True
    assert ROUTE_POLICY["refund_apply_execute"].allowed_tools == ("refund_apply",)
    assert ROUTE_POLICY["refund_apply_execute"].requires_approval is True


def test_unknown_and_unsupported_routes_fall_back():
    for intent in ("unsupported_action", "unknown", "not_a_real_intent"):
        policy = resolve_route_policy(intent)
        assert policy.target_graph == "fallback"
        assert policy.execution_class == "read_only"
        assert policy.allowed_tools == ()
        assert policy.requires_approval is False


def test_execution_class_confidence_thresholds():
    assert route_intent_is_confident("faq", 0.60) is True
    assert route_intent_is_confident("faq", 0.59) is False
    assert route_intent_is_confident("update_phone", 0.70) is True
    assert route_intent_is_confident("update_phone", 0.69) is False
    assert route_intent_is_confident("cancel_order_execute", 0.80) is True
    assert route_intent_is_confident("cancel_order_execute", 0.79) is False


def test_registry_tools_for_names_preserves_registry_order():
    tools = DEFAULT_TOOL_REGISTRY.tools_for_names(["shipment_query", "kb_search"])
    assert [tool.name for tool in tools] == ["kb_search", "shipment_query"]
```

- [ ] **Step 2: Run the new tests and verify they fail**

Run:

```bash
uv run --no-sync pytest tests/test_route_policy.py -q
```

Expected: FAIL because `ai_service_agent.graph.route_policy` and `ToolRegistry.tools_for_names` do not exist.

- [ ] **Step 3: Add registry lookup by explicit names**

In `src/ai_service_agent/tools/registry.py`, add this method to `ToolRegistry` immediately after `for_risk_level`:

```python
    def tools_for_names(self, names: Iterable[str]) -> list[BaseTool]:
        """Build-time/runtime injection by explicit allowlist; registry order is preserved."""
        wanted = set(names)
        return [
            fn
            for name, (_, fn) in self._tools.items()
            if name in wanted
        ]
```

Add this import at the top:

```python
from typing import Iterable, Optional
```

Replace the existing `from typing import Optional` import rather than duplicating it.

- [ ] **Step 4: Implement the route policy module**

Create `src/ai_service_agent/graph/route_policy.py`:

```python
from dataclasses import dataclass
from typing import Literal

from ai_service_agent.tools.registry import ToolRegistry


RouteIntent = Literal[
    "faq",
    "smalltalk",
    "order_query",
    "my_orders",
    "shipment_query",
    "account_profile_update",
    "order_mutation",
    "cancel_order_execute",
    "refund_apply_execute",
    "unsupported_action",
    "unknown",
]

TargetGraph = Literal[
    "read_graph",
    "mutation_graph",
    "high_risk_graph",
    "fallback",
]

ExecutionClass = Literal[
    "read_only",
    "standard_write",
    "high_risk",
]


@dataclass(frozen=True)
class RoutePolicy:
    target_graph: TargetGraph
    execution_class: ExecutionClass
    allowed_tools: tuple[str, ...] = ()
    requires_approval: bool = False


ROUTE_POLICY: dict[str, RoutePolicy] = {
    "faq": RoutePolicy(
        target_graph="read_graph",
        execution_class="read_only",
        allowed_tools=("kb_search",),
    ),
    "smalltalk": RoutePolicy(
        target_graph="read_graph",
        execution_class="read_only",
        allowed_tools=(),
    ),
    "order_query": RoutePolicy(
        target_graph="read_graph",
        execution_class="read_only",
        allowed_tools=("order_lookup",),
    ),
    "my_orders": RoutePolicy(
        target_graph="read_graph",
        execution_class="read_only",
        allowed_tools=("my_orders",),
    ),
    "shipment_query": RoutePolicy(
        target_graph="read_graph",
        execution_class="read_only",
        allowed_tools=("shipment_query",),
    ),
    "account_profile_update": RoutePolicy(
        target_graph="mutation_graph",
        execution_class="standard_write",
        allowed_tools=("update_address", "update_phone"),
    ),
    "order_mutation": RoutePolicy(
        target_graph="mutation_graph",
        execution_class="standard_write",
        allowed_tools=("update_order_note", "create_invoice", "create_service_ticket"),
    ),
    "cancel_order_execute": RoutePolicy(
        target_graph="high_risk_graph",
        execution_class="high_risk",
        allowed_tools=("cancel_order",),
        requires_approval=True,
    ),
    "refund_apply_execute": RoutePolicy(
        target_graph="high_risk_graph",
        execution_class="high_risk",
        allowed_tools=("refund_apply",),
        requires_approval=True,
    ),
    "unsupported_action": RoutePolicy(
        target_graph="fallback",
        execution_class="read_only",
        allowed_tools=(),
    ),
    "unknown": RoutePolicy(
        target_graph="fallback",
        execution_class="read_only",
        allowed_tools=(),
    ),
}


EXECUTION_CLASS_THRESHOLDS: dict[str, float] = {
    "read_only": 0.60,
    "standard_write": 0.70,
    "high_risk": 0.80,
}


def resolve_route_policy(route_intent: str) -> RoutePolicy:
    """Resolve unknown or invalid intents to the safe fallback policy."""
    return ROUTE_POLICY.get(route_intent, ROUTE_POLICY["unknown"])


def route_intent_is_confident(route_intent: str, confidence: float) -> bool:
    """Apply the deterministic execution-class threshold to a route candidate."""
    policy = resolve_route_policy(route_intent)
    return float(confidence) >= EXECUTION_CLASS_THRESHOLDS[policy.execution_class]


def tools_for_policy(registry: ToolRegistry, policy: RoutePolicy):
    """Resolve the policy allowlist into concrete tools using registry order."""
    return registry.tools_for_names(policy.allowed_tools)
```

- [ ] **Step 5: Run the route-policy tests**

Run:

```bash
uv run --no-sync pytest tests/test_route_policy.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/ai_service_agent/graph/route_policy.py src/ai_service_agent/tools/registry.py tests/test_route_policy.py
git commit -m "feat: add deterministic route intent policy"
```

---

### Task 2: Add route-intent configuration, prompt, and test helper

**Files:**
- Modify: `src/ai_service_agent/config.py`
- Modify: `src/ai_service_agent/prompts.py`
- Modify: `src/ai_service_agent/testing.py`
- Modify: `.env.example`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: existing `Settings` and `get_settings.cache_clear()` behavior.
- Produces: `Settings.route_intent_enabled`, `ROUTE_INTENT_SYSTEM`, and `route_intent_json_message(route_intent: str, confidence: float) -> AIMessage`.

- [ ] **Step 1: Write the failing config and helper tests**

Append to `tests/test_config.py`:

```python
def test_route_intent_feature_defaults_off(monkeypatch):
    monkeypatch.delenv("JEV_ROUTE_INTENT_ENABLED", raising=False)
    get_settings.cache_clear()
    try:
        assert get_settings().route_intent_enabled is False
    finally:
        get_settings.cache_clear()


def test_route_intent_feature_can_be_enabled(monkeypatch):
    monkeypatch.setenv("JEV_ROUTE_INTENT_ENABLED", "true")
    get_settings.cache_clear()
    try:
        assert get_settings().route_intent_enabled is True
    finally:
        get_settings.cache_clear()
```

Create `tests/test_route_intent_testing.py`:

```python
import json

from ai_service_agent.testing import route_intent_json_message


def test_route_intent_json_message_contract():
    message = route_intent_json_message("order_query", 0.92)
    assert json.loads(message.content) == {
        "route_intent": "order_query",
        "confidence": 0.92,
    }
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
uv run --no-sync pytest tests/test_config.py tests/test_route_intent_testing.py -q
```

Expected: FAIL because `route_intent_enabled` and `route_intent_json_message` do not exist.

- [ ] **Step 3: Add the feature flag**

In `src/ai_service_agent/config.py`, immediately after `jev_risk_confidence_threshold`, add:

```python
    # Route Intent Router v2：默认关闭；开启后 Router 只输出 route_intent + confidence。
    route_intent_enabled: bool = Field(
        default=False,
        validation_alias="JEV_ROUTE_INTENT_ENABLED",
    )
```

- [ ] **Step 4: Add the route-intent prompt**

In `src/ai_service_agent/prompts.py`, directly after `ROUTER_SYSTEM`, add:

```python
ROUTE_INTENT_SYSTEM = """你是客服路由意图分类器。

根据用户当前消息判断主要业务意图。只输出 JSON，不要输出任何其它内容。

分类标准：
- faq：产品使用、售后政策、商品参数、故障排查等知识问答
- smalltalk：问候、感谢或非业务闲聊
- order_query：根据订单号查询某一笔订单
- my_orders：查询当前用户名下的订单列表
- shipment_query：查询物流或配送进度
- account_profile_update：修改收货地址或联系电话
- order_mutation：修改订单备注、开发票、创建售后工单
- cancel_order_execute：用户明确要求执行取消订单
- refund_apply_execute：用户明确要求执行退款申请
- unsupported_action：当前系统没有对应工具的动作
- unknown：无法判断、缺少上下文、模糊或不在支持范围内

规则：
- “取消订单怎么操作”“退款多久到账”是咨询，不是执行。
- 当前系统不支持的动作必须归类为 unsupported_action。
- 只根据当前请求判断，不要猜测未发生的操作。

输出格式：
{"route_intent":"unknown","confidence":0.20}
"""
```

- [ ] **Step 5: Add the offline Router helper**

In `src/ai_service_agent/testing.py`, directly after `router_json_message`, add:

```python
def route_intent_json_message(route_intent: str, confidence: float) -> AIMessage:
    """Route Intent Router v2 的 json_mode 离线契约。"""
    return AIMessage(
        content=json.dumps(
            {"route_intent": route_intent, "confidence": confidence},
            ensure_ascii=False,
        )
    )
```

- [ ] **Step 6: Update `.env.example`**

After the existing `JEV_RISK_CONFIDENCE_THRESHOLD` line, add:

```dotenv
# Route Intent Router v2：默认关闭；开启后与旧 Router 并存，可通过关闭 flag 回滚。
JEV_ROUTE_INTENT_ENABLED=false
```

- [ ] **Step 7: Run the tests**

Run:

```bash
uv run --no-sync pytest tests/test_config.py tests/test_route_intent_testing.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/ai_service_agent/config.py src/ai_service_agent/prompts.py src/ai_service_agent/testing.py .env.example tests/test_config.py tests/test_route_intent_testing.py
git commit -m "feat: add route intent router configuration"
```

---

### Task 3: Implement the route-intent Router with Jev and DeepSeek fallback

**Files:**
- Modify: `src/ai_service_agent/graph/router.py`
- Test: `tests/test_route_intent_router.py`

**Interfaces:**
- Consumes: `ROUTE_INTENT_SYSTEM`, `Settings.route_intent_enabled`, `resolve_route_policy`, `route_intent_is_confident`.
- Produces: `make_router(model)` behavior, when enabled, returning `{"route_intent": str, "confidence": float, "execution_status": "routing"}`; `_parse_route_intent_content(content) -> dict`; `_classify_route_intent_with_jev(client, state, settings) -> dict | None`.

- [ ] **Step 1: Write failing Router tests**

Create `tests/test_route_intent_router.py`:

```python
import httpx
import pytest

from ai_service_agent.config import get_settings
from ai_service_agent.graph.router import make_router
from ai_service_agent.testing import ToolCallingFakeModel, route_intent_json_message


@pytest.fixture
def route_intent_settings(monkeypatch):
    monkeypatch.setenv("JEV_ROUTE_INTENT_ENABLED", "true")
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


@pytest.fixture
def jev_route_settings(route_intent_settings, monkeypatch):
    monkeypatch.setenv("JEV_ENABLED", "true")
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


class _JevResponse:
    def __init__(self, data: dict):
        self._data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


class _JevClient:
    def __init__(self, data: dict):
        self.data = data

    def post(self, *args, **kwargs):
        return _JevResponse(self.data)


def _jev_route_payload(route_intent: str, confidence: float) -> dict:
    probabilities = {intent: 0.0 for intent in (
        "faq",
        "smalltalk",
        "order_query",
        "my_orders",
        "shipment_query",
        "account_profile_update",
        "order_mutation",
        "cancel_order_execute",
        "refund_apply_execute",
        "unsupported_action",
        "unknown",
    )}
    probabilities[route_intent] = 1.0
    return {
        "answers": {
            "route_intent": {
                "type": "choice",
                "choice": route_intent,
                "confidence": confidence,
                "probabilities": probabilities,
            },
        }
    }


def test_deepseek_route_intent_path(route_intent_settings):
    model = ToolCallingFakeModel(
        responses=[route_intent_json_message("my_orders", 0.94)]
    )
    node = make_router(model)
    out = node({"messages": [], "user_id": "u1", "context": {}})
    assert out == {
        "route_intent": "my_orders",
        "confidence": 0.94,
        "execution_status": "routing",
    }


def test_deepseek_low_confidence_becomes_unknown(route_intent_settings):
    model = ToolCallingFakeModel(
        responses=[route_intent_json_message("cancel_order_execute", 0.79)]
    )
    node = make_router(model)
    out = node({"messages": [], "user_id": "u1", "context": {}})
    assert out["route_intent"] == "unknown"
    assert out["confidence"] == 0.79


def test_jev_route_intent_is_primary(jev_route_settings, monkeypatch):
    client = _JevClient(_jev_route_payload("cancel_order_execute", 0.94))
    monkeypatch.setattr("ai_service_agent.graph.router.httpx.Client", lambda timeout: client)
    model = ToolCallingFakeModel(
        responses=[route_intent_json_message("refund_apply_execute", 0.99)]
    )
    node = make_router(model)
    out = node({"messages": [], "user_id": "u1", "context": {}})
    assert out["route_intent"] == "cancel_order_execute"
    assert out["confidence"] == 0.94


def test_jev_low_confidence_falls_back_to_deepseek(jev_route_settings, monkeypatch):
    client = _JevClient(_jev_route_payload("cancel_order_execute", 0.79))
    monkeypatch.setattr("ai_service_agent.graph.router.httpx.Client", lambda timeout: client)
    model = ToolCallingFakeModel(
        responses=[route_intent_json_message("cancel_order_execute", 0.95)]
    )
    node = make_router(model)
    out = node({"messages": [], "user_id": "u1", "context": {}})
    assert out["route_intent"] == "cancel_order_execute"
    assert out["confidence"] == 0.95


def test_jev_network_error_falls_back_to_deepseek(jev_route_settings, monkeypatch):
    class _TimeoutClient:
        def post(self, *args, **kwargs):
            raise httpx.ConnectTimeout("timeout")

    monkeypatch.setattr("ai_service_agent.graph.router.httpx.Client", lambda timeout: _TimeoutClient())
    model = ToolCallingFakeModel(
        responses=[route_intent_json_message("faq", 0.91)]
    )
    node = make_router(model)
    out = node({"messages": [], "user_id": "u1", "context": {}})
    assert out["route_intent"] == "faq"
    assert out["confidence"] == 0.91


def test_jev_invalid_probability_sum_falls_back_to_deepseek(jev_route_settings, monkeypatch):
    payload = _jev_route_payload("cancel_order_execute", 0.95)
    payload["answers"]["route_intent"]["probabilities"]["cancel_order_execute"] = 0.80
    client = _JevClient(payload)
    monkeypatch.setattr("ai_service_agent.graph.router.httpx.Client", lambda timeout: client)
    model = ToolCallingFakeModel(
        responses=[route_intent_json_message("cancel_order_execute", 0.95)]
    )
    node = make_router(model)
    out = node({"messages": [], "user_id": "u1", "context": {}})
    assert out["route_intent"] == "cancel_order_execute"
    assert out["confidence"] == 0.95


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload["answers"]["route_intent"].update({"type": "noul"}),
        lambda payload: payload["answers"]["route_intent"].pop("probabilities"),
        lambda payload: payload["answers"]["route_intent"]["probabilities"].pop("cancel_order_execute"),
        lambda payload: payload["answers"]["route_intent"].update({"confidence": 1.2}),
    ],
)
def test_jev_invalid_choice_shape_falls_back_to_deepseek(
    jev_route_settings,
    monkeypatch,
    mutate,
):
    payload = _jev_route_payload("cancel_order_execute", 0.95)
    mutate(payload)
    client = _JevClient(payload)
    monkeypatch.setattr("ai_service_agent.graph.router.httpx.Client", lambda timeout: client)
    model = ToolCallingFakeModel(
        responses=[route_intent_json_message("cancel_order_execute", 0.95)]
    )
    node = make_router(model)
    out = node({"messages": [], "user_id": "u1", "context": {}})
    assert out["route_intent"] == "cancel_order_execute"
    assert out["confidence"] == 0.95


def test_feature_off_keeps_legacy_router(monkeypatch):
    monkeypatch.setenv("JEV_ROUTE_INTENT_ENABLED", "false")
    get_settings.cache_clear()
    try:
        model = ToolCallingFakeModel(
            responses=[route_intent_json_message("faq", 0.9)]
        )
        from ai_service_agent.testing import router_json_message

        model = ToolCallingFakeModel(
            responses=[router_json_message("faq", "read", 0.9)]
        )
        node = make_router(model)
        out = node({"messages": [], "user_id": "u1", "context": {}})
        assert out["capability"] == "faq"
        assert out["risk_level"] == "read"
    finally:
        get_settings.cache_clear()
```

- [ ] **Step 2: Run Router tests and verify they fail**

Run:

```bash
uv run --no-sync pytest tests/test_route_intent_router.py -q
```

Expected: FAIL because the feature flag path does not exist.

- [ ] **Step 3: Implement the route-intent Router**

In `src/ai_service_agent/graph/router.py`, add these imports:

```python
import math

from ai_service_agent.graph.route_policy import (
    ROUTE_POLICY,
    route_intent_is_confident,
)
from ai_service_agent.prompts import ROUTE_INTENT_SYSTEM
```

Add this schema after `RouterSchema`:

```python
class RouteIntentSchema(BaseModel):
    route_intent: str
    confidence: float
```

Add this Jev question after `_JEV_QUESTIONS`:

```python
_JEV_ROUTE_INTENT_QUESTION = {
    "route_intent": {
        "type": "choice",
        "instructions": (
            "判断用户当前 turn 的主要业务意图。"
            "只根据当前系统真实支持的业务分类；"
            "不支持的动作必须归为 unsupported_action。"
        ),
        "criteria": {
            "faq": "产品使用、售后政策、商品参数、故障排查等知识问答",
            "smalltalk": "问候、感谢或非业务闲聊",
            "order_query": "根据订单号查询某一笔订单",
            "my_orders": "查询当前用户名下的订单列表",
            "shipment_query": "查询物流或配送进度",
            "account_profile_update": "修改收货地址或联系电话",
            "order_mutation": "修改订单备注、开发票或创建售后工单",
            "cancel_order_execute": "用户明确要求执行取消订单",
            "refund_apply_execute": "用户明确要求执行退款申请",
            "unsupported_action": "当前系统没有对应工具的动作",
            "unknown": "无法判断、缺少上下文、模糊或不在支持范围内",
        },
    }
}
```

Add parse and Jev functions after `_classify_with_jev`:

```python
def _parse_route_intent_content(content) -> dict:
    if isinstance(content, str):
        return json.loads(content)
    if isinstance(content, dict):
        return content
    return RouteIntentSchema.model_validate(content).model_dump()


def _classify_route_intent_with_jev(client, state: dict, settings) -> dict | None:
    response = client.post(
        settings.jev_api_url,
        headers={"Authorization": f"Bearer {settings.jev_api_key}"},
        json={
            "state": _jev_state(state),
            "model": settings.jev_model,
            "questions": _JEV_ROUTE_INTENT_QUESTION,
        },
        timeout=settings.jev_timeout_seconds,
    )
    response.raise_for_status()
    data = response.json()
    answer = data.get("answers", {}).get("route_intent")
    if not isinstance(answer, dict) or answer.get("type") != "choice":
        return None

    choice = answer.get("choice")
    probabilities = answer.get("probabilities")
    try:
        confidence = float(answer.get("confidence"))
        probabilities = {
            str(name): float(value)
            for name, value in probabilities.items()
        }
    except (TypeError, ValueError):
        return None

    if choice not in ROUTE_POLICY or choice not in probabilities:
        return None
    if not 0.0 <= confidence <= 1.0:
        return None
    if set(probabilities) != set(ROUTE_POLICY):
        return None
    if any(value < 0 or value > 1 for value in probabilities.values()):
        return None
    if not math.isclose(sum(probabilities.values()), 1.0, abs_tol=1e-6):
        return None
    if probabilities[choice] < max(probabilities.values()):
        return None

    return {
        "route_intent": choice,
        "confidence": confidence,
        "execution_status": "routing",
    }
```

Add the route-intent Router factory after the existing `make_router`:

```python
def make_route_intent_router(model) -> Callable[[dict], dict]:
    """Route Intent Router v2：Jev primary，DeepSeek fallback，Policy 仍负责路由。"""
    agent = create_agent(_router_model(model), tools=[], system_prompt=ROUTE_INTENT_SYSTEM)

    def deepseek_route(state: dict) -> dict:
        result = agent.invoke({"messages": state["messages"]})
        payload = _parse_route_intent_content(result["messages"][-1].content)
        parsed = RouteIntentSchema.model_validate(payload)
        intent = parsed.route_intent
        confidence = float(parsed.confidence)
        if intent not in ROUTE_POLICY or not route_intent_is_confident(intent, confidence):
            intent = "unknown"
        return {
            "route_intent": intent,
            "confidence": confidence,
            "execution_status": "routing",
        }

    settings = get_settings()
    if not settings.jev_enabled or not settings.jev_api_key or settings.fake_model:
        return deepseek_route

    jev_client = httpx.Client(timeout=settings.jev_timeout_seconds)

    def route(state: dict) -> dict:
        try:
            candidate = _classify_route_intent_with_jev(jev_client, state, settings)
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            return deepseek_route(state)
        if candidate is not None and route_intent_is_confident(
            candidate["route_intent"], candidate["confidence"]
        ):
            return candidate
        return deepseek_route(state)

    return route
```

At the top of the existing `make_router`, immediately after `settings = get_settings()`, add:

```python
    if settings.route_intent_enabled:
        return make_route_intent_router(model)
```

- [ ] **Step 4: Run Router tests**

Run:

```bash
uv run --no-sync pytest tests/test_route_intent_router.py tests/test_router.py tests/test_jev_router.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/ai_service_agent/graph/router.py tests/test_route_intent_router.py
git commit -m "feat: add jev route intent router"
```

---

### Task 4: Add route-intent fields to graph state

**Files:**
- Modify: `src/ai_service_agent/graph/state.py`
- Modify: `docs/specs/state-spec.md`
- Test: `tests/test_state_contract.py`

**Interfaces:**
- Consumes: `RouteIntent`, `TargetGraph`, `ExecutionClass`.
- Produces: state fields `route_intent`, `target_graph`, `execution_class`, `allowed_tools`, and `requires_approval`.

- [ ] **Step 1: Write the failing state contract test**

Create `tests/test_state_contract.py`:

```python
from ai_service_agent.graph.route_policy import RoutePolicy
from ai_service_agent.graph.state import CustomerServiceState


def test_customer_service_state_declares_route_intent_fields():
    hints = CustomerServiceState.__annotations__
    assert "route_intent" in hints
    assert "target_graph" in hints
    assert "execution_class" in hints
    assert "allowed_tools" in hints
    assert "requires_approval" in hints
```

- [ ] **Step 2: Run the test and verify it fails**

Run:

```bash
uv run --no-sync pytest tests/test_state_contract.py -q
```

Expected: FAIL because the fields are absent.

- [ ] **Step 3: Update the state TypedDict**

In `src/ai_service_agent/graph/state.py`, after `risk_level`, add:

```python
    route_intent: str
    target_graph: Literal[
        "read_graph", "mutation_graph", "high_risk_graph", "fallback"
    ]
    execution_class: Literal[
        "read_only", "standard_write", "high_risk"
    ]
    allowed_tools: list[str]
    requires_approval: bool
```

- [ ] **Step 4: Update state spec**

In `docs/specs/state-spec.md`, in the `CustomerServiceState` code block after `risk_level`, add:

```python
    route_intent: str              # Router v2 输出的稳定路由意图
    target_graph: Literal["read_graph", "mutation_graph", "high_risk_graph", "fallback"]
    execution_class: Literal["read_only", "standard_write", "high_risk"]
    allowed_tools: list[str]       # Policy 派生的候选工具边界
    requires_approval: bool        # Policy 派生的高风险审批标记
```

Below the existing rules, add:

```markdown
- `route_intent` 是 Router v2 的唯一语义输出；`target_graph`、`execution_class`、`allowed_tools`、`requires_approval` 必须由 Policy Gate 确定性派生。
- `allowed_tools` 是候选工具边界，不是最终工具选择；最终工具调用仍由子图完成。
- 旧 `capability` / `risk_level` 字段仅在 Router v1 路径继续使用；v2 路径不得依赖它们做路由。
```

- [ ] **Step 5: Run the test**

Run:

```bash
uv run --no-sync pytest tests/test_state_contract.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/ai_service_agent/graph/state.py docs/specs/state-spec.md tests/test_state_contract.py
git commit -m "feat: add route intent state contract"
```

---

### Task 5: Wire Policy Gate to route intent

**Files:**
- Modify: `src/ai_service_agent/graph/builder.py`
- Test: `tests/test_route_intent_policy_gate.py`

**Interfaces:**
- Consumes: `resolve_route_policy`, `Settings.route_intent_enabled`, and state fields from Task 4.
- Produces: `_policy_gate(state) -> dict`, which returns a route-policy update when enabled; `_route_target(state) -> str`, which consumes `state["target_graph"]`.

- [ ] **Step 1: Write failing Policy Gate tests**

Create `tests/test_route_intent_policy_gate.py`:

```python
import pytest

from ai_service_agent.config import get_settings
from ai_service_agent.graph.builder import _guard_aware_target, _policy_gate


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("JEV_ROUTE_INTENT_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_policy_gate_maps_order_query(enabled):
    update = _policy_gate({"route_intent": "order_query"})
    assert update["target_graph"] == "read_graph"
    assert update["execution_class"] == "read_only"
    assert update["allowed_tools"] == ["order_lookup"]
    assert update["requires_approval"] is False


def test_policy_gate_maps_account_update(enabled):
    update = _policy_gate({"route_intent": "update_phone"})
    assert update["target_graph"] == "fallback"
    assert update["execution_class"] == "read_only"
    assert update["allowed_tools"] == []


def test_policy_gate_maps_valid_account_update(enabled):
    update = _policy_gate({"route_intent": "account_profile_update"})
    assert update["target_graph"] == "mutation_graph"
    assert update["execution_class"] == "standard_write"
    assert update["allowed_tools"] == ["update_address", "update_phone"]


def test_policy_gate_maps_cancel_order(enabled):
    update = _policy_gate({"route_intent": "cancel_order_execute"})
    assert update["target_graph"] == "high_risk_graph"
    assert update["execution_class"] == "high_risk"
    assert update["allowed_tools"] == ["cancel_order"]
    assert update["requires_approval"] is True


def test_guard_fail_ignores_route_target(enabled):
    assert _guard_aware_target({"guard_status": "fail", "target_graph": "high_risk_graph"}) == "safety_final"


def test_feature_off_uses_legacy_policy(monkeypatch):
    monkeypatch.setenv("JEV_ROUTE_INTENT_ENABLED", "false")
    get_settings.cache_clear()
    try:
        assert _policy_gate({"route_intent": "order_query"}) == {}
        assert _route_target_legacy() == "fallback"
    finally:
        get_settings.cache_clear()
```

Before running, correct the intentional invalid case name and intent in the second test: rename it to `test_policy_gate_maps_unknown_intent` and use `"update_phone"` only to verify that an unregistered intent safely falls back. The expected values shown above are correct.

Add this helper at the bottom of the test file:

```python
def _route_target_legacy():
    from ai_service_agent.graph.builder import _route_target
    return _route_target({"capability": "unknown", "risk_level": "read"})
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
uv run --no-sync pytest tests/test_route_intent_policy_gate.py -q
```

Expected: FAIL because Policy does not yet map route intent.

- [ ] **Step 3: Implement route-intent Policy Gate**

In `src/ai_service_agent/graph/builder.py`, add:

```python
from ai_service_agent.config import get_settings
from ai_service_agent.graph.route_policy import resolve_route_policy
```

Modify `_route_target`:

```python
def _route_target(state: dict) -> str:
    if get_settings().route_intent_enabled:
        return state.get("target_graph", "fallback")
    return POLICY.get((state.get("capability", "unknown"), state.get("risk_level", "read")), "fallback")
```

Replace `_policy_gate`:

```python
def _policy_gate(state: dict) -> dict:
    """Router/Guard join. v2 deterministically derives the route policy; v1 is a no-op join."""
    if not get_settings().route_intent_enabled:
        return {}
    policy = resolve_route_policy(state.get("route_intent", "unknown"))
    return {
        "route_intent": state.get("route_intent", "unknown"),
        "target_graph": policy.target_graph,
        "execution_class": policy.execution_class,
        "allowed_tools": list(policy.allowed_tools),
        "requires_approval": policy.requires_approval,
    }
```

- [ ] **Step 4: Run Policy Gate tests**

Run:

```bash
uv run --no-sync pytest tests/test_route_intent_policy_gate.py tests/test_builder.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/ai_service_agent/graph/builder.py tests/test_route_intent_policy_gate.py
git commit -m "feat: wire route intent into deterministic policy"
```

---

### Task 6: Add allowed-tools execution boundary

**Files:**
- Create: `src/ai_service_agent/graph/allowed_tools.py`
- Test: `tests/test_allowed_tools.py`

**Interfaces:**
- Consumes: state field `allowed_tools`.
- Produces: `AllowedToolsMiddleware`, `make_allowed_tool_boundary_wrapper_sync(next_wrapper=None)`, and `make_allowed_tool_boundary_wrapper_async(next_wrapper=None)`.

- [ ] **Step 1: Write failing middleware and wrapper tests**

Create `tests/test_allowed_tools.py`:

```python
import json

from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool

from ai_service_agent.graph.allowed_tools import (
    AllowedToolsMiddleware,
    make_allowed_tool_boundary_wrapper_sync,
)


@tool
def alpha_tool() -> dict:
    """Alpha tool."""
    return {"tool": "alpha_tool"}


@tool
def beta_tool() -> dict:
    """Beta tool."""
    return {"tool": "beta_tool"}


def _model_request(allowed_tools):
    return ModelRequest(
        model=None,
        messages=[AIMessage(content="hello")],
        tools=[alpha_tool, beta_tool],
        state={"allowed_tools": allowed_tools},
    )


def test_middleware_filters_model_tools():
    captured = {}

    def handler(request):
        captured["tools"] = request.tools
        return AIMessage(content="ok")

    middleware = AllowedToolsMiddleware()
    middleware.wrap_model_call(_model_request(["beta_tool"]), handler)
    assert [tool.name for tool in captured["tools"]] == ["beta_tool"]


def test_middleware_keeps_all_tools_when_boundary_absent():
    captured = {}

    def handler(request):
        captured["tools"] = request.tools
        return AIMessage(content="ok")

    request = ModelRequest(
        model=None,
        messages=[AIMessage(content="hello")],
        tools=[alpha_tool, beta_tool],
        state={},
    )
    AllowedToolsMiddleware().wrap_model_call(request, handler)
    assert len(captured["tools"]) == 2


def test_tool_boundary_rejects_disallowed_tool():
    request = type(
        "Request",
        (),
        {
            "tool_call": {"name": "beta_tool", "id": "call-1"},
            "state": {"allowed_tools": ["alpha_tool"]},
        },
    )()

    def execute(req):
        raise AssertionError("disallowed tool must not execute")

    result = make_allowed_tool_boundary_wrapper_sync()(request, execute)
    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert json.loads(result.content)["error"] == "tool_not_allowed"


def test_tool_boundary_allows_tool_and_calls_next_wrapper():
    request = type(
        "Request",
        (),
        {
            "tool_call": {"name": "alpha_tool", "id": "call-1"},
            "state": {"allowed_tools": ["alpha_tool"]},
        },
    )()

    def execute(req):
        return "executed"

    def next_wrapper(req, execute):
        return execute(req)

    result = make_allowed_tool_boundary_wrapper_sync(next_wrapper=next_wrapper)(request, execute)
    assert result == "executed"
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
uv run --no-sync pytest tests/test_allowed_tools.py -q
```

Expected: FAIL because the module does not exist.

- [ ] **Step 3: Implement the allowed-tools middleware and wrappers**

Create `src/ai_service_agent/graph/allowed_tools.py`:

```python
"""Deterministic candidate-tool boundary for route-intent execution.

The model may select a tool only after Policy narrows the candidate set. The
middleware filters model-visible tools; ToolNode wrappers reject calls that are
not in the same allowlist.
"""

import json
from collections.abc import Awaitable, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage


def _allowed_tools(state) -> set[str] | None:
    if not isinstance(state, dict) or "allowed_tools" not in state:
        return None
    value = state.get("allowed_tools") or []
    return {str(name) for name in value}


def _not_allowed(request) -> ToolMessage:
    tool_call = request.tool_call
    return ToolMessage(
        content=json.dumps(
            {"error": "tool_not_allowed", "tool": tool_call.get("name")},
            ensure_ascii=False,
        ),
        name=str(tool_call.get("name", "")),
        tool_call_id=str(tool_call.get("id", "")),
        status="error",
    )


class AllowedToolsMiddleware(AgentMiddleware):
    name = "allowed_tools_middleware"

    def wrap_model_call(self, request, handler):
        allowed = _allowed_tools(request.state)
        if allowed is not None:
            request = request.override(
                tools=[tool for tool in request.tools if getattr(tool, "name", "") in allowed]
            )
        return handler(request)

    async def awrap_model_call(self, request, handler):
        allowed = _allowed_tools(request.state)
        if allowed is not None:
            request = request.override(
                tools=[tool for tool in request.tools if getattr(tool, "name", "") in allowed]
            )
        return await handler(request)

    def wrap_tool_call(self, request, handler):
        allowed = _allowed_tools(request.state)
        if allowed is not None and request.tool_call.get("name") not in allowed:
            return _not_allowed(request)
        return handler(request)

    async def awrap_tool_call(self, request, handler):
        allowed = _allowed_tools(request.state)
        if allowed is not None and request.tool_call.get("name") not in allowed:
            return _not_allowed(request)
        return await handler(request)


def make_allowed_tool_boundary_wrapper_sync(next_wrapper=None):
    def wrapper(request, execute):
        allowed = _allowed_tools(request.state)
        if allowed is not None and request.tool_call.get("name") not in allowed:
            return _not_allowed(request)
        if next_wrapper is None:
            return execute(request)
        return next_wrapper(request, execute)

    return wrapper


def make_allowed_tool_boundary_wrapper_async(next_wrapper=None):
    async def wrapper(request, execute):
        allowed = _allowed_tools(request.state)
        if allowed is not None and request.tool_call.get("name") not in allowed:
            return _not_allowed(request)
        if next_wrapper is None:
            return await execute(request)
        return await next_wrapper(request, execute)

    return wrapper
```

- [ ] **Step 4: Run allowed-tools tests**

Run:

```bash
uv run --no-sync pytest tests/test_allowed_tools.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/ai_service_agent/graph/allowed_tools.py tests/test_allowed_tools.py
git commit -m "feat: add policy allowed tools boundary"
```

---

### Task 7: Consume allowed_tools in ReadGraph and write-path tool selection

**Files:**
- Modify: `src/ai_service_agent/graph/agents/read_agent.py`
- Modify: `src/ai_service_agent/graph/workflows/select.py`
- Modify: `src/ai_service_agent/graph/workflows/mutation_agent.py`
- Modify: `src/ai_service_agent/graph/workflows/high_risk_agent.py`
- Test: `tests/test_allowed_tools_execution.py`

**Interfaces:**
- Consumes: `AllowedToolsMiddleware`, `make_allowed_tool_boundary_wrapper_sync`, `make_allowed_tool_boundary_wrapper_async`, and state field `allowed_tools`.
- Produces: model-visible and executable tool sets narrowed by Policy.

- [ ] **Step 1: Write failing execution-boundary tests**

Create `tests/test_allowed_tools_execution.py`:

```python
import pytest
from langchain_core.messages import HumanMessage

from ai_service_agent.graph.workflows.select import make_select_node
from ai_service_agent.testing import ToolCallingFakeModel, select_json_message
from ai_service_agent.tools import DEFAULT_TOOL_REGISTRY


def test_mutation_select_rejects_tool_outside_allowed_tools():
    model = ToolCallingFakeModel(
        responses=[select_json_message(
            "update_address",
            {"order_id": "ORD-PAID-1", "address": "北京市朝阳区"},
        )]
    )
    registry = DEFAULT_TOOL_REGISTRY
    select = make_select_node(
        model,
        "mutation",
        registry.for_risk_level("mutation"),
        registry,
    )
    state = {
        "messages": [HumanMessage(content="帮我改地址")],
        "user_id": "u1",
        "context": {},
        "allowed_tools": ["update_phone"],
    }
    out = select(state)
    assert out["context"]["precheck_reason"] == "tool_not_allowed"


def test_mutation_select_prompt_only_contains_allowed_tools():
    captured = {}

    class _PromptCaptureModel(ToolCallingFakeModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            captured["prompt"] = messages[0].content
            return super()._generate(messages, stop, run_manager, **kwargs)

    model = _PromptCaptureModel(
        responses=[select_json_message(
            "update_phone",
            {"phone": "13800138000"},
        )]
    )
    registry = DEFAULT_TOOL_REGISTRY
    select = make_select_node(
        model,
        "mutation",
        registry.for_risk_level("mutation"),
        registry,
    )
    out = select({
        "messages": [HumanMessage(content="帮我换个手机号，新号 13800138000")],
        "user_id": "u1",
        "context": {},
        "allowed_tools": ["update_phone"],
    })
    assert "update_address" not in captured["prompt"]
    assert "update_phone" in captured["prompt"]
    assert out["context"]["pending_tool"]["tool"] == "update_phone"
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
uv run --no-sync pytest tests/test_allowed_tools_execution.py -q
```

Expected: FAIL because select does not consume `allowed_tools`.

- [ ] **Step 3: Filter model-visible tools in ReadGraph**

In `src/ai_service_agent/graph/agents/read_agent.py`, add:

```python
from ai_service_agent.graph.allowed_tools import AllowedToolsMiddleware
```

In `make_read_graph`, change the middleware list to:

```python
    middleware = [AllowedToolsMiddleware(), ModelCallLimitMiddleware(run_limit=READ_MODEL_CALL_LIMIT, exit_behavior="end")]
```

Also update `ReadAgentState`:

```python
class ReadAgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    user_id: str
    allowed_tools: list[str]
```

- [ ] **Step 4: Filter write-path select prompts and validation**

In `src/ai_service_agent/graph/workflows/select.py`, replace the single `lines` construction inside `make_select_node` with a reusable map:

```python
    tier_tools = [
        t for t in tools
        if (meta := registry.meta_for(t.name)) is not None and meta.min_risk_level == tier
    ]
    tier_allowed = {t.name for t in tier_tools}
    lines_by_name = {}
    for t in tier_tools:
        line = f"- {t.name}: {t.description}"
        contract = _describe_args_schema(registry.meta_for(t.name))
        if contract:
            line += f"\n  参数：{contract}"
        lines_by_name[t.name] = line
```

At the beginning of `select(state)`, after the existing `state = {**state, "context": current_context}` line, add:

```python
        route_allowed = state.get("allowed_tools")
        if route_allowed is None:
            selected_tools = tier_tools
        else:
            route_allowed = {str(name) for name in route_allowed}
            selected_tools = [t for t in tier_tools if t.name in route_allowed]
        allowed = {t.name for t in selected_tools}
        tool_list = "\n".join(lines_by_name[t.name] for t in selected_tools)
```

Inside `select(state)`, replace both uses of the old fixed `tier_tools` in the model invocation:

```python
                examples=_build_examples(selected_tools, registry),
```

After `meta` is resolved and before the existing `if tool_name not in allowed:` check, add:

```python
        route_allowed = state.get("allowed_tools")
        if route_allowed is not None and tool_name not in route_allowed:
            return _reject(state, "tool_not_allowed")
```

- [ ] **Step 5: Enforce the boundary at write-path ToolNode execution**

In both `src/ai_service_agent/graph/workflows/mutation_agent.py` and `src/ai_service_agent/graph/workflows/high_risk_agent.py`, add:

```python
from ai_service_agent.graph.allowed_tools import (
    make_allowed_tool_boundary_wrapper_async,
    make_allowed_tool_boundary_wrapper_sync,
)
```

Replace the existing `ToolNode(...)` construction in each file:

```python
    hook_sync = make_tool_hook_wrapper_sync(tool_hooks) if tool_hooks else None
    hook_async = make_tool_hook_wrapper(tool_hooks) if tool_hooks else None
    boundary_sync = make_allowed_tool_boundary_wrapper_sync(next_wrapper=hook_sync)
    boundary_async = make_allowed_tool_boundary_wrapper_async(next_wrapper=hook_async)
    builder.add_node(
        "tools",
        ToolNode(
            tools,
            wrap_tool_call=boundary_sync,
            awrap_tool_call=boundary_async,
        ),
    )
```

- [ ] **Step 6: Run execution-boundary and existing graph tests**

Run:

```bash
uv run --no-sync pytest tests/test_allowed_tools_execution.py tests/test_mutation_graph.py tests/test_high_risk_graph.py tests/test_read_graph.py -q
```

Expected: PASS. If `tests/test_read_graph.py` does not exist, run `uv run --no-sync pytest tests -k "read_agent or read_graph" -q`.

- [ ] **Step 7: Commit**

```bash
git add src/ai_service_agent/graph/allowed_tools.py src/ai_service_agent/graph/agents/read_agent.py src/ai_service_agent/graph/workflows/select.py src/ai_service_agent/graph/workflows/mutation_agent.py src/ai_service_agent/graph/workflows/high_risk_agent.py tests/test_allowed_tools_execution.py
git commit -m "feat: enforce route policy tool boundaries"
```

---

### Task 8: Add route-intent end-to-end regression

**Files:**
- Test: `tests/test_route_intent_graph.py`

**Interfaces:**
- Consumes: route-intent Router, Policy Gate, state fields, and ReadGraph.
- Produces: integration proof that `route_intent` routes to the expected graph without consuming old `capability` / `risk_level`.

- [ ] **Step 1: Write the failing end-to-end tests**

Create `tests/test_route_intent_graph.py`:

```python
import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver

from ai_service_agent.config import get_settings
from ai_service_agent.graph.builder import build_graph
from ai_service_agent.testing import ToolCallingFakeModel, route_intent_json_message
from ai_service_agent.tools.orders import my_orders, order_lookup, shipment_query
from ai_service_agent.tools.kb import kb_search


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("JEV_ROUTE_INTENT_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_route_intent_graph_routes_order_query(enabled):
    model = ToolCallingFakeModel(responses=[
        route_intent_json_message("order_query", 0.95),
        "ORD-1 是已发货订单，金额 99 元。",
    ])
    graph = build_graph(
        model,
        tools=[kb_search, order_lookup, my_orders, shipment_query],
        checkpointer=MemorySaver(),
    )
    out = graph.invoke(
        {
            "messages": [HumanMessage(content="帮我查订单 ORD-1")],
            "user_id": "u1",
            "session_id": "route-order-1",
            "context": {},
        },
        {"configurable": {"thread_id": "route-order-1"}},
    )
    assert out["route_intent"] == "order_query"
    assert out["target_graph"] == "read_graph"
    assert out["execution_class"] == "read_only"
    assert out["allowed_tools"] == ["order_lookup"]
    assert out["execution_status"] == "completed"


def test_route_intent_graph_routes_unsupported_to_fallback(enabled):
    model = ToolCallingFakeModel(responses=[
        route_intent_json_message("unsupported_action", 0.93),
        "当前系统暂不支持这个操作。",
    ])
    graph = build_graph(
        model,
        tools=[kb_search, order_lookup, my_orders, shipment_query],
        checkpointer=MemorySaver(),
    )
    out = graph.invoke(
        {
            "messages": [HumanMessage(content="清空我的订单历史")],
            "user_id": "u1",
            "session_id": "route-unsupported-1",
            "context": {},
        },
        {"configurable": {"thread_id": "route-unsupported-1"}},
    )
    assert out["route_intent"] == "unsupported_action"
    assert out["target_graph"] == "fallback"
    assert out["allowed_tools"] == []
    assert out["execution_status"] == "completed"
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
uv run --no-sync pytest tests/test_route_intent_graph.py -q
```

Expected: FAIL before Task 5 and Task 7 are complete; after those tasks, this test should expose only integration mismatches.

- [ ] **Step 3: Fix integration mismatches only**

Do not change the contract. If the test fails because a subgraph resets `target_graph` or `allowed_tools`, adjust only the offending state write so Policy-derived fields remain visible after the turn.

- [ ] **Step 4: Run the end-to-end tests**

Run:

```bash
uv run --no-sync pytest tests/test_route_intent_graph.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_route_intent_graph.py
git commit -m "test: cover route intent graph integration"
```

---

### Task 9: Update authoritative specs and environment documentation

**Files:**
- Modify: `docs/specs/graph-spec.md`
- Modify: `docs/specs/tool-spec.md`
- Modify: `.env.example`

**Interfaces:**
- Consumes: implemented behavior from Tasks 1–8.
- Produces: authoritative documentation matching the feature-flagged v2 contract.

- [ ] **Step 1: Update graph-spec Router and Policy sections**

In `docs/specs/graph-spec.md`, replace the Router and Policy summaries with:

```markdown
## 4. Router 节点

- Router v1（默认关闭前的兼容路径）：结构化输出 `capability` + `risk_level` + `confidence`。
- Router v2（`JEV_ROUTE_INTENT_ENABLED=true`）：Jev 只执行一次 `Choice(route_intent)`，输出 `route_intent` + `confidence`。
- v2 `route_intent` 是稳定业务意图标签，不是 `tool_name`，也不是 `target_graph`。
- DeepSeek Router fallback 必须输出同一个 v2 契约。
- Jev 超时、HTTP 错误、非法输出或置信度不足时 fallback 到 DeepSeek；DeepSeek 置信度不足时归为 `unknown`。

## 5. Policy Gate

- Policy Gate 不调用 LLM。
- v1 路径继续使用 `capability x risk_level -> 子图`。
- v2 路径使用 `route_intent -> PolicyDecision`。
- v2 `PolicyDecision` 派生：
  - `target_graph`
  - `execution_class`（`read_only` / `standard_write` / `high_risk`）
  - `allowed_tools`
  - `requires_approval`
- `allowed_tools` 是候选工具边界；子图 LLM 只能在该边界内选择最终工具。
- 未注册 intent、`unsupported_action`、`unknown` 一律 fallback。
```

- [ ] **Step 2: Update tool-spec allowed-tools section**

In `docs/specs/tool-spec.md`, after the existing risk-level injection rules, add:

```markdown
## Allowed Tools（Router v2）

- Router v2 不选择工具。
- Policy Gate 根据 `route_intent` 输出 `allowed_tools`。
- `allowed_tools` 是候选工具边界，子图仍负责最终工具选择与参数提取。
- 子图模型可见工具集必须与 `allowed_tools` 相交。
- ToolNode 执行前必须拒绝不在 `allowed_tools` 内的工具调用。
- 现有 risk-tier 校验、schema 校验、业务预检、审批和 recheck 全部保留。
```

- [ ] **Step 3: Verify environment docs**

Confirm `.env.example` contains:

```dotenv
JEV_ROUTE_INTENT_ENABLED=false
```

- [ ] **Step 4: Run full regression**

Run:

```bash
uv run --no-sync pytest -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add docs/specs/graph-spec.md docs/specs/tool-spec.md .env.example
git commit -m "docs: define route intent policy contract"
```

---

### Task 10: Add deterministic route-intent eval set and report

**Files:**
- Create: `tests/evals/router_benchmark/route_intent.jsonl`
- Create: `scripts/eval_route_intent.py`

**Interfaces:**
- Consumes: `make_router`, `ROUTE_POLICY`, `route_intent_json_message`, and the new eval dataset.
- Produces: reproducible offline accuracy/latency report for the DeepSeek route-intent path.

- [ ] **Step 1: Add the deterministic eval dataset**

Create `tests/evals/router_benchmark/route_intent.jsonl`:

```jsonl
{"id":"RI-001","input":"这个产品怎么安装？","expected_route_intent":"faq"}
{"id":"RI-002","input":"谢谢你啊","expected_route_intent":"smalltalk"}
{"id":"RI-003","input":"帮我查订单 ORD-1","expected_route_intent":"order_query"}
{"id":"RI-004","input":"ORD-10086 现在什么状态？","expected_route_intent":"order_query"}
{"id":"RI-005","input":"我的订单有哪些？","expected_route_intent":"my_orders"}
{"id":"RI-006","input":"帮我看看最近买过什么","expected_route_intent":"my_orders"}
{"id":"RI-007","input":"我的快递到哪了？","expected_route_intent":"shipment_query"}
{"id":"RI-008","input":"帮我改收货地址","expected_route_intent":"account_profile_update"}
{"id":"RI-009","input":"我要换绑手机号","expected_route_intent":"account_profile_update"}
{"id":"RI-010","input":"给订单加个备注：放驿站就行","expected_route_intent":"order_mutation"}
{"id":"RI-011","input":"这笔订单帮我开发票","expected_route_intent":"order_mutation"}
{"id":"RI-012","input":"屏幕有裂痕，创建一个售后工单","expected_route_intent":"order_mutation"}
{"id":"RI-013","input":"订单 ORD-PAID-1 不要了，帮我取消","expected_route_intent":"cancel_order_execute"}
{"id":"RI-014","input":"ORD-1 申请退款，金额 99 元","expected_route_intent":"refund_apply_execute"}
{"id":"RI-015","input":"取消订单的流程是什么？","expected_route_intent":"faq"}
{"id":"RI-016","input":"退款一般多久到账？","expected_route_intent":"faq"}
{"id":"RI-017","input":"清空我的订单历史","expected_route_intent":"unsupported_action"}
{"id":"RI-018","input":"把订单所有权转给我朋友","expected_route_intent":"unsupported_action"}
{"id":"RI-019","input":"取消我的会员预约","expected_route_intent":"unsupported_action"}
{"id":"RI-020","input":"帮我把那个东西处理一下","expected_route_intent":"unknown"}
```

- [ ] **Step 2: Add the offline eval script**

Create `scripts/eval_route_intent.py`:

```python
import argparse
import json
import statistics
import time
from collections import Counter
from pathlib import Path

from langchain_core.messages import HumanMessage

from ai_service_agent.config import get_settings, make_model
from ai_service_agent.graph.router import make_router


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dataset",
        default="tests/evals/router_benchmark/route_intent.jsonl",
    )
    parser.add_argument("--output", default="scripts/route_intent_eval_results.json")
    args = parser.parse_args()

    cases = [
        json.loads(line)
        for line in Path(args.dataset).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    router = make_router(make_model())
    results = []
    for case in cases:
        state = {
            "messages": [HumanMessage(content=case["input"])],
            "user_id": "u1",
            "session_id": "route-intent-eval",
            "context": {},
        }
        started = time.perf_counter()
        output = router(state)
        latency_ms = (time.perf_counter() - started) * 1000
        predicted = output["route_intent"]
        results.append({
            "id": case["id"],
            "input": case["input"],
            "expected_route_intent": case["expected_route_intent"],
            "predicted_route_intent": predicted,
            "confidence": output["confidence"],
            "latency_ms": round(latency_ms, 1),
            "correct": predicted == case["expected_route_intent"],
        })

    confusion = Counter(
        f"{result['expected_route_intent']} -> {result['predicted_route_intent']}"
        for result in results
        if not result["correct"]
    )
    latencies = [result["latency_ms"] for result in results]
    report = {
        "settings": {
            "route_intent_enabled": get_settings().route_intent_enabled,
            "jev_enabled": get_settings().jev_enabled,
            "fake_model": get_settings().fake_model,
        },
        "summary": {
            "total": len(results),
            "route_intent_accuracy": round(
                sum(result["correct"] for result in results) / len(results), 4
            ),
            "mean_ms": round(statistics.mean(latencies), 1),
            "min_ms": round(min(latencies), 1),
            "max_ms": round(max(latencies), 1),
            "confusion": dict(confusion),
        },
        "results": results,
    }
    Path(args.output).write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Run the offline eval**

Run:

```bash
uv run --no-sync python scripts/eval_route_intent.py
```

Expected: the script completes and writes `scripts/route_intent_eval_results.json`. Do not tune thresholds in this task; record the baseline result for review.

- [ ] **Step 4: Commit**

```bash
git add tests/evals/router_benchmark/route_intent.jsonl scripts/eval_route_intent.py scripts/route_intent_eval_results.json
git commit -m "test: add route intent evaluation baseline"
```

---

### Task 11: Final feature-flag verification and review

**Files:**
- No new files.

**Interfaces:**
- Consumes: all prior tasks.
- Produces: regression evidence for both feature-flag states.

- [ ] **Step 1: Run full regression with the feature disabled**

Run:

```bash
$env:JEV_ROUTE_INTENT_ENABLED="false"; uv run --no-sync pytest -q
```

Expected: all tests pass; legacy Router behavior is unchanged.

- [ ] **Step 2: Run targeted regression with the feature enabled**

Run:

```bash
$env:JEV_ROUTE_INTENT_ENABLED="true"; uv run --no-sync pytest tests/test_route_policy.py tests/test_route_intent_router.py tests/test_route_intent_policy_gate.py tests/test_allowed_tools.py tests/test_allowed_tools_execution.py tests/test_route_intent_graph.py -q
```

Expected: all tests pass.

- [ ] **Step 3: Run eval suite**

Run:

```bash
uv run --no-sync pytest tests/evals -q
uv run --no-sync python scripts/eval_route_intent.py
```

Expected: existing eval tests pass and the route-intent eval report is regenerated.

- [ ] **Step 4: Review feature-flag safety**

Check all four points before completion:

```bash
git diff -- .env.example
git diff -- src/ai_service_agent/graph/builder.py
git diff -- src/ai_service_agent/graph/router.py
git diff -- docs/specs/graph-spec.md
```

Verify:

```text
1. JEV_ROUTE_INTENT_ENABLED defaults to false.
2. Feature-off Router still returns capability/risk_level.
3. Feature-on Router never returns tool_name.
4. Policy remains the only source of target_graph/execution_class/allowed_tools/requires_approval.
```

- [ ] **Step 5: Final commit if docs or generated report changed**

```bash
git status --short
git add docs scripts
git commit -m "chore: finalize route intent verification"
```

If there is nothing to commit, record that explicitly in the execution summary.
