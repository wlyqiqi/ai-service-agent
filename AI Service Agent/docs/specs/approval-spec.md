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
| POST | `/approvals/{id}/approve` | 坐席工牌鉴权 → 前置检查 → 有效则 resume approve（见下） |
| POST | `/approvals/{id}/reject` | 坐席工牌鉴权 → 前置检查 → 有效则 resume reject（`note` 来自请求体） |
| GET | `/approvals/{id}` | 客户 JWT：仅本人 Requester 可读；Approval Service 只读 |
| GET | `/approvals?status=pending` | 客户 JWT：仅本人 Requester 行；无客户鉴权时不得全站倾倒 |

- **前置有效性检查（Approval Service 只读）**：approval 存在；status 仍为 `pending`；`now <= expires_at`。任一不满足 → 直接返回错误（`not_found` / `already_processed` / `expired`），**不调用图**；重复提交同一 `approval_id` 由此天然幂等。
- **机械映射，非业务判断**：`approve → {action: "approve"}`、`reject → {action: "reject", note}` 仅是把 HTTP 动作映射为 `graph-spec.md` §3 的 resume schema；"批准后执行什么 / 拒绝后如何收尾"由图中子图决定。
- 审批动作写审计日志：who / what / when / result（harness 观测，经 hook 挂接）；人工 who = `decided_by` 的 `seat:{id}`。

### 3.2.1 身份与授权（ADR-0002）

术语见 `CONTEXT.md`：Requester、Seat Approver、Approval SoD、Approval Side-Channel。

| 动作 | 谁可以 | 备注 |
|---|---|---|
| approve / reject（人工） | 仅 **坐席工牌**：内部服务口令 + 坐席 actor id | 客户 JWT **一律 403**；口令不对或缺少坐席 id → 403 |
| approve（自动） | 仅 `simulate_approval` 路径 | `decided_by=simulate` |
| 过期落地 | 系统惰性 | `decided_by=system` |
| GET 单条 / 列表 | 客户 JWT 且 `requester_user_id` 匹配 | 客户只读自己的单；**不**开放客户写 |

- **Fail-closed**：客户鉴权关闭（`AUTH_ENABLED=false` / 无有效客户身份）时，**禁止**人工 approve/reject（403），只允许 `simulate_approval` 自动批。不得再出现「无 auth 则跳过 ownership、谁都能批」。
- **Approval SoD（v1 通道级）**：客户身份通道永不能 approve；不做坐席↔客户账号绑定表（残余风险见 ADR-0002）。
- **坐席工牌（Seat HTTP badge）**：共享内部 secret（配置）+ 请求携带的坐席 id（对应 Chatwoot `sender.id`）。禁止「仅自报坐席 id、无口令」。未来 Chatwoot 适配器验签后调用**同一**内部决策入口，复用工牌，不另造客户角色列。
- **`decided_by`**：人工 = `seat:{坐席 id}`（`seat:` 即来源命名空间，不内嵌 `chatwoot-agent-`）；保留字 `simulate` / `system`；历史 `"approver"` 为遗留脏值，不得当作坐席 id。

### 3.2.2 审批 resume 横切（与 chat 路径同构）

- **出站 mode 门控**：approve/reject 后立即图 resume + Message Store；向 Chatwoot/Widget **出站**前重读 `conversation_mode`，仅 `ai` 可发送（`handoff-spec` §1.4）。`human` 期间仍可坐席决策。
- **`on_interrupt` / callbacks**：审批 resume **不得**裸 `ainvoke` 以致后续 `approval_required` / `handoff_request` 静默；须挂接与 chat 路径同级的 interrupt 回调（落库 / 建 handoff 等）。

**实现归属（ADR-0002 收敛后）**：执行链唯一归属 `src/ai_service_agent/harness/approval_side_channel.py`（`ApprovalSideChannel`：工牌 → 预检 → `mark_decided` → resume → Message Store / 门控出站；follow-on `approval_required` 在同一事件循环内投影坐席表单）。门控纯函数在 `harness/outbound_gate.decide_delivery`（`TurnOrigin.SEAT_DECISION`：落库不受 mode 影响，仅出站受门控）。Transport（`server.py`）只提供工牌头解析、`resume_runner`、与 `/chat` 共用的 thread lock、interrupt 回调与 Chatwoot 会话映射。

**投影入口单点**：所有 Transport 的 `approval_required` 投影统一走 `ApprovalSideChannel.project()`（`/chat` 事件循环、AgentBot 注入的 `on_approval_pending` 回调、follow-on 中断）。`send_approval_form` 保留为模块内公开薄包装（`project` 的实现 + AgentBot 无回调时的 fallback），**不得**在 Transport 里直接拼表单项。

### 3.3 恢复入口（殊途同归）

- **resume schema 唯一（`graph-spec.md` §3）**：`Command(resume= { "action": "approve" | "reject" | "confirm" | "cancel" | "handoff_resumed", "note"?: str } | { "result": "expired" })`。`handoff_resumed` 于 P1 加入（handoff 中断的主动恢复），与审批 / 用户确认 / 超时**共用同一 schema，不设游离分支**。
- 端点路径：FastAPI → 前置校验 → `graph.ainvoke(Command(resume=...), config={"configurable": {"thread_id": ...}})`，resume schema 见 `graph-spec.md` §3。
- 用户 turn 路径：`/chat` 新消息 → 入口层 resume 调度（`aget_state()` 检测挂起中断并构造同一 resume schema；见 `graph-spec.md` §3）→ 图内被中断节点续接。
- 两条入口的状态转移决策都在图内完成；harness 仅负责传输、前置校验与 resume 调度。

## 4. 超时策略

- `expires_at` 由配置决定（默认 30 分钟，可配）。
- 图内不轮询：下个 turn 进入时若已过期，**入口层 resume 调度**（`aget_state()`）以 `Command(resume={"result":"expired"})` 恢复（schema 见 `graph-spec.md` §3），HighRiskGraph 按过期分支收尾。无图内 ResumeHandler 节点。
- 过期审批在服务侧标记 `expired`，人工端不再展示可操作项（惰性机械落库，见 §3.1，不构成业务决策）。

### 4.1 挂起期间的客户可见性（硬要求）

审批卡只投影到坐席（`private=True`，见 §5），**客户 Widget 看不到审批的存在**。因此：

- **首次挂起**必须给客户一句可见提示：已受理 + 动作 + 订单/金额 + 预计时长。
- **挂起期间的每条客户消息都必须有回应**（「还在审核中」），**禁止静默吞掉**。
  两个 Transport（`/chat` SSE、AgentBot webhook）共用同一文案函数（`approval_wait_notice`），
  不得各写一份。
- 提示只是投影：不 resume、不新起 run；挂起态仍以 checkpointer 的 `__interrupt__` 为准。

> 反例（2026-10-29 实测）：客户提交退款后图停在 `approval_pause`，客户侧零消息；后续
> 「?」「你好」「卧槽」全部无回应，客户以为机器人死了，最后只能点「转人工」。

## 5. 人工端（v1）

- **产品主路径**：Chatwoot 坐席端 **private** 表单/卡片投影 `approval_required`（摘要 + 批准/拒绝）→ 验签后的 `/webhooks/chatwoot` 适配器调用与 §3.2.1 **同一**工牌决策入口（配置口令 + `sender.id` → `decided_by=seat:{id}`）。不得把 Approve/Reject 投到客户 Widget（ADR-0001）。
- **HTTP / 测试路径**：持坐席工牌调 approve/reject；客户 JWT 只读 GET。
- 测试环境提供 `simulate_approval=True` 开关（自动批准），仅测试用；该开关走与真实端点相同的前置检查 + resume 转发路径（自动构造 approve 动作），不改变"决策在图内"的分工；`decided_by=simulate`。

## 6. 与安全预检/转人工的边界

- 审批拒绝 ≠ 自动转人工；是否转人工按 `safety-spec.md` 的 handoff 规则判定。
- 审批是 HighRisk 的 gate；`safety-spec.md` 的预检是**所有**写/高险操作的 gate，先预检后审批，顺序不可颠倒。
