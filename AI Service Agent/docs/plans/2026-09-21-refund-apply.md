# refund_apply Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Replace the `refund_apply` stub with a real HighRiskGraph tool that writes to `mock_ecommerce.db`, enforces a status machine, validates amount against cumulative refunds, and requires HITL approval.

**Architecture:** Follows the proven `cancel_order` pattern: `_run_db_mutation` for idempotent DB writes with same-transaction idempotency, `_own_order` for data boundary, `InjectedState("user_id")` for identity, HighRiskGraph approval lifecycle for HITL, and `precheck_fetch` + `safety_check` for deterministic business rules. A new `refunds` table tracks partial/full refund amounts. `refunding` and `refunded` both count toward occupied refund quota to prevent over-refunding.

**Tech Stack:** Python 3.14, LangGraph 1.2.10, SQLite (via `mock_ecommerce.db`), pytest + offline fake model.

**Spec:** `docs/specs/tool-spec.md` (P8) · `docs/specs/safety-spec.md` §1.2 · `docs/specs/graph-spec.md` §6.3

## Global Constraints

- Python 3.14; no new dependencies.
- Isolated seeded DBs for tests; never pollute `data/mock_ecommerce.db`.
- Git repo root is `D:\AI_Project`; always add files explicitly, never `git add .`.
- Do not commit unrelated outer-repo directories.
- Targeted tests during development; full regression before final commit.
- `refund_apply` ToolMeta: `high_risk`, `idempotent=True`, schema=`RefundApplyArgs`.
- Tool signature: `refund_apply(order_id, amount=None, reason=None, request_id="", user_id=InjectedState)`.

## Design Decisions

1. **`refunds` table has its own `refund_id PK`** — business identity, not idempotency.
2. **Idempotency reuses existing `(tool_name, request_id)` mechanism** via `_run_db_mutation`.
3. **`refunding + refunded` both count toward occupied refund quota** — `SELECT COALESCE(SUM(amount),0) FROM refunds WHERE order_id=? AND status IN ('refunding','refunded')`.
4. **Approval stage locks final `amount`** — the approval summary shows the resolved amount; the tool executes with that same amount from `pending["args"]`. The precheck validates `amount <= total_amount - occupied_refund_amount` and the tool re-validates in-transaction.

---

### Task 1: Add `refunds` table to seed script

**Files:**
- Modify: `scripts/seed_mock_db.py`

**Interfaces:**
- Produces: `refunds` table with columns `(refund_id TEXT PK, order_id TEXT NOT NULL REFERENCES orders(order_id), customer_id TEXT NOT NULL REFERENCES customers(customer_id), amount REAL NOT NULL CHECK(amount>0), reason TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL)`; index `idx_refunds_order ON refunds(order_id)`. Seed inserts deterministic refund rows for existing `refunding`/`refunded` orders. Return dict includes `"refunds"` count.

- [x] **Step 1: Add `CREATE TABLE refunds` DDL to schema**

```sql
CREATE TABLE refunds (
    refund_id   TEXT PRIMARY KEY,
    order_id    TEXT NOT NULL REFERENCES orders(order_id),
    customer_id TEXT NOT NULL REFERENCES customers(customer_id),
    amount      REAL NOT NULL CHECK (amount > 0),
    reason      TEXT,
    status      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX idx_refunds_order ON refunds(order_id);
```

- [x] **Step 2: Generate seed rows**

After `order_items` amount backfill, iterate orders where `status IN ('refunding','refunded')` and insert one refund row each with `amount = orders.total_amount`, `status = 'refunded'` (or `'refunding'` matching order status), `reason = '质量问题'` or `'不想要了'`, `refund_id = f"RFD-{n:05d}"`.

- [x] **Step 3: Add `"refunds"` to the return tables list**

- [x] **Step 4: Run existing seed tests to verify no regression**

Run: `uv run --no-sync pytest tests/test_mock_orders_db.py::test_schema_tables_and_fk_integrity tests/test_mock_orders_db.py::test_deterministic_regeneration -q --basetemp=".pytest-tmp-refund"`
Expected: PASS

- [x] **Step 5: Commit**

```bash
git add scripts/seed_mock_db.py
git commit -m "feat: add refunds table to seed script"
```

---

### Task 2: Add `refund_apply` DB write tool

**Files:**
- Modify: `src/ai_service_agent/tools/write.py`

**Interfaces:**
- Consumes: `_run_db_mutation(tool_name, request_id, user_id, produce, validate_replay)`, `_own_order(conn, order_id, user_id)`
- Produces: `refund_apply(order_id, amount=None, reason=None, request_id="", user_id=InjectedState) -> dict` with result `{"field":"refund_apply","order_id":...,"refund_id":...,"amount":...,"status":"refunding","reason":...}` or error `{"error":"not_found"|"data_boundary"|"refund_not_allowed"|"order_already_refunded"|"refund_amount_exceeds"|"data_source_unavailable"}`.

- [x] **Step 1: Write failing tests**

```python
def test_refund_apply_full_amount_writes_and_replays(monkeypatch, seed):
    """Full refund: amount=None defaults to total_amount; order -> refunding; idempotency row."""
    _, db_path = seed
    _use_db(monkeypatch, db_path)
    conn = sqlite3.connect(db_path)
    try:
        owner, order_id, total = conn.execute(
            "SELECT customer_id, order_id, total_amount FROM orders WHERE status='paid' LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    first = write_mod.refund_apply.invoke(
        {"order_id": order_id, "request_id": "ref-1", "user_id": owner}
    )
    assert first["replayed"] is False
    assert first["result"]["status"] == "refunding"
    assert first["result"]["amount"] == total
    assert first["result"]["refund_id"]
    conn = sqlite3.connect(db_path)
    try:
        status = conn.execute("SELECT status FROM orders WHERE order_id=?", (order_id,)).fetchone()[0]
        refund = conn.execute("SELECT amount, status FROM refunds WHERE order_id=?", (order_id,)).fetchone()
        idem = conn.execute("SELECT tool_name, request_id FROM idempotency WHERE tool_name='refund_apply'").fetchall()
    finally:
        conn.close()
    assert status == "refunding"
    assert refund[0] == total
    assert refund[1] == "refunding"
    assert len(idem) == 1
    replay = write_mod.refund_apply.invoke(
        {"order_id": order_id, "request_id": "ref-1", "user_id": owner}
    )
    assert replay["replayed"] is True


def test_refund_apply_partial_amount_tracks_cumulative(monkeypatch, seed):
    """Partial refund: amount=50; second partial allowed if cumulative <= total."""
    _, db_path = seed
    _use_db(monkeypatch, db_path)
    conn = sqlite3.connect(db_path)
    try:
        owner, order_id, total = conn.execute(
            "SELECT customer_id, order_id, total_amount FROM orders WHERE status='paid' AND total_amount > 100 LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    half = round(total / 2, 2)
    first = write_mod.refund_apply.invoke(
        {"order_id": order_id, "amount": half, "request_id": "ref-p1", "user_id": owner}
    )
    assert first["replayed"] is False
    second = write_mod.refund_apply.invoke(
        {"order_id": order_id, "amount": half, "request_id": "ref-p2", "user_id": owner}
    )
    assert second["replayed"] is False
    third = write_mod.refund_apply.invoke(
        {"order_id": order_id, "amount": 0.01, "request_id": "ref-p3", "user_id": owner}
    )
    assert third == {"error": "refund_amount_exceeds", "order_id": order_id}


def test_refund_apply_blocks_wrong_status_and_cross_user(monkeypatch, seed):
    """Non-refundable status or cross-user -> error, no DB pollution."""
    _, db_path = seed
    _use_db(monkeypatch, db_path)
    conn = sqlite3.connect(db_path)
    try:
        shipped_owner, shipped_order = conn.execute(
            "SELECT customer_id, order_id FROM orders WHERE status='shipped' LIMIT 1"
        ).fetchone()
        _, other_order = conn.execute(
            "SELECT customer_id, order_id FROM orders WHERE status='paid' LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    result = write_mod.refund_apply.invoke(
        {"order_id": shipped_order, "request_id": "ref-s1", "user_id": shipped_owner}
    )
    assert result == {"error": "refund_not_allowed", "order_id": shipped_order, "status": "shipped"}
    cross = write_mod.refund_apply.invoke(
        {"order_id": other_order, "request_id": "ref-x1", "user_id": "nobody"}
    )
    assert cross == {"error": "data_boundary", "order_id": other_order}
```

- [x] **Step 2: Run tests to verify they fail**

Run: `uv run --no-sync pytest tests/test_write_db.py::test_refund_apply_full_amount_writes_and_replays tests/test_write_db.py::test_refund_apply_partial_amount_tracks_cumulative tests/test_write_db.py::test_refund_apply_blocks_wrong_status_and_cross_user -q --basetemp=".pytest-tmp-refund"`
Expected: FAIL (current stub has no `user_id` / DB logic)

- [x] **Step 3: Implement `refund_apply` in `write.py`**

Replace the stub. Add `_REFUNDABLE_STATUSES = ("paid", "completed")` and `_OCCUPIED_REFUND_STATUSES = ("refunding", "refunded")`.

```python
@tool
def refund_apply(
    order_id: str,
    amount: float | None = None,
    reason: str | None = None,
    request_id: str = "",
    user_id: Annotated[str, InjectedState("user_id")] = "",
) -> dict:
    """退款申请（high_risk；需人工审批；仅 paid/completed -> refunding；金额 ≤ 订单总额 - 已累计退款）。"""
    def _produce(conn: sqlite3.Connection) -> dict:
        boundary = _own_order(conn, order_id, user_id)
        if boundary is not None:
            return boundary
        row = conn.execute(
            "SELECT total_amount, status FROM orders WHERE order_id=?", (order_id,)
        ).fetchone()
        if row is None:
            return {"error": "not_found", "order_id": order_id}
        total, status = row
        if status == "refunded":
            return {"error": "order_already_refunded", "order_id": order_id}
        if status == "refunding":
            return {"error": "order_already_refunding", "order_id": order_id}
        if status not in _REFUNDABLE_STATUSES:
            return {"error": "refund_not_allowed", "order_id": order_id, "status": status}
        occupied = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM refunds"
            " WHERE order_id=? AND status IN ('refunding','refunded')",
            (order_id,),
        ).fetchone()[0]
        resolved_amount = round(float(amount), 2) if amount is not None else round(float(total) - float(occupied), 2)
        if resolved_amount <= 0 or resolved_amount > float(total) - float(occupied) + 1e-9:
            return {"error": "refund_amount_exceeds", "order_id": order_id}
        refund_id = f"RFD-{uuid4().hex[:12]}"
        conn.execute(
            "INSERT INTO refunds(refund_id, order_id, customer_id, amount, reason, status, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (refund_id, order_id, user_id, resolved_amount, reason, "refunding", _now()),
        )
        conn.execute(
            "UPDATE orders SET status='refunding' WHERE order_id=? AND status IN ('paid','completed')",
            (order_id,),
        )
        if conn.execute("SELECT status FROM orders WHERE order_id=?", (order_id,)).fetchone()[0] != "refunding":
            return {"error": "write_conflict", "order_id": order_id}
        return {
            "field": "refund_apply",
            "order_id": order_id,
            "refund_id": refund_id,
            "amount": resolved_amount,
            "status": "refunding",
            "reason": reason,
        }

    def _validate_replay(conn: sqlite3.Connection, result: dict) -> dict | None:
        if result.get("order_id") != order_id:
            return {"error": "data_boundary", "order_id": order_id}
        return _own_order(conn, order_id, user_id)

    return _run_db_mutation(
        "refund_apply", request_id or str(uuid4()), user_id, _produce, _validate_replay
    )
```

- [x] **Step 4: Run tests to verify they pass**

Run: `uv run --no-sync pytest tests/test_write_db.py::test_refund_apply_full_amount_writes_and_replays tests/test_write_db.py::test_refund_apply_partial_amount_tracks_cumulative tests/test_write_db.py::test_refund_apply_blocks_wrong_status_and_cross_user -q --basetemp=".pytest-tmp-refund"`
Expected: PASS

- [x] **Step 5: Commit**

```bash
git add src/ai_service_agent/tools/write.py tests/test_write_db.py
git commit -m "feat: implement refund_apply DB mutation"
```

---

### Task 3: Add refund precheck rules to safety.py

**Files:**
- Modify: `src/ai_service_agent/graph/workflows/safety.py`
- Modify: `src/ai_service_agent/tools/orders.py`

**Interfaces:**
- Consumes: `lookup_order_state_with_availability(order_id)` from orders.py
- Produces: `precheck_fetch` for `refund_apply` also returns `occupied_refund_amount` (sum of refunds with status IN ('refunding','refunded')) in `context["precheck_data"]`. `_apply_business_rules` for `refund_apply` checks: (1) status machine — only `paid`/`completed` allowed, (2) data boundary, (3) `refund_amount_exceeds` if `amount > total_amount - occupied_refund_amount`.

- [x] **Step 1: Write failing tests**

```python
def test_refund_precheck_rejects_wrong_status(monkeypatch, tmp_path):
    """refund_apply on shipped order -> refund_not_allowed (not allowed statuses)."""
    # Setup: seed DB, pick a shipped order, select refund_apply
    # Assert: context["precheck_reason"] == "refund_not_allowed", no interrupt


def test_refund_precheck_rejects_over_amount(monkeypatch, tmp_path):
    """refund_apply amount > total - occupied -> refund_amount_exceeds."""
    # Setup: seed DB, pick paid order, select refund_apply with amount=99999
    # Assert: context["precheck_reason"] == "refund_amount_exceeds"
```

- [x] **Step 2: Run tests to verify they fail**

Run: `uv run --no-sync pytest tests/test_high_risk_graph.py::test_refund_precheck_rejects_wrong_status tests/test_high_risk_graph.py::test_refund_precheck_rejects_over_amount -q --basetemp=".pytest-tmp-refund"`
Expected: FAIL (no refund status machine in precheck)

- [x] **Step 3: Add `_db_occupied_refund_total(order_id)` to `orders.py`**

```python
def _db_occupied_refund_total(order_id: str) -> float:
    """已占用退款额度（refunding + refunded）；库缺失/损坏按 0 处理。"""
    try:
        conn = _db_connect()
    except (sqlite3.Error, OSError):
        return 0.0
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM refunds"
            " WHERE order_id=? AND status IN ('refunding','refunded')",
            (order_id,),
        ).fetchone()
    except sqlite3.Error:
        return 0.0
    finally:
        conn.close()
    return float(row[0]) if row else 0.0
```

In `lookup_order_state_with_availability`, add `"occupied_refund_amount": _db_occupied_refund_total(order_id)` to the returned dict.

- [x] **Step 4: Add refund status machine + amount check to `_apply_business_rules`**

```python
if tool_name == "refund_apply":
    status = precheck_data.get("status")
    if status == "data_source_unavailable":
        return "data_source_unavailable"
    if status == "not_found":
        return "order_not_found"
    if status in ("refunded", "refunding"):
        return "order_already_refunded" if status == "refunded" else "order_already_refunding"
    if status not in ("paid", "completed"):
        return "refund_not_allowed"
    amount = (pending.get("args") or {}).get("amount")
    total_amount = precheck_data.get("total_amount")
    occupied = precheck_data.get("occupied_refund_amount", 0.0)
    if (
        isinstance(amount, (int, float)) and amount > 0
        and isinstance(total_amount, (int, float))
        and amount > float(total_amount) - float(occupied) + 1e-9
    ):
        return "refund_amount_exceeds"
```

- [x] **Step 5: Run tests to verify they pass**

- [x] **Step 6: Commit**

```bash
git add src/ai_service_agent/graph/workflows/safety.py src/ai_service_agent/tools/orders.py tests/test_high_risk_graph.py
git commit -m "feat: add refund precheck status machine and amount check"
```

---

### Task 4: Add refund approval summary + missing order_id resolve in HighRiskGraph

**Files:**
- Modify: `src/ai_service_agent/graph/workflows/high_risk_agent.py`
- Modify: `src/ai_service_agent/graph/workflows/select.py`
- Modify: `src/ai_service_agent/graph/workflows/clarify.py`
- Modify: `src/ai_service_agent/tools/orders.py`

**Interfaces:**
- Consumes: `find_own_cancellable_paid_orders(user_id)` pattern; `precheck_data` facts
- Produces: `find_own_refundable_orders(user_id) -> list[dict]` in orders.py (same shape as cancellable: `order_id, total_amount, ordered_at, product_summary` but filtering for `paid`/`completed`). `select.py` handles missing `order_id` for `refund_apply` same as `cancel_order`. `clarify.py` renders refund candidates. `high_risk_agent.py` `prepare_approval` builds refund-specific summary with amount.

- [x] **Step 1: Add `find_own_refundable_orders` to `orders.py`**

```python
def find_own_refundable_orders(user_id: str) -> list[dict] | dict:
    """查询本人可退款的 paid/completed 订单；只读，最多 10 笔。"""
    if not user_id:
        return {"error": "not_logged_in"}
    try:
        conn = _db_connect()
    except (sqlite3.Error, OSError):
        return {"error": "data_source_unavailable"}
    try:
        rows = conn.execute(
            "SELECT order_id, total_amount, ordered_at FROM orders"
            " WHERE customer_id=? AND status IN ('paid','completed')"
            " ORDER BY ordered_at DESC LIMIT 10",
            (user_id,),
        ).fetchall()
    except sqlite3.Error:
        return {"error": "data_source_unavailable"}
    finally:
        conn.close()
    return [
        {"order_id": r[0], "total_amount": r[1], "ordered_at": r[2],
         "product_summary": order_items_summary(r[0])}
        for r in rows
    ]
```

- [x] **Step 2: Add missing order_id resolve in `select.py`**

After the existing `cancel_order` block:

```python
if tool_name == "refund_apply" and not args.get("order_id"):
    candidates = find_own_refundable_orders(state.get("user_id", ""))
    if isinstance(candidates, dict):
        return _reject(state, candidates["error"])
    if len(candidates) == 1:
        args["order_id"] = candidates[0]["order_id"]
    else:
        ctx = {
            **state.get("context", {}),
            "clarify_missing": ["order_id"],
            "cancellation_candidates": candidates,
        }
        return {"context": ctx}
```

- [x] **Step 3: Update `clarify.py` candidates label**

In the `is_cancel_selection` branch, generalize the text to work for both cancel and refund:

```python
options = "、".join(
    f"{item['order_id']}（{item.get('product_summary', '商品信息不可用')}）"
    for item in candidates
)
text = f"您有多笔符合条件的订单，请选择订单号：{options}。"
```

- [x] **Step 4: Add refund summary in `high_risk_agent.py` `prepare_approval`**

Add an `elif pending["tool"] == "refund_apply":` block:

```python
elif pending["tool"] == "refund_apply":
    order_id = pending["args"].get("order_id", "")
    facts = state.get("context", {}).get("precheck_data")
    valid_refund_facts = (
        isinstance(facts, dict)
        and facts.get("order_id") == order_id
        and facts.get("user_id") == state.get("user_id", "")
        and facts.get("status") in ("paid", "completed")
    )
    if valid_refund_facts:
        amount = pending["args"].get("amount")
        occupied = facts.get("occupied_refund_amount", 0.0)
        resolved = round(float(amount), 2) if amount else round(float(facts["total_amount"]) - float(occupied), 2)
        summary = (
            f"退款申请 {facts['order_id']}｜"
            f"退款金额 {resolved}｜"
            f"订单总额 {facts['total_amount']}｜"
            f"商品：{order_items_summary(order_id)}｜"
            f"申请人 {state.get('user_id', '')}｜"
            f"状态 已付款" if facts["status"] == "paid" else "已完成"
        )
    else:
        return {
            "context": {
                **state.get("context", {}),
                "precheck_reason": "refund_apply_facts_unavailable",
            },
            "execution_status": "completed",
        }
```

- [x] **Step 5: Write tests**

```python
def test_refund_order_approval_summary_contains_amount(monkeypatch, tmp_path):
    """Approval card shows refund amount and order facts."""
    # Setup: seed DB, pick paid order owner, select refund_apply
    # Invoke graph, check interrupt value["summary"] contains "退款申请", "退款金额"

def test_refund_missing_order_id_auto_resolves_single(monkeypatch, tmp_path):
    """Single refundable order auto-selects; multiple -> clarification."""
    # Setup: seed DB
    # Assert: single -> args.order_id auto-filled; multiple -> clarification_required with candidates
```

- [x] **Step 6: Run tests**

- [x] **Step 7: Commit**

```bash
git add src/ai_service_agent/graph/workflows/high_risk_agent.py src/ai_service_agent/graph/workflows/select.py src/ai_service_agent/graph/workflows/clarify.py src/ai_service_agent/tools/orders.py tests/test_high_risk_graph.py
git commit -m "feat: add refund approval summary and missing order resolution"
```

---

### Task 5: Approval lifecycle + data boundary tests

**Files:**
- Modify: `tests/test_high_risk_graph.py`
- Modify: `tests/test_write_db.py`

**Interfaces:**
- Consumes: All previous tasks
- Produces: Comprehensive test coverage for refund approve → DB, reject → DB unchanged, expired → DB unchanged, cross-user → data_boundary + no pollution.

- [x] **Step 1: Write approval lifecycle tests**

```python
def test_refund_order_approved_writes_db_once(monkeypatch, tmp_path):
    """Approve -> order becomes refunding, refunds row exists, idempotency row exists."""

def test_refund_order_rejected_does_not_write_db(monkeypatch, tmp_path):
    """Reject -> DB unchanged, no refunds row, no idempotency row."""

def test_refund_order_expired_does_not_write_db(monkeypatch, tmp_path):
    """Expired -> DB unchanged."""

def test_refund_order_replay_binds_to_requester(monkeypatch, tmp_path):
    """Same request_id different order_id -> data_boundary."""

def test_refund_order_post_approval_drift_reports_failure(monkeypatch, tmp_path):
    """If tool fails after approval -> finalize_ok reports execution_status=failed."""
```

- [x] **Step 2: Write data boundary test**

```python
def test_refund_cross_user_blocked_without_pollution(monkeypatch, seed):
    """User A tries to refund User B's order -> data_boundary; User B's data unchanged."""
    # Setup: seed DB, pick orders from different users
    # Invoke refund_apply with wrong user_id
    # Assert: error=data_boundary; DB snapshot of User B's order unchanged
```

- [x] **Step 3: Run tests**

Run: `uv run --no-sync pytest tests/test_high_risk_graph.py tests/test_write_db.py -q --basetemp=".pytest-tmp-refund"`
Expected: PASS

- [x] **Step 4: Commit**

```bash
git add tests/test_high_risk_graph.py tests/test_write_db.py
git commit -m "test: cover refund approval lifecycle and data boundary"
```

---

### Task 6: Documentation + full regression

**Files:**
- Modify: `docs/specs/tool-spec.md`
- Modify: `docs/specs/safety-spec.md`
- Modify: `docs/specs/graph-spec.md`

**Interfaces:**
- Consumes: All previous tasks
- Produces: Updated spec docs with P8 refund_apply implementation notes.

- [x] **Step 1: Update `tool-spec.md`**

Add P8 section describing refund_apply data source replacement, status machine (`paid/completed -> refunding`), amount rules, `refunds` table schema, idempotency identity, occupied refund quota calculation.

- [x] **Step 2: Update `safety-spec.md` §1.2**

Add refund status machine row: `refund_apply 仅允许本人 paid/completed 订单进入审批；未找到、数据源不可用、越权、已退款/退款中、非可退状态、金额超限在预检拒绝`.

- [x] **Step 3: Update `graph-spec.md` §6.3**

Add refund_apply to the HighRiskGraph tool list with approval summary format.

- [x] **Step 4: Run full regression**

```bash
$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"
uv run --no-sync pytest -q --basetemp="D:\AI_Project\AI Service Agent\.pytest-tmp-refund"
```
Expected: All pass

- [x] **Step 5: Commit**

```bash
git add docs/specs/tool-spec.md docs/specs/safety-spec.md docs/specs/graph-spec.md
git commit -m "docs: define refund apply data source semantics"
```

