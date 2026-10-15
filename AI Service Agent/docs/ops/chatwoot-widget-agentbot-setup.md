# Chatwoot Widget × AgentBot 运维接线（v1）

权威 runbook：从零接好 **Website Channel + AgentBot + Identity HMAC**，并映射到本仓 `Settings` / `.env`。  
产品契约见 `docs/adr/0001-widget-agentbot-form-contract.md`（Contact-first Binding；禁止匿名 Widget）。

> **UI 路径可能随 Chatwoot 版本变化。** 下文同时给出控制台常见路径与 **官方字段名**；以官方文档为准，勿凭记忆编造字段。

## 0. 前置

| 项 | 说明 |
|---|---|
| Chatwoot | 可登录管理员账号的实例（自建或 Cloud） |
| Runtime | 已部署且 Chatwoot 能访问的 HTTPS（或内网穿透）基址，记为 `RUNTIME_BASE` |
| 本仓配置源 | `src/ai_service_agent/config.py` 中 `CHATWOOT_*` 字段 |

AgentBot webhook（本仓已实现）：

```text
POST {RUNTIME_BASE}/webhooks/agent_bot
```

Account Webhook / 坐席回写（人工车道 + **Seat Takeover / Handoff Resolve**；生产必开）：

```text
POST {RUNTIME_BASE}/webhooks/chatwoot
```

> **与 AgentBot Outgoing URL 不是同一通道。**  
> AgentBot `outgoing_url` → `/webhooks/agent_bot`（客户消息进 Runtime）。  
> **Account Webhook** → `/webhooks/chatwoot`（坐席消息回写 + `conversation_status_changed`）。  
> Seat-gated handoff 依赖后者订阅 `conversation_status_changed`（`open` = Seat Takeover，`resolved` = Handoff Resolve）；见 [ADR-0003](../adr/0003-seat-gated-handoff.md) / `docs/specs/handoff-spec.md` §3.1。

## 1. Website Inbox + `website_token`

### 控制台（常见路径）

1. **Settings → Inboxes → Add Inbox → Website**（或 Live Chat / Web Widget）
2. 填写网站 URL、欢迎文案等
3. 创建后在 Inbox → **Configuration** 复制 **Website Token**

### 官方依据

- API 创建 Web Widget Inbox：响应体含 `website_token`  
  <https://developers.chatwoot.com/api-reference/inboxes/create-an-inbox>  
  （`channel.type = web_widget`；响应字段 `website_token`、`web_widget_script`、`hmac_mandatory`）

### 落到 Runtime

| Chatwoot | `.env` | Settings |
|---|---|---|
| Website Token | `CHATWOOT_WEBSITE_TOKEN` | `chatwoot_website_token` |
| Chatwoot 站点根 URL（如 `https://app.chatwoot.com` 或自建） | `CHATWOOT_BASE_URL` | `chatwoot_base_url` |

前端非敏感读取：`GET /config` → `{ chatwoot_base_url, chatwoot_website_token }`（见 `server.py`）。  
参考嵌入：`web/landing-auth-widget.js`（`chatwootSDK.run({ websiteToken, baseUrl })`）。

## 2. Identity Validation（HMAC）— v1 必开

本仓 v1 **禁止匿名**：AgentBot 仅在 `contact.identifier` 非空时建 Runtime 会话（Contact-first Binding）。

### 控制台（常见路径）

1. **Settings → Inboxes →〔该 Website Inbox〕→ Configuration → Identity Validation**
2. **Copy** HMAC / Identity Validation token  
3. 开启 **Require identity validation for all conversations**（对应 API 概念 `hmac_mandatory`）

### 官方依据

- 开启位置与 HMAC token：  
  <https://www.chatwoot.com/hc/user-guide/en/categories/website-live-chat>  
  （Identity Validation；Inbox Configuration）
- HMAC 算法（`HMAC-SHA256(hmac_token, identifier)`）：  
  <https://www.chatwoot.com/docs/product/channels/live-chat/sdk/identity-validation>
- SDK `setUser` + `identifier_hash`：  
  <https://www.chatwoot.com/hc/chatwoot-user-guide-cloud-version/en/categories/website-live-chat>
- Inbox 字段 `hmac_mandatory`：  
  <https://developers.chatwoot.com/api-reference/inboxes/create-an-inbox>
- Contact 侧 `identifier` / `identifier_hash`：  
  <https://developers.chatwoot.com/api-reference/contacts-api/update-a-contact>
- 本地 Widget `setUser` 自测页（开发者向）：  
  <https://developers.chatwoot.com/contributing-guide/setup-guide>

### 落到 Runtime

| Chatwoot | `.env` | Settings |
|---|---|---|
| Identity Validation HMAC token | `CHATWOOT_HMAC_SECRET` | `chatwoot_hmac_secret` |

本仓签名（与官方一致：`HMAC-SHA256(secret, user_id)` → hex）：

- `GET /me/chatwoot_identity`（需登录 JWT）→ `{ identifier, identifier_hash }`
- 前端：`window.$chatwoot.setUser(identifier, { identifier_hash, name, ... })`  
  （见 `web/landing-auth-widget.js`）

未配置 `CHATWOOT_HMAC_SECRET` 时 `/me/chatwoot_identity` 返回 503。

## 3. AgentBot + `outgoing_url`

### 控制台（常见路径）

1. **Settings → Agent Bots**（或 Integrations / Applications → Agent Bot）→ **Add**
2. 设置 **Outgoing URL** = `{RUNTIME_BASE}/webhooks/agent_bot`
3. 将 bot **挂到** 上一步的 Website Inbox
4. 复制 AgentBot 用于出站 webhook 签名的 secret / access token（以你实例 UI 标注为准；须与 Runtime 校验用同一密钥）

### 官方依据

- 创建 Agent Bot（字段 `outgoing_url`）：  
  <https://developers.chatwoot.com/api-reference/account-agentbots/create-an-agent-bot>

### 落到 Runtime

| Chatwoot | `.env` | Settings |
|---|---|---|
| AgentBot webhook 签名密钥 | `CHATWOOT_AGENT_BOT_SECRET` | `chatwoot_agent_bot_secret` |
| Outgoing URL | （填在 Chatwoot，不进 `.env`） | → `POST /webhooks/agent_bot` |

本仓校验（Chatwoot v4.x 契约）：

- Headers：`X-Chatwoot-Signature: sha256=<hex>`、`X-Chatwoot-Timestamp`
- 算法：`HMAC-SHA256(secret, "<timestamp>.<raw body>")`  
  实现：`verify_chatwoot_signature`（`harness/chatwoot.py`）

签名失败 → HTTP 401。

## 4. 坐席 API Inbox（人工车道 / 出站消息）

AgentBot 回写与坐席同步依赖 Chatwoot Application API。

### 控制台（常见路径）

1. Profile / Settings → **Access Token**（API access token）
2. 记下 **Account ID**、Website Inbox 的 **Inbox ID**

### 官方依据

- Application API 使用 `api_access_token` 头：见  
  <https://developers.chatwoot.com/api-reference/inboxes/create-an-inbox>（Authorizations）

### 落到 Runtime

| Chatwoot | `.env` | Settings |
|---|---|---|
| 启用坐席客户端 | `CHATWOOT_ENABLED=true` | `chatwoot_enabled` |
| 实例根 URL | `CHATWOOT_BASE_URL` | `chatwoot_base_url` |
| Account ID | `CHATWOOT_ACCOUNT_ID` | `chatwoot_account_id` |
| Inbox ID（Website） | `CHATWOOT_INBOX_ID` | `chatwoot_inbox_id` |
| API access token | `CHATWOOT_API_TOKEN` | `chatwoot_api_token` |
| 坐席 → Runtime 的 webhook 签名 secret | `CHATWOOT_WEBHOOK_SECRET` | `chatwoot_webhook_secret` |

坐席 / Account Webhook 入口：`POST /webhooks/chatwoot`（与 AgentBot 路径不同；secret 也不同，勿混用）。

### 4.1 Account Webhook：`conversation_status_changed`（Seat-gated 必开）

Seat Takeover / Handoff Resolve **不**走 AgentBot Outgoing URL。须在 Chatwoot **Account** 级 Webhook（Settings → Integrations → Webhooks，或等价 Account Webhooks）配置：

1. **URL** = `{RUNTIME_BASE}/webhooks/chatwoot`
2. **Webhook secret** 与 Runtime `CHATWOOT_WEBHOOK_SECRET` 一致（HMAC 校验与既有坐席回写相同）
3. **订阅事件必须包含** `conversation_status_changed`（以及既有坐席回写所需的 `message_created` 等）
4. Runtime 语义：`open` → Seat Takeover（mode=`human` + `handoff_created` + Handoff Joined Notice）；`resolved` → Handoff Resolve（mode=`ai` + `handoff_resolved` + `toggle_status(pending)`）；`pending` 忽略防环

验收：用 Account API / 控制台确认该 webhook 的 subscriptions **含** `conversation_status_changed`。缺订则 Take over / Resolve 不会同步 Conversation Mode。

脚本（读 `.env` 的 `CHATWOOT_*` + `RUNTIME_BASE`）：

```powershell
$env:RUNTIME_BASE="http://<Chatwoot能访问的Runtime基址>"
uv run --no-sync python scripts/check_seat_gated_webhooks.py
```

期望 exit 0：URL = `{RUNTIME_BASE}/webhooks/chatwoot`，订阅含 `conversation_status_changed`，secret 与 `CHATWOOT_WEBHOOK_SECRET` 一致。

自托管投递到 LAN / `host.docker.internal` 时，Chatwoot + Sidekiq 需 `SAFE_FETCH_ALLOW_PRIVATE_NETWORK=true`（否则 Sidekiq 报 `has no public ip addresses`，Take over / Resolve 不会到达 Runtime）。逐步三角见 [seat-gated-handoff-live-checklist.md](./seat-gated-handoff-live-checklist.md)。

术语：`CONTEXT.md`（Seat Takeover / Handoff Resolve）；规格：`docs/specs/handoff-spec.md` §3.1。

## 5. `.env` 清单（与 `Settings` 1:1）

```env
# --- Widget / AgentBot（C 端主路径）---
CHATWOOT_BASE_URL=https://your-chatwoot.example
CHATWOOT_WEBSITE_TOKEN=               # Inbox website_token
CHATWOOT_HMAC_SECRET=                 # Identity Validation token
CHATWOOT_AGENT_BOT_SECRET=            # AgentBot → Runtime 签名

# --- 坐席 API（出站 bot/form + 人工车道）---
CHATWOOT_ENABLED=true
CHATWOOT_ACCOUNT_ID=
CHATWOOT_INBOX_ID=
CHATWOOT_API_TOKEN=
CHATWOOT_WEBHOOK_SECRET=              # Chatwoot → /webhooks/chatwoot
```

权威定义：`src/ai_service_agent/config.py`（`chatwoot_*` / `validation_alias`）。  
**不要**另造 env 名；改名必须同步改 `Settings` 与本文。

## 6. 嵌入与 Identity（前端）

最小顺序（与 `web/landing-auth-widget.js` 一致）：

1. 用户登录 Runtime → 持 JWT  
2. `GET /config` 取 `chatwoot_website_token` + `chatwoot_base_url`  
3. `GET /me/chatwoot_identity` 取 `identifier` + `identifier_hash`  
4. `chatwootSDK.run({ websiteToken, baseUrl })`  
5. `window.$chatwoot.setUser(identifier, { identifier_hash, name })`  
6. 用户在 Widget 发消息 → Chatwoot → AgentBot `outgoing_url`

无有效 Identity 时，Runtime **拒绝**建会话 / 跑图（#43）。

## 7. 验收清单

- [ ] Inbox 为 Website / `web_widget`，`CHATWOOT_WEBSITE_TOKEN` 已填  
- [ ] Identity Validation 已开且 `CHATWOOT_HMAC_SECRET` 与控制台 token 一致  
- [ ] AgentBot `outgoing_url` = `{RUNTIME_BASE}/webhooks/agent_bot`，且已绑定该 Inbox  
- [ ] `CHATWOOT_AGENT_BOT_SECRET` 校验通过（错误 secret 会 401）  
- [ ] `CHATWOOT_ENABLED` + API 字段齐全；Widget 发「你好」有 bot 回复  
- [ ] AgentBot payload 中 `contact.identifier` = 登录 `user_id`  
- [ ] Account Webhook URL = `{RUNTIME_BASE}/webhooks/chatwoot`，secret = `CHATWOOT_WEBHOOK_SECRET`  
- [ ] Account Webhook **已订阅** `conversation_status_changed`（**不是** AgentBot Outgoing URL）  
- [ ] Seat-gated 三角：客户申请后 AI 仍可回复 → 坐席 Take over → 客户收到 Joined Notice / human 车道 → Resolve 后 AI 恢复（逐步表见 [seat-gated-handoff-live-checklist.md](./seat-gated-handoff-live-checklist.md)）  
- [ ] （可选）历史：转人工 tip / 坐席续接 → 见 issue #58  

## 8. 非权威材料

以下仅历史实现笔记，**不得**替代本文或官方文档：

- `docs/superpowers/plans/2026-10-08-chatwoot-agentbot.md`
- `docs/plans/2026-09-17-chatwoot-handoff-integration.md`

## 9. 相关 issue / ADR

- Spec（已关闭）：[#42](https://github.com/wlyqiqi/ai-service-agent/issues/42)  
- 本文跟踪：[#57](https://github.com/wlyqiqi/ai-service-agent/issues/57)  
- Widget 冒烟（人侧）：[#58](https://github.com/wlyqiqi/ai-service-agent/issues/58)  
- Seat-gated docs：[#80](https://github.com/wlyqiqi/ai-service-agent/issues/80)（父规格 [#79](https://github.com/wlyqiqi/ai-service-agent/issues/79)）  
- Seat-gated ops / live checklist：[#82](https://github.com/wlyqiqi/ai-service-agent/issues/82) · [seat-gated-handoff-live-checklist.md](./seat-gated-handoff-live-checklist.md)  
- ADR：`docs/adr/0001-widget-agentbot-form-contract.md` · `docs/adr/0003-seat-gated-handoff.md`
