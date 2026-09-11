# 审批规格（approval-spec.md）

- 关联：`graph-spec.md`（HighRiskGraph 消费方）· `event-spec.md`（事件）· `state-spec.md`（pending_approval 字段）

## 1. 目标

为高风险操作提供**人工审批**通道：操作本身不直接执行，先进入审批流程，人工 approve / reject / 超时后由图恢复继续。

## 2. 审批生命周期

```text
发起(HighRiskGraph interrupt) -> pending
  -> approved -> 图恢复执行
  -> rejected -> 图恢复并拒绝收尾
  -> expired  -> 图恢复按过期收尾（下个 turn 惰性检查）
```

- `ApprovalRequest` 字段：`approval_id` / `type` / `summary`（影响摘要）/ `requester_user_id` / `created_at` / `expires_at` / `status` / `decision` / `decided_by` / `decided_at`。
- 幂等：同一 `approval_id` 重复提交 decision 返回已存在结果，不重复执行。

## 3. 接口（harness 层）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/approvals/{id}/approve` | 批准；`Command(resume={"action":"approve"})`（schema 见 `graph-spec.md` §3） |
| POST | `/approvals/{id}/reject` | 拒绝；`Command(resume={"action":"reject", "note": ...})`，理由入 `note`（schema 见 `graph-spec.md` §3） |
| GET | `/approvals/{id}` | 查询状态 |
| GET | `/approvals?status=pending` | 人工端列表 |

- 审批动作写审计日志：who / what / when / result。
- 审批恢复由 harness 触发图 `invoke(Command(resume=...), thread_id=...)`。

## 4. 超时策略

- `expires_at` 由配置决定（默认 30 分钟，可配）。
- 图内不轮询：下个 turn 进入时若已过期，ResumeHandler 以 `Command(resume={"result":"expired"})` 恢复（schema 见 `graph-spec.md` §3），HighRiskGraph 按过期分支收尾。
- 过期审批在服务侧标记 `expired`，人工端不再展示可操作项。

## 5. 人工端（v1）

- CLI 命令或简单页面：列出 pending、approve/reject、查看摘要。
- 测试环境提供 `simulate_approval=True` 开关（自动批准），仅测试用。

## 6. 与安全预检/转人工的边界

- 审批拒绝 ≠ 自动转人工；是否转人工按 `safety-spec.md` 的 handoff 规则判定。
- 审批是 HighRisk 的 gate；`safety-spec.md` 的预检是**所有**写/高险操作的 gate，先预检后审批，顺序不可颠倒。
