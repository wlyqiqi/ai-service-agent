# Router Architecture Unification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Unify Router to a single `route_intent` contract (JEV primary, DeepSeek fallback), eliminating the legacy `capability + risk_level` path and the `POLICY` dict.

**Architecture:** One Router (LLM) outputs `route_intent + confidence`. One Policy Gate (deterministic) calls `resolve_route_policy(route_intent)` to derive `target_graph + execution_class + allowed_tools + requires_approval`. JEV is primary; DeepSeek fallback uses the same `ROUTE_INTENT_SYSTEM` prompt and output schema.

**Tech Stack:** Python 3.14, LangGraph 1.2.10, LangChain Core 1.5.3, Pydantic, pytest.

**Spec:** `docs/specs/graph-spec.md` §2 Router, §5 Policy Gate

## Global Constraints

- Router only outputs intent; does not execute tools or permissions (AGENTS.md §5, §18).
- Policy Gate is deterministic, does not call LLM (AGENTS.md §7).
- ReadGraph must not receive write tools (AGENTS.md §11).
- HighRisk must go through approval (AGENTS.md §12).
- Do not change subgraph internals (only builder + router + prompts + config + tests + docs).
- `ROUTE_INTENT_SYSTEM` prompt content stays unchanged (already validated for JEV precision).
- All existing `route_intent` values and `ROUTE_POLICY` mappings remain unchanged.

---

### Task 1: Delete `ROUTER_SYSTEM` prompt and `RouterSchema`

**Files:**
- Modify: `src/ai_service_agent/prompts.py` (delete `ROUTER_SYSTEM` block, lines 10–101)
- Modify: `src/ai_service_agent/graph/router.py` (delete `RouterSchema` class, line 40; delete `_parse_router_content`, line 60)

**Interfaces:**
- Consumes: nothing (this is a deletion task)
- Produces: `router.py` no longer imports `ROUTER_SYSTEM`; `RouteIntentSchema` is the only router output schema

- [ ] **Step 1: Write failing test**

```python
# tests/test_router.py — add at bottom
def test_router_system_prompt_deleted():
    from ai_service_agent import prompts
    assert not hasattr(prompts, "ROUTER_SYSTEM")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_router.py::test_router_system_prompt_deleted -v`
Expected: FAIL — `ROUTER_SYSTEM` still exists.

- [ ] **Step 3: Delete `ROUTER_SYSTEM` from `prompts.py` and `RouterSchema` + `_parse_router_content` from `router.py`**

In `prompts.py`: delete the entire `ROUTER_SYSTEM = """..."""` block (lines 10–101).
In `router.py`: delete `class RouterSchema(BaseModel):` block (line 40–43) and `def _parse_router_content(content)` function (line 60–68). Remove `ROUTER_SYSTEM` from the import at line 19.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_router.py::test_router_system_prompt_deleted -v`
Expected: PASS

- [ ] **Step 5: Run router tests to verify no regression**

Run: `pytest tests/test_router.py tests/test_route_intent_router.py -q`
Expected: Legacy tests (`test_router_parses_structured_output`, `test_router_low_confidence_goes_unknown` at lines 9, 21) FAIL because `RouterSchema` is gone. These must be updated in Task 3.

- [ ] **Step 6: Commit**

```bash
git add src/ai_service_agent/prompts.py src/ai_service_agent/graph/router.py
git commit -m "refactor(router): remove legacy ROUTER_SYSTEM prompt and RouterSchema"
```

---

### Task 2: Unify `make_deepseek_router` to always output `route_intent`

**Files:**
- Modify: `src/ai_service_agent/graph/router.py:252-284` (rewrite `make_deepseek_router`)

**Interfaces:**
- Consumes: `ROUTE_INTENT_SYSTEM` (prompts.py), `RouteIntentSchema`, `ROUTE_POLICY`, `route_intent_is_confident`
- Produces: `make_deepseek_router(model)` — signature drops `route_intent_enabled` kwarg; always returns a callable that outputs `route_intent + confidence + routing_backend + routing_fallback + route_control`

- [ ] **Step 1: Write failing test**

```python
# tests/test_router.py — replace legacy tests at lines 9–36 with:
async def test_deepseek_fallback_outputs_route_intent():
    from ai_service_agent.testing import make_fake_model
    from langchain_core.messages import AIMessage
    from ai_service_agent.graph.router import make_deepseek_router
    model = make_fake_model(responses=[AIMessage(content='{"route_intent":"faq","confidence":0.95}')])
    node = make_deepseek_router(model)
    state = {"messages": [{"role": "human", "content": "产品怎么用"}]}
    result = await node(state)
    assert result["route_intent"] == "faq"
    assert result["routing_backend"] == "deepseek"
    assert result["routing_fallback"] is True
    assert result["route_control"] == "policy"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_router.py::test_deepseek_fallback_outputs_route_intent -v`
Expected: FAIL — `make_deepseek_router` still requires `route_intent_enabled` kwarg and old tests fail.

- [ ] **Step 3: Rewrite `make_deepseek_router`**

Replace the entire function (lines 252–284) with:

```python
def make_deepseek_router(model) -> Callable[[dict], Awaitable[dict]]:
    """DeepSeek fallback; always outputs route_intent (same schema as JEV)."""
    agent = create_agent(_router_model(model), tools=[], system_prompt=ROUTE_INTENT_SYSTEM)

    async def deepseek_route(state: dict, config: RunnableConfig | None = None) -> dict:
        result = await agent.ainvoke({"messages": state["messages"]}, config=config)
        payload = _parse_route_intent_content(result["messages"][-1].content)
        parsed = RouteIntentSchema.model_validate(payload)
        intent = parsed.route_intent
        confidence = float(parsed.confidence)
        if intent not in ROUTE_POLICY or not route_intent_is_confident(intent, confidence):
            intent = "unknown"
        return _route_intent_result(intent, confidence, "deepseek", True)

    return deepseek_route
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_router.py::test_deepseek_fallback_outputs_route_intent -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/ai_service_agent/graph/router.py tests/test_router.py
git commit -m "refactor(router): unify deepseek fallback to route_intent contract"
```

---

### Task 3: Update legacy router tests to route_intent contract

**Files:**
- Modify: `tests/test_router.py` (replace all legacy `RouterSchema` tests)

**Interfaces:**
- Consumes: `make_deepseek_router(model)` (Task 2)
- Produces: All router tests verify `route_intent` output

- [ ] **Step 1: Rewrite legacy tests**

Replace `test_router_parses_structured_output` (line 9) and `test_router_low_confidence_goes_unknown` (line 21) with route_intent equivalents:

```python
async def test_router_parses_route_intent():
    from ai_service_agent.testing import make_fake_model
    from langchain_core.messages import AIMessage
    from ai_service_agent.graph.router import make_deepseek_router
    model = make_fake_model(responses=[AIMessage(content='{"route_intent":"faq","confidence":0.95}')])
    node = make_deepseek_router(model)
    result = await node({"messages": [{"role": "human", "content": "产品怎么用"}]})
    assert result["route_intent"] == "faq"
    assert result["confidence"] == 0.95

async def test_router_low_confidence_becomes_unknown():
    from ai_service_agent.testing import make_fake_model
    from langchain_core.messages import AIMessage
    from ai_service_agent.graph.router import make_deepseek_router
    model = make_fake_model(responses=[AIMessage(content='{"route_intent":"faq","confidence":0.3}')])
    node = make_deepseek_router(model)
    result = await node({"messages": [{"role": "human", "content": "在吗"}]})
    assert result["route_intent"] == "unknown"
```

Also update `test_router_parse_accepts_json_mode_string_and_dict` and `test_router_parse_rejects_tool_strategy_wrapper_text` (lines 55, 62) to use `_parse_route_intent_content` instead of `_parse_router_content`.

- [ ] **Step 2: Run all router tests**

Run: `pytest tests/test_router.py -q`
Expected: All PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_router.py
git commit -m "test(router): migrate legacy tests to route_intent contract"
```

---

### Task 4: Remove `POLICY` dict and `route_intent_enabled` from builder

**Files:**
- Modify: `src/ai_service_agent/graph/builder.py` (delete `POLICY` dict lines 33–47, simplify `_route_target` and `_policy_gate`, remove `route_intent_enabled` branches from `build_graph`)

**Interfaces:**
- Consumes: `make_deepseek_router(model)` (Task 2), `resolve_route_policy` (unchanged)
- Produces: `_policy_gate` always calls `resolve_route_policy(route_intent)`; `_route_target` always reads `state["target_graph"]`

- [ ] **Step 1: Write failing test**

```python
# tests/test_builder.py — replace POLICY dict tests (lines 73–76, 139–140) with:
def test_policy_gate_always_resolves_route_intent():
    from ai_service_agent.graph.builder import _policy_gate
    from unittest.mock import patch
    with patch("ai_service_agent.graph.builder.get_settings") as mock_settings:
        mock_settings.return_value.route_intent_enabled = False  # even when flag absent
        result = _policy_gate({"route_intent": "faq"})
        assert result["target_graph"] == "read_graph"
        assert result["execution_class"] == "read_only"
```

Wait — after removing `route_intent_enabled`, `_policy_gate` no longer checks the flag. The test should simply be:

```python
def test_policy_gate_resolves_faq_route_intent():
    from ai_service_agent.graph.builder import _policy_gate
    result = _policy_gate({"route_intent": "faq"})
    assert result["target_graph"] == "read_graph"
    assert result["execution_class"] == "read_only"
    assert result["allowed_tools"] == ["kb_search"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_builder.py::test_policy_gate_resolves_faq_route_intent -v`
Expected: FAIL — `_policy_gate` still returns `{}` when `route_intent_enabled` is false.

- [ ] **Step 3: Simplify builder**

In `builder.py`:

1. Delete `POLICY` dict (lines 33–47).
2. Replace `_route_target`:
```python
def _route_target(state: dict) -> str:
    return state.get("target_graph", "fallback")
```
3. Replace `_policy_gate` (remove the early return `{}`):
```python
def _policy_gate(state: dict) -> dict:
    """Router/Guard join. Deterministically derives route policy from route_intent."""
    route_intent = state.get("route_intent", "unknown")
    if (
        route_intent == "order_mutation"
        and _PROFILE_UPDATE_PATTERN.search(_latest_user_text(state))
    ):
        route_intent = "account_profile_update"
    policy = resolve_route_policy(route_intent)
    return {
        "route_intent": route_intent,
        "target_graph": policy.target_graph,
        "execution_class": policy.execution_class,
        "allowed_tools": list(policy.allowed_tools),
        "requires_approval": policy.requires_approval,
    }
```
4. In `build_graph`, remove all `if use_jev / settings.route_intent_enabled` conditionals:
   - Always register `router_jev` node (primary) and `router_deepseek` node (fallback with `make_deepseek_router(model)` — no kwarg).
   - Always use: `START → guard + router_jev → router_join → (deepseek_fallback? → router_deepseek → policy_gate : policy_gate) → _guard_aware_target`.
   - Delete the `else` branch entirely.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_builder.py::test_policy_gate_resolves_faq_route_intent -v`
Expected: PASS

- [ ] **Step 5: Run builder tests**

Run: `pytest tests/test_builder.py -q`
Expected: POLICY dict tests (lines 73–76, 139–140) FAIL. Delete them and replace with `test_policy_gate_resolves_faq_route_intent` above plus:

```python
def test_policy_gate_resolves_refund_high_risk():
    from ai_service_agent.graph.builder import _policy_gate
    result = _policy_gate({"route_intent": "refund_apply_execute"})
    assert result["target_graph"] == "high_risk_graph"
    assert result["requires_approval"] is True
```

- [ ] **Step 6: Run builder tests again**

Run: `pytest tests/test_builder.py -q`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add src/ai_service_agent/graph/builder.py tests/test_builder.py
git commit -m "refactor(builder): remove POLICY dict and route_intent_enabled branches"
```

---

### Task 5: Remove `route_intent_enabled` from config

**Files:**
- Modify: `src/ai_service_agent/config.py:40-42` (delete `route_intent_enabled` field)
- Modify: `tests/test_config.py:90,97` (delete assertions referencing `route_intent_enabled`)

**Interfaces:**
- Consumes: nothing
- Produces: `Settings` no longer has `route_intent_enabled`

- [ ] **Step 1: Delete config field and test assertions**

In `config.py`, delete:
```python
    route_intent_enabled: bool = Field(
        default=False,
        validation_alias="JEV_ROUTE_INTENT_ENABLED",
    )
```

In `test_config.py`, delete lines 90 and 93–97 (the `route_intent_enabled` assertions and the `JEV_ROUTE_INTENT_ENABLED` monkeypatch block).

- [ ] **Step 2: Run config tests**

Run: `pytest tests/test_config.py -q`
Expected: PASS

- [ ] **Step 3: Run full test suite to catch remaining references**

Run: `pytest -q`
Expected: Any test still referencing `route_intent_enabled` will fail. Fix those tests (they should be limited to `test_config.py` and possibly `test_builder.py`).

- [ ] **Step 4: Commit**

```bash
git add src/ai_service_agent/config.py tests/test_config.py
git commit -m "refactor(config): remove route_intent_enabled flag"
```

---

### Task 6: Update eval runner to use `ROUTE_POLICY`

**Files:**
- Modify: `src/ai_service_agent/evals/runner.py` (replace `POLICY` import with route_intent-based lookup)

**Interfaces:**
- Consumes: `ROUTE_POLICY` from `route_policy.py`
- Produces: eval runner maps expected outcome to `target_graph` via route_intent

- [ ] **Step 1: Update runner**

Replace:
```python
from ai_service_agent.graph.builder import POLICY
```
with:
```python
from ai_service_agent.graph.route_policy import ROUTE_POLICY
```

Replace:
```python
target = POLICY.get((capability, risk_level), "fallback")
```
with a lookup that derives target from the eval dataset's `expected_route_intent` (if present) or falls back to `"unknown"` → `"fallback"`.

Note: The eval runner may need to be updated to match the eval dataset schema. If the dataset already has `expected_route_intent`, use it directly. If not, add a `route_intent` column derived from `capability + risk_level` during dataset loading.

- [ ] **Step 2: Run eval runner tests (if they exist)**

Run: `pytest tests/test_evals/ -q` (or similar)
Expected: PASS or no eval tests exist (skip).

- [ ] **Step 3: Commit**

```bash
git add src/ai_service_agent/evals/runner.py
git commit -m "refactor(evals): use route_intent-based policy lookup"
```

---

### Task 7: Clean up state and update graph-spec

**Files:**
- Modify: `src/ai_service_agent/graph/state.py` (mark `capability` and `risk_level` as deprecated or remove if no other code writes them)
- Modify: `docs/specs/graph-spec.md` (update §2 Router and §5 Policy Gate to single-path description)

**Interfaces:**
- Consumes: nothing
- Produces: spec and state match single-path architecture

- [ ] **Step 1: Check state field usage**

```bash
rg -n '"capability"|state\[.capability.\]|state.get\("capability' src/ --glob '!**/route_policy.py'
```

If no code writes `capability` or `risk_level` to state (other than `RouterSchema` which was already deleted in Task 1), remove them from `CustomerServiceState`.

If mutation/high_risk graphs read them, keep the fields but remove the router as their source.

- [ ] **Step 2: Update graph-spec**

In `docs/specs/graph-spec.md`:
1. §2: Remove v1/v2 split. Router always outputs `route_intent + confidence`.
2. §5: Remove `POLICY (capability × risk_level)` description. Policy Gate always uses `route_intent → resolve_route_policy`.
3. Remove references to `ROUTER_SYSTEM` and `route_intent_enabled`.
4. Update the ASCII diagram (line 29) to show single path.

- [ ] **Step 3: Run full regression**

Run: `pytest -q`
Expected: All tests pass (531+ or updated count).

- [ ] **Step 4: Commit**

```bash
git add src/ai_service_agent/graph/state.py docs/specs/graph-spec.md
git commit -m "docs+state: sync graph-spec and state for unified router"
```

---

### Task 8: Full regression + real model smoke

**Files:** None (verification only)

- [ ] **Step 1: Run full pytest**

Run: `pytest -q`
Expected: All pass.

- [ ] **Step 2: Run real model smoke (if ARK key available)**

Run: `python scripts/real_model_http_smoke.py`
Expected: Same or better results than baseline (Clarify0076 and ToolMatrix "售后+退货" may still be flaky due to known LLM drift).

- [ ] **Step 3: Final commit (if any fixes needed)**

```bash
git add -A
git commit -m "fix: address regression from router unification"
git push origin codex/kb-selected-references
```
