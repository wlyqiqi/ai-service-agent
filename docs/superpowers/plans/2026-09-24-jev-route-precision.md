# JEV Route Precision Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Improve JEV route-intent accuracy for `account_profile_update` vs `order_mutation` while preserving the current single-router architecture and adding narrow deterministic safety at Policy Gate.

**Architecture:** JEV remains the primary Router and emits `route_intent + confidence`. Router observability fields record which backend answered and whether DeepSeek fallback was used. Policy Gate remains deterministic and may correct only one narrow profile-update misclassification before resolving the static route policy.

**Tech Stack:** Python 3.14, LangGraph 1.2.10, langchain-core 1.5.3, pytest, httpx, SQLite tool data.

**Spec:** `docs/specs/graph-spec.md` and `docs/specs/state-spec.md`

## Global Constraints

- JEV remains the primary Router; do not add a second Router layer or a new intent abstraction.
- DeepSeek remains the existing fallback for JEV timeout, HTTP error, or invalid JEV output.
- JEV confidence below the execution-class threshold returns `unknown` directly; it must not invoke DeepSeek.
- Policy Gate must not call an LLM and remains the only writer of `target_graph`, `execution_class`, `allowed_tools`, and `requires_approval`.
- Router may not decide permissions, user identity, or final execution permission.
- Do not add `update_address` or `update_phone` to `order_mutation.allowed_tools` to mask routing errors.
- Preserve current Graph, SSE, ToolRegistry, and tool-call/tool-result event contracts.
- Run targeted tests per task and full regression only at the end.

---

### Task 1: Router Observability State Fields

**Files:**
- Modify: `src/ai_service_agent/graph/state.py`
- Modify: `docs/specs/state-spec.md`

**Interfaces:**
- Produces: two Router-only observability fields in `CustomerServiceState`: `routing_backend: str | None` and `routing_fallback: bool`.
- Consumes: no behavioral state; later Router tasks write these fields.

- [x] **Step 1: Add the fields to state**

In `CustomerServiceState`, immediately after `confidence`, add:

```python
    routing_backend: str | None
    routing_fallback: bool
```

- [x] **Step 2: Update state spec**

In `docs/specs/state-spec.md`, next to `confidence`, add:

```markdown
      routing_backend: str | None   # Router 观测字段：jev / deepseek，不参与路由
      routing_fallback: bool        # Router 观测字段：是否使用 DeepSeek fallback
```

Then add this bullet to the v2 Router constraints:

```markdown
  - `routing_backend` / `routing_fallback` 是 Router 观测字段；Policy Gate 与子图不得将其作为路由依据。
```

- [x] **Step 3: Commit**

```bash
git add src/ai_service_agent/graph/state.py docs/specs/state-spec.md
git commit -m "feat: add router observability state fields"
```

### Task 2: Router Backend Semantics and Direct Low-Confidence Fallback

**Files:**
- Modify: `src/ai_service_agent/graph/router.py:98-220`
- Modify: `tests/test_route_intent_router.py`

**Interfaces:**
- Consumes: `_classify_route_intent_with_jev()`, `route_intent_is_confident()`, and the DeepSeek JSON-mode agent.
- Produces: Router output dictionary now contains:

```python
{
    "route_intent": str,
    "confidence": float,
    "execution_status": "routing",
    "routing_backend": Literal["jev", "deepseek"],
    "routing_fallback": bool,
}
```

Behavior contract:

```text
JEV valid + confidence sufficient -> JEV result; backend=jev; fallback=false
JEV valid + confidence below threshold -> route_intent=unknown; backend=jev; fallback=false
JEV timeout / HTTP error / invalid shape -> DeepSeek; backend=deepseek; fallback=true
DeepSeek valid + confidence sufficient -> DeepSeek result; backend=deepseek; fallback=true
DeepSeek confidence insufficient -> unknown; backend=deepseek; fallback=true
```

- [x] **Step 1: Update the low-confidence test to fail first**

In `tests/test_route_intent_router.py`, replace `test_jev_low_confidence_falls_back_to_deepseek` with:

```python
def test_jev_low_confidence_returns_unknown_without_deepseek(jev_route_settings, monkeypatch):
    client = _JevClient(_jev_route_payload("cancel_order_execute", 0.79))
    monkeypatch.setattr("ai_service_agent.graph.router.httpx.Client", lambda timeout: client)
    model = ToolCallingFakeModel(
        responses=[route_intent_json_message("cancel_order_execute", 0.95)]
    )
    node = make_router(model)
    out = node({"messages": [], "user_id": "u1", "context": {}})
    assert out["route_intent"] == "unknown"
    assert out["confidence"] == 0.79
    assert out["routing_backend"] == "jev"
    assert out["routing_fallback"] is False
```

- [x] **Step 2: Add backend-field assertions to existing tests**

For the successful JEV test, assert:

```python
assert out["routing_backend"] == "jev"
assert out["routing_fallback"] is False
```

For the timeout / invalid-output / network-error tests, assert:

```python
assert out["routing_backend"] == "deepseek"
assert out["routing_fallback"] is True
```

For `test_deepseek_low_confidence_becomes_unknown`, assert:

```python
assert out["routing_backend"] == "deepseek"
assert out["routing_fallback"] is True
```

- [x] **Step 3: Run the focused test**

Run:

```bash
uv run --no-sync pytest tests/test_route_intent_router.py -q
```

Expected: FAIL because Router does not yet return the new fields or use direct low-confidence fallback.

- [x] **Step 4: Implement Router output and fallback behavior**

In `_classify_route_intent_with_jev`, extend the successful return value:

```python
    return {
        "route_intent": choice,
        "confidence": confidence,
        "execution_status": "routing",
        "routing_backend": "jev",
        "routing_fallback": False,
    }
```

In `deepseek_route`, return:

```python
        return {
            "route_intent": intent,
            "confidence": confidence,
            "execution_status": "routing",
            "routing_backend": "deepseek",
            "routing_fallback": True,
        }
```

In the JEV `route` function, replace the current candidate check:

```python
        if candidate is not None and route_intent_is_confident(
            candidate["route_intent"], candidate["confidence"]
        ):
            return candidate
        return deepseek_route(state)
```

with:

```python
        if candidate is not None:
            if route_intent_is_confident(
                candidate["route_intent"], candidate["confidence"]
            ):
                return candidate
            return {
                "route_intent": "unknown",
                "confidence": candidate["confidence"],
                "execution_status": "routing",
                "routing_backend": "jev",
                "routing_fallback": False,
            }
        return deepseek_route(state)
```

- [x] **Step 5: Run Router tests**

Run:

```bash
uv run --no-sync pytest tests/test_route_intent_router.py tests/test_route_policy.py -q
```

Expected: PASS.

- [x] **Step 6: Commit**

```bash
git add src/ai_service_agent/graph/router.py tests/test_route_intent_router.py
git commit -m "feat: classify router backend and stop deepseek low-confidence rerouting"
```

### Task 3: Pin JEV Choice Semantics

**Files:**
- Modify: `src/ai_service_agent/graph/router.py:26-58`
- Modify: `tests/test_route_intent_router.py`

**Interfaces:**
- Consumes: `_JEV_ROUTE_INTENT_QUESTION`.
- Produces: a single JEV `Choice(route_intent)` with explicit profile-update and unsupported-action boundaries.

- [x] **Step 1: Add a failing criteria boundary test**

Add to `tests/test_route_intent_router.py`:

```python
from ai_service_agent.graph.router import _JEV_ROUTE_INTENT_QUESTION


def test_jev_route_choice_pins_profile_update_boundary():
    criteria = _JEV_ROUTE_INTENT_QUESTION["route_intent"]["criteria"]
    assert "收货地址" in criteria["account_profile_update"]
    assert "联系电话" in criteria["account_profile_update"]
    assert "手机号" in criteria["account_profile_update"]
    assert "即使输入包含订单号" in criteria["account_profile_update"]
    assert criteria["order_mutation"] == "修改订单备注、开发票或创建售后工单"
    assert "取消预约" in criteria["unsupported_action"]
    assert "清空订单历史" in criteria["unsupported_action"]
```

- [x] **Step 2: Run the focused test**

Run:

```bash
uv run --no-sync pytest tests/test_route_intent_router.py::test_jev_route_choice_pins_profile_update_boundary -q
```

Expected: FAIL because the current criteria are less explicit.

- [x] **Step 3: Update `_JEV_ROUTE_INTENT_QUESTION`**

Replace `account_profile_update`, `order_mutation`, and `unsupported_action` values:

```python
            "account_profile_update": (
                "修改收货地址、联系电话或手机号。"
                "即使输入包含订单号，也不能因订单号进入 order_mutation。"
            ),
            "order_mutation": "修改订单备注、开发票或创建售后工单",
            "cancel_order_execute": "用户明确要求执行取消订单",
            "refund_apply_execute": "用户明确要求执行退款申请",
            "unsupported_action": (
                "当前系统没有对应工具的动作。"
                "不要把取消预约、清空订单历史、订单所有权转移或人工流程类请求映射到 mutation。"
            ),
```

Keep all other Choice options unchanged.

- [x] **Step 4: Run Router tests**

Run:

```bash
uv run --no-sync pytest tests/test_route_intent_router.py -q
```

Expected: PASS.

- [x] **Step 5: Commit**

```bash
git add src/ai_service_agent/graph/router.py tests/test_route_intent_router.py
git commit -m "fix: pin jev profile update and unsupported action boundaries"
```

### Task 4: Expand Route-Intent Evaluation Dataset

**Files:**
- Modify: `tests/evals/router_benchmark/route_intent.jsonl`

**Interfaces:**
- Consumes: the existing JSONL schema with `id`, `input`, and `expected_route_intent`.
- Produces: 36 deterministic route-intent cases consumed by Task 5.

- [x] **Step 1: Append the missing boundary and confusion cases**

Append these lines to `tests/evals/router_benchmark/route_intent.jsonl`:

```jsonl
{"id":"RI-021","input":"订单 ORD-2026-10015 的收货地址改成北京市朝阳区测试路 99 号","expected_route_intent":"account_profile_update"}
{"id":"RI-022","input":"帮我把订单 ORD-2026-10015 的收货地址改成上海市浦东新区","expected_route_intent":"account_profile_update"}
{"id":"RI-023","input":"我的收货地址改成上海市浦东新区","expected_route_intent":"account_profile_update"}
{"id":"RI-024","input":"把我的手机号换成 18600001111","expected_route_intent":"account_profile_update"}
{"id":"RI-025","input":"订单 ORD-2026-10015 的联系电话换成 13900002222","expected_route_intent":"account_profile_update"}
{"id":"RI-026","input":"给订单 ORD-2026-10015 加个备注：放驿站就行","expected_route_intent":"order_mutation"}
{"id":"RI-027","input":"给订单 ORD-2026-10015 开发票，抬头测试公司","expected_route_intent":"order_mutation"}
{"id":"RI-028","input":"订单 ORD-2026-10015 商品坏了，创建售后工单","expected_route_intent":"order_mutation"}
{"id":"RI-029","input":"订单 ORD-2026-10015 不要了，帮我取消","expected_route_intent":"cancel_order_execute"}
{"id":"RI-030","input":"订单 ORD-2026-10015 申请退款 88 元","expected_route_intent":"refund_apply_execute"}
{"id":"RI-031","input":"删除我的订单历史","expected_route_intent":"unsupported_action"}
{"id":"RI-032","input":"把订单转给我朋友","expected_route_intent":"unsupported_action"}
{"id":"RI-033","input":"取消我的会员预约","expected_route_intent":"unsupported_action"}
{"id":"RI-034","input":"把订单状态改成已发货","expected_route_intent":"unsupported_action"}
{"id":"RI-035","input":"帮我把订单里的收货地址记录删除","expected_route_intent":"unsupported_action"}
{"id":"RI-036","input":"帮我处理一下那个订单","expected_route_intent":"unknown"}
```

- [x] **Step 2: Validate the dataset loads and has no duplicate IDs**

Run:

```bash
uv run --no-sync python -c "import json; from pathlib import Path; rows=[json.loads(line) for line in Path('tests/evals/router_benchmark/route_intent.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]; assert len(rows)==36; assert len({r['id'] for r in rows})==36; print('route-intent dataset ok')"
```

Expected: `route-intent dataset ok`.

- [x] **Step 3: Commit**

```bash
git add tests/evals/router_benchmark/route_intent.jsonl
git commit -m "test: expand route intent boundary evaluation set"
```

### Task 5: Add Route-Intent Eval Runner

**Files:**
- Create: `scripts/eval_route_intent.py`

**Interfaces:**
- Consumes: `tests/evals/router_benchmark/route_intent.jsonl`, `make_router()`, `make_model()`, and Router fields `route_intent`, `confidence`, `routing_backend`, and `routing_fallback`.
- Produces: CLI `python scripts/eval_route_intent.py --out <json>` with summary metrics.

- [x] **Step 1: Create the eval runner**

Create `scripts/eval_route_intent.py` with:

```python
"""Offline or live route-intent evaluation for Router v2."""

import argparse
import json
import math
import time
from collections import Counter, defaultdict
from pathlib import Path

from ai_service_agent.config import make_model
from ai_service_agent.graph.router import make_router


MUTATION_OR_HIGH_RISK = {
    "order_mutation", "cancel_order_execute", "refund_apply_execute"
}


def load_cases(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    ids = [row["id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate route-intent eval id")
    return rows


def percentile(values: list[float], percent: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * percent / 100) - 1))
    return ordered[index]


def latency_summary(values: list[float]) -> dict:
    return {
        "n": len(values),
        "p50_ms": round(percentile(values, 50), 1),
        "p95_ms": round(percentile(values, 95), 1),
        "mean_ms": round(sum(values) / len(values), 1) if values else 0.0,
        "max_ms": round(max(values), 1) if values else 0.0,
    }


def summarize(results: list[dict]) -> dict:
    total = len(results)
    if not total:
        return {"total": 0}

    expected_by_intent: dict[str, list[dict]] = defaultdict(list)
    for result in results:
        expected_by_intent[result["expected_route_intent"]].append(result)

    def recall(intent: str) -> float:
        rows = expected_by_intent.get(intent, [])
        return round(sum(row["correct"] for row in rows) / len(rows), 4) if rows else None

    account_rows = expected_by_intent.get("account_profile_update", [])
    account_confusion = sum(
        row["predicted_route_intent"] == "order_mutation" for row in account_rows
    )
    unsupported_rows = expected_by_intent.get("unsupported_action", [])
    unsupported_leakage = sum(
        row["predicted_route_intent"] in MUTATION_OR_HIGH_RISK
        for row in unsupported_rows
    )
    backend_counts = Counter(row["routing_backend"] for row in results)
    confusion = Counter(
        (row["expected_route_intent"], row["predicted_route_intent"])
        for row in results if not row["correct"]
    )

    return {
        "total": total,
        "route_intent_accuracy": round(sum(row["correct"] for row in results) / total, 4),
        "account_profile_update_recall": recall("account_profile_update"),
        "account_profile_update_to_order_mutation_rate": round(
            account_confusion / len(account_rows), 4
        ) if account_rows else None,
        "cancel_order_execute_recall": recall("cancel_order_execute"),
        "refund_apply_execute_recall": recall("refund_apply_execute"),
        "high_risk_recall": round(
            sum(
                row["correct"] for row in results
                if row["expected_route_intent"] in {
                    "cancel_order_execute", "refund_apply_execute"
                }
            ) / len([
                row for row in results
                if row["expected_route_intent"] in {
                    "cancel_order_execute", "refund_apply_execute"
                }
            ]), 4
        ),
        "unsupported_action_mutation_or_high_risk_leakage": round(
            unsupported_leakage / len(unsupported_rows), 4
        ) if unsupported_rows else None,
        "fallback_rate": round(sum(row["routing_fallback"] for row in results) / total, 4),
        "backend_counts": dict(backend_counts),
        "confusion": {
            f"{expected} -> {predicted}": count
            for (expected, predicted), count in confusion.items()
        },
        "latency": latency_summary([row["latency_ms"] for row in results]),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("tests/evals/router_benchmark/route_intent.jsonl"),
    )
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    router = make_router(make_model())
    results = []
    for case in load_cases(args.dataset):
        started = time.perf_counter()
        output = router({
            "messages": [{"role": "human", "content": case["input"]}],
            "user_id": "u-route-eval",
            "session_id": case["id"],
            "context": {},
        })
        latency_ms = (time.perf_counter() - started) * 1000
        predicted = output["route_intent"]
        results.append({
            "id": case["id"],
            "input": case["input"],
            "expected_route_intent": case["expected_route_intent"],
            "predicted_route_intent": predicted,
            "confidence": output.get("confidence"),
            "routing_backend": output.get("routing_backend", "unknown"),
            "routing_fallback": bool(output.get("routing_fallback", False)),
            "latency_ms": round(latency_ms, 1),
            "correct": predicted == case["expected_route_intent"],
        })

    report = {"summary": summarize(results), "results": results}
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    if args.out:
        args.out.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [x] **Step 2: Run the offline baseline eval**

Run:

```bash
uv run --no-sync python scripts/eval_route_intent.py --out scripts/route_intent_eval_results.json
```

Expected: the command exits 0 and prints the summary. Before live JEV prompt changes, this is the measurable baseline.

- [x] **Step 3: Commit**

```bash
git add scripts/eval_route_intent.py
git commit -m "test: add route intent accuracy and latency evaluator"
```

### Task 6: Add Narrow Policy Gate Profile-Update Correction

**Files:**
- Modify: `src/ai_service_agent/graph/builder.py:1-120`
- Modify: `tests/test_route_intent_policy_gate.py`

**Interfaces:**
- Consumes: Router v2 output `route_intent` and the current turn's latest user message.
- Produces: deterministic normalization only from `order_mutation` to `account_profile_update`; all other intents remain unchanged.

- [x] **Step 1: Add failing Policy Gate tests**

Add to `tests/test_route_intent_policy_gate.py`:

```python
from langchain_core.messages import HumanMessage


def test_policy_gate_corrects_address_mutation(enabled):
    state = {
        "route_intent": "order_mutation",
        "messages": [HumanMessage(content="订单 ORD-2026-10015 的收货地址改成北京市朝阳区测试路 99 号")],
    }
    update = _policy_gate(state)
    assert update["route_intent"] == "account_profile_update"
    assert update["target_graph"] == "mutation_graph"
    assert update["allowed_tools"] == ["update_address", "update_phone"]


def test_policy_gate_corrects_phone_mutation(enabled):
    state = {
        "route_intent": "order_mutation",
        "messages": [HumanMessage(content="订单 ORD-2026-10015 的联系电话换成 13900002222")],
    }
    update = _policy_gate(state)
    assert update["route_intent"] == "account_profile_update"
    assert update["allowed_tools"] == ["update_address", "update_phone"]


def test_policy_gate_does_not_correct_order_note(enabled):
    state = {
        "route_intent": "order_mutation",
        "messages": [HumanMessage(content="给订单 ORD-2026-10015 加个备注：放驿站就行")],
    }
    update = _policy_gate(state)
    assert update["route_intent"] == "order_mutation"
    assert update["allowed_tools"] == ["update_order_note", "create_invoice", "create_service_ticket"]


def test_policy_gate_does_not_rescue_unsupported(enabled):
    state = {
        "route_intent": "unsupported_action",
        "messages": [HumanMessage(content="帮我把订单里的收货地址记录删除")],
    }
    update = _policy_gate(state)
    assert update["route_intent"] == "unsupported_action"
    assert update["target_graph"] == "fallback"
    assert update["allowed_tools"] == []


def test_policy_gate_uses_current_turn_only(enabled):
    state = {
        "route_intent": "order_mutation",
        "messages": [
            HumanMessage(content="我要修改收货地址"),
            HumanMessage(content="给订单 ORD-2026-10015 加个备注：放驿站就行"),
        ],
    }
    update = _policy_gate(state)
    assert update["route_intent"] == "order_mutation"
```

- [x] **Step 2: Run Policy Gate tests**

Run:

```bash
uv run --no-sync pytest tests/test_route_intent_policy_gate.py -q
```

Expected: FAIL because the current `_policy_gate` does not inspect the current user text.

- [x] **Step 3: Implement the narrow correction**

In `src/ai_service_agent/graph/builder.py`, add:

```python
import re
```

Below the existing imports, add:

```python
_PROFILE_UPDATE_PATTERN = re.compile(
    r"(收货地址|联系电话|手机号)[^。\n]{0,24}(修改|改成|改为|换成|换绑|更新)"
    r"|(修改|改成|改为|换成|换绑|更新)[^。\n]{0,24}(收货地址|联系电话|手机号)"
)


def _latest_user_text(state: dict) -> str:
    messages = state.get("messages") or []
    for message in reversed(messages):
        role = getattr(message, "type", "")
        content = getattr(message, "content", "")
        if role == "human":
            return content if isinstance(content, str) else str(content)
    return ""
```

Replace the current `_policy_gate` body with:

```python
def _policy_gate(state: dict) -> dict:
    """Router/Guard join. v2 deterministically derives the route policy; v1 is a no-op join."""
    if not get_settings().route_intent_enabled:
        return {}

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

- [x] **Step 4: Run Policy Gate and graph integration tests**

Run:

```bash
uv run --no-sync pytest tests/test_route_intent_policy_gate.py tests/test_route_intent_graph.py tests/test_route_intent_testing.py -q
```

Expected: PASS.

- [x] **Step 5: Commit**

```bash
git add src/ai_service_agent/graph/builder.py tests/test_route_intent_policy_gate.py
git commit -m "fix: correct profile update routing at policy gate"
```

### Task 7: Record Architecture Decisions

**Files:**
- Modify: `docs/specs/graph-spec.md`
- Create: `docs/decisions/2026-09-24-jev-route-precision.md`

**Interfaces:**
- Consumes: Tasks 1–6.
- Produces: authoritative Router fallback and Policy Gate normalization rules.

- [x] **Step 1: Update Router and Policy Gate spec**

In `docs/specs/graph-spec.md`, update the Router v2 rules:

```markdown
  - Jev timeout、HTTP 错误或非法输出时 fallback 到 DeepSeek。
  - Jev 返回合法 Choice 但置信度低于执行档阈值时，直接归为 `unknown`，不得再调用 DeepSeek。
  - `routing_backend` / `routing_fallback` 只用于观测和评估，不参与路由。
```

In `§5 Policy 映射（纯确定性）`, add:

```markdown
  - v2 Policy Gate 允许一个极窄归一化：LLM 输出为 `order_mutation` 且当前 turn 用户文本明确表达修改收货地址、联系电话或手机号时，Policy Gate 将 `route_intent` 修正为 `account_profile_update`。
  - 该归一化不得 rescue `unsupported_action` 或 `unknown`，不得调用 LLM，不得扩大 `allowed_tools`。
```

- [x] **Step 2: Create a decision record**

Create `docs/decisions/2026-09-24-jev-route-precision.md`:

```markdown
# Decision: JEV Route Precision

## Context

Real E2E testing found that profile-update utterances containing an order id can be confused with `order_mutation`, and a low-frequency run misclassified an address update as `unsupported_action`.

## Decision

1. Keep JEV as the primary Router.
2. Make profile-update semantics explicit in the JEV Choice criteria.
3. Keep DeepSeek only for timeout, HTTP error, or invalid JEV output.
4. Return `unknown` directly when JEV confidence is below the execution-class threshold.
5. Add one narrow deterministic Policy Gate normalization for address/phone/profile-contact update text misclassified as `order_mutation`.
6. Record `routing_backend` and `routing_fallback` as Router observability fields only.

## Non-goals

- No second Router.
- No low-confidence second LLM check.
- No expansion of `order_mutation.allowed_tools`.
- No permission or execution decision inside Router.
```

- [x] **Step 3: Commit**

```bash
git add docs/specs/graph-spec.md docs/decisions/2026-09-24-jev-route-precision.md
git commit -m "docs: define jev router precision rules"
```

### Task 8: Regression and Live Router Verification

**Files:**
- No production source changes in this task.

**Interfaces:**
- Consumes: all prior tasks.
- Produces: final verification evidence.

- [x] **Step 1: Run route-intent evaluation**

Run:

```bash
uv run --no-sync python scripts/eval_route_intent.py --out scripts/route_intent_eval_results.json
```

Expected: exit 0; `account_profile_update_recall` is `1.0`, `account_profile_update_to_order_mutation_rate` is `0.0`, `cancel_order_execute_recall` and `refund_apply_execute_recall` are both `1.0`, and `unsupported_action_mutation_or_high_risk_leakage` is `0.0`.

- [x] **Step 2: Run Router and Policy Gate focused tests**

Run:

```bash
uv run --no-sync pytest tests/test_route_intent_router.py tests/test_route_intent_policy_gate.py tests/test_route_intent_graph.py tests/test_route_policy.py -q
```

Expected: PASS.

- [x] **Step 3: Run complete regression**

Run:

```bash
uv run --no-sync pytest -q
```

Expected: all tests pass.

- [x] **Step 4: Validate graph**

Run:

```bash
langgraph validate
```

Expected: valid.

- [x] **Step 5: Run minimal live Router verification**

With `.env` configured for JEV, run the eval once more and retain the output:

```bash
uv run --no-sync python scripts/eval_route_intent.py --out scripts/route_intent_live_eval_results.json
```

Expected: exit 0; `backend_counts` shows JEV as the primary backend; fallback rate is measured but not used to gate the implementation.

- [x] **Step 6: Commit verification artifacts**

Only commit deterministic result artifacts if they are already part of the repository convention; do not commit `.env` or machine-specific temporary files.

```bash
git status --short
```

Expected: only intentional files remain.
