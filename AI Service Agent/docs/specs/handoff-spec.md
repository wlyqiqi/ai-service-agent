# Handoff 规格（handoff-spec.md）

- 日期：2026-09-14
- 状态：权威定义（`conversation_mode` 状态机 + Handoff 生命周期 + 轻量判断）
- 关联：`architecture.md`（入口层 / 持久化）· `graph-spec.md`（子图 `interrupt(handoff_request)`）· `event-spec.md`（`handoff_created` / `handoff_resolved`）· `safety-spec.md`（转人工触发规则）· `state-spec.md`（`execution_status=waiting_handoff`）

## 1. conversation_mode 状态机

- 两态：`ai` / `human`（不设第三态；读取异常不另设 invalid 分支，见 §1.1 兜底说明）。
- 判断规则为两态：`conversation_mode == "human" ? 进入人工车道 : 进入 AI 路径`。
- 顶层流程见 `docs/diagrams/handoff-overview.svg`（入口判断 → AI / 人工双车道 → 循环回入口）。

### 1.1 初始化

- 新会话创建时（`POST /sessions`），`conversation_mode` **必须被显式写入 `"ai"`**，不允许依赖存储层的隐式默认值。
- 判断为显式两态判断；读取到缺失 / 意外值时按 `ai` 兜底放行（fail-open）——本轮已决定删除 invalid 分支，该后果为接受的设计选择，决策留痕见 `docs/decisions/2026-09-14-handoff-understanding-check.md`。

### 1.2 ai → human（创建 Handoff Record）

- **必须与"创建 Handoff Record"绑定为同一个数据库事务的原子操作**，不允许分两步先后执行（先建 Record 后切 mode / 先切 mode 后建 Record 均不允许）。
- 触发来源：子图 / `check_handoff` 抛出 `interrupt(handoff_request)` 后，由外层（Conversation Runtime）统一执行（见 §3）。

### 1.3 human → ai（人工 Resolve 触发）

- 转换前**必须校验**该 `conversation_id` 名下是否存在状态为 `pending` 的、与某个 HighRiskGraph / MutationGraph interrupt 关联的审批。
- **归宿定案（MVP）**：pending 审批**继续挂起，等 AI 重新接管后经入口层 resume 调度自然恢复**（决策留痕见 `docs/decisions/2026-09-14-handoff-decisions.md`；恢复机制见 `graph-spec.md` §3）。人工坐席不代为审批，不额外开发"代为审批"界面与权限体系。
- **handoff 中断恢复（P1 定案）**：resolve 回切 `conversation_mode=ai` 后，由服务端在 resolve 动作本身触发时**主动完成被 handoff 中断的图执行**（`graph.ainvoke(Command(resume={"action": "handoff_resumed"}, update={"messages": <水合历史>}))`），不依赖等待用户下一条消息；因此用户 resolve 后发送的第一条消息直接进入正常 Router 流程并得到回复。若主动 resume 异常未完成，入口层 resume 调度（见 `graph-spec.md` §3）在下一用户消息时兜底恢复。决策留痕见 `docs/decisions/2026-09-14-checkpointer-resume-handoff.md`。
- 人工接管期间审批到期仍按 `approval-spec.md` §4 惰性过期处理，与本方案兼容。

### 1.4 发送 AI 回复前的强制校验

- 任何一次 AI 回复发送前，必须**重新读取当前 `conversation_mode`**，只有仍为 `"ai"` 才允许发送。
- 如果在生成过程中已被切换为 `"human"`，必须**丢弃该回复，不得发送**。
- 不允许用"生成时刻的 mode 值"代替"发送前的实时值"。

## 2. 轻量 conversation_status（判断必须轻量）

- `conversation_mode` **不得**依赖完整的 `checkpointer.aget()` 读取；单独维护一个轻量的 `conversation_status` 存储（例如独立表或 Redis key），只存 `conversation_id → conversation_mode` 的映射。
- 该判断尽量在 **FastAPI / Conversation Runtime 层、进入任何 LangGraph 逻辑之前**完成；若 `conversation_mode == human`，直接调用 Message Service 写入消息、通知坐席，**不触发 checkpointer、不触发 Graph、不触发 Guard / Router**。
- 长连接（SSE / WebSocket）场景可将 `conversation_mode` 缓存在**连接级内存**，仅在 `handoff_created` / `handoff_resolved` 事件发生时主动更新，不要求每条用户消息重新查库。
- **P0（2026-09-18）入口层显式检测**：显式转人工意图（`safety-spec.md` §2.1 第一类）由入口层 `detect_handoff_intent` 在进入 LangGraph **之前**判定；命中即直接走人工车道，**不触发 checkpointer / Graph / Guard / Router**（本小节「不触发」约束的补充，决策见 `docs/plans/2026-09-18-handoff-intent-gate.md`）。图内 `check_handoff` 保留动态信号与兜底判定。

## 3. 子图中途触发 Handoff 的机制（外层接管）

- ReadGraph / MutationGraph / HighRiskGraph 检测到需要转人工的信号时，**不自行创建 Handoff Record，也不自行决定 `conversation_mode` 切换**，而是抛出一次 interrupt，resume / 退出信号类型为：
  `interrupt({"type": "handoff_request", "reason": ..., "summary": ...})`
- 外层（Conversation Runtime 或等价的入口逻辑）统一负责：接收这个信号 → 创建 Handoff Record → 原子切换 `conversation_mode` → 发出 `handoff_created` 事件。子图本身不直接调用 Handoff 相关的任何 API。
- 子图抛出 `handoff_request` 后 `execution_status` 必须置 `waiting_handoff`（见 `state-spec.md` §1），不得设为 `completed` 或 `failed`。
- 人工 Resolve：回切 `conversation_mode=ai`（前置校验见 §1.3）、发出 `handoff_resolved`，并**由服务端主动 resume 完成被中断子图**（见 §1.3 / `graph-spec.md` §3）；用户下一条消息直接走正常 Router（不重走被中断子图）。

## 4. 事件

- `handoff_created`：外层接管完成后发出（payload 见 `event-spec.md`）。
- `handoff_resolved`：人工 Resolve 回切 mode=ai 后发出。
- 子图自身不直接发出 `handoff_created`；三子图事件集合见 `event-spec.md` §2.1。

## 5. 决策状态

### 5.1 已定案（MVP）

- **ID 映射**：`conversation_id == session_id`（业务 / 契约层会话标识，面向用户与坐席）；`thread_id` 是 LangGraph checkpoint 内部标识，与 `conversation_id` 建立**一对一映射但不是同一个字段**。会话创建时生成 `thread_id`（如 uuid）并持久化映射；同一业务会话需要重置图执行状态时（如严重异常后清空 graph 重来），可更换 `thread_id` 而 `conversation_id` 不变。决策留痕见 `docs/decisions/2026-09-14-handoff-decisions.md`。
- **pending 审批归宿（human → ai 切换时）**：继续挂起，等 AI 重新接管后经入口层 resume 调度自然恢复（见 §1.3 / `graph-spec.md` §3）。

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
- 人工车道用户消息只追加一条 `user` 记录；`HANDOFF_NOTICE` 仅在 handoff 创建时写入一次。

## 6. 关联文档

- `architecture.md` §2 / §4 / §8.1 · `graph-spec.md` §1 / §3 / §6.5 · `state-spec.md` §1 · `event-spec.md` §1 / §2.1 · `safety-spec.md` §2 · `docs/diagrams/handoff-overview.svg`（顶层图）
