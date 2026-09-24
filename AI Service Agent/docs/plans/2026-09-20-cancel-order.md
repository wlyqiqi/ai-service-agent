# Cancel Order Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the `cancel_order` stub with a real, approved-only SQLite cancellation flow in the existing HighRiskGraph.

**Architecture:** Keep the existing HighRiskGraph lifecycle and Approval Service. Add deterministic read helpers for order resolution and approval summary, make `cancel_order` perform an atomic SQLite mutation with transactional idempotency, and extend safety precheck with the cancellation status machine. No refund, stock restoration, external finance integration, or approver account model is added.

**Tech Stack:** Python 3.14, LangGraph 1.2.x, LangChain tools, SQLite (`mock_ecommerce.db`), pytest.

**Spec:** `docs/specs/tool-spec.md`, `docs/specs/safety-spec.md`, `docs/specs/graph-spec.md`, and `docs/specs/approval-spec.md`.

## Global Constraints

- Reuse HighRiskGraph and Approval Service; do not create a separate HITL system.
- Only a logged-in user can cancel their own order.
- The only allowed cancellation transition is `paid -> cancelled`.
- Do not process refunds or restore stock in this task.
- Approval must precede execution; reject and expired must never execute the tool.
- The idempotency key means "one business cancel request can successfully execute once", not "a tool was merely called".
- The idempotency row is written only when the cancellation transaction commits successfully.
- Failure, rejection, expiry, cross-user access, and disallowed status do not consume the idempotency key.
- Use isolated seeded databases for tests; do not mutate `data/mock_ecommerce.db`.
- Do not add dependencies.
- Do not modify `refund_apply` beyond existing stub behavior.

---

### Task 1: Add deterministic cancel-order read helpers

**Files:**

- Modify: `src/ai_service_agent/tools/orders.py`
- Test: `tests/test_mock_orders_db.py`

**Interfaces:**

- Consumes: `_db_connect()` and existing seeded `orders` / `order_items` / `products` tables.
- Produces:
  - `find_own_cancellable_paid_orders(user_id: str) -> list[dict] | dict`: returns candidate dictionaries or `{"error": "not_logged_in" | "data_source_unavailable"}`.
  - `order_items_summary(order_id: str) -> str`: returns a concise product display string; returns `"商品信息不可用"` when unavailable.

- [ ] **Step 1: Write failing tests**

Add these tests to `tests/test_mock_orders_db.py`:

```python
def test_find_own_cancellable_paid_orders_returns_owned_paid_orders(monkeypatch, seed):
    _, db_a, _ = seed

    class _Settings:
        mock_ecommerce_db_path = str(db_a)

    monkeypatch.setattr(orders_mod, "get_settings", lambda: _Settings())
    conn = sqlite3.connect(db_a)
    try:
        owner = conn.execute(
            "SELECT customer_id FROM orders WHERE status='paid' LIMIT 1"
        ).fetchone()[0]
    finally:
        conn.close()

    result = orders_mod.find_own_cancellable_paid_orders(owner)
    assert isinstance(result, list)
    assert result
    assert all(
        {"order_id", "total_amount", "ordered_at", "product_summary"} <= item
        for item in result
    )


def test_find_own_cancellable_paid_orders_rejects_missing_login(monkeypatch, seed):
    _, db_a, _ = seed

    class _Settings:
        mock_ecommerce_db_path = str(db_a)

    monkeypatch.setattr(orders_mod, "get_settings", lambda: _Settings())
    assert orders_mod.find_own_cancellable_paid_orders("") == {
        "error": "not_logged_in"
    }


def test_order_items_summary_handles_owned_and_missing_orders(monkeypatch, seed):
    _, db_a, _ = seed

    class _Settings:
        mock_ecommerce_db_path = str(db_a)

    monkeypatch.setattr(orders_mod, "get_settings", lambda: _Settings())
    conn = sqlite3.connect(db_a)
    try:
        order_id = conn.execute(
            "SELECT o.order_id FROM orders o JOIN order_items oi ON oi.order_id=o.order_id LIMIT 1"
        ).fetchone()[0]
    finally:
        conn.close()

    assert orders_mod.order_items_summary(order_id)
    assert orders_mod.order_items_summary("ORD-NOPE") == "商品信息不可用"
```

- [ ] **Step 2: Run the tests and verify failure**

Run: `uv run --no-sync pytest tests/test_mock_orders_db.py::test_find_own_cancellable_paid_orders_returns_owned_paid_orders tests/test_mock_orders_db.py::test_find_own_cancellable_paid_orders_rejects_missing_login tests/test_mock_orders_db.py::test_order_items_summary_handles_owned_and_missing_orders -q`

Expected: FAIL because the two helper functions do not exist.

- [ ] **Step 3: Implement the helpers**

Add to `src/ai_service_agent/tools/orders.py`:

```python
def find_own_cancellable_paid_orders(user_id: str) -> list[dict] | dict:
    """查询本人可取消的已付款订单；只读，不做裁决，最多返回最近 10 笔。"""
    if not user_id:
        return {"error": "not_logged_in"}
    try:
        conn = _db_connect()
    except (sqlite3.Error, OSError):
        return {"error": "data_source_unavailable"}
    try:
        rows = conn.execute(
            """
            SELECT o.order_id, o.total_amount, o.ordered_at,
                   COALESCE(GROUP_CONCAT(p.product_name || ' x' || oi.quantity), '')
            FROM orders o
            LEFT JOIN order_items oi ON oi.order_id = o.order_id
            LEFT JOIN products p ON p.product_id = oi.product_id
            WHERE o.customer_id = ? AND o.status = 'paid'
            GROUP BY o.order_id, o.total_amount, o.ordered_at
            ORDER BY o.ordered_at DESC
            LIMIT 10
            """,
            (user_id,),
        ).fetchall()
    except sqlite3.Error:
        return {"error": "data_source_unavailable"}
    finally:
        conn.close()
    return [
        {
            "order_id": row[0],
            "total_amount": float(row[1]),
            "ordered_at": row[2],
            "product_summary": row[3] or "商品信息不可用",
        }
        for row in rows
    ]


def order_items_summary(order_id: str) -> str:
    """审批卡片用商品摘要；库缺失或订单不存在时确定性降级。"""
    try:
        conn = _db_connect()
    except (sqlite3.Error, OSError):
        return "商品信息不可用"
    try:
        rows = conn.execute(
            """
            SELECT p.product_name, oi.quantity
            FROM order_items oi
            JOIN products p ON p.product_id = oi.product_id
            WHERE oi.order_id = ?
            ORDER BY oi.id
            """,
            (order_id,),
        ).fetchall()
    except sqlite3.Error:
        return "商品信息不可用"
    finally:
        conn.close()
    if not rows:
        return "商品信息不可用"
    return "、".join(f"{name} ×{quantity}" for name, quantity in rows)
```

- [ ] **Step 4: Run the tests and verify pass**

Run: `uv run --no-sync pytest tests/test_mock_orders_db.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -- src/ai_service_agent/tools/orders.py tests/test_mock_orders_db.py
git commit -m "feat: add cancel order read helpers"
```

---

### Task 2: Implement real `cancel_order` mutation

**Files:**

- Modify: `src/ai_service_agent/tools/write.py:53`
- Modify: `src/ai_service_agent/tools/write.py:240`
- Test: `tests/test_write_db.py`

**Interfaces:**

- Consumes: `_run_db_mutation()`, `_own_order()`, and `_now()`.
- Produces: `cancel_order(order_id, reason=None, request_id="", user_id=InjectedState("user_id")) -> dict`.
- Success result shape:

```python
{
    "ok": True,
    "replayed": False,
    "result": {
        "field": "cancel_order",
        "order_id": "...",
        "status": "cancelled",
        "cancelled_at": "...",
        "reason": None,
    },
}
```

- [ ] **Step 1: Write failing tool tests**

Add to `tests/test_write_db.py`:

```python
def test_cancel_order_success_replay_and_business_rejection(monkeypatch, seed):
    _, db_path = seed
    _use_db(monkeypatch, db_path)
    conn = sqlite3.connect(db_path)
    try:
        owner, paid_order = conn.execute(
            "SELECT customer_id, order_id FROM orders WHERE status='paid' LIMIT 1"
        ).fetchone()
        cancelled_order = conn.execute(
            "SELECT order_id FROM orders WHERE status='cancelled' LIMIT 1"
        ).fetchone()[0]
        shipped_order = conn.execute(
            "SELECT order_id FROM orders WHERE status NOT IN ('paid','cancelled') LIMIT 1"
        ).fetchone()[0]
    finally:
        conn.close()

    before = _snapshot(db_path)
    first = write_mod.cancel_order.invoke(
        {
            "order_id": paid_order,
            "reason": "不想要了",
            "request_id": "cancel-1",
            "user_id": owner,
        }
    )
    assert first["replayed"] is False
    assert first["result"]["status"] == "cancelled"
    assert first["result"]["cancelled_at"]

    replay = write_mod.cancel_order.invoke(
        {
            "order_id": paid_order,
            "reason": "换一个理由",
            "request_id": "cancel-1",
            "user_id": owner,
        }
    )
    assert replay["replayed"] is True
    assert replay["result"] == first["result"]

    after_cancel = _snapshot(db_path)
    assert after_cancel["orders"] != before["orders"]
    assert after_cancel["idempotency"] != before["idempotency"]

    already = write_mod.cancel_order.invoke(
        {
            "order_id": cancelled_order,
            "request_id": "cancel-already",
            "user_id": owner,
        }
    )
    assert already == {"error": "order_already_cancelled", "order_id": cancelled_order}

    not_allowed = write_mod.cancel_order.invoke(
        {
            "order_id": shipped_order,
            "request_id": "cancel-not-allowed",
            "user_id": owner,
        }
    )
    assert not_allowed["error"] == "cancel_not_allowed"
    assert not_allowed["order_id"] == shipped_order

    after_rejections = _snapshot(db_path)
    assert after_rejections["orders"] == after_cancel["orders"]
    assert after_rejections["idempotency"] == after_cancel["idempotency"]


def test_cancel_order_blocks_cross_user_without_pollution(monkeypatch, seed):
    _, db_path = seed
    _use_db(monkeypatch, db_path)
    conn = sqlite3.connect(db_path)
    try:
        victim_owner, order_id = conn.execute(
            "SELECT customer_id, order_id FROM orders WHERE status='paid' LIMIT 1"
        ).fetchone()
        attacker = "u1" if victim_owner != "u1" else "u2"
        before = conn.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
    finally:
        conn.close()

    denied = write_mod.cancel_order.invoke(
        {
            "order_id": order_id,
            "reason": "越权取消",
            "request_id": "cancel-cross-user",
            "user_id": attacker,
        }
    )
    assert denied == {"error": "data_boundary", "order_id": order_id}

    conn = sqlite3.connect(db_path)
    try:
        after = conn.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
        key_count = conn.execute(
            "SELECT COUNT(*) FROM idempotency WHERE tool_name='cancel_order' AND request_id='cancel-cross-user'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert after == before
    assert key_count == 0
```

- [ ] **Step 2: Run the tests and verify failure**

Run: `uv run --no-sync pytest tests/test_write_db.py::test_cancel_order_success_replay_and_business_rejection tests/test_write_db.py::test_cancel_order_blocks_cross_user_without_pollution -q`

Expected: FAIL because `cancel_order` is still an in-memory stub.

- [ ] **Step 3: Implement the DB-backed tool**

Replace `cancel_order` in `src/ai_service_agent/tools/write.py` with:

```python
@tool
def cancel_order(
    order_id: str,
    reason: str | None = None,
    request_id: str = "",
    user_id: Annotated[str, InjectedState("user_id")] = "",
) -> dict:
    """取消本人订单（high_risk，需人工审批；仅 paid -> cancelled）。"""
    def _produce(conn: sqlite3.Connection) -> dict:
        boundary = _own_order(conn, order_id, user_id)
        if boundary is not None:
            return boundary
        row = conn.execute(
            "SELECT status FROM orders WHERE order_id=?", (order_id,)
        ).fetchone()
        status = row[0]
        if status == "cancelled":
            return {"error": "order_already_cancelled", "order_id": order_id}
        if status != "paid":
            return {"error": "cancel_not_allowed", "order_id": order_id, "status": status}
        cancelled_at = _now()
        conn.execute(
            "UPDATE orders SET status='cancelled', cancelled_at=? WHERE order_id=?",
            (cancelled_at, order_id),
        )
        return {
            "field": "cancel_order",
            "order_id": order_id,
            "status": "cancelled",
            "cancelled_at": cancelled_at,
            "reason": reason,
        }

    return _run_db_mutation(
        "cancel_order", request_id or str(uuid4()), user_id, _produce
    )
```

- [ ] **Step 4: Run the tests and verify pass**

Run: `uv run --no-sync pytest tests/test_write_db.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -- src/ai_service_agent/tools/write.py tests/test_write_db.py
git commit -m "feat: implement approved cancel order mutation"
```

---

### Task 3: Enforce cancellation rules before approval

**Files:**

- Modify: `src/ai_service_agent/graph/workflows/safety.py:26`
- Modify: `src/ai_service_agent/graph/workflows/safety.py:59`
- Test: `tests/test_high_risk_graph.py`

**Interfaces:**

- Consumes: `lookup_order_state(order_id)`.
- Produces: deterministic precheck reasons:
  - `order_not_found`
  - `data_boundary`
  - `order_already_cancelled`
  - `cancel_not_allowed`

- [ ] **Step 1: Write failing precheck tests**

Add to `tests/test_high_risk_graph.py`:

```python
def test_cancel_precheck_status_machine():
    """cancel_order 预检：仅 paid 可进入审批；未找到、已取消、其他状态均拒绝。"""
    tool = {"tool": "cancel_order", "args": {"order_id": "ORD-X", "request_id": "r"}}
    assert _apply_business_rules(tool, {"order_id": "ORD-X", "status": "not_found"}, "u1") == "order_not_found"
    assert _apply_business_rules(tool, {"order_id": "ORD-X", "status": "paid"}, "u1") is None
    assert _apply_business_rules(tool, {"order_id": "ORD-X", "status": "cancelled"}, "u1") == "order_already_cancelled"
    assert _apply_business_rules(tool, {"order_id": "ORD-X", "status": "shipped"}, "u1") == "cancel_not_allowed"
    assert _apply_business_rules(
        tool, {"order_id": "ORD-X", "user_id": "u2", "status": "paid"}, "u1"
    ) == "data_boundary"
```

Update imports at the top of the test file:

```python
from ai_service_agent.graph.workflows.safety import _apply_business_rules
```

- [ ] **Step 2: Run the test and verify failure**

Run: `uv run --no-sync pytest tests/test_high_risk_graph.py::test_cancel_precheck_status_machine -q`

Expected: FAIL because non-`paid` statuses other than `cancelled` still pass and missing orders are not marked as facts.

- [ ] **Step 3: Implement the deterministic status machine**

In `_apply_business_rules`, replace the existing `cancel_order` block:

```python
    if tool_name == "cancel_order" and precheck_data.get("status") == "cancelled":
        return "order_already_cancelled"
```

with:

```python
    if tool_name == "cancel_order":
        status = precheck_data.get("status")
        if status == "not_found":
            return "order_not_found"
        if status == "cancelled":
            return "order_already_cancelled"
        if status != "paid":
            return "cancel_not_allowed"
```

In `make_precheck_fetch_node`, replace:

```python
        data = lookup_order_state(order_id)
        if data is None:
            return {}
```

with:

```python
        data = lookup_order_state(order_id)
        if data is None:
            if pending.get("tool") == "cancel_order":
                return {
                    "context": {
                        **ctx,
                        "precheck_data": {"order_id": order_id, "status": "not_found"},
                    }
                }
            return {}
```

- [ ] **Step 4: Run high-risk precheck tests**

Run: `uv run --no-sync pytest tests/test_high_risk_graph.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -- src/ai_service_agent/graph/workflows/safety.py tests/test_high_risk_graph.py
git commit -m "feat: enforce cancel order status machine"
```

---

### Task 4: Resolve missing order ID deterministically

**Files:**

- Modify: `src/ai_service_agent/graph/workflows/select.py:91`
- Modify: `src/ai_service_agent/graph/workflows/clarify.py:15`
- Test: `tests/test_mutation_graph.py`

**Interfaces:**

- Consumes: `find_own_cancellable_paid_orders(user_id)`.
- Produces:
  - One owned paid order: `pending_tool.args.order_id` is filled automatically.
  - Multiple owned paid orders: `clarify_missing=["order_id"]` and `context.cancellation_candidates` is a list.
  - Zero owned paid orders: `context.cancellation_candidates=[]`, followed by the deterministic no-cancellable-order message.

- [ ] **Step 1: Write failing selection tests**

Add to `tests/test_mutation_graph.py`:

```python
def _cancel_model(payload: dict):
    return ToolCallingFakeModel(responses=[
        router_json_message("order", "high_risk", 0.9),
        select_json_message("cancel_order", payload),
    ])


def test_cancel_order_without_id_uses_single_paid_order(monkeypatch, seed):
    _, db_path = seed
    _use_db(monkeypatch, db_path)
    conn = sqlite3.connect(db_path)
    try:
        owner, order_id = conn.execute(
            "SELECT customer_id, order_id FROM orders WHERE status='paid' LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    graph = build_graph(_cancel_model({}), tools=[], checkpointer=MemorySaver())
    thread_id = "cancel-single"
    out = graph.invoke(
        {
            "messages": [HumanMessage(content="取消我的订单")],
            "user_id": owner,
            "session_id": thread_id,
            "context": {},
        },
        {"configurable": {"thread_id": thread_id}},
    )
    assert out["context"]["pending_tool"]["args"]["order_id"] == order_id
    assert out.get("__interrupt__")


def test_cancel_order_without_id_handles_multiple_and_empty(monkeypatch, seed):
    _, db_path = seed
    _use_db(monkeypatch, db_path)
    graph = build_graph(_cancel_model({}), tools=[], checkpointer=MemorySaver())

    conn = sqlite3.connect(db_path)
    try:
        owner = conn.execute(
            """
            SELECT customer_id FROM orders
            WHERE status='paid'
            GROUP BY customer_id
            HAVING COUNT(*) >= 2
            LIMIT 1
            """
        ).fetchone()[0]
    finally:
        conn.close()

    thread_id = "cancel-multiple"
    out = graph.invoke(
        {
            "messages": [HumanMessage(content="取消我的订单")],
            "user_id": owner,
            "session_id": thread_id,
            "context": {},
        },
        {"configurable": {"thread_id": thread_id}},
    )
    assert out["context"]["cancellation_candidates"]
    assert out["context"]["clarify_missing"] == ["order_id"]

    thread_id = "cancel-empty"
    out = graph.invoke(
        {
            "messages": [HumanMessage(content="取消我的订单")],
            "user_id": "user-with-no-paid-order",
            "session_id": thread_id,
            "context": {},
        },
        {"configurable": {"thread_id": thread_id}},
    )
    assert out["context"]["cancellation_candidates"] == []
    assert "没有可取消" in out["messages"][-1].content
```

If `_use_db` is not present in this test module, add the same helper used by `tests/test_write_db.py`.

- [ ] **Step 2: Run the tests and verify failure**

Run: `uv run --no-sync pytest tests/test_mutation_graph.py::test_cancel_order_without_id_uses_single_paid_order tests/test_mutation_graph.py::test_cancel_order_without_id_handles_multiple_and_empty -q`

Expected: FAIL because missing `order_id` currently goes through generic clarification and never resolves one paid order.

- [ ] **Step 3: Implement deterministic resolution**

In `src/ai_service_agent/graph/workflows/select.py`, import:

```python
from ai_service_agent.tools.orders import find_own_cancellable_paid_orders
```

Inside `select()`, immediately after the payload is parsed and before schema validation, add:

```python
        if tool_name == "cancel_order" and not args.get("order_id"):
            candidates = find_own_cancellable_paid_orders(state.get("user_id", ""))
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

In `src/ai_service_agent/graph/workflows/clarify.py`, update the missing-field branch with two clear variables. When selection later succeeds, also remove `cancellation_candidates` from context so stale candidates do not leak into the next turn.

```python
        else:
            missing = ctx.get("clarify_missing") or []
            candidates = ctx.get("cancellation_candidates")
            is_cancel_selection = candidates is not None and missing == ["order_id"]
            data = {"missing": missing, "candidates": candidates or []}
            if is_cancel_selection and candidates:
                options = "、".join(
                    f"{item['order_id']}（{item.get('product_summary', '商品信息不可用')}）"
                    for item in candidates
                )
                text = f"您有多笔可取消订单，请选择订单号：{options}。"
            elif is_cancel_selection:
                text = "当前没有可取消的已付款订单。"
            else:
                text = clarify_text(missing)
```

- [ ] **Step 4: Run selection and clarification tests**

Run: `uv run --no-sync pytest tests/test_mutation_graph.py tests/test_clarify.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -- src/ai_service_agent/graph/workflows/select.py src/ai_service_agent/graph/workflows/clarify.py tests/test_mutation_graph.py
git commit -m "feat: resolve cancel order target deterministically"
```

---

### Task 5: Improve approval summary for cancel order

**Files:**

- Modify: `src/ai_service_agent/graph/workflows/high_risk_agent.py:33`
- Test: `tests/test_high_risk_graph.py`

**Interfaces:**

- Consumes: `lookup_order_state()` and `order_items_summary()`.
- Produces: `approval_required.summary` for `cancel_order` in this exact display order: order ID, amount, product summary, requester, current status.

- [ ] **Step 1: Write a failing summary test**

Add to `tests/test_high_risk_graph.py`:

```python
def test_cancel_order_approval_summary_contains_order_facts(monkeypatch, tmp_path):
    import importlib.util
    import sqlite3
    from pathlib import Path

    from ai_service_agent.tools import orders as orders_mod
    from ai_service_agent.tools import write as write_mod

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "seed_mock_db_cancel_summary", root / "scripts" / "seed_mock_db.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    db_path = tmp_path / "cancel-summary.db"
    module.build_database(str(db_path))

    class _Settings:
        mock_ecommerce_db_path = str(db_path)

    monkeypatch.setattr(orders_mod, "get_settings", lambda: _Settings())
    monkeypatch.setattr(write_mod, "get_settings", lambda: _Settings())
    conn = sqlite3.connect(db_path)
    try:
        owner, order_id = conn.execute(
            "SELECT customer_id, order_id FROM orders WHERE status='paid' LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    model = ToolCallingFakeModel(responses=[
        router_json_message("order", "high_risk", 0.9),
        select_json_message("cancel_order", {"order_id": order_id}),
    ])
    graph = build_graph(model, tools=[], checkpointer=MemorySaver())
    thread_id = "cancel-summary"
    out = graph.invoke(
        {
            "messages": [HumanMessage(content="取消这个订单")],
            "user_id": owner,
            "session_id": thread_id,
            "context": {},
        },
        {"configurable": {"thread_id": thread_id}},
    )
    summary = next(
        it.value["summary"] for it in out.get("__interrupt__", [])
        if it.value.get("type") == "approval_required"
    )
    assert order_id in summary
    assert owner in summary
    assert "已付款" in summary
    assert "商品" in summary
```

- [ ] **Step 2: Run the test and verify failure**

Run: `uv run --no-sync pytest tests/test_high_risk_graph.py::test_cancel_order_approval_summary_contains_order_facts -q`

Expected: FAIL because the current summary only prints raw tool arguments.

- [ ] **Step 3: Build a business-readable summary**

In `src/ai_service_agent/graph/workflows/high_risk_agent.py`, import:

```python
from ai_service_agent.tools.orders import lookup_order_state, order_items_summary
```

Replace the first three lines of `prepare_approval` with:

```python
        pending = state["context"]["pending_tool"]
        if pending["tool"] == "cancel_order":
            order_id = pending["args"].get("order_id", "")
            facts = lookup_order_state(order_id)
            if facts is None:
                summary = f"即将执行「cancel_order」，参数：{pending['args']}"
            else:
                summary = (
                    f"取消订单 {facts['order_id']}｜"
                    f"金额 {facts['total_amount']}｜"
                    f"商品：{order_items_summary(order_id)}｜"
                    f"申请人 {state.get('user_id', '')}｜"
                    f"状态 已付款"
                )
        else:
            summary = f"即将执行「{pending['tool']}」，参数：{pending['args']}"
```

- [ ] **Step 4: Run the test and verify pass**

Run: `uv run --no-sync pytest tests/test_high_risk_graph.py::test_cancel_order_approval_summary_contains_order_facts -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -- src/ai_service_agent/graph/workflows/high_risk_agent.py tests/test_high_risk_graph.py
git commit -m "feat: show cancel order approval facts"
```

---

### Task 6: Add graph approval and no-pollution tests

**Files:**

- Modify: `tests/test_high_risk_graph.py`
- Test: `tests/test_high_risk_graph.py`

**Interfaces:**

- Consumes: completed `cancel_order`, safety status machine, and HighRiskGraph approval interrupt.
- Produces: regression proof for approve / reject / expired and database non-pollution.

- [ ] **Step 1: Add shared seeded-DB fixtures**

Add to `tests/test_high_risk_graph.py`:

```python
def _use_cancel_db(monkeypatch, db_path):
    from ai_service_agent.tools import orders as orders_mod
    from ai_service_agent.tools import write as write_mod

    class _Settings:
        mock_ecommerce_db_path = str(db_path)

    monkeypatch.setattr(orders_mod, "get_settings", lambda: _Settings())
    monkeypatch.setattr(write_mod, "get_settings", lambda: _Settings())


def _load_cancel_seed(tmp_path):
    import importlib.util
    import sqlite3
    from pathlib import Path

    from ai_service_agent.tools import orders as orders_mod
    from ai_service_agent.tools import write as write_mod

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "seed_mock_db_cancel_graph", root / "scripts" / "seed_mock_db.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    db_path = tmp_path / "cancel-graph.db"
    module.build_database(str(db_path))
    conn = sqlite3.connect(db_path)
    try:
        owner, order_id = conn.execute(
            "SELECT customer_id, order_id FROM orders WHERE status='paid' LIMIT 1"
        ).fetchone()
        before = conn.execute("SELECT * FROM orders ORDER BY order_id").fetchall()
        idempotency_before = conn.execute(
            "SELECT * FROM idempotency ORDER BY tool_name, request_id"
        ).fetchall()
    finally:
        conn.close()
    return module, db_path, owner, order_id, before, idempotency_before


def _cancel_graph(monkeypatch, tmp_path, order_id):
    model = ToolCallingFakeModel(responses=[
        router_json_message("order", "high_risk", 0.9),
        select_json_message("cancel_order", {"order_id": order_id}),
    ])
    graph = build_graph(model, tools=[], checkpointer=MemorySaver())
    return graph
```

- [ ] **Step 2: Write approve / reject / expired tests**

Add to `tests/test_high_risk_graph.py`:

```python
def test_cancel_order_approved_writes_db_once(monkeypatch, tmp_path):
    _, db_path, owner, order_id, before, idempotency_before = _load_cancel_seed(tmp_path)
    _use_cancel_db(monkeypatch, db_path)
    graph = _cancel_graph(monkeypatch, tmp_path, order_id)
    thread_id = "cancel-approved"

    graph.invoke(
        {
            "messages": [HumanMessage(content="取消这个订单")],
            "user_id": owner,
            "session_id": thread_id,
            "context": {},
        },
        {"configurable": {"thread_id": thread_id}},
    )
    out = graph.invoke(Command(resume={"action": "approve"}), {
        "configurable": {"thread_id": thread_id}
    })

    assert out["execution_status"] == "completed"
    tool_msgs = [m for m in out["messages"] if getattr(m, "type", "") == "tool"]
    assert tool_msgs and getattr(tool_msgs[-1], "name", "") == "cancel_order"
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT status, cancelled_at FROM orders WHERE order_id=?", (order_id,)
        ).fetchone()
        idempotency_count = conn.execute(
            "SELECT COUNT(*) FROM idempotency WHERE tool_name='cancel_order'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert row[0] == "cancelled"
    assert row[1]
    assert idempotency_count == 1
    assert all(entry[0] != "cancel_order" for entry in idempotency_before)


def test_cancel_order_rejected_does_not_write_db(monkeypatch, tmp_path):
    _, db_path, owner, order_id, before, idempotency_before = _load_cancel_seed(tmp_path)
    _use_cancel_db(monkeypatch, db_path)
    graph = _cancel_graph(monkeypatch, tmp_path, order_id)
    thread_id = "cancel-rejected"

    graph.invoke(
        {
            "messages": [HumanMessage(content="取消这个订单")],
            "user_id": owner,
            "session_id": thread_id,
            "context": {},
        },
        {"configurable": {"thread_id": thread_id}},
    )
    out = graph.invoke(Command(resume={"action": "reject"}), {
        "configurable": {"thread_id": thread_id}
    })

    assert out["execution_status"] == "completed"
    assert [m for m in out["messages"] if getattr(m, "type", "") == "tool"] == []
    conn = sqlite3.connect(db_path)
    try:
        after = conn.execute("SELECT * FROM orders ORDER BY order_id").fetchall()
        after_idempotency = conn.execute(
            "SELECT * FROM idempotency ORDER BY tool_name, request_id"
        ).fetchall()
    finally:
        conn.close()
    assert after == before
    assert after_idempotency == idempotency_before


def test_cancel_order_expired_does_not_write_db(monkeypatch, tmp_path):
    _, db_path, owner, order_id, before, idempotency_before = _load_cancel_seed(tmp_path)
    _use_cancel_db(monkeypatch, db_path)
    graph = _cancel_graph(monkeypatch, tmp_path, order_id)
    thread_id = "cancel-expired"

    graph.invoke(
        {
            "messages": [HumanMessage(content="取消这个订单")],
            "user_id": owner,
            "session_id": thread_id,
            "context": {},
        },
        {"configurable": {"thread_id": thread_id}},
    )
    out = graph.invoke(Command(resume={"result": "expired"}), {
        "configurable": {"thread_id": thread_id}
    })

    assert out["execution_status"] == "completed"
    assert [m for m in out["messages"] if getattr(m, "type", "") == "tool"] == []
    conn = sqlite3.connect(db_path)
    try:
        after = conn.execute("SELECT * FROM orders ORDER BY order_id").fetchall()
        after_idempotency = conn.execute(
            "SELECT * FROM idempotency ORDER BY tool_name, request_id"
        ).fetchall()
    finally:
        conn.close()
    assert after == before
    assert after_idempotency == idempotency_before
```

At the top of the test file, ensure these imports exist:

```python
import sqlite3
```

- [ ] **Step 3: Run the graph tests and verify pass**

Run: `uv run --no-sync pytest tests/test_high_risk_graph.py -q`

Expected: PASS. The behavior should already be satisfied by Tasks 2–5; if any test fails, fix only the cancel-order path in this task.

- [ ] **Step 4: Commit**

```bash
git add -- tests/test_high_risk_graph.py
git commit -m "test: cover cancel order approval lifecycle"
```

---

### Task 7: Update specifications

**Files:**

- Modify: `docs/specs/tool-spec.md:52`
- Modify: `docs/specs/safety-spec.md:21`
- Modify: `docs/specs/graph-spec.md:97`

**Interfaces:**

- Produces: authoritative documentation for the implemented behavior; no API/schema changes.

- [ ] **Step 1: Update tool specification**

Change the `cancel_order` row in `docs/specs/tool-spec.md` to:

```markdown
| `cancel_order` | 取消订单 | high_risk | 幂等；需人工审批；内部实现写模拟电商库 `orders.status/cancelled_at`；仅限本人 `paid` 订单；`paid -> cancelled`；不触发退款、不回补库存；同一 `request_id` 仅在取消事务成功提交后生成幂等记录 |
```

Append this implementation note after the P6 paragraph:

```markdown
- **P7 实现（2026-09-20）**：`cancel_order` 从内存 stub 替换为模拟电商库事务写入，沿用 `_run_db_mutation` 模式。幂等语义是**同一业务取消请求只能成功执行一次**：成功提交后同一 `request_id` 返回 `replayed=true`；失败、拒绝、过期、越权和状态不允许都不写幂等表。订单取消成功后，更换新 `request_id` 的重复请求仍会被状态机拒绝为 `order_already_cancelled`。
```

- [ ] **Step 2: Update safety specification**

In the business-rules table in `docs/specs/safety-spec.md`, add:

```markdown
| 取消状态机（2026-09-20） | `cancel_order` 仅允许本人 `paid` 订单进入审批；未找到、已取消、非 `paid` 状态在预检拒绝；工具执行事务内再次校验 | `cancelled -> order_already_cancelled`；`shipped -> cancel_not_allowed` |
```

- [ ] **Step 3: Update graph specification**

In `docs/specs/graph-spec.md` §6.3, append:

```markdown
- `cancel_order` 缺少 `order_id` 时由确定性代码解析：本人仅 1 笔 `paid` 订单自动选中；多笔进入 `clarification_required` 并列出候选；没有可取消订单时明确收尾，不进入审批。
```

- [ ] **Step 4: Validate docs references**

Run: `rg -n "cancel_order|order_already_cancelled|cancel_not_allowed|refund|库存" docs/specs/tool-spec.md docs/specs/safety-spec.md docs/specs/graph-spec.md`

Expected: the three documents contain the new semantics and do not describe `cancel_order` as a stub.

- [ ] **Step 5: Commit**

```bash
git add -- docs/specs/tool-spec.md docs/specs/safety-spec.md docs/specs/graph-spec.md
git commit -m "docs: define cancel order data source semantics"
```

---

### Task 8: Regression and completion gate

**Files:**

- No source changes expected.

**Interfaces:**

- Consumes: all prior tasks.
- Produces: final verification result.

- [ ] **Step 1: Run targeted test set**

Run: `uv run --no-sync pytest tests/test_mock_orders_db.py tests/test_write_db.py tests/test_high_risk_graph.py tests/test_mutation_graph.py tests/test_clarify.py -q`

Expected: PASS.

- [ ] **Step 2: Run full regression**

Run: `uv run --no-sync pytest -q`

Expected: all tests pass with no new failures.

- [ ] **Step 3: Check diff and repository hygiene**

Run: `git diff --check && git status --short`

Expected: no whitespace errors; only intended project files are modified or staged.

- [ ] **Step 4: Optional real-machine E2E gate**

Before manual E2E, create an isolated seeded database and point the runtime to it. Verify from the page:

1. Login as the owner.
2. Ask to cancel an order.
3. If the user owns multiple paid orders, verify deterministic candidate clarification.
4. Verify approval card shows order ID, amount, product summary, requester, status, and expiry.
5. Approve and verify `orders.status='cancelled'`, `cancelled_at` is set, and one `idempotency` row exists.
6. Repeat the same business request and verify `order_already_cancelled` / replay behavior without a second mutation.
7. Run a second isolated E2E with reject and expired; verify both leave orders and idempotency unchanged.

- [ ] **Step 5: Final report**

Report only: implementation scope, test result, database behavior, approval lifecycle result, and any known limitations.
