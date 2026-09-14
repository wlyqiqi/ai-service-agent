# Handoff 待裁决项定案（2026-09-14）

> 定案来源：Claude 建议 + 用户确认（MVP 阶段）。
> 关联：`docs/specs/handoff-spec.md` §5.1 · `docs/decisions/2026-09-14-handoff-understanding-check.md`。

## 1. conversation_id / thread_id / session_id 映射

- **决定**：`conversation_id == session_id`（业务 / 契约层会话标识，面向用户与坐席，同一值）；`thread_id` 是 LangGraph checkpoint 内部标识，与 `conversation_id` 建立**一对一映射但非同一字段**。
- 会话创建时生成 `thread_id`（如 uuid）并持久化 `conversation_id ↔ thread_id` 映射；API 契约继续用 `session_id`，图调用用 `config={"thread_id": ...}`。
- **收益**：同一业务会话可在 LangGraph 层重置执行状态（如严重异常后清空 graph 重来但保留业务会话）——`thread_id` 可换、`conversation_id` 不变，保留技术退路。

## 2. human → ai 切换时 pending 审批归宿

- **决定（MVP）**：**继续挂起，等 AI 重新接管后经图内 ResumeHandler / ACTIVE 判断自然恢复**。
- **理由**：实现成本最低（无需给坐席开发"代为审批"界面与权限体系）；业务上合理——审批应由触发它的原流程收尾，人工客服只处理沟通，不代为做业务决策。
- **兼容性**：人工接管期间审批到期仍按 `approval-spec.md` §4 惰性过期处理。

> 修订（2026-09-14，P1）：恢复机制已由 `docs/decisions/2026-09-14-checkpointer-resume-handoff.md` 定案——入口层 resume 调度 + 图内被中断节点续接（原"图内 ResumeHandler / ACTIVE"表述被取代）；pending 审批归宿语义不变。

## 未决（保留）

- LangGraph `state.messages` 与 canonical Message Store 谁是唯一真相源——仍待裁决（`handoff-spec.md` §5.2）。

> 已裁决（2026-09-14）：Message Store 为唯一真相源，`state.messages` 为水合工作视图，见 `docs/decisions/2026-09-14-message-store-truth-source.md`。
