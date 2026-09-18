# 用户侧 AI 客服页面 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付单文件 HTML 桌面端用户页面：登录（占位）→ 与 AI 客服对话、在对话内完成业务操作、转人工后继续对话（轮询坐席回复）。

**Architecture:** `web/index.html` 单文件，内嵌 CSS + JS。JS 分两层：纯逻辑层（store / applyEvent / buildView / api / mock，DOM 无关，Node vm 可测）与 DOM 装配层（`typeof document` 守卫，仅浏览器执行）。hash 路由 `#/login` / `#/chat` + 鉴权守卫；`mock=true` 开关用离线 fake 事件流驱动全部 UI，后端契约占位（TODO）。

**Tech Stack:** 原生 HTML/CSS/JS（无构建、无外部 CDN）；测试用 pytest（结构断言）+ Node ≥ 18（`vm.runInNewContext` 跑纯逻辑，Node 24 已在本机可用，缺失时跳过）。

**Spec:** `docs/specs/user-page-spec.md`（本计划实现该 spec；执行者两个文件都要读）

## Global Constraints

- 交付物必须是**单文件** `web/index.html`：无构建工具、无外部网络资源（离线可打开预览）。
- 视觉定案「干净商务」：`--bg:#f6f8fb`、`--primary:#2563eb`、`--text:#1f2937`，白色卡片、细描边。
- 布局定案「1 · 居中单栏对话（约 860px）」，无左栏；两个视图 `#view-login` / `#view-chat`，hash 路由。
- 事件协议以 `docs/specs/event-spec.md` 为准；页面行为以 `docs/specs/user-page-spec.md` 为准；**不修改任何后端代码 / 图内逻辑 / 既有 spec**。
- 后端契约全部为**占位**（`docs/specs/user-page-spec.md` §10）：`POST /auth/login`、`POST /sessions`、`POST /chat`（SSE）、`GET /conversations/{id}/messages`、confirm / approve 动作接口——api 层函数标注 TODO，真实对接由后续后端任务补齐。
- 测试命令前缀（仓库统一）：`$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest tests/test_user_page.py -v`
- Node 缺失时 JS 逻辑测试 `pytest.skip`，结构测试仍须通过；Node 版本要求 ≥ 18（`fetch` / `ReadableStream` 用于 SSE 测试）。
- 沙箱拦截 git 写：**commit 由用户执行**（git 根 `D:\AI_Project`）；每次 commit 只 `git add` 明确列出的文件，禁止 `git add -A` / `git add .`。
- 不做"万能 Agent"、不扩展子图能力；本计划只产出页面与页面测试。

## File Structure

- `web/index.html` — 单文件页面。内部结构（自上而下）：
  - `<style>`：干净商务主题 CSS 变量 + 组件样式（topbar / chat-wrap / chips / msg / card / btn / error-bar / handoff-banner / login）。
  - `<body>`：`#view-login`、`#view-chat` 两个视图容器。
  - `<script>`：纯逻辑层（`createInitialStore`、`resolveView`、`applyEvent`、`buildView`、`createApi`、`createMockApi`、`mockStream`、`nextPollDelay`，通过 `window.UserPage` 暴露）→ DOM 装配层（`typeof document` 守卫，路由 / 登录 / 发送 / 轮询装配）。
- `tests/test_user_page.py` — 唯一的页面测试文件：`_extract_script` + `_run_js` 辅助函数 + 各任务的结构 / 逻辑测试。

任务分解（每任务独立可测、独立 commit）：

| Task | 内容 | 可测产物 |
|---|---|---|
| 1 | 页面骨架 + 视图 + 路由 + 鉴权守卫 | 结构测试 + `resolveView` 逻辑测试 |
| 2 | store + `applyEvent`（13 种事件） | 每事件状态迁移测试 |
| 3 | `buildView`（vnode 渲染）+ DOM mount | vnode 结构测试 |
| 4 | api 层 + mock 模式 + 登录/发送装配 | mock 流覆盖 + fake fetch SSE 测试 |
| 5 | 人工车道轮询 + 断线重连 | 退避/轮询逻辑测试 |
| 6 | 验证收尾 + 索引登记 | 全量 pytest + 手测清单 |

---

### Task 1: 页面骨架 + 视图路由 + 鉴权守卫

**Files:**
- Create: `web/index.html`
- Create: `tests/test_user_page.py`

**Interfaces:**
- Consumes: 无（首个任务）
- Produces:
  - `window.UserPage.createInitialStore() -> store`（store 形状见下，全计划共用）
  - `window.UserPage.resolveView(hash: string, hasToken: boolean) -> 'login' | 'chat'`
  - HTML 结构：`#view-login`、`#view-chat`、`#login-account`、`#login-password`、`#btn-login`、`#btn-logout`、`#msg-input`、`#btn-send`、`#msg-list`、`#status-tag`、`#conn-text`、`#chips`
  - 注释锚点（后续任务插入实现的位置）：`/* __APPLY_EVENT__ */`、`/* __BUILD_VIEW__ */`、`/* __API_LAYER__ */`、`/* __POLL_LAYER__ */`、`/* __WIRING__ */`

store 形状（全计划共用，Task 2–5 只增不删字段）：

```js
{
  view: 'login' | 'chat' | null,
  auth: { token: string, user: { name: string } } | null,
  conversation: { conversation_id: string | null, mode: 'ai' | 'human' },
  messages: [],                       // { id, role: user|assistant|system, kind, content, meta }
  conn: { status: 'idle'|'streaming'|'reconnecting'|'polling', lastError: null },
  pendingAction: null,                // { kind: 'confirm'|'approval', ... }（Task 2 起使用）
  config: { mock: boolean, baseUrl: string },
}
```

- [ ] **Step 1: 写失败测试**

创建 `tests/test_user_page.py`：

```python
"""用户页面测试（web/index.html 单文件）。

结构测试：pytest 直接读 HTML。
逻辑测试：提取内嵌 <script>，用 Node vm 在无 DOM 沙箱中执行纯逻辑层，
断言 window.UserPage 暴露的函数行为。Node 缺失时跳过逻辑测试。
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HTML_PATH = PROJECT_ROOT / "web" / "index.html"


@pytest.fixture
def html() -> str:
    return HTML_PATH.read_text(encoding="utf-8")


def _extract_script(html: str) -> str:
    m = re.search(r"<script>([\s\S]*?)</script>", html)
    assert m, "web/index.html 必须包含一个内嵌 <script> 块"
    return m.group(1)


def _run_js(expr: str, tmp_path: Path):
    """在 Node vm 沙箱中执行 expr（作用域内 UserPage = window.UserPage），返回 JSON 解析结果。"""
    if shutil.which("node") is None:
        pytest.skip("node 不可用，跳过前端逻辑测试（需要 Node >= 18）")
    script = _extract_script(HTML_PATH.read_text(encoding="utf-8"))
    (tmp_path / "site.js").write_text(script, encoding="utf-8")
    (tmp_path / "expr.js").write_text(expr, encoding="utf-8")
    runner = tmp_path / "runner.js"
    runner.write_text(
        "const fs=require('fs');const vm=require('vm');\n"
        "const sandbox={window:{},console};\n"
        "vm.runInNewContext(fs.readFileSync(__dirname+'/site.js','utf8'),sandbox);\n"
        "const {UserPage}=sandbox.window;\n"
        "const expr=fs.readFileSync(__dirname+'/expr.js','utf8');\n"
        "console.log(JSON.stringify(eval(expr)));\n",
        encoding="utf-8",
    )
    proc = subprocess.run(["node", str(runner)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node 执行失败:\n{proc.stderr}"
    return json.loads(proc.stdout)


def test_single_file_no_external_network(html):
    assert "<script src=" not in html, "禁止外部 JS"
    assert '<link rel="stylesheet"' not in html, "禁止外部 CSS"
    assert "http://" not in html and "https://" not in html, "禁止外部网络资源"


def test_has_two_views_and_login_fields(html):
    for marker in (
        'id="view-login"', 'id="view-chat"',
        'id="login-account"', 'id="login-password"',
        'id="btn-login"', 'id="btn-logout"',
        'id="msg-input"', 'id="btn-send"',
        'id="msg-list"', 'id="status-tag"', 'id="chips"',
    ):
        assert marker in html, f"缺少 {marker}"


def test_resolve_view_requires_token(tmp_path):
    out = _run_js(
        "UserPage.resolveView('#/chat', false) + '|' + UserPage.resolveView('#/chat', true) + '|' "
        "+ UserPage.resolveView('#/login', true)",
        tmp_path,
    )
    assert out == "login|chat|login"


def test_initial_store_shape(tmp_path):
    out = _run_js("UserPage.createInitialStore()", tmp_path)
    assert out["view"] is None
    assert out["auth"] is None
    assert out["conversation"] == {"conversation_id": None, "mode": "ai"}
    assert out["messages"] == []
    assert out["conn"]["status"] == "idle"
    assert out["pendingAction"] is None
```

- [ ] **Step 2: 运行确认失败**

Run: `$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest tests/test_user_page.py -v`
Expected: FAIL——`web/index.html` 不存在（FileNotFoundError）。

- [ ] **Step 3: 实现 `web/index.html` 骨架**

创建 `web/index.html`（完整骨架，含注释锚点；`__WIRING__` 锚点本任务先实现路由/鉴权，登录发送在 Task 4 补）：

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>智家客服中心</title>
<style>
:root{--bg:#f6f8fb;--surface:#ffffff;--primary:#2563eb;--primary-weak:#e5edfb;
--text:#1f2937;--text-2:#55617a;--text-3:#8a94a6;--border:#e2e8f0;--danger:#dc2626;--radius:10px}
*{box-sizing:border-box}
body{margin:0;font-family:system-ui,-apple-system,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;
background:var(--bg);color:var(--text)}
.topbar{height:56px;background:var(--surface);border-bottom:1px solid var(--border);
display:flex;align-items:center;padding:0 24px;gap:12px}
.brand{display:flex;align-items:center;gap:8px;font-weight:700}
.brand-logo{background:var(--primary);color:#fff;border-radius:6px;padding:2px 8px}
.topbar-user{margin-left:auto;display:flex;align-items:center;gap:8px}
.view{display:none}.view.active{display:block}
.chat-wrap{max-width:860px;margin:24px auto;background:var(--surface);
border:1px solid var(--border);border-radius:var(--radius);
display:flex;flex-direction:column;height:calc(100vh - 56px - 48px);min-height:480px}
.chat-head{padding:14px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px}
.status-tag{font-size:12px;padding:2px 10px;border-radius:999px}
.status-tag.ai{background:var(--primary-weak);color:#1d4ed8}
.status-tag.human{background:#fef3c7;color:#b45309}
.chips{padding:10px 20px;display:flex;gap:8px;flex-wrap:wrap;border-bottom:1px solid var(--border)}
.chip{font-size:13px;border:1px solid var(--border);border-radius:999px;padding:4px 12px;
cursor:pointer;background:#fff;color:var(--text-2)}
.chip:hover{border-color:var(--primary);color:var(--primary)}
.msg-list{flex:1;overflow-y:auto;padding:20px;display:flex;flex-direction:column;gap:12px}
.msg{max-width:76%;padding:10px 14px;border-radius:10px;font-size:14px;line-height:1.6;white-space:pre-wrap}
.msg.user{align-self:flex-end;background:var(--primary);color:#fff}
.msg.assistant{align-self:flex-start;background:#fff;border:1px solid var(--border)}
.msg.system{align-self:center;background:#eef2f7;color:var(--text-2);font-size:12px;
border-radius:999px;padding:4px 14px}
.card{border:1px solid var(--border);border-radius:8px;padding:12px 14px;font-size:13px;
background:#fbfcfe;align-self:flex-start;max-width:76%}
.card h4{margin:0 0 6px;font-size:13px}
.btn{border:none;border-radius:6px;padding:6px 14px;font-size:13px;cursor:pointer}
.btn.primary{background:var(--primary);color:#fff}
.btn.ghost{background:#fff;border:1px solid var(--border);color:var(--text-2)}
.btn:disabled{opacity:.5;cursor:not-allowed}
.error-bar{background:#fef2f2;border:1px solid #fecaca;color:var(--danger);
border-radius:8px;padding:8px 12px;font-size:13px;align-self:stretch}
.handoff-banner{background:var(--primary-weak);border:1px solid #bfdbfe;color:#1d4ed8;
border-radius:8px;padding:10px 14px;font-size:13px;align-self:stretch}
.notice{background:#fffbeb;border:1px solid #fde68a;color:#92400e;border-radius:8px;
padding:8px 12px;font-size:13px;align-self:stretch}
.input-bar{display:flex;gap:10px;padding:14px 20px;border-top:1px solid var(--border)}
.input-bar input{flex:1;border:1px solid var(--border);border-radius:8px;padding:10px 14px;
font-size:14px;outline:none}
.input-bar input:focus{border-color:var(--primary)}
.send-btn{background:var(--primary);color:#fff;border:none;border-radius:8px;width:44px;cursor:pointer}
.login-wrap{max-width:360px;margin:120px auto;background:var(--surface);
border:1px solid var(--border);border-radius:var(--radius);padding:28px}
.login-wrap h2{margin:0 0 16px;font-size:18px}
.field{margin-bottom:14px}
.field label{display:block;font-size:12px;color:var(--text-2);margin-bottom:6px}
.field input{width:100%;border:1px solid var(--border);border-radius:8px;padding:10px;font-size:14px}
.hint{font-size:12px;color:var(--text-3);margin-top:10px}
</style>
</head>
<body>
<header class="topbar">
  <div class="brand"><span class="brand-logo">智家</span><span>AI 客服中心</span></div>
  <div class="topbar-user" id="topbar-user" hidden>
    <span id="user-name"></span>
    <button class="btn ghost" id="btn-logout">退出登录</button>
  </div>
</header>

<main id="view-login" class="view">
  <div class="login-wrap">
    <h2>登录</h2>
    <div class="field"><label>账号</label><input id="login-account" autocomplete="username"></div>
    <div class="field"><label>密码</label><input id="login-password" type="password" autocomplete="current-password"></div>
    <button class="btn primary" id="btn-login" style="width:100%">登录</button>
    <p class="hint" id="login-error" style="color:var(--danger)" hidden></p>
    <p class="hint">占位登录：mock 模式任意账号密码；真实认证后端待对接。</p>
  </div>
</main>

<main id="view-chat" class="view">
  <div class="chat-wrap">
    <div class="chat-head">
      <span class="brand-logo">智家</span>
      <div>
        <div style="font-weight:600">智家客服中心</div>
        <div style="font-size:12px;color:var(--text-3)" id="conn-text">连接正常</div>
      </div>
      <span class="status-tag ai" id="status-tag">AI 在线</span>
    </div>
    <div class="chips" id="chips"></div>
    <div class="msg-list" id="msg-list"></div>
    <div class="input-bar">
      <input id="msg-input" placeholder="输入消息…" disabled>
      <button class="send-btn" id="btn-send" disabled>➤</button>
    </div>
  </div>
</main>

<script>
(function () {
  'use strict';

  /* ============ 纯逻辑层（DOM 无关，Node vm 可测） ============ */

  function createInitialStore() {
    return {
      view: null,
      auth: null,
      conversation: { conversation_id: null, mode: 'ai' },
      messages: [],
      conn: { status: 'idle', lastError: null },
      pendingAction: null,
      config: { mock: false, baseUrl: '' },
    };
  }

  function resolveView(hash, hasToken) {
    if (!hasToken) return 'login';
    return hash === '#/login' ? 'login' : 'chat';
  }

  /* __APPLY_EVENT__ */
  /* __BUILD_VIEW__ */
  /* __API_LAYER__ */
  /* __POLL_LAYER__ */
  /* __WIRING__ */

  if (typeof window !== 'undefined') {
    window.UserPage = { createInitialStore: createInitialStore, resolveView: resolveView };
  }

  /* ============ DOM 装配层（仅浏览器） ============ */
  if (typeof document === 'undefined') return;

  var AUTH_KEY = 'user_page_auth';

  function loadAuth() {
    try {
      var raw = window.sessionStorage.getItem(AUTH_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch (e) {
      return null;
    }
  }

  function saveAuth(auth) {
    window.sessionStorage.setItem(AUTH_KEY, JSON.stringify(auth));
  }

  function clearAuth() {
    window.sessionStorage.removeItem(AUTH_KEY);
  }

  function showView(name) {
    var login = document.getElementById('view-login');
    var chat = document.getElementById('view-chat');
    login.classList.toggle('active', name === 'login');
    chat.classList.toggle('active', name === 'chat');
    document.getElementById('topbar-user').hidden = name === 'login';
    if (name === 'chat') {
      document.getElementById('user-name').textContent =
        (store.auth && store.auth.user && store.auth.user.name) || '';
    }
  }

  var store = createInitialStore();
  store.auth = loadAuth();
  store.view = resolveView(window.location.hash, !!store.auth);
  showView(store.view);

  window.addEventListener('hashchange', function () {
    store.view = resolveView(window.location.hash, !!store.auth);
    showView(store.view);
  });

  document.getElementById('btn-logout').addEventListener('click', function () {
    clearAuth();
    store.auth = null;
    window.location.hash = '#/login';
    store.view = resolveView(window.location.hash, false);
    showView(store.view);
  });

  /* __WIRING_RUNTIME__ */
})();
</script>
</body>
</html>
```

- [ ] **Step 4: 运行确认通过**

Run: `$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest tests/test_user_page.py -v`
Expected: PASS（4 个测试；Node 可用时逻辑测试也过）。

- [ ] **Step 5: Commit（用户执行）**

```bash
cd "D:\AI_Project\AI Service Agent"
git add web/index.html tests/test_user_page.py
git commit -m "feat: 用户页面骨架 + 视图路由 + 鉴权守卫（Task 1）"
```

### Task 2: store 与事件处理（applyEvent 全 13 种事件）

**Files:**
- Modify: `web/index.html`（在 `/* __APPLY_EVENT__ */` 锚点插入实现；更新 `window.UserPage` 暴露对象）
- Test: `tests/test_user_page.py`（追加测试）

**Interfaces:**
- Consumes: `createInitialStore()`（Task 1）
- Produces:
  - `EVENT_TYPES: string[]` —— 13 种事件，与 `docs/specs/event-spec.md` §2 一一对应
  - `applyEvent(store, event) -> newStore` —— **不可变**：返回新 store，不修改入参
  - messages 元素：`{ id, role: 'user'|'assistant'|'system', kind: 'text'|'tool'|'clarify'|'confirm'|'approval'|'notice'|'handoff'|'error', content, meta }`
  - `pendingAction`：`{ kind:'confirm', status:'pending'|'confirmed'|'canceled', summary }` 或 `{ kind:'approval', id, status, summary, expires_at }`

- [ ] **Step 1: 追加失败测试**

在 `tests/test_user_page.py` 末尾追加：

```python
def test_apply_token_appends_streaming_text(tmp_path):
    out = _run_js(
        "UserPage.applyEvent(UserPage.applyEvent(UserPage.createInitialStore(), "
        "{type:'token',data:{text:'您'}}), {type:'token',data:{text:'好'}})",
        tmp_path,
    )
    assert out["messages"][-1]["kind"] == "text"
    assert out["messages"][-1]["content"] == "您好"
    assert out["conn"]["status"] == "streaming"


def test_apply_done_ends_turn(tmp_path):
    out = _run_js(
        "UserPage.applyEvent(UserPage.applyEvent(UserPage.createInitialStore(), "
        "{type:'token',data:{text:'x'}}), {type:'done',data:{final:true}})",
        tmp_path,
    )
    assert out["conn"]["status"] == "idle"


def test_apply_tool_call_then_result(tmp_path):
    out = _run_js(
        "UserPage.applyEvent(UserPage.applyEvent(UserPage.applyEvent(UserPage.createInitialStore(), "
        "{type:'tool_call',data:{name:'order_lookup',args:{order_id:'A1001'}}}), "
        "{type:'tool_result',data:{name:'order_lookup',ok:true,summary:'已发货'}}), "
        "{type:'done',data:{}})",
        tmp_path,
    )
    tool = [m for m in out["messages"] if m["kind"] == "tool"][0]
    assert tool["content"]["name"] == "order_lookup"
    assert tool["meta"] == {"phase": "result", "ok": True, "summary": "已发货"}


def test_apply_clarify_and_safety(tmp_path):
    out = _run_js(
        "var a=UserPage.createInitialStore();"
        "var b=UserPage.applyEvent(a,{type:'clarification_required',data:{missing:['order_id']}});"
        "var c=UserPage.applyEvent(b,{type:'safety_rejected',data:{rule:'refund_blacklist',reason:'超期'}});"
        "({kinds:c.messages.map(function(m){return m.kind;})})",
        tmp_path,
    )
    assert out["kinds"] == ["clarify", "notice"]


def test_apply_confirm_and_update(tmp_path):
    out = _run_js(
        "var a=UserPage.createInitialStore();"
        "var b=UserPage.applyEvent(a,{type:'confirmation_required',data:{summary:'修改收货地址'}});"
        "var c=UserPage.applyEvent(b,{type:'confirmation_updated',data:{action:'confirmed'}});"
        "({pa:c.pendingAction,meta:c.messages[0].meta})",
        tmp_path,
    )
    assert out["pa"]["kind"] == "confirm" and out["pa"]["status"] == "confirmed"
    assert out["meta"]["status"] == "confirmed"


def test_apply_approval_and_update(tmp_path):
    out = _run_js(
        "var a=UserPage.createInitialStore();"
        "var b=UserPage.applyEvent(a,{type:'approval_required',data:{approval_id:'ap-1',summary:'取消订单',expires_at:'2026-09-17T23:59:59Z'}});"
        "var c=UserPage.applyEvent(b,{type:'approval_updated',data:{approval_id:'ap-1',status:'approved'}});"
        "({pa:c.pendingAction,meta:c.messages[0].meta})",
        tmp_path,
    )
    assert out["pa"] == {
        "kind": "approval", "id": "ap-1", "status": "approved",
        "summary": "取消订单", "expires_at": "2026-09-17T23:59:59Z",
    }
    assert out["meta"]["status"] == "approved"


def test_apply_handoff_cycle(tmp_path):
    out = _run_js(
        "var a=UserPage.createInitialStore();"
        "var b=UserPage.applyEvent(a,{type:'handoff_created',data:{reason:'情绪升级'}});"
        "var c=UserPage.applyEvent(b,{type:'handoff_resolved',data:{}});"
        "({mode:c.conversation.mode,status:c.conn.status,states:c.messages.map(function(m){return m.meta.state;})})",
        tmp_path,
    )
    assert out == {"mode": "ai", "status": "idle", "states": ["created", "resolved"]}


def test_apply_error_sets_lasterror(tmp_path):
    out = _run_js(
        "UserPage.applyEvent(UserPage.createInitialStore(),"
        "{type:'error',data:{code:'E_TIMEOUT',message:'超时'}})",
        tmp_path,
    )
    assert out["conn"]["lastError"] == {"code": "E_TIMEOUT", "message": "超时"}
    assert out["messages"][-1]["kind"] == "error"


def test_apply_event_immutable(tmp_path):
    out = _run_js(
        "var a=UserPage.createInitialStore();"
        "var b=UserPage.applyEvent(a,{type:'handoff_created',data:{}});"
        "({before:a.conversation.mode,after:b.conversation.mode,msgs:b.messages.length})",
        tmp_path,
    )
    assert out == {"before": "ai", "after": "human", "msgs": 1}


def test_event_types_cover_spec(tmp_path):
    out = _run_js("UserPage.EVENT_TYPES", tmp_path)
    assert sorted(out) == sorted([
        "token", "tool_call", "tool_result", "clarification_required",
        "confirmation_required", "confirmation_updated", "approval_required",
        "approval_updated", "safety_rejected", "handoff_created",
        "handoff_resolved", "error", "done",
    ])
```

- [ ] **Step 2: 运行确认失败**

Run: `$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest tests/test_user_page.py -v`
Expected: 新增用例 FAIL——`UserPage.applyEvent is not a function`。

- [ ] **Step 3: 实现 applyEvent**

把 `/* __APPLY_EVENT__ */` 替换为以下代码（`msgId` 为纯逻辑层内部辅助函数）：

```js
  var EVENT_TYPES = [
    'token', 'tool_call', 'tool_result', 'clarification_required',
    'confirmation_required', 'confirmation_updated', 'approval_required',
    'approval_updated', 'safety_rejected', 'handoff_created',
    'handoff_resolved', 'error', 'done',
  ];

  function msgId() {
    return 'm-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 8);
  }

  function applyEvent(store, event) {
    var type = event.type;
    var data = event.data || {};
    var s = JSON.parse(JSON.stringify(store));
    s.conn.lastError = null;

    switch (type) {
      case 'token':
        s.conn.status = 'streaming';
        var last = s.messages[s.messages.length - 1];
        if (last && last.role === 'assistant' && last.kind === 'text') {
          last.content += data.text || '';
        } else {
          s.messages.push({ id: msgId(), role: 'assistant', kind: 'text', content: data.text || '', meta: {} });
        }
        break;
      case 'tool_call':
        s.conn.status = 'streaming';
        s.messages.push({ id: msgId(), role: 'assistant', kind: 'tool',
          content: { name: data.name, args: data.args || {} }, meta: { phase: 'call' } });
        break;
      case 'tool_result':
        for (var i = s.messages.length - 1; i >= 0; i--) {
          if (s.messages[i].kind === 'tool' && s.messages[i].meta.phase === 'call') {
            s.messages[i].meta = { phase: 'result', ok: !!data.ok, summary: data.summary || '' };
            break;
          }
        }
        break;
      case 'clarification_required':
        s.messages.push({ id: msgId(), role: 'assistant', kind: 'clarify',
          content: { missing: data.missing || [], reason: data.reason || null }, meta: {} });
        break;
      case 'confirmation_required':
        s.pendingAction = { kind: 'confirm', status: 'pending', summary: data.summary || '' };
        s.messages.push({ id: msgId(), role: 'assistant', kind: 'confirm',
          content: { summary: data.summary || '' }, meta: { status: 'pending' } });
        break;
      case 'confirmation_updated':
        var confirmStatus = data.action === 'confirmed' ? 'confirmed' : 'canceled';
        if (s.pendingAction && s.pendingAction.kind === 'confirm') s.pendingAction.status = confirmStatus;
        for (var j = s.messages.length - 1; j >= 0; j--) {
          if (s.messages[j].kind === 'confirm') { s.messages[j].meta.status = confirmStatus; break; }
        }
        break;
      case 'approval_required':
        s.pendingAction = { kind: 'approval', id: data.approval_id, status: 'pending',
          summary: data.summary || '', expires_at: data.expires_at || null };
        s.messages.push({ id: msgId(), role: 'assistant', kind: 'approval',
          content: { approval_id: data.approval_id, summary: data.summary || '', expires_at: data.expires_at || null },
          meta: { status: 'pending' } });
        break;
      case 'approval_updated':
        var approvalStatus = data.status || 'pending';
        if (s.pendingAction && s.pendingAction.kind === 'approval' && s.pendingAction.id === data.approval_id) {
          s.pendingAction.status = approvalStatus;
        }
        for (var k = s.messages.length - 1; k >= 0; k--) {
          if (s.messages[k].kind === 'approval') { s.messages[k].meta.status = approvalStatus; break; }
        }
        break;
      case 'safety_rejected':
        s.messages.push({ id: msgId(), role: 'assistant', kind: 'notice',
          content: { rule: data.rule || '', reason: data.reason || '' }, meta: {} });
        break;
      case 'handoff_created':
        s.conversation.mode = 'human';
        s.conn.status = 'polling';
        s.messages.push({ id: msgId(), role: 'system', kind: 'handoff',
          content: { reason: data.reason || '', summary: data.summary || '' }, meta: { state: 'created' } });
        break;
      case 'handoff_resolved':
        s.conversation.mode = 'ai';
        s.conn.status = 'idle';
        s.messages.push({ id: msgId(), role: 'system', kind: 'handoff', content: {}, meta: { state: 'resolved' } });
        break;
      case 'error':
        s.conn.lastError = { code: data.code || 'E_UNKNOWN', message: data.message || '未知错误' };
        s.messages.push({ id: msgId(), role: 'system', kind: 'error',
          content: { code: data.code || 'E_UNKNOWN', message: data.message || '未知错误' }, meta: {} });
        break;
      case 'done':
        s.conn.status = s.conversation.mode === 'human' ? 'polling' : 'idle';
        break;
    }
    return s;
  }
```

同时把 `window.UserPage` 暴露对象更新为：

```js
  if (typeof window !== 'undefined') {
    window.UserPage = {
      createInitialStore: createInitialStore,
      resolveView: resolveView,
      applyEvent: applyEvent,
      EVENT_TYPES: EVENT_TYPES,
    };
  }
```

- [ ] **Step 4: 运行确认通过**

Run: `$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest tests/test_user_page.py -v`
Expected: 全部 PASS（结构测试 4 + 逻辑测试 14）。

- [ ] **Step 5: Commit（用户执行）**

```bash
cd "D:\AI_Project\AI Service Agent"
git add web/index.html tests/test_user_page.py
git commit -m "feat: store + applyEvent 全 13 种事件（Task 2）"
```

### Task 3: buildView 渲染层（vnode）+ DOM mount

**Files:**
- Modify: `web/index.html`（在 `/* __BUILD_VIEW__ */` 锚点插入；`window.UserPage` 暴露对象追加 `buildView`；DOM 装配层追加 `applyView` / `mountNode`）
- Test: `tests/test_user_page.py`（追加测试）

**Interfaces:**
- Consumes: `applyEvent` / `createInitialStore`（Task 1–2）
- Produces:
  - `buildView(store) -> view`，view 形状：
    - `headerStatus: { text, cls: 'ai'|'human', connText }`
    - `chips: vnode[]`（5 个快捷指令：新会话 / 查订单 / 改地址 / 申请售后 / 转人工，`data-cmd` 携带指令文本）
    - `messages: vnode[]`（按消息 kind 映射，见下）
    - `inputDisabled: boolean`（`streaming` / `reconnecting` 时为 true）
  - vnode 形状：`{ tag: string, attrs: object, children: (vnode|string)[] }`
  - DOM 层：`applyView()`（用 `buildView(store)` 更新 `#status-tag` / `#conn-text` / `#chips` / `#msg-list` / `#msg-input` / `#btn-send`）、`mountNode(vnode) -> Element`

消息 kind → vnode 映射（对齐 spec §5）：

| kind | 表现 |
|---|---|
| `text` | `.msg.user` / `.msg.assistant` 气泡 |
| `tool` | `.card.tool-card`：`执行中…` / `✓ summary` / `✗ summary` |
| `clarify` | `.card`：`需要补充信息 · 请提供：<missing 列表>` |
| `confirm` | `.card`：摘要 + pending 时 `[确认][取消]`（`data-action` + `data-msg-id`），非 pending 显示 `已确认`/`已取消` |
| `approval` | `.card`：`高风险操作 · 人工审批中` + 摘要 + 状态（审批中/已通过/已拒绝/已过期）+ 到期时间，**无按钮**（用户侧不审批） |
| `notice` | `.notice`：`安全拦截：<reason>` |
| `handoff` | `.handoff-banner`：created → `已转人工客服：<summary>`；resolved → `人工服务已结束，AI 客服继续为您服务` |
| `error` | `.error-bar`：`[code] message` |

- [ ] **Step 1: 追加失败测试**

在 `tests/test_user_page.py` 末尾追加：

```python
def test_build_view_header_status(tmp_path):
    out = _run_js("UserPage.buildView(UserPage.createInitialStore()).headerStatus", tmp_path)
    assert out == {"text": "AI 在线", "cls": "ai", "connText": "连接正常"}


def test_build_view_header_human_after_handoff(tmp_path):
    out = _run_js(
        "UserPage.buildView(UserPage.applyEvent(UserPage.createInitialStore(), "
        "{type:'handoff_created',data:{}})).headerStatus",
        tmp_path,
    )
    assert out == {"text": "人工接待中", "cls": "human", "connText": "当前由人工客服接待"}


def test_build_view_input_disabled_on_streaming(tmp_path):
    out = _run_js(
        "var a=UserPage.createInitialStore();"
        "var b=UserPage.applyEvent(a,{type:'token',data:{text:'x'}});"
        "var c=UserPage.applyEvent(b,{type:'done',data:{}});"
        "({streaming:UserPage.buildView(b).inputDisabled,idle:UserPage.buildView(c).inputDisabled})",
        tmp_path,
    )
    assert out == {"streaming": True, "idle": False}


def test_build_view_chips_and_cmds(tmp_path):
    out = _run_js(
        "UserPage.buildView(UserPage.createInitialStore()).chips.map(function(n){return n.attrs['data-cmd'];})",
        tmp_path,
    )
    assert out == ["重新开始", "查询我的订单", "修改收货地址", "我要申请售后", "转人工客服"]


def test_build_view_confirm_card_buttons(tmp_path):
    out = _run_js(
        "var a=UserPage.createInitialStore();"
        "var b=UserPage.applyEvent(a,{type:'confirmation_required',data:{summary:'修改收货地址'}});"
        "var c=UserPage.buildView(b);"
        "({kind:c.messages[0].tag,actions:c.messages[0].children[2].children.map(function(x){return x.attrs['data-action'];})})",
        tmp_path,
    )
    assert out == {"kind": "div", "actions": ["confirm", "cancel"]}


def test_build_view_confirm_card_settled(tmp_path):
    out = _run_js(
        "var a=UserPage.createInitialStore();"
        "var b=UserPage.applyEvent(a,{type:'confirmation_required',data:{summary:'x'}});"
        "var c=UserPage.applyEvent(b,{type:'confirmation_updated',data:{action:'confirmed'}});"
        "var v=UserPage.buildView(c);"
        "var hasButton=false;"
        "function scan(n){if(n&&n.tag==='button')hasButton=true;if(n&&n.children)(n.children||[]).forEach(scan);}"
        "scan(v.messages[0]);"
        "({hasButton:hasButton,last:v.messages[0].children[2].children[0]})",
        tmp_path,
    )
    assert out["hasButton"] is False
    assert out["last"] == "已确认"


def test_build_view_approval_no_buttons(tmp_path):
    out = _run_js(
        "var a=UserPage.createInitialStore();"
        "var b=UserPage.applyEvent(a,{type:'approval_required',data:{approval_id:'ap-1',summary:'取消订单',expires_at:'2026-09-17T23:59:59Z'}});"
        "var v=UserPage.buildView(b);"
        "({title:v.messages[0].children[0].children[0],status:v.messages[0].children[2].children[0]})",
        tmp_path,
    )
    assert out["title"] == "高风险操作 · 人工审批中"
    assert "审批中" in out["status"]
    assert "2026-09-17T23:59:59Z" in out["status"]


def test_build_view_full_event_stream(tmp_path):
    out = _run_js(
        "var s=UserPage.createInitialStore();"
        "var events=[{type:'token',data:{text:'您好'}},"
        "{type:'tool_call',data:{name:'order_lookup',args:{order_id:'A1'}}},"
        "{type:'tool_result',data:{name:'order_lookup',ok:true,summary:'已发货'}},"
        "{type:'clarification_required',data:{missing:['order_id']}},"
        "{type:'confirmation_required',data:{summary:'修改地址'}},"
        "{type:'safety_rejected',data:{rule:'r',reason:'超期'}},"
        "{type:'error',data:{code:'E_1',message:'超时'}},"
        "{type:'done',data:{}}];"
        "for(var i=0;i<events.length;i++){s=UserPage.applyEvent(s,events[i]);}"
        "var v=UserPage.buildView(s);"
        "({count:v.messages.length,classes:v.messages.map(function(m){return m.attrs['class'];})})",
        tmp_path,
    )
    assert out["count"] == 6
    assert out["classes"] == [
        "msg assistant", "card tool-card", "card", "card", "notice", "error-bar",
    ]
```

- [ ] **Step 2: 运行确认失败**

Run: `$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest tests/test_user_page.py -v`
Expected: 新增用例 FAIL——`UserPage.buildView is not a function`。

- [ ] **Step 3: 实现 buildView**

把 `/* __BUILD_VIEW__ */` 替换为以下代码：

```js
  var QUICK_CMDS = [
    { label: '新会话', cmd: '重新开始' },
    { label: '查订单', cmd: '查询我的订单' },
    { label: '改地址', cmd: '修改收货地址' },
    { label: '申请售后', cmd: '我要申请售后' },
    { label: '转人工', cmd: '转人工客服' },
  ];

  function statusLabel(status) {
    if (status === 'approved') return '已通过';
    if (status === 'rejected') return '已拒绝';
    if (status === 'expired') return '已过期';
    return '审批中';
  }

  function messageNode(m) {
    switch (m.kind) {
      case 'text':
        return { tag: 'div', attrs: { class: 'msg ' + m.role }, children: [m.content] };
      case 'tool':
        return { tag: 'div', attrs: { class: 'card tool-card' }, children: [
          { tag: 'h4', children: ['工具 · ' + m.content.name] },
          { tag: 'p', children: [m.meta.phase === 'result'
            ? (m.meta.ok ? '✓ ' + m.meta.summary : '✗ ' + m.meta.summary)
            : '执行中…'] },
        ] };
      case 'clarify':
        return { tag: 'div', attrs: { class: 'card' }, children: [
          { tag: 'h4', children: ['需要补充信息'] },
          { tag: 'p', children: ['请提供：' + m.content.missing.join('、')] },
        ] };
      case 'confirm':
        return { tag: 'div', attrs: { class: 'card' }, children: [
          { tag: 'h4', children: ['请确认操作'] },
          { tag: 'p', children: [m.content.summary] },
          m.meta.status === 'pending'
            ? { tag: 'div', attrs: { class: 'row' }, children: [
                { tag: 'button', attrs: { class: 'btn primary', 'data-action': 'confirm', 'data-msg-id': m.id }, children: ['确认'] },
                { tag: 'button', attrs: { class: 'btn ghost', 'data-action': 'cancel', 'data-msg-id': m.id }, children: ['取消'] },
              ] }
            : { tag: 'p', children: [m.meta.status === 'confirmed' ? '已确认' : '已取消'] },
        ] };
      case 'approval':
        return { tag: 'div', attrs: { class: 'card' }, children: [
          { tag: 'h4', children: ['高风险操作 · 人工审批中'] },
          { tag: 'p', children: [m.content.summary] },
          { tag: 'p', children: ['状态：' + statusLabel(m.meta.status) +
            (m.content.expires_at ? '（' + m.content.expires_at + ' 到期）' : '')] },
        ] };
      case 'notice':
        return { tag: 'div', attrs: { class: 'notice' }, children: ['安全拦截：' + m.content.reason] };
      case 'handoff':
        return m.meta.state === 'created'
          ? { tag: 'div', attrs: { class: 'handoff-banner' }, children: ['已转人工客服' + (m.content.summary ? '：' + m.content.summary : '')] }
          : { tag: 'div', attrs: { class: 'handoff-banner' }, children: ['人工服务已结束，AI 客服继续为您服务'] };
      case 'error':
        return { tag: 'div', attrs: { class: 'error-bar' }, children: ['[' + m.content.code + '] ' + m.content.message] };
    }
  }

  function buildView(store) {
    var human = store.conversation.mode === 'human';
    var reconnecting = store.conn.status === 'reconnecting';
    return {
      headerStatus: human
        ? { text: '人工接待中', cls: 'human', connText: '当前由人工客服接待' }
        : { text: 'AI 在线', cls: 'ai', connText: reconnecting ? '连接中断，重连中…' : '连接正常' },
      chips: QUICK_CMDS.map(function (c) {
        return { tag: 'button', attrs: { class: 'chip', 'data-cmd': c.cmd }, children: [c.label] };
      }),
      messages: store.messages.map(messageNode),
      inputDisabled: store.conn.status === 'streaming' || store.conn.status === 'reconnecting',
    };
  }
```

在 DOM 装配层（`typeof document === 'undefined'` 守卫之后）追加 `mountNode` / `applyView`，并把每次 `store` 变更后调用 `applyView()`：

```js
  function mountNode(node) {
    if (typeof node === 'string') return document.createTextNode(node);
    var el = document.createElement(node.tag);
    for (var key in node.attrs) {
      if (key === 'class') el.className = node.attrs[key];
      else el.setAttribute(key, node.attrs[key]);
    }
    node.children.forEach(function (child) { el.appendChild(mountNode(child)); });
    return el;
  }

  function applyView() {
    var v = UserPage.buildView(store);
    var tag = document.getElementById('status-tag');
    tag.textContent = v.headerStatus.text;
    tag.className = 'status-tag ' + v.headerStatus.cls;
    document.getElementById('conn-text').textContent = v.headerStatus.connText;
    var chips = document.getElementById('chips');
    chips.innerHTML = '';
    v.chips.forEach(function (node) { chips.appendChild(mountNode(node)); });
    var list = document.getElementById('msg-list');
    list.innerHTML = '';
    v.messages.forEach(function (node) { list.appendChild(mountNode(node)); });
    document.getElementById('msg-input').disabled = v.inputDisabled;
    document.getElementById('btn-send').disabled = v.inputDisabled;
    list.scrollTop = list.scrollHeight;
  }
```

`window.UserPage` 暴露对象追加 `buildView: buildView`。初始渲染：在 DOM 装配层最后调用 `applyView()`。

- [ ] **Step 4: 运行确认通过**

Run: `$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest tests/test_user_page.py -v`
Expected: 全部 PASS。

- [ ] **Step 5: Commit（用户执行）**

```bash
cd "D:\AI_Project\AI Service Agent"
git add web/index.html tests/test_user_page.py
git commit -m "feat: buildView vnode 渲染层 + DOM mount（Task 3）"
```

### Task 4: api 层 + mock 模式 + 登录 / 发送装配

**Files:**
- Modify: `web/index.html`（在 `/* __API_LAYER__ */` 锚点插入；`window.UserPage` 暴露对象追加 `createApi` / `createMockApi` / `mockStream`；`/* __WIRING_RUNTIME__ */` 处实现登录 / 发送 / 确认按钮装配）
- Modify: `tests/test_user_page.py`（`_run_js` 的 runner 升级为 async 版并扩展 sandbox；末尾追加测试）

**Interfaces:**
- Consumes: `applyEvent`（Task 2）、`buildView` / `applyView` / `msgId`（Task 3）
- Produces:
  - `createApi(config) -> { login, createSession, getMessages, sendMessage, confirm, approve }`（全部返回 Promise；`config = { baseUrl, token, fetchImpl }`）
  - `createMockApi() -> 同形状`（离线 fake；`sendMessage` 用 `mockStream()` 逐条回调 `onEvent`）
  - `mockStream() -> event[]`（覆盖 EVENT_TYPES 全部 13 种，演示脚本）
  - 后端契约**占位**（TODO，spec §10）：`POST {base}/auth/login`、`POST {base}/sessions`、`POST {base}/chat`（SSE）、`GET {base}/conversations/{id}/messages`、`POST {base}/actions/confirm`、`POST {base}/approvals/{id}`
  - 轮询响应契约（占位）：`{ messages: [...], mode: 'ai'|'human' }`（Task 5 消费）

- [ ] **Step 1: 升级测试辅助并追加失败测试**

把 `tests/test_user_page.py` 中 `_run_js` 的 runner 文本替换为（支持 `await`，注入 Node 全局）：

```python
    runner.write_text(
        "const fs=require('fs');const vm=require('vm');\n"
        "const sandbox={window:{},console,setTimeout,clearTimeout,setInterval,clearInterval,\n"
        "ReadableStream,TextEncoder,TextDecoder};\n"
        "vm.runInNewContext(fs.readFileSync(__dirname+'/site.js','utf8'),sandbox);\n"
        "const {UserPage}=sandbox.window;\n"
        "const expr=fs.readFileSync(__dirname+'/expr.js','utf8');\n"
        "(async()=>{console.log(JSON.stringify(await eval(expr)));})()"
        ".catch(e=>{console.error(e);process.exit(1);});\n",
        encoding="utf-8",
    )
```

文件末尾追加：

```python
def test_mock_stream_covers_all_event_types(tmp_path):
    out = _run_js(
        "Array.from(new Set(UserPage.mockStream().map(function(e){return e.type;}))).sort()",
        tmp_path,
    )
    assert out == sorted([
        "token", "tool_call", "tool_result", "clarification_required",
        "confirmation_required", "confirmation_updated", "approval_required",
        "approval_updated", "safety_rejected", "handoff_created",
        "handoff_resolved", "error", "done",
    ])


def test_mock_api_login_returns_token(tmp_path):
    out = _run_js("(await UserPage.createMockApi().login('u1', 'p1')).token", tmp_path)
    assert out == "mock-token"


def test_api_login_posts_credentials(tmp_path):
    expr = (
        "var sent=null;"
        "var fake={ok:true,status:200,json:async function(){return {token:'t'};}};"
        "var api=UserPage.createApi({fetchImpl:async function(url,opts){sent={url:url,opts:opts};return fake;},baseUrl:'http://x'});"
        "var r=await api.login('a','b');"
        "({token:r.token,url:sent.url,body:sent.opts.body})"
    )
    out = _run_js(expr, tmp_path)
    assert out["token"] == "t"
    assert out["url"] == "http://x/auth/login"
    assert json.loads(out["body"]) == {"account": "a", "password": "b"}


def test_api_send_message_parses_sse(tmp_path):
    expr = (
        "var enc=new TextEncoder();"
        "var stream=new ReadableStream({start:function(c){"
        "c.enqueue(enc.encode('data: {\"type\":\"token\",\"data\":{\"text\":\"你\"}}\\n\\n'));"
        "c.enqueue(enc.encode('data: {\"type\":\"done\",\"data\":{}}\\n\\n'));"
        "c.close();}});"
        "var fake={ok:true,status:200,body:stream,json:async function(){return {};}};"
        "var api=UserPage.createApi({fetchImpl:async function(){return fake;},baseUrl:'http://x'});"
        "var got=[];"
        "await api.sendMessage('c1','hi',function(e){got.push(e.type);});"
        "got"
    )
    out = _run_js(expr, tmp_path)
    assert out == ["token", "done"]


def test_api_create_session_throws_on_401(tmp_path):
    expr = (
        "var api=UserPage.createApi({fetchImpl:async function(){"
        "return {ok:false,status:401,json:async function(){return {};}};},baseUrl:'http://x'});"
        "var err=null;try{await api.createSession();}catch(e){err=e.message;}"
        "err"
    )
    out = _run_js(expr, tmp_path)
    assert out == "SESSION_FAILED 401"
```

- [ ] **Step 2: 运行确认失败**

Run: `$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest tests/test_user_page.py -v`
Expected: 新增用例 FAIL——`UserPage.createMockApi is not a function`。

- [ ] **Step 3: 实现 api 层与 mock**

把 `/* __API_LAYER__ */` 替换为以下代码：

```js
  function createApi(config) {
    var base = (config && config.baseUrl) || '';
    var token = (config && config.token) || null;
    var fetchImpl = (config && config.fetchImpl) || (typeof fetch !== 'undefined' ? fetch : null);

    function headers() {
      var h = { 'Content-Type': 'application/json' };
      if (token) h['Authorization'] = 'Bearer ' + token;
      return h;
    }

    // TODO(后端契约): 路径为占位，真实对接时与后端对齐（spec §10）。
    async function login(account, password) {
      var r = await fetchImpl(base + '/auth/login', {
        method: 'POST', headers: headers(),
        body: JSON.stringify({ account: account, password: password }),
      });
      if (!r.ok) throw new Error('LOGIN_FAILED ' + r.status);
      return await r.json();
    }

    async function createSession() {
      var r = await fetchImpl(base + '/sessions', { method: 'POST', headers: headers() });
      if (!r.ok) throw new Error('SESSION_FAILED ' + r.status);
      return await r.json();
    }

    async function getMessages(conversationId) {
      var r = await fetchImpl(base + '/conversations/' + encodeURIComponent(conversationId) + '/messages', { headers: headers() });
      if (!r.ok) throw new Error('MESSAGES_FAILED ' + r.status);
      return await r.json();
    }

    async function sendMessage(conversationId, text, onEvent) {
      var r = await fetchImpl(base + '/chat', {
        method: 'POST', headers: headers(),
        body: JSON.stringify({ conversation_id: conversationId, text: text }),
      });
      if (!r.ok || !r.body) throw new Error('SEND_FAILED ' + r.status);
      var reader = r.body.getReader();
      var decoder = new TextDecoder();
      var buf = '';
      while (true) {
        var chunk = await reader.read();
        if (chunk.done) break;
        buf += decoder.decode(chunk.value, { stream: true });
        var idx;
        while ((idx = buf.indexOf('\n\n')) >= 0) {
          var block = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          block.split('\n').forEach(function (line) {
            if (line.indexOf('data: ') === 0) onEvent(JSON.parse(line.slice(6)));
          });
        }
      }
      return { ok: true };
    }

    // TODO(后端契约): 动作接口为占位路径。
    async function confirm(conversationId, action) {
      var r = await fetchImpl(base + '/actions/confirm', {
        method: 'POST', headers: headers(),
        body: JSON.stringify({ conversation_id: conversationId, action: action }),
      });
      if (!r.ok) throw new Error('CONFIRM_FAILED ' + r.status);
      return await r.json();
    }

    async function approve(approvalId, verdict) {
      var r = await fetchImpl(base + '/approvals/' + encodeURIComponent(approvalId), {
        method: 'POST', headers: headers(),
        body: JSON.stringify({ verdict: verdict }),
      });
      if (!r.ok) throw new Error('APPROVE_FAILED ' + r.status);
      return await r.json();
    }

    return {
      login: login, createSession: createSession, getMessages: getMessages,
      sendMessage: sendMessage, confirm: confirm, approve: approve,
    };
  }

  function createMockApi() {
    var seatReplied = false;
    return {
      async login(account) {
        return { token: 'mock-token', user: { name: account || '演示用户' } };
      },
      async createSession() {
        return { conversation_id: 'mock-1', mode: 'ai' };
      },
      async getMessages() {
        if (!seatReplied) {
          seatReplied = true;
          return { messages: [{ id: 'seat-1', role: 'assistant', text: '人工客服：您好，您的退款申请已加急处理，预计 24 小时内到账。' }], mode: 'human' };
        }
        return { messages: [], mode: 'ai' };
      },
      async sendMessage(conversationId, text, onEvent) {
        // mock 演示流去掉 handoff_resolved：让转人工后的轮询真实跑起来（getMessages 第二次返回 mode=ai 模拟人工解决）
        var events = mockStream().filter(function (e) { return e.type !== 'handoff_resolved'; });
        for (var i = 0; i < events.length; i++) onEvent(events[i]);
        return { ok: true };
      },
      async confirm() { return { ok: true }; },
      async approve() { return { ok: true }; },
    };
  }

  function mockStream() {
    return [
      { type: 'token', data: { text: '您好，我是智家小助手，请问有什么可以帮您？\n可以查订单、改收货信息、申请售后。' } },
      { type: 'tool_call', data: { name: 'order_lookup', args: { order_id: 'A1001' } } },
      { type: 'tool_result', data: { name: 'order_lookup', ok: true, summary: '订单 A1001：已发货，预计明天送达。' } },
      { type: 'clarification_required', data: { missing: ['order_id'] } },
      { type: 'confirmation_required', data: { summary: '修改订单 A1001 收货地址为：北京市朝阳区 xx 路 1 号' } },
      { type: 'confirmation_updated', data: { action: 'confirmed' } },
      { type: 'approval_required', data: { approval_id: 'ap-1', summary: '取消订单 A1001（金额 ¥1,299.00）', expires_at: '2026-09-17T23:59:59Z' } },
      { type: 'approval_updated', data: { approval_id: 'ap-1', status: 'approved' } },
      { type: 'safety_rejected', data: { rule: 'refund_blacklist', reason: '该订单已超出 7 天无理由退款期限' } },
      { type: 'handoff_created', data: { reason: '用户情绪升级', summary: '用户多次要求人工介入' } },
      { type: 'handoff_resolved', data: {} },
      { type: 'error', data: { code: 'E_TIMEOUT', message: '服务响应超时，请重试' } },
      { type: 'done', data: { final: true } },
    ];
  }
```

`window.UserPage` 暴露对象追加 `createApi` / `createMockApi` / `mockStream`。

把 `/* __WIRING_RUNTIME__ */` 替换为登录 / 发送 / 快捷指令 / 确认按钮装配：

```js
  function getConfig() {
    var mock = false;
    try {
      mock = new URLSearchParams(window.location.search).get('mock') === 'true' ||
        window.localStorage.getItem('mock') === 'true';
    } catch (e) { /* ignore */ }
    return { mock: mock, baseUrl: '' };
  }

  store.config = getConfig();
  var api = store.config.mock
    ? createMockApi()
    : createApi({ fetchImpl: window.fetch ? window.fetch.bind(window) : null, baseUrl: store.config.baseUrl });

  function pushUserMessage(text) {
    store.messages.push({ id: msgId(), role: 'user', kind: 'text', content: text, meta: {} });
    store.conn.status = 'streaming';
    applyView();
  }

  function pushHistory(messages) {
    messages.forEach(function (m) {
      store.messages.push({
        id: m.id || msgId(),
        role: m.role === 'user' ? 'user' : 'assistant',
        kind: 'text',
        content: m.text || m.content || '',
        meta: {},
      });
    });
    applyView();
  }

  async function loadHistory() {
    try {
      var data = await api.getMessages(store.conversation.conversation_id);
      if (data && data.messages) pushHistory(data.messages);
    } catch (e) { /* 历史加载失败不阻塞对话 */ }
  }

  function doSend() {
    var input = document.getElementById('msg-input');
    var text = input.value.trim();
    if (!text) return;
    input.value = '';
    pushUserMessage(text);
    api.sendMessage(store.conversation.conversation_id, text, function (event) {
      store = applyEvent(store, event);
      applyView();
    }).catch(function (e) {
      store = applyEvent(store, { type: 'error', data: { code: 'E_SEND', message: e.message } });
      applyView();
    }).then(function () {
      if (store.conversation.mode === 'human' && typeof startPolling === 'function') startPolling();
    });
  }

  async function doLogin() {
    var account = document.getElementById('login-account').value;
    var password = document.getElementById('login-password').value;
    var errEl = document.getElementById('login-error');
    errEl.hidden = true;
    try {
      var auth = await api.login(account, password);
      saveAuth(auth);
      store.auth = auth;
      var session = await api.createSession();
      store.conversation.conversation_id = session.conversation_id || session.session_id;
      store.conversation.mode = session.mode || 'ai';
      window.location.hash = '#/chat';
      store.view = resolveView('#/chat', true);
      showView(store.view);
      applyView();
      loadHistory();
    } catch (e) {
      errEl.textContent = '登录失败：' + e.message;
      errEl.hidden = false;
    }
  }

  document.getElementById('btn-login').addEventListener('click', doLogin);
  document.getElementById('btn-send').addEventListener('click', doSend);
  document.getElementById('msg-input').addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter' && !ev.shiftKey) { ev.preventDefault(); doSend(); }
  });

  document.getElementById('chips').addEventListener('click', function (ev) {
    var chip = ev.target.closest('button.chip');
    if (!chip) return;
    document.getElementById('msg-input').value = chip.getAttribute('data-cmd');
    doSend();
  });

  document.getElementById('msg-list').addEventListener('click', function (ev) {
    var btn = ev.target.closest('button[data-action]');
    if (!btn) return;
    var action = btn.getAttribute('data-action') === 'confirm' ? 'confirmed' : 'canceled';
    if (!store.pendingAction || store.pendingAction.kind !== 'confirm') return;
    api.confirm(store.conversation.conversation_id, action)
      .then(function () {
        store = applyEvent(store, { type: 'confirmation_updated', data: { action: action } });
        applyView();
      })
      .catch(function (e) {
        store = applyEvent(store, { type: 'error', data: { code: 'E_CONFIRM', message: e.message } });
        applyView();
      });
  });

  if (store.view === 'chat') applyView();
```

> 说明：`doSend` 末尾 `typeof startPolling === 'function'` 守卫——`startPolling` 由 Task 5 提供，本任务尚未定义时静默跳过，保证 Task 4 可独立测试与提交。

- [ ] **Step 4: 运行确认通过**

Run: `$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest tests/test_user_page.py -v`
Expected: 全部 PASS（新增 5 个用例；Task 1–3 用例保持通过）。

- [ ] **Step 5: Commit（用户执行）**

```bash
cd "D:\AI_Project\AI Service Agent"
git add web/index.html tests/test_user_page.py
git commit -m "feat: api 层占位 + mock 模式 + 登录/发送装配（Task 4）"
```

### Task 5: 人工车道轮询 + 断线重连

**Files:**
- Modify: `web/index.html`（在 `/* __POLL_LAYER__ */` 锚点插入纯逻辑；`window.UserPage` 暴露对象追加 `nextPollDelay` / `markReconnecting` / `applyPollResult`；DOM 装配层追加轮询 / 重连；`doSend` 的 catch 改为重连逻辑；退出登录时清理定时器）
- Test: `tests/test_user_page.py`（追加测试）

**Interfaces:**
- Consumes: `applyEvent` / `createApi` / `createMockApi`（Task 2 / 4）
- Produces:
  - `nextPollDelay(attempt: int) -> int`（指数退避：`1000 * 2^attempt`，封顶 15000ms）
  - `markReconnecting(store, message) -> newStore`（`conn.status='reconnecting'`，`lastError={code:'E_RECONNECT', message}`）
  - `applyPollResult(store, data) -> newStore`（`data = { messages, mode }`：追加坐席消息 `meta.seat=true`；`mode==='ai'` 且当前为 human 时回切 `mode='ai'` + `conn.status='idle'` + 追加 `handoff`(resolved) 消息；否则 `conn.status='polling'`）
  - DOM 层：`startPolling()` / `stopPolling()` / `pollOnce()` / `scheduleReconnect(text, attempt)`

- [ ] **Step 1: 追加失败测试**

文件末尾追加：

```python
def test_next_poll_delay_exponential_cap(tmp_path):
    out = _run_js(
        "[0,1,2,3,4,5,20].map(function(a){return UserPage.nextPollDelay(a);})",
        tmp_path,
    )
    assert out == [1000, 2000, 4000, 8000, 15000, 15000, 15000]


def test_apply_poll_result_appends_seat_message(tmp_path):
    out = _run_js(
        "var a=UserPage.applyEvent(UserPage.createInitialStore(),{type:'handoff_created',data:{}});"
        "var b=UserPage.applyPollResult(a,{messages:[{id:'s1',role:'assistant',text:'人工客服：您好'}],mode:'human'});"
        "({mode:b.conversation.mode,status:b.conn.status,last:b.messages[b.messages.length-1]})",
        tmp_path,
    )
    assert out["mode"] == "human"
    assert out["status"] == "polling"
    assert out["last"]["role"] == "assistant"
    assert out["last"]["meta"]["seat"] is True


def test_apply_poll_result_resolves_handoff(tmp_path):
    out = _run_js(
        "var a=UserPage.applyEvent(UserPage.createInitialStore(),{type:'handoff_created',data:{}});"
        "var b=UserPage.applyPollResult(a,{messages:[],mode:'ai'});"
        "({mode:b.conversation.mode,status:b.conn.status,last:b.messages[b.messages.length-1]})",
        tmp_path,
    )
    assert out["mode"] == "ai"
    assert out["status"] == "idle"
    assert out["last"]["meta"]["state"] == "resolved"


def test_mark_reconnecting(tmp_path):
    out = _run_js(
        "var s=UserPage.markReconnecting(UserPage.createInitialStore(),'连接中断');"
        "({status:s.conn.status,err:s.conn.lastError})",
        tmp_path,
    )
    assert out["status"] == "reconnecting"
    assert out["err"]["code"] == "E_RECONNECT"
```

- [ ] **Step 2: 运行确认失败**

Run: `$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest tests/test_user_page.py -v`
Expected: 新增用例 FAIL——`UserPage.nextPollDelay is not a function`。

- [ ] **Step 3: 实现轮询与重连**

把 `/* __POLL_LAYER__ */` 替换为：

```js
  function nextPollDelay(attempt) {
    var ms = 1000 * Math.pow(2, attempt);
    return ms > 15000 ? 15000 : ms;
  }

  function markReconnecting(store, message) {
    var s = JSON.parse(JSON.stringify(store));
    s.conn.status = 'reconnecting';
    s.conn.lastError = { code: 'E_RECONNECT', message: message || '连接中断，重连中…' };
    return s;
  }

  function applyPollResult(store, data) {
    var s = JSON.parse(JSON.stringify(store));
    (data.messages || []).forEach(function (m) {
      s.messages.push({
        id: m.id || msgId(),
        role: m.role === 'user' ? 'user' : 'assistant',
        kind: 'text',
        content: m.text || m.content || '',
        meta: { seat: true },
      });
    });
    if (data.mode === 'ai' && s.conversation.mode === 'human') {
      s.conversation.mode = 'ai';
      s.conn.status = 'idle';
      s.messages.push({ id: msgId(), role: 'system', kind: 'handoff', content: {}, meta: { state: 'resolved' } });
    } else {
      s.conn.status = 'polling';
    }
    return s;
  }
```

DOM 装配层（`__WIRING_RUNTIME__` 区域末尾）追加：

```js
  var pollTimer = null;
  var pollAttempt = 0;
  var reconnectTimer = null;

  function startPolling() {
    if (pollTimer !== null || store.conversation.mode !== 'human') return;
    pollAttempt = 0;
    schedulePoll();
  }

  function stopPolling() {
    if (pollTimer !== null) { clearTimeout(pollTimer); pollTimer = null; }
  }

  function schedulePoll() {
    if (store.conversation.mode !== 'human') { pollTimer = null; return; }
    pollTimer = setTimeout(pollOnce, nextPollDelay(pollAttempt));
  }

  async function pollOnce() {
    if (store.conversation.mode !== 'human') { pollTimer = null; return; }
    try {
      var data = await api.getMessages(store.conversation.conversation_id);
      pollAttempt = 0;
      store = applyPollResult(store, data);
      applyView();
      if (store.conversation.mode === 'ai') { pollTimer = null; return; }
    } catch (e) {
      pollAttempt = Math.min(pollAttempt + 1, 10);
    }
    schedulePoll();
  }

  function scheduleReconnect(text, attempt) {
    if (reconnectTimer !== null) clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(function () {
      reconnectTimer = null;
      api.sendMessage(store.conversation.conversation_id, text, function (event) {
        store = applyEvent(store, event);
        applyView();
      }).then(function () {
        if (store.conversation.mode === 'human') startPolling();
      }).catch(function () {
        store = markReconnecting(store, '连接中断，' + nextPollDelay(attempt + 1) + 'ms 后重试');
        applyView();
        scheduleReconnect(text, attempt + 1);
      });
    }, nextPollDelay(attempt));
  }
```

把 Task 4 中 `doSend` 的失败分支替换为：

```js
    }).catch(function () {
      store = markReconnecting(store, '连接中断，正在重连…');
      applyView();
      scheduleReconnect(text, 0);
    }).then(function () {
      if (store.conversation.mode === 'human' && typeof startPolling === 'function') startPolling();
    });
```

退出登录处理器（Task 1 的 `btn-logout` click 内）在 `clearAuth()` 之前追加：

```js
    stopPolling();
    if (reconnectTimer !== null) { clearTimeout(reconnectTimer); reconnectTimer = null; }
```

`window.UserPage` 暴露对象追加 `nextPollDelay` / `markReconnecting` / `applyPollResult`。

- [ ] **Step 4: 运行确认通过**

Run: `$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest tests/test_user_page.py -v`
Expected: 全部 PASS。

- [ ] **Step 5: Commit（用户执行）**

```bash
cd "D:\AI_Project\AI Service Agent"
git add web/index.html tests/test_user_page.py
git commit -m "feat: 人工车道轮询 + 断线重连（Task 5）"
```

### Task 6: 验证收尾 + 索引登记

**Files:**
- Modify: `docs/plans/README.md`（登记本计划）
- 无代码改动（验证通过后即可）

- [ ] **Step 1: 全量回归**

Run: `$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest tests/test_user_page.py -v`
Expected: 全部 PASS（结构 + 逻辑；无 Node 时逻辑测试 skip，结构测试仍过）。

- [ ] **Step 2: 浏览器手测清单（mock 模式）**

1. 启动预览：`uv run python -m http.server 8000 --directory web`，打开 `http://localhost:8000/?mock=true`。
2. 未登录访问 `#/chat` → 重定向登录视图。
3. 任意账号密码登录 → 进入对话视图，顶栏显示用户名。
4. 发送任意消息 → 依次看到：流式文本 → 工具卡片（`执行中…` → `✓ 已发货`）→ 澄清卡片（`请提供：order_id`）→ 确认卡片 → 点「确认」→ 卡片变「已确认」→ 审批卡片（`人工审批中` + 到期时间）→ 安全拦截 notice → 转人工横幅 + 标签「人工接待中」→ 轮询刷出坐席回复 → 「人工服务已结束」横幅 → error-bar。
5. 转人工后继续发送消息：输入框可用，消息进入消息流（mock 下无坐席响应，符合占位）。
6. 刷新页面 → 历史消息保留（Message Store 重建，mock 下为本地 store）。
7. 浏览器控制台无报错；`streaming` 期间输入框禁用，`done` 后恢复。

- [ ] **Step 3: 登记计划索引**

`docs/plans/README.md` 追加一行：

```markdown
- `2026-09-17-user-page-frontend.md` — 用户侧 AI 客服页面（单文件 HTML）实现计划（Task 1–6，TDD，每任务 commit），对应 `docs/specs/user-page-spec.md`。
```

- [ ] **Step 4: 最终 Commit（用户执行）**

```bash
cd "D:\AI_Project\AI Service Agent"
git add docs/plans/README.md
git commit -m "docs: 登记用户页面实现计划（Task 6）"
```
