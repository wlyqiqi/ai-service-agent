# 状态规格（state-spec.md）

- 关联：`architecture.md`（总览）· `graph-spec.md`（消费方）

## 1. 总图状态 `CustomerServiceState`

```python
class CustomerServiceState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]   # 对话历史（唯一跨子图累积字段）
    user_id: str
    session_id: str
    capability: str                # Router 输出的能力标签
    risk_level: Literal["read", "mutation", "high_risk"]
    confidence: float
    guard_status: Literal["pass", "fail"] | None   # Guard 并行检测结果（每轮覆盖，见 graph-spec.md §2）
    context: dict                  # 业务上下文：订单、账户、工单等
    pending_approval: ApprovalRequest | None
    handoff: bool                  # 是否转人工
```

规则：

- `messages` 用 `add_messages` reducer，全图唯一累积通道；其余字段按需覆盖。
- `__interrupt__` 由 LangGraph 自动维护，不放进业务字段；它是恢复判断的**权威依据**，`pending_approval` 为派生业务字段（供审批服务查询），两者保持一致。

## 2. 子图局部状态

- Read / Mutation / HighRisk 子图共享 `CustomerServiceState` 的 `messages` / `context`，各自编译为独立 `StateGraph`，作为总图节点嵌套。
- 子图只返回自身负责的字段更新，写回总图；不得擅自覆盖其他子图字段。

## 3. 字段变更纪律

- 新增字段必须在此登记，并同步更新 `graph-spec.md` 中读写该字段的节点清单。
- 禁止在子图内引入与总图重名的私有状态字段（避免 reducer 冲突）。
