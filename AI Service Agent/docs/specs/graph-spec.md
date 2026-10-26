# 图规格（graph-spec.md）

- 关联：`state-spec.md`（状态契约）· `tool-spec.md`（工具集）· `safety-spec.md`（预检与转人工）· `approval-spec.md`（审批服务）· `architecture.md`（总览）

## 0. 节点命名约定（LLM 节点标记）

- 图内**包含 LLM 推理的节点，节点 ID 一律带 `_llm` 后缀**，便于在 LangGraph Studio / 图可视化中直接辨识（本节为唯一权威约定）。
- 当前 `_llm` 节点清单（v0.1.0）：
  - 顶层：`router_llm`（json_mode 结构化分类）
  - ReadGraph：`agent_llm`（ReAct agent，`create_agent`）
  - MutationGraph：`mutation_select_llm`（Mutation Select；工具选择或内联 Operation Clarify）
  - HighRiskGraph：`high_risk_select_llm`（HighRisk Select；cancel/refund 工具选择；无 tool_call → tier_clarify）
- 确定性节点（关键词 / 模板 / 预检 / 中断 / 收尾）**不得**使用 `_llm` 后缀。
- `guard` 节点当前为实现**确定性 v1**（关键词/正则），非 `_llm` 节点；若未来升级为 LLM Guard，须改名并同步本清单。
- 新增任何 LLM 节点时必须遵循本约定，并在本节登记。

## 1. 总图结构与入口 resume 调度

```text
入口（Conversation Runtime / harness，每次图调用前）
  |
  +- aget_state() 读挂起中断（权威依据 = checkpointer 的 __interrupt__）
  |    +- YES -> graph.ainvoke(Command(resume=<唯一 schema>, update={"messages": 水合历史}))
  |    |          = 从中断点继续被中断子图（天然不重走 Router，见 §3 实测依据）
  |    +- NO  -> graph.ainvoke({"messages": 水合历史}) 新起 run
  |                  |
  |                  v
  |        GuardNode ∥ RouterNode（并行，同一份输入，互不依赖）[P3 起]
  |                 |                |
  |                 +---(pass)-------+---> Policy Gate（route_intent -> PolicyDecision）-> 子图
  |                 +---(fail)------------> 安全收尾（丢弃 Router 并行结果）
  |                                                     |
  |                                                     v
  |                                         Read / Mutation / HighRisk / Complaint / Fallback
  |                                                      |
  |                    +---------------------------------+  任意子图检测到转人工信号
  |                    v
  |        interrupt(handoff_request) -> 外层接管（建 Record + 原子切 mode + 发 handoff_created）
  |
  +- 正常完成 -> END（Final Response 统一出口）
```

- **入口 resume 调度（实测修正）**：LangGraph 1.2.11 中，带输入的新调用在图内节点 / START 条件边**读不到 `state["__interrupt__"]`**，且会弃置挂起中断并新起 run；恢复只能由调用方以 `Command(resume=...)` 在 invoke 层发起（实测依据见 `docs/decisions/2026-09-14-checkpointer-resume-handoff.md`）。因此「有挂起中断时新 turn 必须走入口层 resume 调度」由 harness 在每次图调用前经 `aget_state()` 判定实现；恢复语义 = 回应上一个问题，不重新走 Router。
- 子图检测到需要转人工的信号时，**不自行创建 Handoff Record、不自行切换 `conversation_mode`**，统一抛出 `interrupt({"type": "handoff_request", "reason": ..., "summary": ...})`；外层（Conversation Runtime）负责接收信号 → 创建 Handoff Record → 原子切换 `conversation_mode` → 发出 `handoff_created` 事件。子图不直接调用任何 Handoff API（机制与事件见 `handoff-spec.md` §3 / `event-spec.md`）。
- 抛出 `handoff_request` 后 `execution_status` 置 `waiting_handoff`（见 `state-spec.md` §1），不得置 `completed` / `failed`。
- Final Response：子图结果写回 `messages`，由 harness 统一推送事件（见 `event-spec.md`）。
- **Runtime 依赖注入（2026-10-10）**：顶层 `StateGraph` 使用 `context_schema=GraphContext`。`GraphContext` 提供 `order_repository` / `approval_service` / `user_id`，节点与子图通过 LangGraph `Runtime` 读取，不通过全局 settings 直接取数据库连接。默认仍可由 `build_graph` 构造 `SQLiteOrderRepository`，但生产可注入其他 adapter。

## 2. Guard 节点（并行检测）

- 与 Router **并行**执行，输入为同一份原始用户输入（`messages` + `user_id`），**不依赖 Router 的分类结果**，谁先完成不影响正确性。
- 只输出 `guard_status`（pass / fail）与可选 `guard_reason`（fail 时的规则包名），不参与 `route_intent` 分类，不执行 tools，不修改业务状态。
- `fail` → 走安全收尾（见 `safety-spec.md` §1.5），Router 并行结果**不被消费**：实现上若调用可取消则取消，否则以"不消费其输出"兜底。
- 检测信号与阈值定义见 `safety-spec.md` §1.5。
- **实现形态（Guard v2）**：确定性命名规则包（阈值可配置，无模型 key，离线可测，低延迟）。**本阶段明确不做 LLM Guard**；语义绕过的残余风险由 Policy / `safety_check` / 审批与 handoff 上限承接。

## 3. Resume 调度（实测修正：入口层调度 + 图内续接）

- **LangGraph 1.2.11 实测**：图内节点与 START 条件边无法读取 `__interrupt__`（带输入的新调用读不到、且会弃置挂起中断）；resume 必须由调用方（harness / 审批端点）以 `Command(resume=...)` 在 invoke 层发起。恢复 = 从挂起中断点继续原被中断子图，**天然不重走 Router**（实测：resume turn 模型调用次数不增）。"ResumeHandler 节点"不作为图内路由节点存在；语义由两部分承担：
  1. **入口层调度（harness）**：每次图调用前 `aget_state()` 读挂起中断，构造 resume payload（审批 / 确认 / 超时 / handoff 共用唯一 schema，见下），以 `Command(resume=payload, update={"messages": <水合历史>})` 发起；无挂起中断则以普通输入新起 run。
  2. **图内续接（被中断节点）**：`interrupt()` 返回 resume payload，由发起 interrupt 的节点（或其子图续接逻辑）解析并完成状态转移；决策一律在图内，不允许挪到 harness 组件。
- resume payload 唯一 schema（审批 / 用户确认 / 超时 / handoff 共用，harness 直调与用户 turn 两条恢复入口均须遵守）：
  `Command(resume= { "action": "approve" | "reject" | "confirm" | "cancel" | "handoff_resumed", "note"?: str } | { "result": "expired" })`
- 中断过期：`Command(resume={"result": "expired"})`，子图按过期分支收尾。
- `handoff_request` 中断的恢复：人工 Resolve 回切 `conversation_mode=ai` 后，**由服务端在 resolve 动作本身触发时主动完成 resume**（`Command(resume={"action": "handoff_resumed", "note"?: ...})`，不等待用户下一条消息；见 `handoff-spec.md` §1.3）。被中断子图（ReadGraph / MutationGraph / HighRiskGraph / Fallback，取决于中断来源）从中断点继续至完成，才汇入 Final Response；不重新走 Router。用户 resolve 后发送的第一条消息直接进入正常 Router 流程；若主动 resume 异常未完成，入口层 resume 调度在下一用户消息时兜底恢复。恢复执行后的状态转移决策必须在图内完成（同 §3 原则，见 `approval-spec.md` §3.1）。

## 4. Router 节点

- Router 统一输出 `route_intent` + `confidence`（单一契约，无 v1/v2 分支）。
- JEV 可用时作为 primary 路由；JEV 超时、HTTP 错误或非法输出时 fallback 到 DeepSeek（同一个 `ROUTE_INTENT_SYSTEM` prompt）。
- `route_intent` 是稳定业务意图标签，不是 `tool_name`，也不是 `target_graph`。
- Jev 超时、HTTP 错误或非法输出时 fallback 到 DeepSeek。
- Jev 返回合法 Choice 但置信度低于执行档阈值时，直接归为 `unknown`，不得再调用 DeepSeek。
- `routing_backend` / `routing_fallback` 只用于观测和评估，不参与路由。
- `route_control` 是唯一的 fallback 控制信号：Jev 失败写入 `deepseek_fallback`，`router_join` 据此执行一次 DeepSeek；DeepSeek 写入 `policy` 后必须进入 Policy Gate，不得再回到自身。
- 每轮执行；多轮中用户可切换意图。
- JEV 请求 state 只包含 `current_user_message` 和最多 2 条 `recent_context`（旧 -> 新）。`current_user_message` 必须来自 `messages[-1]` 且 role 为 `human`；否则不调用 JEV，直接写入 `unknown`（`routing_backend=none`）。
- Router 仅写入自身分类字段（`route_intent` / `confidence`），不得修改 `context` 等业务状态。
- Router 失败/超时 → 直接走 Fallback。

## 5. Policy Gate（纯确定性）

- Policy Gate 不调用 LLM，并且是 `target_graph` / `execution_class` / `allowed_tools` / `requires_approval` 的唯一确定性写入方。
- Policy Gate 始终使用 `route_intent -> PolicyDecision`（唯一路径）。
- `PolicyDecision` 派生：
  - `target_graph`
  - `execution_class`（`read_only` / `standard_write` / `high_risk` / `complaint`）
  - `allowed_tools`
  - `requires_approval`
- `allowed_tools` 是候选工具边界；子图 LLM 只能在该边界内选择最终工具。
- 未注册 intent、`unsupported_action`、`unknown` 一律 fallback；例如 `update_phone` 有意不注册为 route intent，账户资料变更使用 `account_profile_update`。
- 高风险操作继续保留安全预检与人工审批。
- Policy Gate 允许一个极窄归一化：LLM 输出为 `order_mutation` 且当前 turn 用户文本明确表达修改收货地址、联系电话或手机号时，Policy Gate 将 `route_intent` 修正为 `account_profile_update`。
- 该归一化不得 rescue `unsupported_action` 或 `unknown`，不得调用 LLM，不得扩大 `allowed_tools`。
- **投诉硬闸（PR-A）**：查表前若当前用户句命中 `detect_complaint_intent` 且**未**命中 `detect_handoff_intent`，强制 `route_intent=complaint`、`confidence=1.0`，覆盖错误的 LLM 标签。不切 `conversation_mode`，不挂 `fallback`。
- **投诉询问待答**：`context["complaint_offer_pending"]` 为真时，先解释本句。窄肯定（转人工 / 要人工 / 帮我转）标 `complaint_offer_decision=accept`（图内不 `interrupt`；入口或 harness 复用既有 `request_handoff`，reason=`complaint`）。裸「要」标 `reconfirm`，仍进 `complaint_graph`。拒绝或改提业务则清 pending，再按本轮 Router 查表。显式转人工仍由入口 `detect_handoff_intent` 先于本解释。
- 路由落盘后、进入子图前：若本轮 `route_intent` 或 `target_graph` 相对 checkpoint 上一轮发生变化，将 `context["clarify_count"]` 置 `0`。上一轮落盘路由记在 `context["clarify_route_intent"]` / `context["clarify_target_graph"]`（Router 本轮已覆盖 State 上的 `route_intent`，不能拿它当上一轮值）。尚无这两键的旧 checkpoint 只比较上一轮 `target_graph`。没有上一轮路由时不清零。同一 `route_intent` 且同一 `target_graph` 的连续澄清仍累计，超过 `clarify_max_turns`（默认 3）由 clarify / Mutation Operation Clarify 转 fallback。

## 6. 子图

### 6.1 ReadGraph

- `create_agent` + 只读工具集（知识库检索、订单查询等）+ ToolNode + `tools_condition`。
- 无**业务** interrupt（不发起确认 / 审批）；检测到需要转人工的信号时同样经 `interrupt(handoff_request)` 退出（见 §6.5）。
- 工具结果直接汇入回答。

### 6.2 MutationGraph（workflow）

- 确定性流程（Mutation Form / Form Interrupt，见 `mutation-form-interrupt-spec.md` / `CONTEXT.md`）：**select（确认 tool；业务字段可不全）** → **安全预检**（`safety-spec.md`；缺参时仅做登录/档位等闸门，字段完备性延后）→ **Form Interrupt**（`interrupt(value=..., response_schema=...)`，SSE 投影 `form_required` / `form_rejected`）→ **Form Resume**（`Command(resume=...)` Confirm/Cancel）→ **recheck** → ToolNode 写一次 / 取消收尾。
- **不再**把 Mutation 主路径建成聊天 `clarification_required` → `confirmation_required` / `confirm_pause` 仪式。业务字段由 Mutation Form 收集（Form-Only Write）；Operation Clarify（tool 未定）由 Mutation Select（`mutation_select_llm`）同一次调用用自然语言追问完成（ADR 0004 / `CONTEXT.md`），不经入口 Hybrid / 子图外澄清节点。
- select：tool 已知即写 `pending_tool`（已知字段作 prefills）；真 schema 类型/未知字段错误仍 `safety_rejected`。Mutation 允许集档内严格：只读/未绑定工具 → `unknown_tool`（无 `tier_clarify`）。HighRisk 选中已注册低档工具仍 → `tier_clarify` → clarify（`reason:"tier_mismatch"`）。
- Form Resume Confirm 对应结构化字段 + `action:"confirm"`；Cancel 为 `action:"cancel"` 哨兵（schema 见 §3 / event-spec §2.2）。Cancel → 收尾不写库。
- **Form Resume 后、执行前 recheck（D6 + Form Interrupt）**：校验 Form Resume 字段 + 业务预检；失败则同一 `form_session_id` 再次 Form Interrupt（`form_rejected`），不走 `safety_rejected` 收尾。确认结果写入 `context`（§9），不新增 State 字段。

### 6.3 HighRiskGraph

- **select（参数完备性）** → 必填未齐且名下**有**可操作订单：与 Mutation 同一 **Form Interrupt**（Chatwoot `form`：Order Picker + 字段 + 确认/取消；不自动选中唯一订单）→ Form Resume Confirm 后进入安全预检与人工审批；Cancel 不写库、不进审批。**零**可操作订单仍 **clarify（普通轮，非 interrupt）**：确定性模板文案 + `execution_status=clarifying` + `clarification_required`；`context["clarify_count"]` 自增，超过 `clarify_max_turns`（默认 3）→ fallback（换 `route_intent` 或 `target_graph` 时 Policy Gate 已先清零）。字段已齐 → 不插表单，直接 **安全预检**（含业务规则，D3）→ `interrupt()` 发起人工审批（见 `approval-spec.md`）→ 人工 approve/reject 后 `Command(resume=...)` 恢复 → 执行/拒绝收尾。审批通过且写工具成功时，Chatwoot 出站用与 Mutation 相同的成功 `cards`（`MutationSuccessCardSpec`）加短跟进气泡；拒绝、过期、执行失败仍是纯文本气泡。
- **档内严格（P5 B2b）**：写图 select 只允许 `min_risk_level == tier` 的写工具；只读工具不可选，
  选中已注册低档工具 → tier_mismatch 澄清（`clarification_required` + `reason:"tier_mismatch"`）。
- **显式 `order_id`（2026-10-10，2026-10-16 收集改表单）**：`cancel_order` / `refund_apply` 必须由当前用户输入、可见上下文或 Form Resume 给出 `order_id`；即使名下只有一笔可操作订单也不自动选中。有候选时用 Form Order Picker 收集，零笔仍 `clarification_required` 说明无可操作订单，均不直接进入审批。
- `cancel_order` 审批摘要展示订单 ID、金额、商品摘要、requester 与当前状态；由于预检只放行 `paid` 订单，进入审批时的状态预期为 `paid`。
- `refund_apply` 审批摘要展示退款金额、订单总额、商品摘要、requester 与当前状态。
- 超时由下一 turn 惰性检查处理为过期。
- **审批承接（D7/D8 + 2026-10-10）**：由 Approval Service（SQLite 轻量存储 + 前置校验端点，见 `approval-spec.md` §3）承接；同一 `approval_id` 重复 decision 天然幂等。approve 恢复后必须重新读取当前订单快照，校验订单仍存在、归属正确、状态仍允许、版本/rowid 未变化；任一失败均进入 `safety_rejected`，避免 TOCTOU。

### 6.4 Fallback / Concierge

- 兜底 agent：解释边界、收集更多信息；`check_handoff` 确定性节点按 `safety-spec.md` §2 规则检测转人工信号，命中后经 `interrupt(handoff_request)` 抛出（见 §6.5），由外层接管；Fallback 自身不直接创建 Handoff / 切换 mode。
- **节点顺序（P1 定案）**：`START -> check_handoff`，条件边 `handoff? -> pause | else -> explain`；`explain` 只服务非 handoff 兜底回复。命中 handoff 时**不在图内生成"已转人工"文案**——该文案由外层在 `handoff_created` 时发 token + 落 Message Store（见 `handoff-spec.md` §5.2 / `event-spec.md`），避免被中断 super-step 在 resume 时重放重复文本（实测依据见 `docs/decisions/2026-09-14-checkpointer-resume-handoff.md`）。

#### 6.4.1 `explain_llm` 边界（2026-09-23 定案）

- **物理隔离**：`explain_llm` 必须以 `create_agent(model, tools=[], system_prompt=FALLBACK_SYSTEM_PROMPT)` 构建（当前实现见 `src/ai_service_agent/graph/workflows/fallback.py`）。工具集为空属于机械隔离，不依赖 prompt 约束"不要调用工具"。
- **调用上限分层**：当前 `READ_MODEL_CALL_LIMIT=5` 只适用于 ReadGraph（给 multi_hop 连环检索留余量后再作答）；Fallback 的调用上限指 `explain_llm` **单次 run 内模型调用次数**，后续实现也必须使用官方 `ModelCallLimitMiddleware`（建议 `run_limit=3`、`exit_behavior="end"`），不是跨 turn 的 fallback 次数限制。
- **跨 turn 兜底升级（2026-09-23 实现；2026-10-11 阈值调整）**：同一会话最多产生 `fallback_max_turns=4` 次 fallback 回复；第 5 次 fallback 由 `check_handoff` 确定性触发 `interrupt(handoff_request)`，`reason="fallback limit exceeded"`，并把 `context["fallback_count"]` 清零（人工 Resolve 后重新计数）。当前 `fallback_count` 是首个落地触发源；同一工具连续失败后续应复用同一 `check_handoff` 消费入口与 `context` 计数语义，禁止各自维护独立计数逻辑；hook 只读观测，不得修改 State。
- **发送前复检**：`explain_llm` 的 `done.final` 与所有子图一样复用 `/chat` 出口统一复检 `conversation_mode == "ai"`；非 `ai` 时不落 Message Store，`done.final` 置空并返回 `ModeChanged`。需要注意：SSE token 会在图流执行期间先行透出，最终复检兜底的是最终回复的持久化与 `done.final`；如后续要求 token 级绝对不透出，应统一改造 `event_stream` 的发送策略，而不是给 fallback 增加独立复检路径。

### 6.5 子图中途触发 Handoff（统一机制）

- ReadGraph / MutationGraph / HighRiskGraph 检测到需要转人工的信号时，不自行创建 Handoff Record，也不自行决定 `conversation_mode` 切换；统一抛出一次 interrupt，resume / 退出信号类型为：
  `interrupt({"type": "handoff_request", "reason": ..., "summary": ...})`
- 外层（Conversation Runtime 或等价的入口逻辑）统一负责：接收该信号 → 创建 Handoff Record → 原子切换 `conversation_mode` → 发出 `handoff_created` 事件。子图自身不直接调用 Handoff 相关的任何 API。
- 抛出后 `execution_status` 置 `waiting_handoff`（见 `state-spec.md` §1），不得置 `completed` / `failed`。
- 触发信号来源为 `check_handoff` 等确定性规则（见 `safety-spec.md` §2）；LLM 无权临时决定转人工。
- 投诉询问的同意**不**走这条 interrupt：复用入口 `request_handoff`（见 §6.6）。`complaint_offer_pending` 期间情绪规则不得自动抛 `handoff_request`。

### 6.6 ComplaintGraph（薄节点，PR-A）

- 父图节点 `complaint_graph`（确定性模板，无 `_llm`）。Policy `target_graph=complaint_graph`，`allowed_tools=()`，`requires_approval=False`，`execution_class=complaint`（阈值 0.80，**不**进 high_risk 审批写路径）。
- **不挂 `fallback`**，因此不增加 `fallback_count`，也不走 Concierge `explain_llm`。
- 本轮：固定澄元安抚（致歉 + 承认感受）并询问是否转人工；写 `context["complaint_offer_pending"]` 与 `complaint_summary`。正常结束：无 `interrupt(handoff_request)`，无 `awaiting_seat`。
- 下一用户句重新进入主图。入口仍先跑 `detect_handoff_intent`。待答期间裸「要」只再确认（转人工还是继续办业务）；拒绝或改提业务清 pending 后走 Router。明确同意复用入口 `request_handoff`（reason=`complaint`），不另起 interrupt 系统。若该句已进入主图，节点只置 `complaint_handoff_accept`，由 harness 调用同一个 `request_handoff`。

## 7. 节点级工程参数（横切，见 architecture.md §5）

- 模型/工具节点配置 `RetryPolicy` 与 `TimeoutPolicy`；工具节点配置 `error_handler`。

## 8. 变更纪律

- 改路由/子图行为：先改本文件，再实现。
- 新增子图：在此登记 Policy 映射条目与所需状态字段（回写 `state-spec.md`）。

## 9. 节点-字段读写表

| 节点/子图 | 读 | 写 |
|---|---|---|
| GuardNode | `messages`（原始输入）、`user_id` | `guard_status`、`guard_reason` |
| Router | `messages`、`user_id` | `route_intent`、`confidence`、`execution_status`、`routing_backend`、`routing_fallback`、`route_control` |
| Policy Gate | `route_intent`、`messages`（地址/电话归一化、投诉硬闸、投诉询问回复）、`context`（`clarify_count`、`clarify_route_intent`、`clarify_target_graph`、`complaint_offer_pending`）、上一轮 `target_graph` | `route_intent`、`confidence`（硬闸时 1.0）、`target_graph`、`execution_class`、`allowed_tools`、`requires_approval`、`context`（路由变化时 `clarify_count=0`；写入本轮 `clarify_route_intent` / `clarify_target_graph`；询问回复写 `complaint_offer_decision` 或清 pending） |
| complaint_graph | `messages`、`context`（`complaint_offer_decision`、`complaint_summary`） | `messages`（安抚或再确认）、`context`（`complaint_offer_pending`、`complaint_summary`、`complaint_handoff_accept`）、`execution_status=completed`；本节点不抛 interrupt |
| 入口 resume 调度（harness） | checkpointer `__interrupt__`（`aget_state`）、`messages` | 以 `Command(resume=..., update={messages})` 发起，不写业务字段 |
| ReadGraph | `messages`、`context`、`user_id`、`allowed_tools` | `messages`、`execution_status` |
| MutationGraph | `messages`、`context`、`user_id`、`allowed_tools`、`execution_class` | `messages`、`context`（Form / pending_tool 等）、`execution_status` |
| HighRiskGraph | `messages`、`context`、`user_id`、`allowed_tools`、`execution_class`、`requires_approval` | `messages`、`context`、`pending_approval`、`execution_status` |
| mutation_select_llm | `messages`、`context` | `context`（`pending_tool`，业务字段可不全作 Form prefills；或无 tool_call 时内联追问：写 `messages` + `clarification_required`/`operation_unclear` + `clarify_count` 自增 / 超限 fallback；成功/进表单时清零 `clarify_count`） |
| high_risk_select_llm | `messages`、`context`（`clarify_count`、`cancellation_candidates`） | `context`（有候选缺参：`pending_tool` 供 Form；零候选：`clarify_missing`；或 `tier_clarify` / `cancellation_candidates`；完整 select 或进表单时清零 `clarify_count`。P0 不内联 Operation Clarify） |
| prepare_form / form_interrupt（high_risk） | `context`（`pending_tool`、`form_session_id`） | Form Interrupt（缺参收集）；Confirm 后写 `pending_tool.args` 再进安全预检/审批；Cancel / 零候选不进审批 |
| clarify | `context`（`clarify_missing` / `tier_clarify`、`clarify_count`、`cancellation_candidates`） | `messages`（确定性模板文案）、`context`（`clarify_count` 自增）、`execution_status=clarifying`，并发出 `clarification_required`（仅 HighRisk：零候选缺参 / `tier_mismatch`；Mutation Operation Clarify 不经本节点） |
| prepare_form / form_interrupt（mutation） | `context`（`pending_tool`、`form_session_id`、`form_errors`） | Form Interrupt（对外权威）；子图内 `execution_status=waiting_confirmation`（不并回父图）；Confirm 后写 `pending_tool.args`（Form Resume 真源） |
| precheck_fetch（mutation/high_risk） | `context`（`pending_tool`） | `context`（`precheck_data`：仅事实，订单状态快照；不做授权/裁决） |
| Fallback / Concierge | `messages`、`context` | `messages` |
| check_handoff | `context`（工具失败计数、`complaint_offer_pending`）、`messages` | `handoff`（置位）、`execution_status=waiting_handoff`，并抛出 `interrupt(handoff_request)`。`complaint_offer_pending` 期间情绪规则不得置位 |

- `execution_status` 由入口与图内节点按 turn 阶段维护（取值见 `state-spec.md` §1）；子图抛出 `handoff_request` 时置 `waiting_handoff`，不得置 `completed` / `failed`。

