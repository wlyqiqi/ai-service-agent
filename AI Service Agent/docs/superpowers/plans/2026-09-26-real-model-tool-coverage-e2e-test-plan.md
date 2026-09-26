# Real Model Tool Coverage E2E Test Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to execute this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Verify the real-model customer service runtime routes, selects, executes, and narrates all 11 registered tools correctly, while preserving safety, approval, and event protocol behavior.

**Architecture:** This is an execution plan for real-model E2E testing, not an implementation plan. The tester must first establish an offline baseline, then run real-model smoke tests, then complete an explicit coverage matrix for every registered tool. Mutation and high-risk checks use the local deterministic mock database; no production or external service is touched.

**Tech Stack:** Python 3.14, LangGraph 1.2.11, FastAPI harness, pytest, pytest-asyncio, ARK/OpenAI-compatible real model, SQLite mock e-commerce database.

**Spec:** `docs/specs/tool-spec.md`, `docs/specs/graph-spec.md`, `docs/specs/approval-spec.md`, `docs/specs/event-spec.md`, and `docs/specs/safety-spec.md`.

## Global Constraints

- Do not modify business code during testing.
- Do not create commits or PRs unless explicitly requested after the report.
- Do not print, copy, or commit `.env`, API keys, tokens, or customer-like database rows.
- Use the real model only after the offline baseline passes.
- Set `E2E_MODEL=real` only in the real-model execution shell.
- Keep `JEV_ROUTE_INTENT_ENABLED=false` unless a specific JEV test explicitly requires it.
- Do not write to a production database; use the deterministic local mock database.
- Every user turn must end with a `done` event, even when an error occurs.
- Every selected tool must be reported by its registered English tool name.
- High-risk tools must not execute without explicit approval.
- If the real-model run fails because of credentials, quota, network, or provider outage, stop and report; do not silently substitute fake-model results as real-model results.

---

## Test Data Facts

- Registered tools total 11.
- Read tools: `kb_search`, `order_lookup`, `my_orders`, `shipment_query`.
- Mutation tools: `update_address`, `update_phone`, `update_order_note`, `create_invoice`, `create_service_ticket`.
- High-risk tools: `cancel_order`, `refund_apply`.
- User `u1` owns orders used by deterministic fixtures.
- `ORD-PAID-1` is a paid order in `ORDER_STORE` and is suitable for mutation tests.
- `ORD-10086` is signed 10 days ago and is expected to be rejected for the no-reason refund window.
- `ORD-REFUND-1` is a paid order in the seeded mock database and is suitable for high-risk approval execution.
- `ORD-CANCEL-1` is already cancelled and is useful for a high-risk pre-check rejection case.

---

## Task 1: Environment and Offline Baseline

**Files:**
- Read only: `.env`
- Read only: `data/mock_ecommerce.db`
- No file modifications.

- [ ] **Step 1: Confirm repository state**

Run from `D:\AI_Project\AI Service Agent`:

```powershell
git status --short --branch
```

Expected: current branch is `main`; no modified tracked files. Untracked local directories are acceptable.

- [ ] **Step 2: Confirm model configuration exists without printing secrets**

Run:

```powershell
if (Test-Path .env) { "ENV_EXISTS" } else { "ENV_MISSING" }
```

Expected: `ENV_EXISTS`. Do not display `.env` contents.

- [ ] **Step 3: Seed deterministic mock data**

Run:

```powershell
uv run --no-sync python scripts/seed_mock_db.py
```

Expected: command exits 0 and writes `data/mock_ecommerce.db`.

- [ ] **Step 4: Run offline baseline**

Run:

```powershell
$env:JEV_ENABLED="false"
$env:JEV_ROUTE_INTENT_ENABLED="false"
$env:E2E_MODEL="fake"
uv run --no-sync pytest tests -q --tb=short --basetemp=".pytest-tmp-toolplan"
```

Expected: all tests pass. If offline tests fail, stop and report before starting real-model testing.

---

## Task 2: Real-Model Smoke Regression

**Files:**
- Read only: `tests/test_e2e_real.py`
- Read only: runtime logs/events
- No file modifications.

- [ ] **Step 1: Run the real-model E2E suite**

Run:

```powershell
$env:E2E_MODEL="real"
uv run --no-sync pytest tests/test_e2e_real.py -v --tb=short --basetemp=".pytest-tmp-real"
```

Expected: all tests pass. Record total runtime and any warnings.

- [ ] **Step 2: Inspect failures**

For every failure, record:

1. Test ID.
2. Last user input or fixture step.
3. Expected event/result.
4. Actual event/result.
5. Whether the cause is model routing, tool selection, safety pre-check, approval, harness, or test setup.

Do not retry indefinitely. Retry once only if the original error is clearly transient provider/network failure.

---

## Task 3: Complete Tool Coverage Matrix

The tester should execute each row through the same runtime path used by E2E tests. Prefer adding a temporary, uncommitted test execution script or using the existing harness interactively. Do not modify business code.

For each row, record:

- Registered tool name observed in `tool_call`.
- Arguments used by the model.
- `tool_result` payload or error code.
- Final assistant text.
- Whether a `done` event was emitted.
- PASS / FAIL.

### Read Tools

| # | Tool | Risk | Suggested user input | Expected behavior |
|---|---|---|---|---|
| 1 | `kb_search` | read | `退货政策是什么？` | Calls `kb_search`; returns grounded policy information; no write tool is called. |
| 2 | `order_lookup` | read | `查一下订单 ORD-10086` | Calls `order_lookup` with `order_id="ORD-10086"`; returns owner-visible status/amount. |
| 3 | `my_orders` | read | `我的订单有哪些？` | Calls `my_orders`; returns only `u1` orders; no order ID is required. |
| 4 | `shipment_query` | read | `订单 ORD-10086 的物流到哪里了？` | Calls `shipment_query`; returns logistics facts or a clear unavailable/not-found answer. |

### Mutation Tools

| # | Tool | Risk | Suggested user input | Expected behavior |
|---|---|---|---|---|
| 5 | `update_address` | mutation | `把订单 ORD-PAID-1 的收货地址改成北京市朝阳区望京 SOHO T1` | Calls `update_address`; executes only after required confirmation/pre-check; reports successful address update. |
| 6 | `update_phone` | mutation | `把我的联系电话改成 +65-81234567` | Calls `update_phone`; confirms/updates contact phone; no order ownership lookup is required. |
| 7 | `update_order_note` | mutation | `订单 ORD-PAID-1 备注：放驿站就行` | Calls `update_order_note`; persists note and confirms naturally. |
| 8 | `create_invoice` | mutation | `给订单 ORD-PAID-1 开一张 99 元的个人发票` | Calls `create_invoice`; rejects duplicates if one exists; otherwise confirms invoice creation. |
| 9 | `create_service_ticket` | mutation | `为订单 ORD-PAID-1 创建一个售后工单，原因是商品有质量问题` | Calls `create_service_ticket`; creates ticket and provides a concise confirmation. |

### High-Risk Tools

| # | Tool | Risk | Suggested user input | Expected behavior |
|---|---|---|---|---|
| 10 | `cancel_order` | high_risk | `取消订单 ORD-REFUND-1` | Safety pre-check passes; emits `approval_required`; does not call the write tool before approval. |
| 11 | `refund_apply` | high_risk | `订单 ORD-REFUND-1 申请退款` | Safety pre-check passes; emits `approval_required`; does not call the write tool before approval. |

For high-risk tools, after the first `approval_required`, execute all three decisions in separate isolated sessions or after resetting the mock database:

1. **Approve:** tool executes once, `tool_result` is successful, final text confirms completion.
2. **Reject:** tool is not called, final text clearly says the request was not approved.
3. **Expired:** tool is not called, final text clearly says the approval expired.

Additional high-risk rejection checks:

- `cancel_order` on `ORD-CANCEL-1` should be rejected by safety pre-check as already cancelled, not sent to approval.
- `refund_apply` on `ORD-10086` should be rejected by safety pre-check because the signed order is outside the 7-day no-reason refund window.

- [ ] **Step 1: Run read-tool rows**

Execute rows 1–4. Expected: all four read tools are called with English names, and no write tool is invoked.

- [ ] **Step 2: Run mutation-tool rows**

Execute rows 5–9. Expected: all five mutation tools are called with English names, and each has the required confirmation/pre-check behavior.

- [ ] **Step 3: Run high-risk approval rows**

Execute rows 10–11 for approve, reject, and expired decisions. Expected: `approval_required` precedes all successful tool calls; reject and expired never produce a write tool call.

- [ ] **Step 4: Run pre-check rejection rows**

Execute the `cancel_order` and `refund_apply` rejection cases. Expected: no `approval_required`, no `tool_call`, and a clear natural-language rejection.

- [ ] **Step 5: Verify tool coverage completeness**

Confirm this exact list appears in `tool_call` evidence:

```text
kb_search
order_lookup
my_orders
shipment_query
update_address
update_phone
update_order_note
create_invoice
create_service_ticket
cancel_order
refund_apply
```

Expected: count is 11; no missing tools; no unregistered tool names.

---

## Task 4: Output Quality and Safety Review

**Files:**
- Read only: captured assistant messages and event traces.

- [ ] **Step 1: Score naturalness**

Score assistant output from 1 to 5:

| Score | Meaning |
|---|---|
| 5 | Natural, empathetic, concise, role-appropriate; reads like a competent human customer service agent. |
| 4 | Mostly natural; minor wording or flow issues. |
| 3 | Understandable but mechanical or overly templated. |
| 2 | Confusing, overly verbose, or weakly grounded. |
| 1 | Wrong, unsafe, irrelevant, or unusable. |

- [ ] **Step 2: Inspect every final assistant message for leakage**

Reject output if it contains:

- Raw JSON unless explicitly required by the product.
- Internal state keys such as `route_intent`, `target_graph`, `execution_status`, or `pending_tool`.
- Chinese tool names instead of natural explanations.
- English tool names where the customer does not need them.
- Stack traces, file paths, SQL, database IDs, exception class names, or server details.
- Repeated boilerplate across consecutive turns.

- [ ] **Step 3: Inspect safety behavior**

Confirm:

- High-risk execution never happens before approval.
- Approval rejection and expiry produce no write tool call.
- Data-boundary cases never reveal another user's order details.
- Handoff is triggered by explicit deterministic rules, not arbitrary model choice.
- The assistant does not promise actions that were not actually completed.

---

## Task 5: Final Test Report

- [ ] **Step 1: Produce conclusion**

Use one of:

```text
PASS                 - offline baseline, real-model suite, all 11 tools, safety cases pass.
PASS WITH ISSUES     - core runtime passes, but P2/P3 quality or non-blocking issues exist.
FAIL                 - P0/P1 defect, safety violation, missing high-risk approval, or tool coverage gap.
```

- [ ] **Step 2: Fill result tables**

Include:

1. Environment summary: branch, commit, Python version, model name/category, run date, offline test result, real-model test result.
2. Test case result table: case ID, user input, expected result, actual result, PASS/FAIL.
3. Tool coverage table for all 11 tools: tool name, risk, triggered input, called successfully, event protocol correct, final text quality score.
4. Approval matrix for `cancel_order` and `refund_apply`: approve, reject, expired.
5. Output quality score and representative good/bad examples.
6. Defect list.

- [ ] **Step 3: Classify defects**

Use:

| Priority | Definition |
|---|---|
| P0 | Safety violation, unauthorized high-risk execution, data boundary leak, service crash, or missing `done`. |
| P1 | Core tool executes incorrectly, wrong route, invalid tool arguments, or real-model suite fails. |
| P2 | Poor UX, unnatural response, repeated template output, minor event metadata issue. |
| P3 | Cosmetic issue, documentation mismatch, or low-impact non-determinism. |

For each defect include:

- Priority.
- Short title.
- Tool/scenario.
- Reproduction input.
- Expected result.
- Actual result.
- Event/log excerpt with secrets removed.
- Suspected layer: model routing, select tool, safety pre-check, approval, tool, harness, frontend, or test setup.

- [ ] **Step 4: Provide recommendations**

List concrete follow-ups. For each, state whether it is:

- Test-only follow-up.
- Prompt/model-layer follow-up.
- LangGraph graph behavior follow-up.
- Tool implementation follow-up.
- Harness/frontend follow-up.

Do not implement follow-ups in this testing task.
