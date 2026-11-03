# Handoff Request 自动进入人工队列并轮询分配

**Status:** accepted

**Date:** 2026-11-03

## Context

Demo 坐席需要在 AI 阶段查看全部会话，但不希望每条 AI 消息都被分配或弹窗。客户明确请求人工后，才应进入人工队列，并由 Chatwoot 在在线坐席之间轮询分配。

Chatwoot 原生 assignment policy 只处理 `open + unassigned`，且 AgentBot 仍占用会话时不会执行 auto assignment。仅用 Automation 将会话设为 `open` 会保留 `assignee_agent_bot_id`，无法完成分配。

## Decision

- AI 阶段保持 `pending + unassigned`，坐席通过“全部会话”和“待处理 Bot”视图查看。
- 客户请求人工时，Runtime 写入 `handoff-request` 标签和 private note。
- Chatwoot Automation 将带标签的会话加入“人工接管队列”团队。
- Runtime 随后使用 AgentBot access token 调用 Chatwoot 官方 `toggle_status(open)`，触发 `Conversation#bot_handoff!`，清除 AgentBot 占用。
- 收件箱绑定的 assignment policy 使用 `round_robin`，只从在线团队成员中选择坐席。
- Runtime `conversation_mode` 在收到 `open + User assignee` 后才切为 `human`。
- `open + handoff-request + 无人工 assignee` 是等待自动分配的预期状态，不得按 AgentBot 超时错误拉回 `pending`。
- 无在线坐席时会话保持 `open + unassigned`，由 Chatwoot periodic assignment job 在坐席上线后继续尝试分配。

## Consequences

- 只有明确请求人工的会话才进入轮询分配和坐席通知。
- AI 阶段不产生个人分配通知；坐席仍可通过待处理视图和未读计数查看。
- 本决策替代 ADR-0003 中“Handoff Request 不得改变 Chatwoot status”的部署细则；Runtime 在分配到人工 assignee 前仍保持 `mode=ai`，其余 Seat-gated 正确性约束继续有效。
