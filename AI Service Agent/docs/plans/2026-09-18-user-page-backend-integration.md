# 用户页面 ↔ 真实后端 契约对齐 + 人工车道端到端测试 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `web/index.html` 的 api 层从占位（`user-page-spec.md` §10）对齐到真实后端契约，使页面在**非 mock 模式**下能连上 Runtime（登录 → 建会话 → AI 对话 → 转人工 → 人工车道收发 → resolve 恢复）；并新增一条**离线端到端人工车道单链路测试**，验证「用户 → Runtime → Chatwoot（坐席）→ webhook → Message Store → 用户轮询」全链路正确。

**Architecture:** 只改 Transport / 页面 api 层，不动 LangGraph 图内逻辑。后端 FastAPI 只做 Transport（AGENTS.md §4）；`Message Store` 是唯一真相源（handoff-spec §5.2）；`conversation_mode` 轻量判断在入口层（handoff-spec §2）。页面在 mock 与真实后端之间按 `config.mock` 切换；后端不可达时保持既有「自动降级 mock」逻辑（基线 `fd01c32`）。

**Tech Stack:** Python 3.14；LangGraph 1.2.10（固定）；FastAPI（Transport）；pytest + TestClient；Node ≥ 18（页面纯逻辑测试）。无新外部依赖。

**Spec:** `docs/specs/user-page-spec.md`（§10 占位接口为对齐基准）· `docs/specs/handoff-spec.md`（§2 mode 两态 / §5.2 Message Store）· `docs/specs/event-spec.md`（事件协议）。

## Contract Decisions（本计划裁定）

- **D1（login 占位端点）**：后端新增 `POST /auth/login` 占位端点，返回 `{"token": "dev-<account>", "user": {"id": <account>, "name": <account>}}`，满足 `user-page-spec.md` §10 的占位契约；真实认证方案定案后替换，不改页面调用方式。页面 api 层 `login()` 继续 `POST /auth/login`（不引入本地 stub）。
- **D2（createSession 对齐）**：页面 `createSession()` 发送 `{"user_id": ...}`（取 `auth.user.id || name`，兜底 `anonymous`）；后端返回 `{session_id, mode}`，页面读取沿用 `conversation_id || session_id` 兜底（现有 `bootstrapSession` 已支持）。
- **D3（/chat 对齐）**：页面 `sendMessage()` 发送 `{"session_id": <conversationId>, "message": <text>}`（后端 `ChatBody(session_id, message)`）。
- **D4（GET messages 对齐）**：后端 `GET /conversations/{id}/messages` 返回体增加 `mode` 字段（`{items, mode}`），供页面刷新后恢复车道；页面 `getMessages()` 归一化为 `{messages: items, mode}` 供 `loadHistory` / `applyPollResult` 消费。
- **D5（动作接口保持占位）**：`confirm` / `approve` / `clarify` 保持占位（真实后端走 `/chat` resume 或 `/approvals/{id}/approve|reject`），**不在本轮实现**——它们属 AI 车道业务操作，不属于「用户 ↔ 坐席」通信链路（人工车道）。后续单独任务对接。
- **D6（E2E 断言基准）**：离线 E2E 用 fake model + `FakeChatwoot`；handoff 触发走确定性规则（`fallback.py check_handoff` 关键词命中 → `interrupt(handoff_request)`）；webhook 用 `verify_chatwoot_signature` 正确签名构造。

## File Structure

- Modify: `src/ai_service_agent/harness/server.py`（`POST /auth/login` 占位 + `GET messages` 返回 `mode`）
- Modify: `web/index.html`（api 层 `createApi`：createSession body、sendMessage body、getMessages 归一化；wiring 传 user 给 createSession）
- Create: `tests/test_e2e_human_lane.py`（人工车道端到端单链路）
- Modify: `tests/test_server_chatwoot.py`（可选：GET messages mode 断言）
- Modify: `tests/test_user_page.py`（api 契约断言）
- Modify: `docs/plans/README.md`（登记本计划）
- Modify: `docs/specs/user-page-spec.md` §10/§11（对齐后移除已对齐项的占位标注；真实后端对接记录）——**仅在 Task 4 确认后执行**

任务分解（每任务独立可测、独立 commit）：

| Task | 内容 | 可测产物 |
|---|---|---|
| 1 | 后端：`POST /auth/login` 占位 + `GET messages` 加 `mode` | `tests/test_server_auth.py` + 既有 chatwoot/messages 测试 |
| 2 | 页面 api 层契约对齐（D2/D3/D4） | `tests/test_user_page.py` 新增契约断言 |
| 3 | 离线人工车道 E2E 单链路 | `tests/test_e2e_human_lane.py` |
| 4 | 全量回归 + spec/README 更新 + 真机验证清单登记 | 全量 pytest + 文档 |

---

### Task 1: 后端 Transport 占位契约（login + messages.mode）

**Files:**
- Modify: `src/ai_service_agent/harness/server.py`
- Create: `tests/test_server_auth.py`

**Interfaces:**
- `POST /auth/login`（body `{account, password}`）→ `{"token", "user": {id, name}}`（占位；user-page-spec §10）
- `GET /conversations/{id}/messages` → `{"items": [...], "mode": "ai"|"human"}`

- [x] **Step 1: 写失败测试**（`tests/test_server_auth.py`）
- [x] **Step 2: 运行确认失败**
- [x] **Step 3: 实现**（server.py：`LoginBody` + `@app.post("/auth/login")`；`get_messages` 返回体加 `mode`）
- [x] **Step 4: 运行确认通过**（含既有 chatwoot/messages 测试不回归）
- [x] **Step 5: Commit**

```bash
cd "D:\AI_Project\AI Service Agent"
git add src/ai_service_agent/harness/server.py tests/test_server_auth.py
git commit -m "feat: Transport 占位契约——POST /auth/login（user-page-spec §10）+ GET messages 返回 mode"
```

### Task 2: 页面 api 层契约对齐

**Files:**
- Modify: `web/index.html`（`createApi` + `bootstrapSession` wiring）
- Modify: `tests/test_user_page.py`

**Interfaces:**
- `createSession(user)` → `POST /sessions` body `{"user_id": ...}`
- `sendMessage(conversationId, text)` → `POST /chat` body `{"session_id": ..., "message": ...}`
- `getMessages(conversationId)` → 归一化 `{messages: data.items, mode: data.mode}`
- mock api 形状不变（向后兼容）

- [x] **Step 1: 写失败测试**（api 层契约断言：body 形状 / items→messages 归一化 / user_id 传递）
- [x] **Step 2: 运行确认失败**
- [x] **Step 3: 实现**
- [x] **Step 4: 运行确认通过**（`tests/test_user_page.py` 全绿）
- [x] **Step 5: Commit**

```bash
git add web/index.html tests/test_user_page.py
git commit -m "feat: 用户页面 api 层契约对齐——createSession/sendMessage/getMessages 对接真实后端（D2/D3/D4）"
```

### Task 3: 离线人工车道端到端单链路

**Files:**
- Create: `tests/test_e2e_human_lane.py`

**链路（TestClient + FakeChatwoot + fake model）：**
1. `POST /sessions` 建会话（mode=ai）
2. `POST /chat "我要转人工"` → SSE 断言 `handoff_created` + `conversation_status.mode=human` + `FakeChatwoot.ensure` 被调
3. `POST /chat "在吗"`（人工车道）→ Message Store 落 user + `FakeChatwoot.send` 被调
4. 构造坐席 webhook（`message_created`, `sender.type=user`, 正确 HMAC 签名）→ Message Store 追加 assistant
5. `GET /conversations/{id}/messages` → 按序返回 user/assistant（含坐席回复）+ `mode=human`
6. `POST /handoffs/{id}/resolve` → `mode=ai` + handoff record resolved；下一条 `/chat` 正常走 AI
- 负向：坏签名 401、非 agent、私信、未知会话、AI mode 的 webhook 一律忽略不落库；Chatwoot 抛错时人工车道仍 200 + `done`

- [x] **Step 1: 写失败测试**
- [x] **Step 2: 运行确认失败**
- [x] **Step 3: 实现（仅测试代码；如暴露后端 bug 则最小修复）**
- [x] **Step 4: 运行确认通过**
- [x] **Step 5: Commit**

```bash
git add tests/test_e2e_human_lane.py
git commit -m "test: 人工车道端到端单链路——用户→Runtime→Chatwoot→webhook→Message Store→轮询→resolve"
```

### Task 4: 全量回归 + 文档登记

- [x] **Step 1: 全量回归**（`uv run --no-sync pytest tests/ -q`，含 test_user_page / server / chatwoot / e2e）
- [x] **Step 2: 更新 `docs/specs/user-page-spec.md` §10/§11**（已对齐项移除占位标注；login/messages 记录真实契约；动作接口保留占位说明）
- [x] **Step 3: `docs/plans/README.md` 登记本计划**
- [x] **Step 4: Commit**

```bash
git add docs/specs/user-page-spec.md docs/plans/README.md
git commit -m "docs: 用户页面↔后端契约对齐登记（user-page-spec §10/§11 + plans README）"
```

## 验证附录（阶段 2/3 真机，非本轮自动执行）

### 阶段 2：真机坐席端链路（Chatwoot + Runtime）
1. `docker compose up -d chatwoot`（:3000）+ `uvicorn` 起 Runtime（:8000），确认 inbox 2 webhook 指向 `http://<IP>:8000/webhooks/chatwoot`
2. `POST /sessions` → `/chat "我要转人工"` → 断言 `handoff_created` + Chatwoot conversation 已建
3. `/chat "在吗"` → Chatwoot API 断言消息（`sender_type=Contact`）
4. Chatwoot 坐席发 outgoing → webhook → `GET messages` 断言回复落库
5. `/handoffs/{id}/resolve` → `mode=ai` + AI 恢复
- 断言点：SSE 事件序、Chatwoot 两侧一致、Message Store 无重复、mode 最终态
- 风险：真实 Router 分类波动可能使「我要转人工」未进 Fallback（联调记录约 1/3），容忍或重试新会话

### 阶段 3：用户端页面真机 E2E（契约对齐后）
- 页面去掉 `?mock=true` 对真实 Runtime 打开：登录（占位）→ 对话 → 转人工 → 页面轮询刷出坐席回复 → 人工结束 → AI 恢复；复用 Chrome headless + CDP 驱动
- 判定：页面无 console 报错、人工车道消息/回复/状态标签全链路正确


## 完成状态（2026-09-18）

- **Task 1–4 全部完成并已提交**：`55c5bc1`（login 占位 + messages.mode）、`553ba56`（页面 api 契约对齐）、
  `1968ce5`（离线人工车道 E2E）、`0a5e5e8`（文档登记）、`fadff66`（真机 10 步复验脚本 + Chatwoot 代理修复）、
  `d1cae7c`（Stage3 页面真机 E2E）。
- **阶段 2（真机坐席端链路）**：人工车道 + 页面契约真机复验 10 步全 PASS（`scripts/real_human_lane_smoke.py`，
  real-model-smoke.md §11）；`ChatwootClient` 修复本机代理拦截（trust_env=False）。
- **阶段 3（用户端页面真机 E2E）**：Chrome headless + CDP 7 步全 PASS（`scripts/page_e2e_smoke.mjs`，
  real-model-smoke.md §12）；配套 `server.py` 静态同源托管 + `GET /handoffs?conversation_id=`、`main.py` host 可配。
- 全量回归：**225 passed**；已推送远端（`d1cae7c`）。
- 仍在占位（计划 D5）：页面 `confirm / clarify / approve` 动作接口属 AI 车道业务操作，留待后续任务。
