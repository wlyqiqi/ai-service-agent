# Frontend Mutation Forms Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add P0 frontend forms for `update_phone` and `update_address` without changing the Router, Graph, Policy Gate, tool schemas, or SSE event protocol.

**Architecture:** The backend continues to return the existing `clarification_required` event with only `missing`. The frontend locally maps missing-field combinations to a form configuration, displays current profile values from a read-only `/me/profile` presentation endpoint, validates the user input once, converts the form values into a normalized text message, and submits it through the existing `POST /chat` flow. Router, MutationGraph, Policy Gate, and tools remain unchanged.

**Tech Stack:** Python 3.14, FastAPI, SQLite, LangGraph, single-file JavaScript frontend, pytest, Node VM frontend tests.

**Spec:** `docs/superpowers/plans/2026-10-07-frontend-mutation-forms.md` (see Global Constraints and Decoupled Design).

## Global Constraints

- Do not change `event-spec.md` or add a `form` field to `clarification_required`.
- Do not change Router / JEV, MutationGraph, HighRiskGraph, Policy Gate, tool schemas, tool registry, or LangGraph State.
- Do not let the frontend call write tools directly.
- All mutations continue through `POST /chat`.
- `GET /me/profile` is read-only, authenticated, and only returns the caller's own profile.
- Old profile values are display-only and are never submitted as tool parameters.
- Every business value is entered once; do not add confirm-phone, confirm-address, or similar second-entry fields.
- P0 supports only `update_phone` and `update_address`.
- If a `clarification_required.missing` combination cannot be uniquely mapped, the frontend falls back to the existing text clarification card.
- Offline tests must not depend on real LightRAG, real ARK, or real model APIs.
- Do not add dependencies.

## Decoupled Design

```text
clarification_required(missing)
  -> frontend maps missing to local form config
  -> frontend shows old_info from GET /me/profile
  -> user fills each value once and clicks submit
  -> frontend formats a normalized text message
  -> POST /chat
  -> Router -> MutationGraph -> Policy Gate -> confirmation/approval -> tool
```

The frontend owns form rendering and validation. The backend owns intent, business parameter validation, authorization, confirmation, approval, and execution.

---

### Task 1: Add authenticated read-only profile endpoint

**Files:**
- Modify: `src/ai_service_agent/harness/server.py`
- Test: `tests/test_profile_endpoint.py`

**Interfaces:**
- Consumes: `create_app(..., profile_db_path: str | None = None)`.
- Produces: `GET /me/profile` returning `{"user_id": str, "phone": str, "address": str}`.

- [ ] **Step 1: Write failing endpoint tests**

Create `tests/test_profile_endpoint.py`:

```python
import importlib.util
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from ai_service_agent.harness.auth import create_access_token
from ai_service_agent.harness.server import create_app


_ROOT = Path(__file__).resolve().parents[1]


def _load_seed_module():
    spec = importlib.util.spec_from_file_location(
        "seed_mock_db_profile", _ROOT / "scripts" / "seed_mock_db.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _client(tmp_path):
    main_db = tmp_path / "main.db"
    mock_db = tmp_path / "mock.db"
    _load_seed_module().build_database(str(mock_db))
    secret = "test-secret-with-at-least-32-bytes!!"
    app = create_app(
        str(main_db),
        model="fake",
        auth_enabled=True,
        jwt_secret=secret,
        profile_db_path=str(mock_db),
    )
    return TestClient(app), secret


def test_me_profile_returns_caller_profile(tmp_path):
    client, secret = _client(tmp_path)
    token = create_access_token("u0001", secret, expires_minutes=60)
    response = client.get(
        "/me/profile",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "user_id": "u0001",
        "phone": "+84-92722552",
        "address": "No. 1, Chengyuan VN",
    }


def test_me_profile_requires_bearer_token(tmp_path):
    client, _ = _client(tmp_path)
    response = client.get("/me/profile")

    assert response.status_code == 401


def test_me_profile_returns_404_for_unknown_user(tmp_path):
    client, secret = _client(tmp_path)
    token = create_access_token("u9999", secret, expires_minutes=60)
    response = client.get(
        "/me/profile",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 404
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
uv run --no-sync pytest tests/test_profile_endpoint.py -q
```

Expected: FAIL because `create_app` has no `profile_db_path` parameter and `/me/profile` does not exist.

- [ ] **Step 3: Add the read-only endpoint**

In `src/ai_service_agent/harness/server.py`, update the `create_app` signature:

```python
def create_app(
    db_path: str,
    model=None,
    simulate_approval: bool | None = None,
    chatwoot: object | None = None,
    chatwoot_webhook_secret: str | None = None,
    auth_enabled: bool | None = None,
    jwt_secret: str | None = None,
    profile_db_path: str | None = None,
):
```

After `settings = get_settings()`, add:

```python
resolved_profile_db_path = profile_db_path or settings.mock_ecommerce_db_path
```

Inside the app, next to the other authorization helpers, add:

```python
def _require_auth_user(request: Request):
    user_id = _auth_user(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="authentication required")
    return user_id
```

After the `/auth/login` route, add:

```python
@app.get("/me/profile")
def me_profile(request: Request):
    """Read-only presentation data for frontend form old-info display."""
    user_id = _require_auth_user(request)
    profile_path = Path(resolved_profile_db_path)
    if not profile_path.exists():
        raise HTTPException(status_code=503, detail="profile data source unavailable")

    try:
        conn = sqlite3.connect(profile_path)
        row = conn.execute(
            "SELECT phone, address FROM customers WHERE customer_id = ?",
            (user_id,),
        ).fetchone()
    except sqlite3.Error as exc:
        raise HTTPException(status_code=503, detail="profile data source unavailable") from exc
    finally:
        if "conn" in locals():
            conn.close()

    if row is None:
        raise HTTPException(status_code=404, detail="profile not found")

    return {"user_id": user_id, "phone": row[0], "address": row[1]}
```

- [ ] **Step 4: Run endpoint tests and verify they pass**

Run:

```bash
uv run --no-sync pytest tests/test_profile_endpoint.py -q
```

Expected: `3 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/ai_service_agent/harness/server.py tests/test_profile_endpoint.py
git commit -m "feat(harness): add read-only profile endpoint"
```

---

### Task 2: Add frontend profile API and local form mapping

**Files:**
- Modify: `web/index.html`
- Modify: `tests/test_user_page.py`

**Interfaces:**
- Consumes: `GET /me/profile`.
- Produces:
  - `UserPage.maskPhone(value: string): string`
  - `UserPage.resolveFormType(missing: string[]): string | null`
  - `UserPage.buildFormContent(missing: string[], profile: object | null): object | null`
  - `store.profile: {user_id: string, phone: string, address: string} | null`

- [ ] **Step 1: Write failing pure-function tests**

Add to `tests/test_user_page.py`:

```python
def test_initial_store_has_null_profile(tmp_path):
    out = _run_js("UserPage.createInitialStore().profile", tmp_path)
    assert out is None


def test_mask_phone_hides_middle_digits(tmp_path):
    out = _run_js("UserPage.maskPhone('+8492722552')", tmp_path)
    assert out == "+84****2552"


def test_resolve_form_type_maps_p0_missing_fields(tmp_path):
    out = _run_js(
        "({phone:UserPage.resolveFormType(['phone']),"
        "address:UserPage.resolveFormType(['address']),"
        "address_with_order:UserPage.resolveFormType(['order_id','address']),"
        "unknown:UserPage.resolveFormType(['order_id'])})",
        tmp_path,
    )
    assert out == {
        "phone": "update_phone",
        "address": "update_address",
        "address_with_order": "update_address",
        "unknown": None,
    }


def test_build_phone_form_content(tmp_path):
    out = _run_js(
        "UserPage.buildFormContent(['phone'], "
        "{user_id:'u0001',phone:'+8492722552',address:'No. 1, Chengyuan VN'})",
        tmp_path,
    )
    assert out["form_type"] == "update_phone"
    assert out["title"] == "修改联系电话"
    assert out["old_info"] == {"当前手机号": "+84****2552"}
    assert out["fields"] == [
        {"name": "phone", "label": "新手机号", "type": "tel", "required": True}
    ]
    assert out["submit_label"] == "确认修改"


def test_build_address_form_content_requires_order_id_when_missing(tmp_path):
    out = _run_js(
        "UserPage.buildFormContent(['order_id','address'], "
        "{user_id:'u0001',phone:'+8492722552',address:'No. 1, Chengyuan VN'})",
        tmp_path,
    )
    assert out["form_type"] == "update_address"
    assert out["old_info"] == {"当前收货地址": "No. 1, Chengyuan VN"}
    assert [field["name"] for field in out["fields"]] == ["order_id", "address"]


def test_build_address_form_content_omits_order_id_when_not_missing(tmp_path):
    out = _run_js(
        "UserPage.buildFormContent(['address'], "
        "{user_id:'u0001',phone:'+8492722552',address:'No. 1, Chengyuan VN'})",
        tmp_path,
    )
    assert [field["name"] for field in out["fields"]] == ["address"]


def test_build_form_content_falls_back_to_none(tmp_path):
    out = _run_js("UserPage.buildFormContent(['order_id'], null)", tmp_path)
    assert out is None
```

- [ ] **Step 2: Run frontend mapping tests and verify they fail**

Run:

```bash
uv run --no-sync pytest tests/test_user_page.py -q
```

Expected: FAIL because the new functions and `store.profile` do not exist.

- [ ] **Step 3: Add profile state, API method, mapping helpers, and bootstrap loading**

In `web/index.html`, update `createInitialStore`:

```js
function createInitialStore() {
  return {
    view: null,
    auth: null,
    conversation: { conversation_id: null, mode: 'ai' },
    messages: [],
    conn: { status: 'idle', lastError: null },
    pendingAction: null,
    profile: null,
    config: { mock: false, baseUrl: '' },
  };
}
```

Add these pure helpers before `applyEvent`:

```js
function maskPhone(value) {
  var text = String(value || '');
  if (text.length <= 7) return '****';
  return text.slice(0, 3) + '****' + text.slice(-4);
}

function resolveFormType(missing) {
  var fields = missing || [];
  if (fields.indexOf('phone') >= 0) return 'update_phone';
  if (fields.indexOf('address') >= 0) return 'update_address';
  return null;
}

function buildFormContent(missing, profile) {
  var formType = resolveFormType(missing);
  if (!formType) return null;
  var safeProfile = profile || {};

  if (formType === 'update_phone') {
    return {
      form_type: formType,
      title: '修改联系电话',
      old_info: { '当前手机号': maskPhone(safeProfile.phone || '未知') },
      fields: [
        { name: 'phone', label: '新手机号', type: 'tel', required: true },
      ],
      submit_label: '确认修改',
    };
  }

  var fields = [];
  if ((missing || []).indexOf('order_id') >= 0) {
    fields.push({ name: 'order_id', label: '订单号', type: 'text', required: true });
  }
  fields.push({ name: 'address', label: '新收货地址', type: 'text', required: true });
  return {
    form_type: formType,
    title: '修改收货地址',
    old_info: { '当前收货地址': safeProfile.address || '未知' },
    fields: fields,
    submit_label: '确认修改',
  };
}
```

In `createApi`, add:

```js
async function getProfile() {
  var r = await fetchImpl(base + '/me/profile', { headers: headers() });
  if (!r.ok) throw apiError('PROFILE_FAILED', r);
  return await r.json();
}
```

Update the returned API object:

```js
return {
  login: login, createSession: createSession, getMessages: getMessages,
  sendMessage: sendMessage, confirm: confirm, approve: approve, reject: reject,
  clarify: clarify, getProfile: getProfile,
};
```

In `createMockApi`, add a profile object:

```js
var profile = { user_id: 'demo-user', phone: '+6581234567', address: 'No. 1, Demo Road' };
```

Add to the mock API:

```js
async getProfile() { return profile; },
```

Update `bootstrapChat` after session bootstrap and before history loading:

```js
try {
  store.profile = await api.getProfile();
} catch (e) {
  if (isAuthExpiredError(e)) { handleAuthExpired(); return false; }
  store.profile = null;
}
```

Expose the new helpers in `window.UserPage`:

```js
maskPhone: maskPhone,
resolveFormType: resolveFormType,
buildFormContent: buildFormContent,
```

- [ ] **Step 4: Run mapping tests and verify they pass**

Run:

```bash
uv run --no-sync pytest tests/test_user_page.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add web/index.html tests/test_user_page.py
git commit -m "feat(web): map clarifications to local mutation forms"
```

---

### Task 3: Render forms and submit through existing chat flow

**Files:**
- Modify: `web/index.html`
- Modify: `tests/test_user_page.py`

**Interfaces:**
- Consumes: `buildFormContent`, `store.profile`, `api.sendMessage`.
- Produces:
  - `kind: 'form'` messages for mapped clarifications.
  - `UserPage.validateFormValues(form: object, values: object, profile: object | null): string`
  - `UserPage.formatFormSubmission(formType: string, values: object): string`

- [ ] **Step 1: Write failing validation, formatting, event, and view tests**

Add to `tests/test_user_page.py`:

```python
def test_apply_clarification_creates_phone_form(tmp_path):
    out = _run_js(
        "var s=UserPage.createInitialStore();"
        "s.profile={user_id:'u0001',phone:'+8492722552',address:'No. 1, Chengyuan VN'};"
        "s=UserPage.applyEvent(s,{type:'clarification_required',data:{missing:['phone']}});"
        "s.messages[0]",
        tmp_path,
    )
    assert out["kind"] == "form"
    assert out["content"]["form_type"] == "update_phone"
    assert out["meta"] == {"status": "pending"}


def test_apply_clarification_keeps_text_card_for_unknown_fields(tmp_path):
    out = _run_js(
        "UserPage.applyEvent(UserPage.createInitialStore(), "
        "{type:'clarification_required',data:{missing:['order_id']}})",
        tmp_path,
    )
    assert out["messages"][0]["kind"] == "clarify"


def test_validate_phone_form(tmp_path):
    out = _run_js(
        "var form={form_type:'update_phone',fields:[{name:'phone',label:'新手机号',required:true}]};"
        "({empty:UserPage.validateFormValues(form,{phone:''},null),"
        "invalid:UserPage.validateFormValues(form,{phone:'123'},null),"
        "same:UserPage.validateFormValues(form,{phone:'+8492722552'},"
        "{phone:'+8492722552'}),"
        "ok:UserPage.validateFormValues(form,{phone:'+8413800138000'},"
        "{phone:'+8492722552'})})",
        tmp_path,
    )
    assert out["empty"] == "请填写新手机号"
    assert out["invalid"] == "请输入有效的新手机号"
    assert out["same"] == "新手机号不能与当前手机号相同"
    assert out["ok"] == ""


def test_validate_address_form(tmp_path):
    out = _run_js(
        "var form={form_type:'update_address',fields:["
        "{name:'order_id',label:'订单号',required:true},"
        "{name:'address',label:'新收货地址',required:true}]};"
        "({missing_order:UserPage.validateFormValues(form,{order_id:'',address:'New address'},null),"
        "same:UserPage.validateFormValues(form,{order_id:'ORD-1',address:'No. 1, Chengyuan VN'},"
        "{address:'No. 1, Chengyuan VN'}),"
        "ok:UserPage.validateFormValues(form,{order_id:'ORD-1',address:'New address'},"
        "{address:'No. 1, Chengyuan VN'})})",
        tmp_path,
    )
    assert out["missing_order"] == "请填写订单号"
    assert out["same"] == "新收货地址不能与当前收货地址相同"
    assert out["ok"] == ""


def test_format_form_submissions(tmp_path):
    out = _run_js(
        "({phone:UserPage.formatFormSubmission('update_phone',{phone:'+8413800138000'}),"
        "address:UserPage.formatFormSubmission('update_address',"
        "{order_id:'ORD-1',address:'New address'})})",
        tmp_path,
    )
    assert out["phone"] == "[表单提交] 修改联系电话\n新手机号：+8413800138000"
    assert out["address"] == "[表单提交] 修改收货地址\n订单号：ORD-1\n新地址：New address"


def test_build_view_renders_form_card_without_second_entry(tmp_path):
    out = _run_js(
        "var s=UserPage.createInitialStore();"
        "s.profile={user_id:'u0001',phone:'+8492722552',address:'No. 1, Chengyuan VN'};"
        "s=UserPage.applyEvent(s,{type:'clarification_required',data:{missing:['phone']}});"
        "var v=UserPage.buildView(s);"
        "({class:v.messages[0].attrs.class,text:v.messages[0].children.map(String).join('|')})",
        tmp_path,
    )
    assert out["class"] == "card form-card"
    assert "修改联系电话" in out["text"]
    assert "+84****2552" in out["text"]
    assert "新手机号" in out["text"]
    assert "确认修改" in out["text"]
```

- [ ] **Step 2: Run frontend form tests and verify they fail**

Run:

```bash
uv run --no-sync pytest tests/test_user_page.py -q
```

Expected: FAIL because `applyEvent` still creates a text clarification card for phone/address and validation/render helpers do not exist.

- [ ] **Step 3: Map mapped clarifications to form messages**

Replace the current `clarification_required` case in `applyEvent` with:

```js
case 'clarification_required': {
  var form = buildFormContent(data.missing || [], s.profile);
  if (form) {
    s.messages.push({
      id: msgId(), role: 'assistant', kind: 'form',
      content: form, meta: { status: 'pending' },
    });
  } else {
    s.messages.push({
      id: msgId(), role: 'assistant', kind: 'clarify',
      content: { missing: data.missing || [], reason: data.reason || null },
      meta: {},
    });
  }
  break;
}
```

- [ ] **Step 4: Add validation and submission formatting helpers**

Add after `buildFormContent`:

```js
var PHONE_PATTERN = /^\+?[0-9][0-9\- ]{5,19}$/;

function validateFormValues(form, values, profile) {
  var safeProfile = profile || {};
  var fields = form.fields || [];
  for (var i = 0; i < fields.length; i++) {
    var field = fields[i];
    var value = String(values[field.name] || '').trim();
    if (field.required && !value) return '请填写' + field.label;
    if (field.name === 'order_id' && value && value.length > 64) {
      return '订单号长度不能超过 64 个字符';
    }
  }

  if (form.form_type === 'update_phone') {
    var phone = String(values.phone || '').trim();
    if (!PHONE_PATTERN.test(phone)) return '请输入有效的新手机号';
    if (safeProfile.phone && phone === safeProfile.phone) {
      return '新手机号不能与当前手机号相同';
    }
  }

  if (form.form_type === 'update_address') {
    var address = String(values.address || '').trim();
    if (address.length < 5) return '新收货地址长度不能少于 5 个字符';
    if (safeProfile.address && address === safeProfile.address) {
      return '新收货地址不能与当前收货地址相同';
    }
  }

  return '';
}

function formatFormSubmission(formType, values) {
  if (formType === 'update_phone') {
    return '[表单提交] 修改联系电话\n新手机号：' + String(values.phone || '').trim();
  }
  if (formType === 'update_address') {
    var lines = ['[表单提交] 修改收货地址'];
    var orderId = String(values.order_id || '').trim();
    if (orderId) lines.push('订单号：' + orderId);
    lines.push('新地址：' + String(values.address || '').trim());
    return lines.join('\n');
  }
  return '';
}
```

Expose them:

```js
validateFormValues: validateFormValues,
formatFormSubmission: formatFormSubmission,
```

- [ ] **Step 5: Add form card rendering**

Add this CSS near the existing card styles:

```css
.form-card .old-info{margin:0 0 12px;padding:10px;background:var(--surface-2);border-radius:10px}
.form-card .old-info div{font-size:13px;color:var(--ink-2)}
.form-card label{display:block;margin:10px 0 5px;font-size:13px;color:var(--ink-2)}
.form-card input{width:100%;margin-bottom:4px}
.form-error{display:none;margin-top:8px;color:#ef4444;font-size:13px}
```

Add a `case 'form':` branch to `messageNode`:

```js
case 'form': {
  var oldInfo = m.content.old_info || {};
  var oldKeys = Object.keys(oldInfo);
  var oldKids = oldKeys.map(function (key) {
    return { tag: 'div', children: [key + '：' + oldInfo[key]] };
  });
  var fieldNodes = (m.content.fields || []).map(function (field) {
    var attrs = {
      name: field.name,
      type: field.type || 'text',
      'data-form-field': field.name,
      'aria-label': field.label,
    };
    if (field.required) attrs.required = 'required';
    return { tag: 'div', children: [
      { tag: 'label', children: [field.label] },
      { tag: 'input', attrs: attrs },
    ] };
  });
  var buttonAttrs = {
    class: 'btn primary',
    'data-action': 'form-submit',
    'data-form-id': m.id,
  };
  if (m.meta.status === 'submitted') buttonAttrs.disabled = 'disabled';
  return { tag: 'div', attrs: { class: 'card form-card', 'data-form-id': m.id }, children: [
    { tag: 'h4', children: [m.content.title] },
    { tag: 'div', attrs: { class: 'old-info' }, children: oldKids },
    fieldNodes,
    { tag: 'div', attrs: { class: 'form-error' }, children: [''] },
    { tag: 'div', attrs: { class: 'row' }, children: [
      { tag: 'button', attrs: buttonAttrs, children: [m.content.submit_label] },
    ] },
  ] };
}
```

- [ ] **Step 6: Handle submit clicks and share the chat send path**

Refactor `doSend` by adding a shared helper before it:

```js
function startUserTurn(text) {
  pushUserMessage(text);
  api.sendMessage(store.conversation.conversation_id, text, function (event) {
    store = applyEvent(store, event);
    applyView();
  }).catch(function (e) {
    if (isAuthExpiredError(e)) { handleAuthExpired(); return; }
    store = markReconnecting(store, '连接中断，正在重连…');
    applyView();
    scheduleReconnect(text, 0);
  }).then(function () {
    if (store.conversation.mode === 'human' && typeof startPolling === 'function') startPolling();
  });
}

function doSend() {
  var input = document.getElementById('msg-input');
  var text = input.value.trim();
  if (!text) return;
  if (store.conn.status === 'streaming' || store.conn.status === 'reconnecting') return;
  input.value = '';
  startUserTurn(text);
}
```

Add a DOM helper near `mountNode`:

```js
function collectFormValues(formElement) {
  var inputs = formElement.querySelectorAll('[data-form-field]');
  var values = {};
  for (var i = 0; i < inputs.length; i++) {
    values[inputs[i].getAttribute('data-form-field')] = inputs[i].value.trim();
  }
  return values;
}

function showFormError(formElement, message) {
  var errorElement = formElement.querySelector('.form-error');
  if (!errorElement) return;
  errorElement.textContent = message;
  errorElement.style.display = message ? 'block' : 'none';
}
```

In the `msg-list` click handler, add this branch before the existing `clarify` branch:

```js
if (action === 'form-submit') {
  var formElement = btn.closest('.form-card');
  var formId = btn.getAttribute('data-form-id');
  if (!formElement || !formId) return;
  if (store.conn.status === 'streaming' || store.conn.status === 'reconnecting') return;
  var formMessage = null;
  for (var formIndex = 0; formIndex < store.messages.length; formIndex++) {
    if (store.messages[formIndex].id === formId) {
      formMessage = store.messages[formIndex];
      break;
    }
  }
  if (!formMessage || formMessage.meta.status === 'submitted') return;

  var values = collectFormValues(formElement);
  var validationError = UserPage.validateFormValues(formMessage.content, values, store.profile);
  if (validationError) {
    showFormError(formElement, validationError);
    return;
  }

  for (var submittedIndex = 0; submittedIndex < store.messages.length; submittedIndex++) {
    if (store.messages[submittedIndex].id === formId) {
      store.messages[submittedIndex].meta.status = 'submitted';
      break;
    }
  }
  showFormError(formElement, '');
  startUserTurn(UserPage.formatFormSubmission(formMessage.content.form_type, values));
  return;
}
```

- [ ] **Step 7: Run frontend form tests and verify they pass**

Run:

```bash
uv run --no-sync pytest tests/test_user_page.py -q
```

Expected: all tests pass.

- [ ] **Step 8: Commit**

```bash
git add web/index.html tests/test_user_page.py
git commit -m "feat(web): render single-entry mutation forms"
```

---

### Task 4: Update user page documentation

**Files:**
- Modify: `docs/specs/user-page-spec.md`

**Interfaces:**
- Produces: Documentation for frontend-only form mapping and the read-only profile endpoint.

- [ ] **Step 1: Update the clarification behavior section**

Replace the existing `clarification_required` row in the event behavior table with:

```markdown
| `clarification_required` | 澄清卡片：列出缺失字段（如“请提供订单号”）+ 快捷补全选项。P0 中，前端可将 `missing` 包含 `phone` 或 `address` 的澄清映射为本地单次填写表单；无法唯一映射时仍使用文本澄清。表单旧信息来自只读 `GET /me/profile`，提交后仍通过 `POST /chat` 走后端流程 |
```

Add a new short subsection below the event table:

```markdown
## Mutation form display

- Form definitions and field validation are frontend presentation logic.
- The backend does not send or understand a form schema.
- `GET /me/profile` is read-only and returns only the authenticated caller's phone and address.
- Old profile values are display-only and are not submitted as tool arguments.
- `update_phone` and `update_address` P0 forms collect each new value once.
- Form submission produces a normalized text message and continues through the existing chat flow.
```

- [ ] **Step 2: Verify docs render as intended**

Run:

```bash
Get-Content docs\specs\user-page-spec.md | Select-Object -Skip 45 -First 30
```

Expected: the updated `clarification_required` row and new section are visible.

- [ ] **Step 3: Commit**

```bash
git add docs/specs/user-page-spec.md
git commit -m "docs(web): document local mutation form behavior"
```

---

### Task 5: Regression and handoff

**Files:**
- No source changes.

**Interfaces:**
- Produces: Regression evidence for the P0 form feature.

- [ ] **Step 1: Run targeted tests**

Run:

```bash
uv run --no-sync pytest tests/test_profile_endpoint.py tests/test_user_page.py tests/test_server.py -q
```

Expected: all targeted tests pass.

- [ ] **Step 2: Run full offline regression**

Run:

```bash
uv run --no-sync pytest -q
```

Expected: full offline suite passes with no new failures.

- [ ] **Step 3: Manually verify the two P0 forms**

With the real frontend and authenticated demo account:

1. Ask: `我要修改电话号码`.
2. When `clarification_required` returns `missing: ["phone"]`, verify:
   - A phone form appears.
   - Current phone is displayed masked.
   - The new phone is entered once.
   - Invalid input shows a field error.
   - Submit sends a `[表单提交] 修改联系电话` message through `/chat`.
   - Backend still asks for confirmation.
3. Ask: `我要修改收货地址`.
4. When `clarification_required` returns `missing` containing `address`, verify:
   - An address form appears.
   - Current address is displayed.
   - If `order_id` is missing, it appears as a one-time field.
   - The new address is entered once.
   - Submit sends a `[表单提交] 修改收货地址` message through `/chat`.
   - Backend still asks for confirmation.

- [ ] **Step 4: Final commit if manual checks required cleanup**

```bash
git status --short
git diff --check
```

If cleanup was required, commit it with:

```bash
git add <changed-files>
git commit -m "fix(web): stabilize mutation form behavior"
```

Otherwise, no commit is needed.

## Self-Review

- Spec coverage: The plan covers the read-only profile source, P0 phone/address forms, single-entry input, old-info display, fallback behavior, existing chat submission, frontend tests, endpoint authorization tests, docs, and regression.
- Placeholder scan: No implementation placeholders remain.
- Type consistency: Form names, field names, endpoint response keys, and helper names are consistent across tasks.
