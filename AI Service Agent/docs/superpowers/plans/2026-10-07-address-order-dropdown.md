# Address Order Dropdown Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace free-text order ID entry in the address form with a read-only, authenticated list of the caller's address-editable orders.

**Architecture:** Add one read-only `/me/orders` presentation endpoint that derives `user_id` from JWT and returns only orders the caller owns. Centralize the current address-editability rule in the orders data layer and reuse it from both safety precheck and the endpoint. The frontend loads that list at session bootstrap, renders a single read-only target order or a dropdown, and continues to submit the same normalized message through the existing chat flow.

**Tech Stack:** Python 3.14, FastAPI, SQLite, LangGraph-compatible service code, single-file vanilla JS frontend, pytest + Node-vm frontend tests.

**Spec:** `docs/specs/user-page-spec.md` (Mutation form display) and `docs/specs/tool-spec.md` (`update_address` status rule).

## Global Constraints

- Do not change the event protocol, Router, Policy Gate, MutationGraph, or `POST /chat` flow.
- Do not let the frontend send or spoof `user_id`; the endpoint must derive it from the bearer token.
- Only `paid` orders are address-editable in the current system.
- `shipped`, `signed`, `completed`, `cancelled`, `refunding`, and `refunded` are not address-editable.
- Keep the read endpoint presentation-focused; it must not execute write tools.
- The form shows old phone/address as display-only values and collects each new value once.
- UI-generated form submission messages remain hidden from chat bubbles.
- Preserve existing source and API behavior; do not regress prior P0 tests.

---

### Task 1: Centralize Address-Editable Rule

**Files:**
- Modify: `src/ai_service_agent/tools/orders.py`
- Modify: `src/ai_service_agent/graph/workflows/safety.py`
- Test: `tests/test_high_risk_graph.py`

**Interfaces:**
- Consumes: existing normalized order statuses.
- Produces: `ORDER_DISPLAY_STATUS: dict[str, str]`, `ADDRESS_EDITABLE_STATUSES: set[str]`, and `is_address_editable_status(status: str | None) -> bool`.

- [ ] **Step 1: Write failing rule tests**

Add to `tests/test_high_risk_graph.py` near the existing address business-rule test:

```python
def test_address_editable_rule_is_whitelisted():
    from ai_service_agent.tools.orders import is_address_editable_status

    assert is_address_editable_status("paid") is True
    for status in ("shipped", "signed", "completed", "cancelled", "refunding", "refunded", ""):
        assert is_address_editable_status(status) is False
```

Add a precheck regression test immediately below it:

```python
def test_address_precheck_requires_paid_status():
    pending = {
        "tool": "update_address",
        "args": {"order_id": "ORD-1", "address": "New address"},
    }
    facts = {"order_id": "ORD-1", "user_id": "u1", "status": "shipped"}
    assert _apply_business_rules(pending, facts, "u1") == "address_not_editable"
```

- [ ] **Step 2: Run the new tests and verify failure**

Run: `& "D:\常用软件\LangGragh\.venv\Scripts\python.exe" -m pytest tests/test_high_risk_graph.py::test_address_editable_rule_is_whitelisted tests/test_high_risk_graph.py::test_address_precheck_requires_paid_status -q`

Expected: FAIL because `is_address_editable_status` does not exist.

- [ ] **Step 3: Add the shared rule in orders.py**

After `_ORDER_STATUS_FILTERS`, add:

```python
ORDER_DISPLAY_STATUS = _DISPLAY_STATUS
ADDRESS_EDITABLE_STATUSES = {"paid"}


def is_address_editable_status(status: str | None) -> bool:
    """Address changes are allowed only before fulfillment starts."""
    return status in ADDRESS_EDITABLE_STATUSES
```

In `src/ai_service_agent/graph/workflows/safety.py`, extend the import from `ai_service_agent.tools.orders`:

```python
from ai_service_agent.tools.orders import (
    is_address_editable_status,
    lookup_order_state_with_availability,
)
```

Replace the current blacklist block:

```python
if tool_name == "update_address":
    status = precheck_data.get("status")
    if status in ("shipped", "signed", "completed", "cancelled", "refunding", "refunded"):
        return "address_not_editable"
```

with:

```python
if tool_name == "update_address" and not is_address_editable_status(
    precheck_data.get("status")
):
    return "address_not_editable"
```

- [ ] **Step 4: Run focused tests**

Run: `& "D:\常用软件\LangGragh\.venv\Scripts\python.exe" -m pytest tests/test_high_risk_graph.py tests/test_mutation_graph.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/ai_service_agent/tools/orders.py src/ai_service_agent/graph/workflows/safety.py tests/test_high_risk_graph.py
git commit -m "refactor(orders): centralize address editability"
```

### Task 2: Add Read-Only `/me/orders` Endpoint

**Files:**
- Modify: `src/ai_service_agent/harness/server.py`
- Test: `tests/test_profile_endpoint.py`

**Interfaces:**
- Consumes: `is_address_editable_status`, `ORDER_DISPLAY_STATUS`, and the profile SQLite path already used by `/me/profile`.
- Produces: `GET /me/orders?address_editable=true` returning:

```json
{
  "user_id": "u0010",
  "address_editable": true,
  "orders": [
    {
      "order_id": "ORD-2026-10119",
      "status_display": "已付款",
      "ordered_at": "2026-08-21T21:24:29+08:00",
      "amount": 6360.2,
      "currency": "THB"
    }
  ],
  "count": 1
}
```

- [ ] **Step 1: Write failing endpoint tests**

Add to `tests/test_profile_endpoint.py`:

```python
def test_me_orders_requires_bearer_token(tmp_path):
    client, _ = _client(tmp_path)
    response = client.get("/me/orders?address_editable=true")

    assert response.status_code == 401


def test_me_orders_filters_address_editable_orders(tmp_path):
    client, secret = _client(tmp_path)
    token = create_access_token("u0010", secret, expires_minutes=60)
    response = client.get(
        "/me/orders?address_editable=true",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "user_id": "u0010",
        "address_editable": True,
        "orders": [
            {
                "order_id": "ORD-2026-10119",
                "status_display": "已付款",
                "ordered_at": "2026-08-21T21:24:29+08:00",
                "amount": 6360.2,
                "currency": "THB",
            }
        ],
        "count": 1,
    }


def test_me_orders_returns_404_for_unknown_user(tmp_path):
    client, secret = _client(tmp_path)
    token = create_access_token("u9999", secret, expires_minutes=60)
    response = client.get(
        "/me/orders?address_editable=true",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 404
```

- [ ] **Step 2: Run endpoint tests and verify failure**

Run: `& "D:\常用软件\LangGragh\.venv\Scripts\python.exe" -m pytest tests/test_profile_endpoint.py -q`

Expected: FAIL with `404 Not Found` for `/me/orders`.

- [ ] **Step 3: Implement the endpoint**

Extend the import at the top of `server.py`:

```python
from ai_service_agent.tools.orders import (
    ADDRESS_EDITABLE_STATUSES,
    ORDER_DISPLAY_STATUS,
)
```

Add this route immediately after `/me/profile`:

```python
@app.get("/me/orders")
def me_orders(request: Request, address_editable: bool = False):
    """Read-only order options for frontend forms."""
    user_id = _require_auth_user(request)
    profile_path = Path(resolved_profile_db_path)
    if not profile_path.exists():
        raise HTTPException(status_code=503, detail="profile data source unavailable")

    conn = None
    try:
        conn = sqlite3.connect(profile_path)
        exists = conn.execute(
            "SELECT 1 FROM customers WHERE customer_id = ?",
            (user_id,),
        ).fetchone()
        if exists is None:
            raise HTTPException(status_code=404, detail="profile not found")

        sql = (
            "SELECT order_id, status, ordered_at, total_amount, currency "
            "FROM orders WHERE customer_id = ?"
        )
        params: list = [user_id]
        if address_editable:
            placeholders = ",".join("?" for _ in ADDRESS_EDITABLE_STATUSES)
            sql += f" AND status IN ({placeholders})"
            params.extend(sorted(ADDRESS_EDITABLE_STATUSES))
        sql += " ORDER BY ordered_at DESC LIMIT 20"
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.Error as exc:
        raise HTTPException(status_code=503, detail="profile data source unavailable") from exc
    finally:
        if conn is not None:
            conn.close()

    orders = [
        {
            "order_id": row[0],
            "status_display": ORDER_DISPLAY_STATUS.get(row[1], row[1]),
            "ordered_at": row[2],
            "amount": row[3],
            "currency": row[4],
        }
        for row in rows
    ]
    return {
        "user_id": user_id,
        "address_editable": address_editable,
        "orders": orders,
        "count": len(orders),
    }
```

Then change the function signature to use FastAPI dependency injection rather than the `None` default:

```python
@app.get("/me/orders")
def me_orders(request: Request, address_editable: bool = False):
```

- [ ] **Step 4: Run endpoint tests**

Run: `& "D:\常用软件\LangGragh\.venv\Scripts\python.exe" -m pytest tests/test_profile_endpoint.py tests/test_server.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/ai_service_agent/harness/server.py tests/test_profile_endpoint.py
git commit -m "feat(harness): add read-only editable orders endpoint"
```

### Task 3: Load Order Options in the Frontend Store

**Files:**
- Modify: `web/index.html`
- Test: `tests/test_user_page.py`

**Interfaces:**
- Consumes: `GET /me/orders?address_editable=true`.
- Produces: `store.orders: Array<{order_id: string, status_display: string, ordered_at: string, amount: number, currency: string}>`, `store.ordersStatus: 'idle' | 'ready' | 'error'`, and `api.getOrders()`.

- [ ] **Step 1: Write failing store/API tests**

Add to `tests/test_user_page.py`:

```python
def test_initial_store_includes_order_options_state(tmp_path):
    out = _run_js(
        "var s=UserPage.createInitialStore();"
        "({orders:s.orders,status:s.ordersStatus})",
        tmp_path,
    )
    assert out["orders"] == []
    assert out["status"] == "idle"


def test_api_get_orders_requests_editable_orders(tmp_path):
    out = _run_js(
        "var sent=null;"
        "var api=UserPage.createApi({fetchImpl:async function(url,opts){sent={url:url,opts:opts};"
        "return {ok:true,status:200,json:async function(){return {orders:[],count:0};}};},"
        "baseUrl:'http://x'});"
        "var result=await api.getOrders();"
        "({url:sent.url,result:result})",
        tmp_path,
    )
    assert out["url"] == "http://x/me/orders?address_editable=true"
    assert out["result"] == {"orders": [], "count": 0}


def test_mock_api_get_orders_returns_options(tmp_path):
    out = _run_js(
        "var api=await UserPage.createMockApi();"
        "var result=await api.getOrders();"
        "({count:result.count,first:result.orders[0]})",
        tmp_path,
    )
    assert out["count"] == 1
    assert out["first"]["order_id"] == "ORD-PAID-1"
    assert out["first"]["status_display"] == "已付款"
```

- [ ] **Step 2: Run frontend tests and verify failure**

Run: `& "D:\常用软件\LangGragh\.venv\Scripts\python.exe" -m pytest tests/test_user_page.py -q`

Expected: FAIL because `orders`, `ordersStatus`, and `getOrders` do not exist.

- [ ] **Step 3: Implement store state and API**

In `createInitialStore()`, add:

```javascript
orders: [],
ordersStatus: 'idle',
```

In the real API object, add:

```javascript
async function getOrders() {
  var r = await fetchImpl(base + '/me/orders?address_editable=true', { headers: headers() });
  if (!r.ok) throw apiError('ORDERS_FAILED', r);
  return await r.json();
}
```

Expose it as `getOrders: getOrders` in the returned API object.

In the mock profile object, add:

```javascript
orders: [{
  order_id: 'ORD-PAID-1',
  status_display: '已付款',
  ordered_at: '2026-10-07T00:00:00+08:00',
  amount: 99.0,
  currency: 'CNY',
}],
```

Add the mock method:

```javascript
async getOrders() { return { orders: orders, count: orders.length }; },
```

In `bootstrapChat()`, after the profile load block, add:

```javascript
try {
  var orderResult = await api.getOrders();
  store.orders = orderResult.orders || [];
  store.ordersStatus = 'ready';
} catch (e) {
  if (isAuthExpiredError(e)) { handleAuthExpired(); return false; }
  store.orders = [];
  store.ordersStatus = 'error';
}
```

- [ ] **Step 4: Run frontend tests**

Run: `& "D:\常用软件\LangGragh\.venv\Scripts\python.exe" -m pytest tests/test_user_page.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/index.html tests/test_user_page.py
git commit -m "feat(web): load address-editable order options"
```

### Task 4: Render Single Order or Dropdown in the Address Form

**Files:**
- Modify: `web/index.html`
- Test: `tests/test_user_page.py`

**Interfaces:**
- Consumes: `store.orders`.
- Produces: `buildFormContent(missing, profile, orders)` address forms with either `form.order_id` for one order or a select field named `order_id` for multiple orders.

- [ ] **Step 1: Write failing form-render tests**

Add to `tests/test_user_page.py`:

```python
def test_build_address_form_uses_single_order_without_input(tmp_path):
    orders = [{"order_id": "ORD-1", "status_display": "已付款"}]
    out = _run_js(
        "var form=UserPage.buildFormContent(['order_id','address'],"
        "{address:'Old address'},[{order_id:'ORD-1',status_display:'已付款'}]);"
        "({form:form})",
        tmp_path,
    )
    assert out["form"]["order_id"] == "ORD-1"
    assert "目标订单" in out["form"]["old_info"]["目标订单"]
    assert all(field["name"] != "order_id" for field in out["form"]["fields"])
    assert any(field["name"] == "address" for field in out["form"]["fields"])


def test_build_address_form_renders_dropdown_for_multiple_orders(tmp_path):
    orders = [
        {"order_id": "ORD-1", "status_display": "已付款"},
        {"order_id": "ORD-2", "status_display": "已付款"},
    ]
    out = _run_js(
        "var form=UserPage.buildFormContent(['order_id','address'],"
        "{address:'Old address'}," + json.dumps(orders) + ");"
        "var field=form.fields.find(function(item){return item.name==='order_id';});"
        "({field:field})",
        tmp_path,
    )
    assert out["field"]["type"] == "select"
    assert out["field"]["options"] == [
        {"value": "ORD-1", "label": "ORD-1 · 已付款"},
        {"value": "ORD-2", "label": "ORD-2 · 已付款"},
    ]
```

- [ ] **Step 2: Run form tests and verify failure**

Run: `& "D:\常用软件\LangGragh\.venv\Scripts\python.exe" -m pytest tests/test_user_page.py -q`

Expected: FAIL because `buildFormContent` still renders an order-ID text input.

- [ ] **Step 3: Implement order-aware address form**

Change the signature:

```javascript
function buildFormContent(missing, profile, orders) {
```

For the address branch, use:

```javascript
var orderOptions = orders || [];
var requiresOrder = (missing || []).indexOf('order_id') >= 0;
if (requiresOrder && orderOptions.length === 0) {
  return {
    form_type: formType,
    empty_reason: '当前没有可修改收货地址的订单。',
  };
}

var oldInfo = { '当前收货地址': safeProfile.address || '未知' };
var fields = [];

if ((missing || []).indexOf('order_id') >= 0) {
  if (orderOptions.length === 1) {
    var onlyOrder = orderOptions[0];
    oldInfo['目标订单'] = onlyOrder.order_id + ' · ' + (onlyOrder.status_display || '订单');
    return {
      form_type: formType,
      title: '修改收货地址',
      old_info: oldInfo,
      order_id: onlyOrder.order_id,
      fields: [{ name: 'address', label: '新收货地址', type: 'text', required: true }],
      submit_label: '确认修改',
    };
  }

  fields.push({
    name: 'order_id',
    label: '选择订单',
    type: 'select',
    required: true,
    options: orderOptions.map(function (order) {
      return {
        value: order.order_id,
        label: order.order_id + ' · ' + (order.status_display || '订单'),
      };
    }),
  });
}

fields.push({ name: 'address', label: '新收货地址', type: 'text', required: true });
return {
  form_type: formType,
  title: '修改收货地址',
  old_info: oldInfo,
  fields: fields,
  submit_label: '确认修改',
};
```

Update the `clarification_required` event call:

```javascript
var form = buildFormContent(data.missing || [], s.profile, s.orders);
```

In the DOM form-submit handler, add before validation:

```javascript
if (formMessage.content.order_id) {
  values.order_id = formMessage.content.order_id;
}
```

In `mountNode`, render select options before the default input handling:

```javascript
if (node.tag === 'select') {
  (node.children || []).forEach(function (option) {
    el.appendChild(mountNode(option));
  });
  return el;
}
```

In the address field mapping inside `messageNode`, render selects:

```javascript
if (field.type === 'select') {
  return { tag: 'div', children: [
    { tag: 'label', children: [field.label] },
    { tag: 'select', attrs: attrs, children: (field.options || []).map(function (option) {
      return {
        tag: 'option',
        attrs: { value: option.value },
        children: [option.label],
      };
    }) },
  ] };
}
```

Also set `attrs.required = 'required'` for required select fields.

- [ ] **Step 4: Run frontend form tests**

Run: `& "D:\常用软件\LangGragh\.venv\Scripts\python.exe" -m pytest tests/test_user_page.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/index.html tests/test_user_page.py
git commit -m "feat(web): render address order selector"
```

### Task 5: Handle Empty and Failed Order Options

**Files:**
- Modify: `web/index.html`
- Test: `tests/test_user_page.py`

**Interfaces:**
- Consumes: `store.ordersStatus` and `buildFormContent(...).empty_reason`.
- Produces: no manual order-ID fallback when options are unavailable.

- [ ] **Step 1: Write failing empty/error tests**

Add to `tests/test_user_page.py`:

```python
def test_empty_editable_orders_render_notice_not_form(tmp_path):
    out = _run_js(
        "var s=UserPage.createInitialStore();"
        "s.profile={address:'Old address'};"
        "s.orders=[];s.ordersStatus='ready';"
        "s=UserPage.applyEvent(s,{type:'clarification_required',data:{missing:['order_id','address']}});"
        "({kinds:s.messages.map(function(m){return m.kind;}),"
        "reason:s.messages[0].content.reason})",
        tmp_path,
    )
    assert out["kinds"] == ["notice"]
    assert out["reason"] == "当前没有可修改收货地址的订单。"


def test_failed_order_options_render_retry_notice_not_form(tmp_path):
    out = _run_js(
        "var s=UserPage.createInitialStore();"
        "s.profile={address:'Old address'};"
        "s.orders=[];s.ordersStatus='error';"
        "s=UserPage.applyEvent(s,{type:'clarification_required',data:{missing:['order_id','address']}});"
        "({kind:s.messages[0].kind,reason:s.messages[0].content.reason})",
        tmp_path,
    )
    assert out["kind"] == "notice"
    assert out["reason"] == "订单列表加载失败，请重新打开会话或稍后重试。"
```

- [ ] **Step 2: Run tests and verify failure**

Run: `& "D:\常用软件\LangGragh\.venv\Scripts\python.exe" -m pytest tests/test_user_page.py::test_empty_editable_orders_render_notice_not_form tests/test_user_page.py::test_failed_order_options_render_retry_notice_not_form -q`

Expected: FAIL because the current code falls back to a text clarification or input form.

- [ ] **Step 3: Implement non-form notices**

In the `clarification_required` event branch, use:

```javascript
var form = buildFormContent(data.missing || [], s.profile, s.orders);
if (form && form.empty_reason) {
  s.messages.push({
    id: msgId(),
    role: 'assistant',
    kind: 'notice',
    content: {
      rule: 'address_options_unavailable',
      reason: s.ordersStatus === 'error'
        ? '订单列表加载失败，请重新打开会话或稍后重试。'
        : form.empty_reason,
    },
    meta: {},
  });
  break;
}
```

In `messageNode`, update the notice branch to prefer a human-readable reason:

```javascript
return {
  tag: 'div',
  attrs: { class: 'notice' },
  children: [m.content.reason || '安全拦截：' + (m.content.reason || '')],
};
```

If the existing safety notice needs a rule label, render:

```javascript
children: [m.content.rule === 'address_options_unavailable'
  ? m.content.reason
  : '安全拦截：' + m.content.reason]
```

- [ ] **Step 4: Run frontend tests**

Run: `& "D:\常用软件\LangGragh\.venv\Scripts\python.exe" -m pytest tests/test_user_page.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/index.html tests/test_user_page.py
git commit -m "fix(web): avoid address form without editable orders"
```

### Task 6: Update Specification and Run Regression

**Files:**
- Modify: `docs/specs/user-page-spec.md`

**Interfaces:**
- Consumes: implemented `/me/orders` route.
- Produces: updated page spec for future maintainers.

- [ ] **Step 1: Update the Mutation form display section**

Add these bullets after the existing `GET /me/profile` bullet:

```markdown
- `GET /me/orders?address_editable=true` is read-only and returns only the authenticated caller's address-editable orders.
- The address form displays a single editable order as read-only context, or a dropdown when multiple editable orders exist.
- The address form does not offer free-text order ID entry and does not display orders that safety precheck will reject.
- If no editable orders exist or order options fail to load, the frontend shows a status notice instead of a manual order-ID fallback.
```

- [ ] **Step 2: Run focused regression**

Run: `& "D:\常用软件\LangGragh\.venv\Scripts\python.exe" -m pytest tests/test_profile_endpoint.py tests/test_user_page.py tests/test_server.py tests/test_mutation_graph.py tests/test_high_risk_graph.py -q`

Expected: PASS.

- [ ] **Step 3: Run full regression**

Run: `& "D:\常用软件\LangGragh\.venv\Scripts\python.exe" -m pytest -q`

Expected: PASS with no unrelated failures.

- [ ] **Step 4: Commit**

```bash
git add docs/specs/user-page-spec.md
git commit -m "docs(web): document address order selector"
```

### Task 7: Manual Smoke

**Files:**
- No source changes expected.

**Interfaces:**
- Consumes: updated frontend and `/me/orders`.
- Produces: manual verification result.

- [ ] **Step 1: Restart the worktree service**

Use the existing 8001 local service command and confirm it loads code from this worktree.

- [ ] **Step 2: Verify single editable order**

Login as `+65-83909998` / `123456`, send `我要修改收货地址`, and verify the form shows the current address and a read-only target order rather than an order-ID input.

- [ ] **Step 3: Verify multiple editable orders**

Use a seeded account with at least two `paid` orders, or temporarily seed one in the local mock database, and verify the form shows an order dropdown with `order_id · status` labels.

- [ ] **Step 4: Verify empty state**

Login as `+66-83765108` / `123456`, send `我要修改收货地址`, and verify a no-editable-orders notice appears instead of an order-ID input.

- [ ] **Step 5: Verify end-to-end mutation**

Select an editable order, enter a new address, submit, confirm, and verify the existing mutation confirmation and completion flow succeeds.
