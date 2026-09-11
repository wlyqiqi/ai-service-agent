# 事件规格（event-spec.md）

- 关联：`architecture.md`（传输层）· `graph-spec.md`（事件来源）· `approval-spec.md`（审批事件）· `safety-spec.md`（拒绝/转人工事件）

## 1. 传输

- SSE（REST 长连接）与 WebSocket 双通道，同一归一化事件格式。
- 由 `harness/streaming.py` 将 `graph.astream(stream_mode=["messages","updates","custom"])` 转成事件。

## 2. 事件类型

| type | data | 来源 |
|---|---|---|
| `token` | 文本增量 | 模型流式输出 |
| `tool_call` | `{name, args}` | 工具调用发起 |
| `tool_result` | `{name, ok, summary}` | 工具执行结果 |
| `safety_rejected` | `{rule, reason}` | 安全预检拒绝（见 safety-spec.md） |
| `approval_required` | `{approval_id, summary, expires_at}` | HighRisk interrupt（见 approval-spec.md） |
| `approval_updated` | `{approval_id, status}` | 审批状态变更 |
| `confirmation_required` | `{summary}` | Mutation interrupt（等待用户确认，见 graph-spec.md §6.2） |
| `confirmation_updated` | `{action}` | 用户确认结果（confirm / cancel） |
| `handoff` | `{reason, ticket_id?}` | 触发转人工（见 safety-spec.md） |
| `done` | `{final}` | 本 turn 结束 |
| `error` | `{code, message}` | 归一化错误 |

统一结构：`{ "type": str, "data": object }`。

## 3. 事件流时序（示例：HighRisk turn）

```text
token... -> tool_call(order_cancel) -> safety_rejected? / approval_required -> done
```

## 4. 约定

- `done` 必有且为每个 turn 最后一个事件；`error` 后仍应发 `done`。
- 事件必须可独立消费：前端不需要维护图内部状态。
- 新增事件类型：更新本文件 + `README.md` 索引（若影响对接）。
