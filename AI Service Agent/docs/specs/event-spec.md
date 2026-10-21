# 事件规格（event-spec.md）

- 关联：`architecture.md`（传输层）· `graph-spec.md`（事件来源）· `approval-spec.md`（审批事件）· `safety-spec.md`（拒绝/转人工事件）· `handoff-spec.md`（Seat-gated 生命周期）· [ADR-0003](../adr/0003-seat-gated-handoff.md)
- 术语：根目录 `CONTEXT.md`（Handoff Request / Seat Takeover / Handoff Resolve）

## 1. 传输

- SSE（REST 长连接）与 WebSocket 双通道，同一归一化事件格式。
- 由 `harness/streaming.py` 将 `graph.astream(stream_mode=["messages","updates","custom"])` 转成事件。
- `tool_call` / `tool_result` 等工具事件由 harness hook 在工具执行前后挂接发出，源头不在子图 / 工具内部（见 `architecture.md` §8.1）。
- `handoff_requested` / `handoff_created` / `handoff_resolved` 由**外层（Conversation Runtime）**发出（见 `handoff-spec.md` §4）；子图自身不直接发出这些事件。
  - `handoff_requested`：Handoff Request 挂起建立后（Conversation Mode 仍为 `ai`）。
  - `handoff_created`：**仅** Seat Takeover 完成后（真正进入 `human` 车道）；不得在申请挂起时发出。
  - `handoff_resolved`：Handoff Resolve 回切 `ai` 后。
- resolve 动作触发的**服务端主动 resume** 不产生 SSE 事件流（无长连接上下文），仅更新图状态；用户下一条消息仍按正常 turn 输出事件。

## 2. 事件类型

| type | data | 来源 |
|---|---|---|
| `token` | 文本增量 | 模型流式输出 |
| `tool_call` | `{name, args}` | 工具调用发起 |
| `tool_result` | `{name, ok, summary}` | 工具执行结果 |
| `safety_rejected` | `{rule, reason}` | 安全预检拒绝（见 safety-spec.md） |
| `approval_required` | `{approval_id, summary, expires_at}` | HighRisk interrupt（见 approval-spec.md） |
| `approval_updated` | `{approval_id, status}` | 审批状态变更 |
| `clarification_required` | `{missing: [...], reason?: "tier_mismatch" \| "operation_unclear", candidates?: [...]}` | **HighRisk** select 参数不完整 / 低档工具档不匹配 / 无 tool_call（tier_mismatch）→ clarify（普通轮，见 graph-spec.md §6.3）；**Operation Clarify**（mutation tool 未定：入口层 Hybrid Tool Resolution，或 Mutation select 在 `tool_choice=auto` 下未发起 tool_call 的逃逸，`reason:"operation_unclear"`，不发 `form_required`）；Mutation **档不匹配** `tier_mismatch` 仍可发。Mutation 业务字段缺参**不再**走本事件（改由 Mutation Form / `form_required`）。`candidates` 仅用于 cancel-order 缺单号选择，含最多最新 10 个可取消已付款订单（空数组表示没有可取消已付款订单）；其他 clarify 事件不应携带该字段 |
| `confirmation_required` | `{summary}` | **已弃用（Mutation 主路径）**：旧聊天确认 interrupt。Mutation 写路径现用 Form Interrupt → `form_required` / `form_rejected`（§2.2）。保留类型仅为兼容旧客户端 / harness 映射；新实现不得再从 MutationGraph 发出 |
| `confirmation_updated` | `{action}` | 旧 Mutation 确认结果（confirm / cancel）；主路径由 Form Resume 取代，不再作为 Mutation happy path 事件 |
| `form_required` | 见 §2.2 | Mutation **Form Interrupt** 首次挂起的 SSE 投影（权威挂起态仍是 checkpointer `__interrupt__`，见 `mutation-form-interrupt-spec.md` / `CONTEXT.md`） |
| `form_rejected` | 见 §2.2 | 同一 Mutation Form 会话上校验 / 业务预检失败后**再次 Form Interrupt** 的 SSE 投影（原地更新错误，不新开无关表单会话） |
| `handoff_requested` | `{handoff_id?, reason?, summary?}` | Handoff Request 挂起确认（mode 仍为 `ai`；Conversation Runtime 发出，见 handoff-spec.md §2 / §4） |
| `handoff_created` | `{handoff_id?, reason?, summary?, ticket_id?}` | **Seat Takeover** 完成、进入 human 车道（Conversation Runtime 发出，见 handoff-spec.md §1.2）；**不是**申请挂起时发出；无事先申请的单边 Take over 时 `reason` 可缺省 |
| `handoff_resolved` | `{handoff_id?, conversation_id?}` | Handoff Resolve 完成、mode 回切 ai（Conversation Runtime 发出，见 handoff-spec.md §1.3） |
| `done` | `{final, message_id?}` | 本 turn 结束（`message_id` = 最后一条 assistant 消息的 langchain id，供 Message Store 对账持久化，见 `handoff-spec.md` §5.2） |
| `error` | `{code, message, detail?}` | 归一化错误；`message` 为用户可读文案，`detail` 可选（节点异常原文，供观测/eval，前端可忽略） |

统一结构：`{ "type": str, "data": object }`。

## 2.1 子图事件集合

| 子图 | 允许发出的事件 |
|---|---|
| ReadGraph | `tool_call` / `tool_result` / `token` / `error` / `done` |
| MutationGraph | `tool_call` / `tool_result` / `form_required` / `form_rejected` / `token` / `error` / `done`（可选：`clarification_required` 的 `tier_mismatch`，以及 select 无 tool_call 时的 `operation_unclear`；**不得**再发 `confirmation_required` / `confirmation_updated`） |
| HighRiskGraph | `tool_call` / `tool_result` / `clarification_required` / `approval_required` / `approval_updated` / `token` / `error` / `done` |

- 任意子图触发 Handoff 时，外层先发 `handoff_requested`（挂起、mode 仍 `ai`）；`handoff_created` **仅**在后续 Seat Takeover 时发出。子图自身不直接发出这些事件。
- 安全预检失败时由预检节点发出 `safety_rejected`（见 `safety-spec.md` §1.3），不改变上述子图事件集合。

## 2.2 Form Interrupt → `form_required` / `form_rejected` 映射

> 术语：`CONTEXT.md`（Form Interrupt / Form Resume / Mutation Form / Order Picker / Form-Only Write / Form Cancel / Implicit Cancel）。  
> 运行时：Form Interrupt = `interrupt(value=..., response_schema=...)`（需 `langgraph>=1.2.12`）；Form Resume = `Command(resume=...)`。  
> **本协议事件是投影，不是第二套挂起源**：是否在等表单以 checkpointer 的 pending interrupt 为准。  
> Transport（`/chat`、AgentBot）在挂起时可先 cancel resume 再新 turn（Implicit Cancel）；见 §2.2.1 / §2.2.3。

### 2.2.1 何时发哪种事件

| 图内事实 | SSE `type` | 说明 |
|---|---|---|
| 节点首次（或新开表单会话）Form Interrupt，`value` 无 field/business errors（或 `errors` 空） | `form_required` | 客户端据此渲染 Mutation Form + Order Picker |
| 同一表单会话上校验失败或业务预检失败后**再次** Form Interrupt，`value` 携带 `errors` | `form_rejected` | 原地刷新同一表单；不得当作新会话清空草稿；**不得**当作 Implicit Cancel |
| 用户 Confirm / 显式 Cancel 走 Form Resume | （无单独 `form_*` 成功事件） | Confirm 成功后续走既有 `tool_call` / `tool_result` / `done`；Cancel 不写库并以 `done`（或约定 `error`）收尾。Adapter（如 AgentBot）可在 Confirm 成功后额外投影成功小卡（`cards`），与 Graph `done` 并存；不以新的 SSE `form_*` 类型表达成功 |
| Form Interrupt 挂起且入站**不是**合法 Form 提交（Implicit Cancel，见 `CONTEXT.md`） | （无 `form_*`；随后为新 turn 事件） | Transport 可先对同一 `thread_id` 执行 `Command(resume={"action":"cancel"})` 清挂起（不写库、可无用户可见 cancel tip），再把该消息当**普通新 turn** 跑图；新 turn 按路由正常发事件。不得把该入站当 Form Resume 字段写入 |

### 2.2.2 `data` 形状（与 interrupt 对齐）

统一结构仍为 `{ "type": str, "data": object }`。`data` **由 interrupt `value` 投影**，并附带可选的 schema 元数据：

| 字段 | 来源 | 说明 |
|---|---|---|
| `tool` | `value.tool` | 已确定的 mutation tool 名（Operation Clarify 完成后才进子图） |
| `order_candidates` | `value.order_candidates` | Order Picker 候选项（始终展示；不得因仅一笔而省略） |
| `fields` / `prefills` | `value.fields` / `value.prefills` | 字段定义与聊天已收集预填（具体键由后续 Mutation Form 实现票定稿） |
| `errors` | `value.errors` | **仅 `form_rejected` 必填/非空**：字段级或业务级错误列表/映射，供同一表单展示 |
| `form_session_id` | `value.form_session_id` 或 interrupt `id` | 标识同一表单会话，便于客户端把 `form_rejected` 叠到已打开表单 |
| `response_schema` | `Interrupt.response_schema`（非 `value` 内嵌） | Form Resume（Confirm）期望的 JSON Schema；Cancel 哨兵形状由实现约定，不要求写进该 schema |
| `summary` | `value.summary?` | 可选人类可读摘要 |

约束：

- 客户端**不得**另建「Form Pending」会话库作为权威；pending = 线程上仍有 Form Interrupt。
- Form Resume（Confirm / 显式 Cancel）走既有 resume 通道，**不**再当新用户消息进 Router。
- Implicit Cancel 是 Transport 编排的「先官方 cancel resume，再新 turn」；第二步才是普通用户消息进 Router，第一步仍必须是 `Command(resume=...)`，不得用 `Command(update=...)` 绕过 interrupt。
- `form_required` / `form_rejected` **不**用于 HighRisk 审批（审批仍用 `approval_required`）。
- 成功态：Graph 侧仍以 `done`（及既有 `tool_*`）收尾；Chatwoot Adapter 可另发成功 `cards` / 短文本，不新增 SSE 成功表单事件类型。

### 2.2.3 时序（Mutation Form 主路径）

```text
… -> form_required -> (用户 Confirm: Form Resume) -> tool_call -> tool_result -> done
      # Adapter 可选：成功 cards（与 done 并存；非新 form_* 类型）
… -> form_required -> (校验/预检失败再 interrupt) -> form_rejected -> (修正后 Confirm) -> …
… -> form_required -> (显式 Cancel: Form Resume) -> done   # 不写库
… -> form_required -> (Implicit Cancel: Transport resume cancel) -> (同内容新 turn 事件流…)
      # 静默；不重推旧 form；新 turn 按路由发 token/tool_*/form_*/done 等
```

Mutation happy path **不再**经过聊天 `clarification_required`（缺业务字段）或 `confirmation_required`。tool 未定时的 Operation Clarify 仍可在进图前发 `clarification_required`（`reason:"operation_unclear"`）。

## 3. 事件流时序（示例：HighRisk turn）

```text
token... -> tool_call(cancel_order) -> safety_rejected? / approval_required -> done
```

## 4. 约定

- `done` 必有且为每个 turn 最后一个事件；`error` 后仍应发 `done`。
- 事件必须可独立消费：前端不需要维护图内部状态（含 Mutation Form：不另建 pending-form store）。
- 新增事件类型：更新本文件 + `README.md` 索引（若影响对接）。
