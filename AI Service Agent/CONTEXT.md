# AI Service Agent

LangGraph 客服运行时：按风险档分流读/写/高风险操作，写操作以结构化收集参数为主；**能同功能时优先官方 HITL / 预置组件**。

## Language

**MutationGraph**:
处理 `min_risk_level == mutation` 的写操作子图（改地址、改电话、备注、开票、售后工单等）；不含退款/取消。显式 `StateGraph` workflow，不用 ReAct agent 主路径。
_Avoid_: 把 HighRisk 审批流称作 Mutation；Mutation 主路径用 create_react_agent

**Mutation Select**:
MutationGraph 内专属的写操作选择 LLM 节点（图内名 `mutation_select_llm`）：自有模型工厂与提示词，一次调用要么发出本档 tool_call，要么自然语言做 Operation Clarify；缺业务字段仍选 tool，交 Mutation Form。不与 HighRisk / Read 的 select 共享节点、提示词或模型实例。
_Avoid_: 与 HighRisk 共用 `make_select_node` / `make_select_model` / 通用 select 提示词；把 Mutation Select 做成 ReAct 工具循环

**HighRisk Select**:
HighRiskGraph 内专属的高风险操作选择 LLM 节点（图内名 `high_risk_select_llm`）：自有模型工厂（`HIGH_RISK_SELECT_*` 缺省回落 `SELECT_*`）与专属提示词，本档仅 cancel/refund；缺参/无 tool_call/档不匹配仍走既有 clarify 与 Form/审批拓扑（P0 不内联 Operation Clarify）。不与 Mutation / Read 的 select 共享节点、提示词或模型实例。
_Avoid_: 继续叫通用 `select_llm` / 保留 `make_select_node` 或 `select_model` 别名；P0 把 Mutation 式口语追问塞进 HighRisk Select；把 HighRisk Select 做成 ReAct 工具循环

**Mutation Form**:
仅在 **tool 已确定** 且需要业务字段时，由客户端按 interrupt 载荷 / `response_schema` 渲染的结构化填参 UI；不是操作类型菜单。
_Avoid_: 用表单做操作类型选择；自造与 interrupt 无关的第二套挂起语义

**Form Interrupt**:
Mutation 等人填表的官方挂起：节点内 `interrupt(value=..., response_schema=FormModel)` + checkpointer/`thread_id`。载荷供 UI 渲染；`response_schema`（langgraph≥1.2.12）描述确认提交的 JSON 形状。
_Avoid_: 用「普通轮 END + Form Pending 假状态」代替 interrupt；图内再发明 confirmation_required 挂起

**Form Resume**:
用户提交表单后，经 harness 转为 **`Command(resume=...)`** 续跑同一 thread；resume 值即 `interrupt()` 返回值。Confirm 常见形状含 `action=confirm` 与业务字段；显式 Cancel / Implicit Cancel 为 `action=cancel`（不写库）。对外可仍走 HTTP/SSE，对内必须是官方 resume，而不是再当新用户消息进 Router。
_Avoid_: Form Submit 当普通 chat 再跑 Router；后端另设「跳过 Router」旁路假装 resume

**Order Picker**:
Mutation Form 中订单字段一律以下拉选择（含仅一笔可操作订单）；candidates 放在 interrupt `value` 里。
_Avoid_: 单笔自动填 order_id；聊天里口述选单后再出表单

**Operation Clarify**:
Mutation 内 tool 未定（Mutation Select 未发出 tool_call）时，由**同一次**响应的自然语言追问问清要办哪种写操作；本轮普通 END，用户下一句从总图再进。问清并选出 tool 后才 Form Interrupt。Mutation 不设 `tier_clarify` 固定澄清；未绑定工具走拒绝。
_Avoid_: 用表单做操作类型菜单；入口层 Hybrid / 子图外专用澄清节点；再挂第二个 LLM 只为生成追问；Mutation 固定模板澄清（含 tier_clarify）；与 HighRisk 共用同一套 select 提示词或 select 节点

**Form-Only Write**:
Mutation 写库只接受 Form Resume `action=confirm` 成功路径上的结构化值；Form Interrupt 挂起期间纯文本不得写库。挂起中的非 Form 提交可由 Transport 走 Implicit Cancel 清 interrupt 后再按新 turn 路由，仍不得把该纯文本当写库真源。
_Avoid_: 文本降级 clarify 写库；把挂起期间的纯文本当表单字段写入

**Closed MutationGraph**:
子图不内跳 Read/HighRisk/Fallback；校验/预检失败则**同一节点再次 interrupt**（或回 ensure_form 再 interrupt）以原地更新同一表单会话；新意图在取消或完成后下一 turn 重走总图 Router。
_Avoid_: 子图内部业务转交；失败后新开无关表单会话

**Form Cancel**:
表单上的显式 Cancel 控件（如按钮）→ `Command(resume={"action":"cancel", ...})`，图内收尾不写库并结束 Form Interrupt。
_Avoid_: 仅靠打字「取消」作为主路径；无 resume 的「口头取消」

**Implicit Cancel**:
Form Interrupt 挂起时，入站不是合法 Mutation Form 提交 → Transport/Adapter 先对同一 `thread_id` 做官方 `Command(resume={"action":"cancel"})` 清挂起（不写库），再把该消息当普通新 turn 路由；静默、不重推旧表。`form_rejected` 同 session 重推不得走此路径。**所有 Transport 一致**（Widget/AgentBot 与 SSE `/chat`）；决策由 `harness/form_gate.py` 单点产出，不在各 Adapter 内联。
_Avoid_: tip+重推旧表当主路径；按入口分叉策略；用 `Command(update=...)` 绕过 interrupt；把新消息当表单字段写入

**ToolNode**:
写工具执行使用 `langgraph.prebuilt.ToolNode`（或等价官方工具执行积木）；execute 节点只构造受控单次 tool_call。
_Avoid_: 在 Mutation 主路径让 LLM 自由循环选工具

**Chatwoot Widget**:
C 端唯一对话壳：Website Channel 气泡与 Chatwoot native 消息类型（文本 / `form` / `input_select` 等）；不持有业务挂起真相。
_Avoid_: 自建 SSE 聊天气泡作为主入口；在 Widget 侧另建 Form Pending 状态库

**AgentBot Adapter**:
Runtime 上的 Transport：把 Graph/`__interrupt__` 与事件投影适配为 Chatwoot 出站消息，并把用户提交适配为 `Command(resume=...)` 或新 turn；挂起中非 Form 提交可编排 Implicit Cancel；Mutation 写路径必须遵守 Form Interrupt / Form-Only Write。
_Avoid_: 把 AgentBot 当成业务编排层；表单提交改写成自然语言再进 Router

**Contact-first Binding**:
Website Channel 会话为入口；以登录 `user_id` + Identity HMAC 标识 contact，AgentBot 首次触达时懒建 Runtime `conversation_id` 并写入 mapping；`thread_id` 仅在首次进入 AI Graph 前分配。
_Avoid_: 强制先调自建 `/sessions` 再建 Widget；匿名与登录会话无规则合并

**Requester**:
发起 HighRisk 操作并进入审批挂起的客户身份。
_Avoid_: Approver；把申请人当成可自批的审批资源 owner

**Seat Approver**:
在坐席端（Chatwoot）对 HighRisk 审批做通过/拒绝的操作者；`decided_by` 记为 `seat:{sender.id}`——`seat:` 即来源命名空间，不再内嵌 `chatwoot-agent-`（`system` / `simulate` 为非人工保留字）。
_Avoid_: 客户 JWT 自批；用字面量 `"approver"` 冒充身份；在鉴权关闭时假装存在人工审批人；把无 `seat:` 前缀的旧值当作坐席 id；写成 `seat:chatwoot-agent-{id}`

**Approval SoD**:
同一审批单上 Seat Approver 不得与 Requester 为同一身份。
_Avoid_: 申请人自批；用「有审批能力」绕过身份分离

**Approval Side-Channel**:
HighRisk 审批在坐席端旁路完成（表单/卡片 → Runtime）；不因此把 `conversation_mode` 切为 `human`。Handoff 仍是会话接管，与审批 gate 分离。坐席决策后立即图 resume + Message Store；出站消息仍受 `conversation_mode==ai` 门控。执行链唯一归属 `harness/approval_side_channel.py`（投影 → 解析 → 工牌/SoD → `mark_decided` → resume → 落库/门控出站），Transport 只提供工牌头解析与图调用；门控纯函数在 `harness/outbound_gate.decide_delivery`。
_Avoid_: 进入审批必须先转人工；坐席聊天接管等同于审批通过；human 期间批准却跳过出站门控直接推给用户；把工牌 / resume / 出站门控各写一份在 Transport 里

**Conversation Mode**:
会话车道两态：`ai`（进 Graph）与 `human`（人工车道，不跑 Graph）。与 Chatwoot 会话 status、handoff 申请挂起不是同一概念。Seat-gated 决策见 `docs/adr/0003-seat-gated-handoff.md`；生命周期权威 `docs/specs/handoff-spec.md`。
_Avoid_: 用第三态表示「等坐席」；把 Chatwoot `pending`/`open` 直接当成 Conversation Mode

**Handoff Request**:
客户或图内信号发起的转人工**申请**；Runtime 记为挂起（如 `awaiting_seat`），**不**因此把 Conversation Mode 切为 `human`；挂起期间后续未命中门控的消息仍走 AI（含 Router）。客户显式申请由**入口层** `detect_handoff_intent` 命中（不经 Router）；与子图 `handoff_request` 同一挂起门控。申请时 Chatwoot **强提示** = private note + label slug **`handoff-request`**，不改会话 status。入口命中申请的本 turn **只**回挂起确认后结束，不顺带跑 Router。入口判定（human 车道 / 取消 / 申请 / AI）由 `harness/handoff_entry.decide_entry` 单点产出，所有 Transport 共用同一顺序。
_Avoid_: 一请求就切 human；显式申请进 Router；显式转人工与图内 handoff 两套挂起语义；仅靠备注无标签；申请 turn 里夹带业务问答；按入口各写一份门控顺序

**Handoff Joined Notice**:
Seat Takeover 成功后由 Bot 发给客户的短提示（人工已接入）；与挂起申请文案区分。
_Avoid_: 与 HANDOFF_NOTICE 旧「已转接请稍候」混用同一句在申请阶段停 AI

**Seat Takeover**:
坐席用 Chatwoot 官方 **Take over** 接手：status `open` 且能看到人工 assignee（`assignee_type=User` 或 self-assign）。此时才将 Handoff Request 转为接入中，并把 Conversation Mode 切为 `human`。无申请时坐席主动 Take over 同样切 `human`。没有人工 assignee 的 `open`（Agent Bot 失败后系统自动打开）不是接手。若当时有未提交的 Mutation Form Interrupt，先 cancel（不写库）再进 human。
_Avoid_: 自定义「同意」卡当作主路径；把系统 bot-error 自动 open 当成人工接入；Bot 或客户路径 `toggle_status(open)`；接管后仍让迟到的表单提交写库

**Handoff Resolve**:
交回 Bot：Chatwoot status 回到 `pending`（官方 Agent Bot），Conversation Mode 回 `ai`。兼容路径是官方 **Resolve / 已解决**（status=`resolved`），Runtime 再 `toggle_status(pending)`。mode 已是 `ai` 时的 `pending` 忽略，防止环。
_Avoid_: 只改 Chatwoot 已解决却不回切 Runtime mode；把已是 `ai` 的 `pending` 再当成一次 Resolve；用客户「结束会话」代替坐席交回主路径

**Handoff Cancel**:
坐席 Take over 之前，客户取消挂起的 Handoff Request；与申请同属**入口层确定性检测**（`detect_handoff_cancel`，无 Router/LLM），且与申请共用同一 `decide_entry` 判定（取消先于申请，因为「取消转人工」同时命中两者），清挂起且不切 Conversation Mode。之后坐席仍可主动 Seat Takeover。
_Avoid_: 仅靠坐席忽略申请；把 Implicit Cancel（表单）与取消转人工混为一谈；用 Router 自由文本理解取消；申请走入口、取消走 Router 的不对称门控
