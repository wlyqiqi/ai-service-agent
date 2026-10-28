# 坐席审批卡复用 LangGraph HITL 词汇，但不套 `HumanInTheLoopMiddleware`

坐席端高风险审批（`highRiskGraph` 的 `approval_required`）继续用 **LangGraph 原语 `interrupt()` / `Command(resume=...)`** 暂停与恢复，并把审批载荷与决策词汇**对齐官方 HITL**：`action_requests` + `review_configs`，决策为 `approve` / `reject`（带 `message` 作为拒绝理由）/ `edit`（二期）。不另造 `decision` 枚举，也不再自造 `approval_required` 顶层结构。

**不使用 `HumanInTheLoopMiddleware`。** 它只作用于 `create_agent` 的 tool-call 阶段：本项目 HighRisk 是显式 StateGraph（select → 预检 → prepare_approval → interrupt → recheck → execute → finalize），把执行段改成 `create_agent` 会同时丢掉 precheck/recheck 两道业务闸门；且它的 `reject` 会产出一条 `status="error"` 的 `ToolMessage`，与 `finalize` 里「工具 error → `approval_failed_text`」的判定直接冲突（拒绝会被渲染成"执行失败"）。审批记录、TTL、`decided_by` 审计、座位语义本来也不在中间件职责内。若将来 `fallback.py`（唯一的 `create_agent`）需要工具级审批，在那里直接挂中间件即可。

**不使用 Agent Inbox 作为坐席端 UI。** 它是官方独立 Next.js 应用（npm 无可嵌入组件），需要 LangGraph Server deployment URL + LangSmith key；其信息模型是「线程队列」，与 Chatwoot 的「会话 + 坐席分配 + 客户上下文」不一致，也不接管 `approval_record` 的 TTL/幂等/审计。它只作为内部演示面板（旁路，可选）。

审批卡由 Runtime 渲染为 **private note 的 HTML `content`**：Chatwoot 坐席端 `FormBubble` 只渲染 `content` 与已提交值、不渲染 `items` 控件；`cards` 在坐席端无组件（落到 `TextBubble`）。

**Status:** accepted（2026-10-28）

## Considered Options

- **改造成 `create_agent` + `HumanInTheLoopMiddleware`** — rejected：丢 precheck/recheck 闸门；`reject` 的 `ToolMessage(status="error")` 与 `finalize` 冲突；审计/TTL/座位仍需自持，只剩 schema 复用
- **用 Agent Inbox 当坐席端** — rejected：独立应用不可嵌入；需 LangGraph Server + LangSmith；队列模型与会话模型不符
- **给 Chatwoot 打补丁加 `CardsBubble`** — rejected：每次升级重打镜像 + 回归 Vue 组件，长期成本高于收益
- **维持现状（纯文本私备注）** — rejected：坐席端无法操作，实测 3 条审批 0 条被决策
- **继续用 `form` + `items` 承载批准/拒绝** — rejected：坐席端不渲染 items，等于没有入口

## Consequences

- 需要自持两块官方没有的东西：**HTML 卡片渲染**（`harness/approval_card.py`）与**签名决策链接**（`GET/POST /approvals/{id}/decide`）。它们复用既有 `ApprovalSideChannel.decide`，不复制决策逻辑。
- 升级 `langchain` 时需复核 HITL 字段名（`action_requests` / `review_configs` / `allowed_decisions`）；本项目只依赖**字段形状**，不 import 私有模块 `langchain.agents.middleware.human_in_the_loop`（其 TypedDict 未从包顶层导出，属私有路径）。
- 载荷保持可映射到 Agent Inbox 的 `HumanInterrupt`（`action_request` / `config` / `description`），将来要接官方 UI 时只加一层 adapter。
- 卡片是静态 HTML：倒计时不实时刷新（阶段一显示"30 分钟内有效"，阶段二由注入 JS 接管）。
- 客户侧不可见：审批消息必须 `private=true`（ADR-0002 不变）。
