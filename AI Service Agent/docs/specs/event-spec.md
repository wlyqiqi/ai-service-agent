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
| `confirmation_required` | `{summary}` | Mutation interrupt（等待用户确认，见 graph-spec.md §6.2） |
| `confirmation_updated` | `{action}` | 用户确认结果（confirm / cancel） |
| `handoff_created` | `{handoff_id?, reason, summary?, ticket_id?}` | 转人工接管完成（Conversation Runtime 外层发出，见 handoff-spec.md §3） |
| `handoff_resolved` | `{handoff_id?, conversation_id?}` | 人工 Resolve 完成、mode 回切 ai（Conversation Runtime 发出） |
| `done` | `{final, message_id?}` | 本 turn 结束（`message_id` = 最后一条 assistant 消息的 langchain id，供 Message Store 对账持久化，见 `handoff-spec.md` §5.2） |
| `error` | `{code, message}` | 归一化错误 |

统一结构：`{ "type": str, "data": object }`。

## 2.1 子图事件集合

| 子图 | 允许发出的事件 |
|---|---|
| ReadGraph | `tool_call` / `tool_result` / `token` / `error` / `done` |
| MutationGraph | `tool_call` / `tool_result` / `clarification_required` / `confirmation_required` / `confirmation_updated` / `token` / `error` / `done` |
| HighRiskGraph | `tool_call` / `tool_result` / `clarification_required` / `approval_required` / `approval_updated` / `token` / `error` / `done` |

- 任意子图触发 Handoff 时额外可能发出 `handoff_created`：**由外层统一发出**，子图自身不直接发出这个事件。
- 安全预检失败时由预检节点发出 `safety_rejected`（见 `safety-spec.md` §1.3），不改变上述子图事件集合。

## 3. 事件流时序（示例：HighRisk turn）

```text
token... -> tool_call(cancel_order) -> safety_rejected? / approval_required -> done
```

## 4. 约定

- `done` 必有且为每个 turn 最后一个事件；`error` 后仍应发 `done`。
- 事件必须可独立消费：前端不需要维护图内部状态。
- 新增事件类型：更新本文件 + `README.md` 索引（若影响对接）。
