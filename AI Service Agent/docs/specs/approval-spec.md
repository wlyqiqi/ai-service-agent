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

- **恢复执行后的状态转移决策必须在图内完成**：由发起 `interrupt()` 的节点自身的续接逻辑执行（经入口层 resume 调度恢复，见 `graph-spec.md` §3），不允许挪到 Conversation Runtime 或任何 harness 组件。
- **Approval Service = 轻量基础设施状态存储**（定位同 conversation_status）：只持久化 `pending / approved / rejected / expired` 状态与 `expires_at` 时间戳，供 UI 查询展示、供 HTTP 端点做前置有效性检查；不承担任何业务决策。
- **FastAPI 端点只做「前置校验 + 转发」**：不判断"批准 / 拒绝意味着什么"，该判断由图内部完成。
- **ApprovalRequest 落库**：HighRiskGraph 发起审批时通过 `interrupt()` 携带审批数据；harness 在观察到该 interrupt 时经 hook 写入 Approval Service（纯持久化，见 `architecture.md` §8.1）。
- **过期状态**："超期"是纯时间比较（`now > expires_at`）的机械结论，由 Approval Service 在读取 / 前置检查时惰性落地为 `expired`；"超期后业务如何收尾"的决策在图内（见 §4）。

- **P3 实现（D8 定案）**：SQLite 轻量表（pending/approved/rejected/expired + expires_at）+ FastAPI 端点（approve/reject/GET，只做前置有效性检查 + 转发）+ `simulate_approval` 测试开关（走同一路径）；业务决策在图内。**迁移触发条件（登记）**：出现多进程/容器化部署需求时，将 Approval Service 迁移至独立存储（如 Postgres），并同步评估线程锁与 checkpointer 的迁移（类比 checkpointer SQLite→Postgres），避免"先用着，后面才想起要迁移"。

### 3.2 端点行为（FastAPI）

| 方法 | 路径 | 行为 |
|---|---|---|
| POST | `/approvals/{id}/approve` | 前置检查 → 有效则 `graph.ainvoke(Command(resume={"action":"approve"}), config={"thread_id": ...})` |
| POST | `/approvals/{id}/reject` | 前置检查 → 有效则 `graph.ainvoke(Command(resume={"action":"reject", "note": ...}), config={"thread_id": ...})`（`note` 来自请求体） |
| GET | `/approvals/{id}` | 查询状态（Approval Service 只读） |
| GET | `/approvals?status=pending` | 人工端列表（Approval Service 只读） |

- **前置有效性检查（Approval Service 只读）**：approval 存在；status 仍为 `pending`；`now <= expires_at`。任一不满足 → 直接返回错误（`not_found` / `already_processed` / `expired`），**不调用图**；重复提交同一 `approval_id` 由此天然幂等。
- **职责分离（SoD）**：前置检查通过之后、写终态之前，approve / reject 的决策人必须是去空白后非空、且不等于 `anonymous`（大小写不敏感）的身份，并且不得等于 `requester_user_id`。不满足 → HTTP 403 `approval separation of duties`，不调用图，记录保持 `pending`。
  - `AUTH_ENABLED` 代码默认 `true`。开启时决策人只取 JWT `sub`；缺少或无效 token 为 401。请求头 `X-Approver-User-Id` 被忽略，申请人不能伪造成他人。
  - 本地与测试必须显式 `AUTH_ENABLED=false`（pytest 在 `tests/conftest.py`）。此模式下没有可信登录身份，决策必须带 `X-Approver-User-Id`。空头、`anonymous`、或与申请人相同一律 403，不能靠“未登录”绕过。该请求头不是认证，不得用于共享环境。
  - GET 与列表在认证开启时仍只对申请人可见。决策路径不再要求决策人等于申请人。
  - `simulate_approval` 使用 `decided_by=simulate`（与申请人不同），不走 HTTP 自审批。
- **机械映射，非业务判断**：`approve → {action: "approve"}`、`reject → {action: "reject", note}` 仅是把 HTTP 动作映射为 `graph-spec.md` §3 的 resume schema；"批准后执行什么 / 拒绝后如何收尾"由图中子图决定。
- 审批动作写审计日志：who / what / when / result（harness 观测，经 hook 挂接）。`decided_by` 记录实际决策人，而不是固定占位字符串。

### 3.3 恢复入口（殊途同归）

- **resume schema 唯一（`graph-spec.md` §3）**：`Command(resume= { "action": "approve" | "reject" | "confirm" | "cancel" | "handoff_resumed", "note"?: str } | { "result": "expired" })`。`handoff_resumed` 于 P1 加入（handoff 中断的主动恢复），与审批 / 用户确认 / 超时**共用同一 schema，不设游离分支**。
- 端点路径：FastAPI → 前置校验 → `graph.ainvoke(Command(resume=...), config={"configurable": {"thread_id": ...}})`，resume schema 见 `graph-spec.md` §3。
- 用户 turn 路径：`/chat` 新消息 → 入口层 resume 调度（`aget_state()` 检测挂起中断并构造同一 resume schema；见 `graph-spec.md` §3）→ 图内被中断节点续接。
- 两条入口的状态转移决策都在图内完成；harness 仅负责传输、前置校验与 resume 调度。

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
