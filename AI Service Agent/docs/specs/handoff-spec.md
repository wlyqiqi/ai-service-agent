# Handoff 规格（handoff-spec.md）

- 日期：2026-09-14（Seat-gated 修订：2026-10-15）
- 状态：权威定义（`conversation_mode` 状态机 + Seat-gated Handoff 生命周期 + 轻量判断）
- 决策：[ADR-0003](../adr/0003-seat-gated-handoff.md)（Seat-gated handoff：申请挂起 ≠ human 车道）
- 术语：根目录 `CONTEXT.md`（Conversation Mode / Handoff Request / Seat Takeover / Handoff Resolve / Handoff Cancel / Handoff Joined Notice）
- 关联：`architecture.md`（入口层 / 持久化）· `graph-spec.md`（子图 `interrupt(handoff_request)`）· `event-spec.md`（`handoff_requested` / `handoff_created` / `handoff_resolved`）· `safety-spec.md`（转人工触发规则）· `state-spec.md`（`execution_status=waiting_handoff`）· `docs/ops/chatwoot-widget-agentbot-setup.md`（Account Webhook）

## 1. conversation_mode 状态机

- 两态：`ai` / `human`（**不设第三态**表示「等坐席」；读取异常不另设 invalid 分支，见 §1.1 兜底说明）。
- 判断规则为两态：`conversation_mode == "human" ? 进入人工车道 : 进入 AI 路径`。
- **挂起 ≠ mode**：Handoff Request 挂起（如 record status `awaiting_seat`）期间 Conversation Mode **仍为 `ai`**；客户后续未命中门控的消息仍走 AI（含 Router）。见 `CONTEXT.md` Conversation Mode / Handoff Request。
- 顶层流程见 `docs/diagrams/handoff-overview.svg`（入口判断 → AI / 人工双车道 → 循环回入口；图中「立即切 human」语义以本文件 Seat-gated 修订为准）。

### 1.1 初始化

- 新会话创建时（`POST /sessions`），`conversation_mode` **必须被显式写入 `"ai"`**，不允许依赖存储层的隐式默认值。
- 判断为显式两态判断；读取到缺失 / 意外值时按 `ai` 兜底放行（fail-open）——本轮已决定删除 invalid 分支，该后果为接受的设计选择，决策留痕见 `docs/decisions/2026-09-14-handoff-understanding-check.md`。

### 1.2 Seat Takeover：`ai` → `human`（激活 Handoff，非申请本身）

> **Superseded for the request step by [ADR-0003](../adr/0003-seat-gated-handoff.md).**  
> 历史 MVP：「创建 Handoff Record」即与 `mode=human` 同事务。Seat-gated 后：**Handoff Request 只挂起、不切 mode**；仅 **Seat Takeover**（坐席接手后的 status `open`）才切 `human`。

- **Seat Takeover** 时：将挂起的 Handoff Request 转为接入中（或无申请时懒创建 active handoff），并与切 `conversation_mode=human` **绑定为同一数据库事务**，不允许分两步先后执行。
- 触发来源（权威）：Account Webhook 上能确认是坐席接手的 `open`。见 §3.1 / ops runbook。信号是 `meta.assignee_type=User`（或 `changed_attributes.assignee_id`），或 status 仍为 `open` 时的 self-assign / `Assigned to` activity，或 `conversation_updated` 带人工 assignee。
- **不是 Take over**：没有人工 assignee 的 `open`。Agent Bot webhook 读超时后，Chatwoot 会把 pending 会话标成 open，并写 activity「Conversation was marked open by system due to an error with the agent bot」。这条不发 Joined Notice、不切 `human`。默认 `toggle_status(pending)` 拉回 Bot（`CHATWOOT_REVERT_BOT_ERROR_OPEN`，默认 true）。裸 open 先等 `CHATWOOT_BOT_ERROR_OPEN_GRACE_SECONDS`（默认 2），以便官方 Take over「先 open、再 self-assign」落到人工信号上。
- **不得**在 `request_handoff` / 创建挂起 Record / 发 `handoff_requested` 时切 `mode=human`。Bot 与客户路径不得调用 `toggle_status(open)`。
- 无事先 Handoff Request 时，坐席主动 Take over **仍**切 `human`（允许懒建 active handoff）。
- 成功后发出 `handoff_created`（真正进入 human 车道），并由 Bot 发送 **Handoff Joined Notice**（短提示：人工已接入；与挂起确认文案区分）。
- **未提交的 Mutation Form**：进入 human 之前，若该会话线程上仍有 Form Interrupt，Runtime 在同一 thread lock 内以 `Command(resume={"action":"cancel"})` 清掉（不写库）。客户可见提示「尚未提交的修改已取消」；可选坐席 private note。审批 interrupt（HighRisk）不在此取消。清掉之后，迟到的 Form Resume（`/actions/confirm` 或 AgentBot 表单提交）不得写库。客户自己的 Implicit Cancel / 表单确认路径不变。

### 1.3 Handoff Resolve：`human` → `ai`

- **权威交回（官方 Agent Bot）**：Account Webhook `conversation_status_changed` 且新 status = `pending`，并且 Runtime 当前 mode 为 `human`。此时 **不再** `toggle_status(pending)`（已经是 pending，避免环）。mode 回 `ai`，发出 `handoff_resolved`，并发送双方可见短提示「已交回AI客服助手」。
- **兼容**：坐席官方 **Resolve / 已解决**（status=`resolved`）同样走完成路径，随后 Runtime `toggle_status(pending)` 把会话交回 Bot；紧接着的 `pending` webhook 因 mode 已是 `ai` 而忽略。HTTP `POST /handoffs/{id}/resolve` 与 webhook resolve **共用同一完成路径**，行为不得漂移。
- mode 已是 `ai` 时收到 `pending`：**忽略**（Bot 本就持有会话，或刚 toggle 回来的回声）。
- 转换前**必须校验**该 `conversation_id` 名下是否存在状态为 `pending` 的、与某个 HighRiskGraph / MutationGraph interrupt 关联的审批（校验本身保留；审批**归宿**见下条 supersede）。
- **归宿定案（MVP）**：
  > **Superseded by [ADR-0002](../adr/0002-seat-approver-side-channel.md).** 审批由 Seat Approver 经 Approval Side-Channel 决策；不再要求「坐席不代为审批 / 等 AI 接管后才收尾」。以下为历史 MVP 原文，仅供追溯。
  >
  > pending 审批**继续挂起，等 AI 重新接管后经入口层 resume 调度自然恢复**（决策留痕见 `docs/decisions/2026-09-14-handoff-decisions.md`；恢复机制见 `graph-spec.md` §3）。人工坐席不代为审批，不额外开发"代为审批"界面与权限体系。
- Resolve 完成后：`conversation_mode=ai`，发出 `handoff_resolved`，Chatwoot 会话经官方 `toggle_status` 回到 `pending`（交回 Bot）；**禁止**用 PATCH body 改 conversation status。
- **handoff 中断恢复（P1 定案）**：resolve 回切 `conversation_mode=ai` 后，由服务端在 resolve 动作本身触发时**主动完成被 handoff 中断的图执行**（`graph.ainvoke(Command(resume={"action": "handoff_resumed"}, update={"messages": <水合历史>}))`），不依赖等待用户下一条消息；因此用户 resolve 后发送的第一条消息直接进入正常 Router 流程并得到回复。若主动 resume 异常未完成，入口层 resume 调度（见 `graph-spec.md` §3）在下一用户消息时兜底恢复。决策留痕见 `docs/decisions/2026-09-14-checkpointer-resume-handoff.md`。
- 人工接管期间审批到期仍按 `approval-spec.md` §4 惰性过期处理，与本方案兼容。

### 1.4 发送 AI 回复前的强制校验

- 任何一次 AI 回复发送前，必须**重新读取当前 `conversation_mode`**，只有仍为 `"ai"` 才允许发送。
- 如果在生成过程中已被切换为 `"human"`，必须**丢弃该回复，不得发送**。
- mode 为 `human` 时，客户消息只写入 Message Store，不调用 Chatwoot `create_message` 发 bot 气泡。坐席正在输入时用 Chatwoot 官方 typing（`conversation.typing_on`）；Runtime 只在客户发言后把 Agent Bot 的 typing 关掉，不另造气泡。Widget 顶栏在此期间显示「人工服务中」。系统通知只有两条：进入 human 的「人工客服已接入，接下来将由人工为您服务。」，以及交回时的「已交回AI客服助手」。
- 不允许用"生成时刻的 mode 值"代替"发送前的实时值"。
- 挂起（`awaiting_seat`）期间 mode 仍为 `ai`，本门控**允许**继续发 AI 回复（这是 Seat-gated 的产品意图）。

## 2. 轻量 conversation_status（判断必须轻量）

- `conversation_mode` **不得**依赖完整的 `checkpointer.aget()` 读取；单独维护一个轻量的 `conversation_status` 存储（例如独立表或 Redis key），只存 `conversation_id → conversation_mode` 的映射。
- 该判断尽量在 **FastAPI / Conversation Runtime 层、进入任何 LangGraph 逻辑之前**完成；若 `conversation_mode == human`，直接调用 Message Service 写入消息、通知坐席，**不触发 checkpointer、不触发 Graph、不触发 Guard / Router**。
- 长连接（SSE / WebSocket）场景可将 `conversation_mode` 缓存在**连接级内存**，仅在 `handoff_created` / `handoff_resolved`（以及可选的 `handoff_requested` 供 UI 提示）事件发生时主动更新，不要求每条用户消息重新查库。
- **入口层确定性检测（Seat-gated；扩展 P0 意图门控）**：
  - **单一判定点**：`harness/handoff_entry.decide_entry(mode, awaiting, text)` 产出 `human_lane / cancel / request / ai` 四选一，`/chat` 与 AgentBot 共用同一顺序（mode 门控 → 取消 → 申请）；Transport 只翻译载荷、落库与投影，不得各自重写门控顺序。
  - **Handoff Request**：显式转人工意图（`safety-spec.md` §2.1 第一类）由入口层 `detect_handoff_intent` 在进入 LangGraph **之前**判定。命中则创建/复用挂起 Record、发 `handoff_requested`、Chatwoot 强提示（§2.1），本 turn **只回挂起确认**后结束——**不切 `mode=human`**，**不触发 checkpointer / Graph / Guard / Router**。
  - **Handoff Cancel**：挂起期间由入口层 `detect_handoff_cancel` 判定（无 Router / LLM）；命中则清挂起、不切 Conversation Mode。
  - 挂起期间**重复**转人工措辞：幂等，不新建 Record，仅短 tip。
  - 图内 `check_handoff` 保留动态信号与兜底；其外层效果与显式申请**同一挂起门控**（见 §3）。
  - 历史「命中即直接走人工车道」口径 **superseded by ADR-0003**（效果改为 hang，不是立即 human）。决策背景见 `docs/plans/2026-09-18-handoff-intent-gate.md`。

### 2.1 Chatwoot 强提示（Handoff Request）

- 申请时：Chatwoot **private note** + label slug **`handoff-request`**；**不**改 Chatwoot conversation status（保持 `pending`，Bot 仍可回复）。
- 不做自定义坐席 Accept / 同意卡；坐席用官方 Take over / Resolve UI。

## 3. 子图中途触发 Handoff（与显式申请同一门控）

- ReadGraph / MutationGraph / HighRiskGraph 检测到需要转人工的信号时，**不自行创建 Handoff Record，也不自行决定 `conversation_mode` 切换**，而是抛出一次 interrupt：
  `interrupt({"type": "handoff_request", "reason": ..., "summary": ...})`
- 外层（Conversation Runtime）统一负责：接收信号 → 创建/复用 **Handoff Request 挂起**（mode **仍为 `ai`**）→ 发 `handoff_requested` + 强提示（§2.1）。子图本身不直接调用 Handoff 相关 API。
- **不得**在此外层路径上切 `mode=human` 或发 `handoff_created`；human 车道仅由 Seat Takeover（§1.2）进入。
- 子图抛出 `handoff_request` 后，**该被中断的子图执行** `execution_status` 必须置 `waiting_handoff`（见 `state-spec.md` §1），不得设为 `completed` 或 `failed`。这只冻结**当前**被中断的图 turn，**不**把 Conversation Mode 切为 `human`：挂起期间客户未命中 cancel 的**新**用户消息仍走 AI 车道（新 turn / Router）；被中断子图的 resume 仍由 Handoff Resolve（§1.3）完成。
- **Handoff Resolve**：回切 `conversation_mode=ai`（前置校验见 §1.3）、发出 `handoff_resolved`，并**由服务端主动 resume 完成被中断子图**（见 §1.3 / `graph-spec.md` §3）；用户下一条消息直接走正常 Router（不重走被中断子图）。

### 3.1 Account Webhook（Take over / Resolve 同步）

- 入口：`POST /webhooks/chatwoot`（与 AgentBot `outgoing_url` **不是同一通道**；secret 为 `CHATWOOT_WEBHOOK_SECRET`）。
- 必须处理 `conversation_status_changed`（**不是** AgentBot Outgoing URL）：
  - status → `open` 且人工 assignee（或随后的 self-assign activity）：**Seat Takeover**（§1.2）；若有未提交 Form Interrupt，先 cancel 再切 `human`
  - status → `open` 且没有人工 assignee：不是接手。Bot 失败系统自动打开时拉回 `pending`（§1.2），不发 Joined Notice
  - status → `pending` 且 mode=`human`：**交回 Bot**（§1.3），不再 toggle
  - status → `pending` 且 mode=`ai`：**忽略**（防环 / Bot 本就持有）
  - status → `resolved`：兼容的 Handoff Resolve（§1.3），再 `toggle_status(pending)`
- 运维要求见 `docs/ops/chatwoot-widget-agentbot-setup.md`（Account Webhook 须订阅 `conversation_status_changed`）。

## 4. 事件

| 事件 | 何时发出 |
|---|---|
| `handoff_requested` | Handoff Request 挂起建立/确认后（mode 仍为 `ai`） |
| `handoff_created` | **仅** Seat Takeover 完成后（真正进入 human 车道） |
| `handoff_resolved` | Handoff Resolve 回切 mode=`ai` 后 |

- payload 见 `event-spec.md`。子图自身不直接发出上述事件；三子图事件集合见 `event-spec.md` §2.1。
- 客户端**不得**在仍挂起、mode=`ai` 时把 `handoff_created` 当作「已人工接入」。

## 5. 决策状态

### 5.1 已定案（MVP）

- **ID 映射**：`conversation_id == session_id`（业务 / 契约层会话标识，面向用户与坐席）；`thread_id` 是 LangGraph checkpoint 内部标识，与 `conversation_id` **不是同一个字段**。`POST /sessions` 只创建 conversation draft，不生成 thread；第一次进入 AI Graph 前才分配当前 `thread_id` 并先提交指针，显式 Handoff Request / human-only 消息不创建 checkpoint。同一业务会话需要重置图执行状态时可更换当前 `thread_id`，历史 thread 仅由离线清理工具按保护规则处理。实现细节见 `thread-lifecycle-spec.md`。
- **pending 审批归宿（human → ai 切换时）**：
  > **Superseded by [ADR-0002](../adr/0002-seat-approver-side-channel.md)**（与 §1.3 归宿段同口径）。以下为历史 MVP 原文，仅供追溯。
  >
  > 继续挂起，等 AI 重新接管后经入口层 resume 调度自然恢复（见 §1.3 / `graph-spec.md` §3）。
- **Seat-gated handoff（申请 ≠ human）**：见 [ADR-0003](../adr/0003-seat-gated-handoff.md)；术语见 `CONTEXT.md`。

### 5.2 已定案（消息真相源，2026-09-14）

- **Message Store（`conversation_message` 表）是会话消息唯一真相源**；`state.messages` 降级为每次 graph 调用时从 Store 水合的工作视图（`user→HumanMessage`、`assistant→AIMessage`，按写入顺序），不构成权威记录。
- 只持久化 user / assistant 轮次；工具调用 / Router 结构化输出等图内消息不入 Store。
- 人工接管期间 Graph 不执行，消息只写 Message Store；回切 `ai` 后下一 turn 水合即可见完整历史。
- **对账机制（P1 定案）**：`conversation_message` 增 `message_id` 列（langchain message id；assistant 消息落库时由 harness 从流式输出记录）；`hydrate()` 为 user 消息分配稳定 id `store-{row_id}`、assistant 消息复用 `message_id`（缺失则 `store-{row_id}`）；每次图调用输入 = 完整水合历史，经 `add_messages` 按 id 对账，使 `state.messages` 恒等于 Store 快照（checkpointer 中同 id 消息被替换，不产生重复）。
- 决策留痕见 `docs/decisions/2026-09-14-message-store-truth-source.md` 与 `docs/decisions/2026-09-14-checkpointer-resume-handoff.md`。

### 5.2.1 Chatwoot 坐席消息回传（2026-09-19）

- Chatwoot `message_created` 中，只有 `message_type=outgoing/1`、`private=false`、`sender.type=user` 且文本非空的公开坐席回复，才写入 Message Store 并映射为 `assistant`。
- 私密备注、系统事件、模板消息、bot/system sender、Runtime 回显均不得进入 AI 历史。
- Chatwoot 消息使用 `message_id = chatwoot-{Chatwoot message id}` 幂等落库；该 ID 同时作为 LangChain `add_messages` 的稳定 ID。
- Resolve 时先通过 Chatwoot Application API 幂等同步最近公开坐席消息，再执行 `MessageStore.hydrate()` 与 `Command(resume=...)`；同步失败只记日志，不阻塞 Resolve。
- 人工车道用户消息只追加一条 `user` 记录；**Handoff Joined Notice** 仅在 Seat Takeover 时写入一次（挂起确认文案属申请阶段，不得与 Joined Notice 混用旧 `HANDOFF_NOTICE` 语义在申请阶段停 AI）。

## 6. 关联文档

- [ADR-0003](../adr/0003-seat-gated-handoff.md) · `CONTEXT.md`（术语）· `architecture.md` §2 / §4 / §8.1 · `graph-spec.md` §1 / §3 / §6.5 · `state-spec.md` §1 · `event-spec.md` §1 / §2.1 · `safety-spec.md` §2 · `docs/ops/chatwoot-widget-agentbot-setup.md` · `docs/diagrams/handoff-overview.svg`（顶层图；Seat-gated 语义以本文为准）
