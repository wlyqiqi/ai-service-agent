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

## 3. 接口与职责分工（FastAPI 端点 + Approval Service）

### 3.1 职责分工

- **恢复执行后的状态转移决策必须在图内完成**：由 ResumeHandler 节点（或发起 `interrupt()` 的节点自身的续接逻辑）执行，不允许挪到 Conversation Runtime 或任何 harness 组件。
- **Approval Service = 轻量基础设施状态存储**（定位同 conversation_status）：只持久化 `pending / approved / rejected / expired` 状态与 `expires_at` 时间戳，供 UI 查询展示、供 HTTP 端点做前置有效性检查；不承担任何业务决策。
- **FastAPI 端点只做「前置校验 + 转发」**：不判断"批准 / 拒绝意味着什么"，该判断由图内部完成。
- **ApprovalRequest 落库**：HighRiskGraph 发起审批时通过 `interrupt()` 携带审批数据；harness 在观察到该 interrupt 时经 hook 写入 Approval Service（纯持久化，见 `architecture.md` §8.1）。
- **过期状态**："超期"是纯时间比较（`now > expires_at`）的机械结论，由 Approval Service 在读取 / 前置检查时惰性落地为 `expired`；"超期后业务如何收尾"的决策在图内（见 §4）。

### 3.2 端点行为（FastAPI）

| 方法 | 路径 | 行为 |
|---|---|---|
| POST | `/approvals/{id}/approve` | 前置检查 → 有效则 `graph.ainvoke(Command(resume={"action":"approve"}), config={"thread_id": ...})` |
| POST | `/approvals/{id}/reject` | 前置检查 → 有效则 `graph.ainvoke(Command(resume={"action":"reject", "note": ...}), config={"thread_id": ...})`（`note` 来自请求体） |
| GET | `/approvals/{id}` | 查询状态（Approval Service 只读） |
| GET | `/approvals?status=pending` | 人工端列表（Approval Service 只读） |

- **前置有效性检查（Approval Service 只读）**：approval 存在；status 仍为 `pending`；`now <= expires_at`。任一不满足 → 直接返回错误（`not_found` / `already_processed` / `expired`），**不调用图**；重复提交同一 `approval_id` 由此天然幂等。
- **机械映射，非业务判断**：`approve → {action: "approve"}`、`reject → {action: "reject", note}` 仅是把 HTTP 动作映射为 `graph-spec.md` §3 的 resume schema；"批准后执行什么 / 拒绝后如何收尾"由图中子图决定。
- 审批动作写审计日志：who / what / when / result（harness 观测，经 hook 挂接）。

### 3.3 恢复入口（殊途同归）

- 端点路径：FastAPI → 前置校验 → `graph.ainvoke(Command(resume=...), config={"thread_id": ...})`，resume schema 见 `graph-spec.md` §3。
- 用户 turn 路径：`/chat` 新消息 → 图内入口条件边 → ResumeHandler 解析 → 同一 resume schema。
- 两条入口的状态转移决策都在图内完成；harness 仅负责传输与前置校验。

## 4. 超时策略

- `expires_at` 由配置决定（默认 30 分钟，可配）。
- 图内不轮询：下个 turn 进入时若已过期，ResumeHandler 以 `Command(resume={"result":"expired"})` 恢复（schema 见 `graph-spec.md` §3），HighRiskGraph 按过期分支收尾。
- 过期审批在服务侧标记 `expired`，人工端不再展示可操作项（惰性机械落库，见 §3.1，不构成业务决策）。

## 5. 人工端（v1）

- CLI 命令或简单页面：列出 pending、approve/reject、查看摘要。
- 测试环境提供 `simulate_approval=True` 开关（自动批准），仅测试用；该开关走与真实端点相同的前置检查 + resume 转发路径（自动构造 approve 动作），不改变"决策在图内"的分工。

## 6. 与安全预检/转人工的边界

- 审批拒绝 ≠ 自动转人工；是否转人工按 `safety-spec.md` 的 handoff 规则判定。
- 审批是 HighRisk 的 gate；`safety-spec.md` 的预检是**所有**写/高险操作的 gate，先预检后审批，顺序不可颠倒。
