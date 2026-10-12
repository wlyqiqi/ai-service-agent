# 事件规格（event-spec.md）

- 关联：`architecture.md`（传输层）· `graph-spec.md`（事件来源）· `approval-spec.md`（审批事件）· `safety-spec.md`（拒绝/转人工事件）

## 1. 传输

- SSE（REST 长连接）与 WebSocket 双通道，同一归一化事件格式。
- 由 `harness/streaming.py` 将 `graph.astream(stream_mode=["messages","updates","custom"])` 转成事件。
- `tool_call` / `tool_result` 等工具事件由 harness hook 在工具执行前后挂接发出，源头不在子图 / 工具内部（见 `architecture.md` §8.1）。
- `handoff_created` / `handoff_resolved` 由**外层（Conversation Runtime）**在接管 / Resolve 时发出（见 `handoff-spec.md` §3），子图自身不直接发出 `handoff_created`。
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
| `clarification_required` | `{missing: [...], reason?: "tier_mismatch", candidates?: [...]}` | select 参数不完整 / 低档工具档不匹配（tier_mismatch）→ clarify 节点（普通轮、非 interrupt，D1/B2b，见 graph-spec.md §6.2/§6.3）。`candidates` 仅用于 cancel-order 缺单号选择，含最多最新 10 个可取消已付款订单（空数组表示没有可取消已付款订单）；其他 clarify 事件不应携带该字段 |
| `confirmation_required` | `{summary}` | Mutation interrupt（等待用户确认，见 graph-spec.md §6.2）。**Mutation Form 主路径将由 `form_required` 取代**（见 §2.2）；在 Form Interrupt 落地前仍为当前写路径事件 |
| `confirmation_updated` | `{action}` | 用户确认结果（confirm / cancel） |
| `form_required` | 见 §2.2 | Mutation **Form Interrupt** 首次挂起的 SSE 投影（权威挂起态仍是 checkpointer `__interrupt__`，见 `mutation-form-interrupt-spec.md` / `CONTEXT.md`） |
| `form_rejected` | 见 §2.2 | 同一 Mutation Form 会话上校验 / 业务预检失败后**再次 Form Interrupt** 的 SSE 投影（原地更新错误，不新开无关表单会话） |
| `handoff_created` | `{handoff_id?, reason, summary?, ticket_id?}` | 转人工接管完成（Conversation Runtime 外层发出，见 handoff-spec.md §3） |
| `handoff_resolved` | `{handoff_id?, conversation_id?}` | 人工 Resolve 完成、mode 回切 ai（Conversation Runtime 发出） |
| `done` | `{final, message_id?}` | 本 turn 结束（`message_id` = 最后一条 assistant 消息的 langchain id，供 Message Store 对账持久化，见 `handoff-spec.md` §5.2） |
| `error` | `{code, message, detail?}` | 归一化错误；`message` 为用户可读文案，`detail` 可选（节点异常原文，供观测/eval，前端可忽略） |

统一结构：`{ "type": str, "data": object }`。

## 2.1 子图事件集合

| 子图 | 允许发出的事件 |
|---|---|
| ReadGraph | `tool_call` / `tool_result` / `token` / `error` / `done` |
| MutationGraph | `tool_call` / `tool_result` / `clarification_required` / `confirmation_required` / `confirmation_updated` / `form_required` / `form_rejected` / `token` / `error` / `done` |
| HighRiskGraph | `tool_call` / `tool_result` / `clarification_required` / `approval_required` / `approval_updated` / `token` / `error` / `done` |

- 任意子图触发 Handoff 时额外可能发出 `handoff_created`：**由外层统一发出**，子图自身不直接发出这个事件。
- 安全预检失败时由预检节点发出 `safety_rejected`（见 `safety-spec.md` §1.3），不改变上述子图事件集合。

## 2.2 Form Interrupt → `form_required` / `form_rejected` 映射

> 术语：`CONTEXT.md`（Form Interrupt / Form Resume / Mutation Form / Order Picker）。  
> 运行时：Form Interrupt = `interrupt(value=..., response_schema=...)`（需 `langgraph>=1.2.12`）；Form Resume = `Command(resume=...)`。  
> **本协议事件是投影，不是第二套挂起源**：是否在等表单以 checkpointer 的 pending interrupt 为准。

### 2.2.1 何时发哪种事件

| 图内事实 | SSE `type` | 说明 |
|---|---|---|
| 节点首次（或新开表单会话）Form Interrupt，`value` 无 field/business errors（或 `errors` 空） | `form_required` | 客户端据此渲染 Mutation Form + Order Picker |
| 同一表单会话上校验失败或业务预检失败后**再次** Form Interrupt，`value` 携带 `errors` | `form_rejected` | 原地刷新同一表单；不得当作新会话清空草稿 |
| 用户 Confirm / Cancel 走 Form Resume | （无单独 `form_*` 成功事件） | Confirm 成功后续走既有 `tool_call` / `tool_result` / `done`；Cancel 不写库并以 `done`（或约定 `error`）收尾 |

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
- Form Resume（Confirm/Cancel）走既有 resume 通道，**不**再当新用户消息进 Router。
- `form_required` / `form_rejected` **不**用于 HighRisk 审批（审批仍用 `approval_required`）。

### 2.2.3 时序（Mutation Form 目标路径）

```text
… -> form_required -> (用户 Confirm: Form Resume) -> tool_call -> tool_result -> done
… -> form_required -> (校验/预检失败再 interrupt) -> form_rejected -> (修正后 Confirm) -> …
… -> form_required -> (Cancel: Form Resume) -> done   # 不写库
```

当前生产 Mutation 写路径仍可能发出 `confirmation_required`（见 §2）；上表为 Form Interrupt 落地后的目标投影契约。

## 3. 事件流时序（示例：HighRisk turn）

```text
token... -> tool_call(cancel_order) -> safety_rejected? / approval_required -> done
```

## 4. 约定

- `done` 必有且为每个 turn 最后一个事件；`error` 后仍应发 `done`。
- 事件必须可独立消费：前端不需要维护图内部状态（含 Mutation Form：不另建 pending-form store）。
- 新增事件类型：更新本文件 + `README.md` 索引（若影响对接）。
