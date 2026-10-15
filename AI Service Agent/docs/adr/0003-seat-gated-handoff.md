# Seat-gated handoff (request ≠ human lane)

客户或图内转人工先落 **Handoff Request**（挂起），Conversation Mode 仍为 `ai`；仅当坐席 **Seat Takeover**（Chatwoot Take over / status `open`）才进 `human`；交回 Bot 是 status 回到 `pending`（兼容：Resolve 后再 `toggle_status(pending)`）。无申请时坐席主动 Take over 同样进 `human`。客户申请/取消均入口层确定性检测（无 Router）；申请 turn 只回挂起确认。强提示 = private note + label `handoff-request`。事件：`handoff_requested` / `handoff_created` / `handoff_resolved`。Take over 后发 Handoff Joined Notice。进入 human 时取消未提交的 Mutation Form。

选用 Seat-gated（申请挂起 ≠ human）而非「一请求就切 human」、亦非「申请/取消进 Router」，使 AI 未接入前不断档、意图门控保持确定性，并对齐 Chatwoot Take over / Resolve。

**Status:** accepted（2026-10-15）

## Spec / ops

- 术语：根目录 `CONTEXT.md`（Conversation Mode / Handoff Request / Seat Takeover / Handoff Resolve / Handoff Cancel / Handoff Joined Notice）
- 权威生命周期：`docs/specs/handoff-spec.md`
- 事件契约：`docs/specs/event-spec.md`（`handoff_requested` / `handoff_created` on takeover / `handoff_resolved`）
- Account Webhook（非 AgentBot Outgoing URL）：`docs/ops/chatwoot-widget-agentbot-setup.md` §4.1 — 必须订阅 `conversation_status_changed`

## Considered Options

- 一请求即切 `human`（历史 P0）— rejected：无坐席时 Widget 静默
- 申请/取消进 Router — rejected：破坏 Deterministic authority
- 第三 Conversation Mode「waiting」— rejected：挂起用 Handoff record status
- 自定义坐席 Accept 卡 — rejected：用官方 Take over / Resolve
- 依赖 AgentBot Outgoing URL 收 `conversation_status_changed` — rejected：走 Account Webhook

## Consequences

- Runtime `request_handoff` 不得切 mode；Seat Takeover 与激活 handoff 同事务切 `human`
- HTTP resolve 与 webhook `resolved` 共用完成路径；status 变更用 `toggle_status`
- 客户端不得在挂起阶段把 `handoff_created` 当成已接入

## Amendment（2026-10-15）：官方 pending ↔ open

Chatwoot Agent Bot 的持有关系以会话 status 为准，经 **Account Webhook** `conversation_status_changed` 同步 Conversation Mode（不是 AgentBot Outgoing URL）：

- `pending` = Bot 持有（mode `ai`）。mode 已是 `ai` 时忽略，避免 `toggle_status(pending)` 回声成环。
- `open` = 人工持有（mode `human`）。Bot `toggle_status(open)` 或坐席 Take over / 派给人都会到这一条。
- 交回 Bot 的权威信号是 status 回到 `pending`（不再 toggle）。`resolved` 仍走同一完成路径并 `toggle_status(pending)`，作为兼容。
- 进入 `human` 时取消未提交的 Mutation Form Interrupt（不写库）。不改 HighRisk 审批，也不改客户表单 UX。
