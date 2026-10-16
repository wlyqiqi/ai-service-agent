# Seat-gated handoff — live triangular checklist (#82)

任意已登录客户账号跑一遍三角路径。权威配置见 [chatwoot-widget-agentbot-setup.md](./chatwoot-widget-agentbot-setup.md) §4.1 / §7；Runtime 行为见 `docs/specs/handoff-spec.md` 与 [ADR-0003](../adr/0003-seat-gated-handoff.md)。

## 0. Webhook 订阅（可脚本）

```powershell
$env:UV_CACHE_DIR="<repo>\\.uv-cache"; $env:UV_LINK_MODE="copy"
$env:RUNTIME_BASE="http://<Chatwoot能访问的Runtime基址>"   # 例 http://172.99.46.165:8000
uv run --no-sync python scripts/check_seat_gated_webhooks.py
```

期望：`PASS` — Account Webhook URL = `{RUNTIME_BASE}/webhooks/chatwoot`，订阅含 `conversation_status_changed`，secret = Runtime `CHATWOOT_WEBHOOK_SECRET`。

缺订或 secret 不一致时先修 Chatwoot Settings → Integrations → Webhooks，再继续三角。

自托管 Chatwoot（v4.14+ SafeFetch）若 Sidekiq 报 `Hostname ... has no public ip addresses`，须在 Chatwoot / Sidekiq 环境启用：

```bash
SAFE_FETCH_ALLOW_PRIVATE_NETWORK=true
```

然后重启 `chatwoot` + `chatwoot-sidekiq`。`RUNTIME_BASE` 用 Chatwoot 容器能路由到的宿主机地址（常见：宿主机 LAN IP，如 `http://192.168.x.x:8766`）；`host.docker.internal` 在未开上述开关时会被 SSRF 拦截。

自动化三角（`/chat` + 真实 Chatwoot：先把会话派给 API 用户，再 `toggle_status(open)`，然后设回 pending。**不是** Website Widget UI 点击）。裸 `open`（没有人工 assignee）与 Agent Bot 读超时后的系统自动 open 相同，不会进 human。Widget 登录路径仍按 §1 表手测或补跑。

```powershell
$env:AI_SERVICE_PORT="8766"   # 须与 Account Webhook URL 端口一致
uv run --no-sync python scripts/live_seat_gated_triangular.py
```

## 1. 三角路径（人工 / Widget）

登录任意客户（seed 口令见 `docs/manual-e2e-fullchain-test-cases.md`），打开 Website Widget。

| Step | Actor | Action | Expect |
|---|---|---|---|
| A | Customer | 说「我要转人工」（或等价申请语） | 仅挂起确认 tip；**Conversation Mode 仍为 `ai`**；Chatwoot 出现 private note + label `handoff-request`；会话 status 保持 `pending` |
| B | Customer | 再问一条 FAQ（如「退货政策是什么」） | **AI 仍回复**（非 human 车道静默） |
| C | Seat | **Take over**（官方按钮先 open 再 self-assign；脚本先 assign API 用户再 open） | 客户收到 **Handoff Joined Notice**；Runtime mode=`human`；事件 `handoff_created` |
| D | Customer | 再发一条消息 | human 车道 tip / 无 AI Graph 回复（坐席侧可继续聊） |
| E | Seat | 把 status 设回 **pending**（官方交回 Bot） | Runtime mode=`ai`；**不再** toggle（已经是 pending）；双方可见「已交回助手」；事件 `handoff_resolved` |
| E2 | Seat | （兼容）**Resolve**（status → `resolved`） | Runtime mode=`ai`，并 `toggle_status(pending)`；随后的 pending 回声不再切一次 |
| F | Customer | 再问一条 FAQ | **AI 再次回复** |

## 2. 记录模板

复制到 `docs/reports/live-smoke-seat-gated-handoff-YYYY-MM-DD.md`：

```markdown
# Live smoke — seat-gated handoff (#82)

Date:
Runtime:
Chatwoot:
Login:

| Step | Result | Notes |
|---|---|---|
| 0 Webhook GET | PASS/FAIL | |
| A Request hang | | |
| B AI still replies | | |
| C Take over + Joined Notice | | |
| D Human lane tip | | |
| E pending → ai + 已交回助手 | | |
| F AI resumes | | |
```

## 3. 相关

- Issue：[#82](https://github.com/wlyqiqi/ai-service-agent/issues/82)（父 [#79](https://github.com/wlyqiqi/ai-service-agent/issues/79)）
- 脚本：`scripts/check_seat_gated_webhooks.py`
- 离线门禁：`tests/test_seat_gated_handoff.py`（非本 checklist）
