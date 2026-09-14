# 状态规格（state-spec.md）

- 关联：`architecture.md`（总览）· `graph-spec.md`（消费方）

## 1. 总图状态 `CustomerServiceState`

```python
class CustomerServiceState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]   # 对话历史（唯一跨子图累积字段；真相源=Message Store，每次图调用水合的工作视图，见 handoff-spec.md §5.2）
    user_id: str
    session_id: str
    capability: str                # Router 输出的能力标签
    risk_level: Literal["read", "mutation", "high_risk"]
    confidence: float
    guard_status: Literal["pass", "fail"] | None   # Guard 并行检测结果（每轮覆盖，见 graph-spec.md §2）
    context: dict                  # 业务上下文：订单、账户、工单等
    pending_approval: ApprovalRequest | None
    handoff: bool                  # 转人工信号标记（check_handoff 置位，见 graph-spec.md §6.5）
    execution_status: Literal["idle", "routing", "clarifying", "running",
                              "waiting_confirmation", "waiting_approval",
                              "waiting_handoff", "completed", "failed"]
```

规则：

- `messages` 用 `add_messages` reducer，全图唯一累积通道；其余字段按需覆盖。
- `messages` 为入口层从 Message Store 水合的工作视图（`handoff-spec.md` §5.2）；图内只累积 / 工作，持久化由入口层完成，工具 / Router 内部消息不入 Store。
- `__interrupt__` 由 LangGraph 自动维护，不放进业务字段；它是恢复判断的**权威依据**，`pending_approval` 为派生业务字段（供审批服务查询），两者保持一致。
- `execution_status` 为每个 turn 的执行阶段状态：`idle`（会话创建 / 无活动 turn）→ `routing`（Router 分类中）→ `running`（子图执行中）；`waiting_confirmation`（Mutation 等待用户确认）、`waiting_approval`（HighRisk 等待人工审批）、`waiting_handoff`（**新增**：因中途触发 Handoff 而暂停，既非正常完成也非失败）、`completed`（正常完成）、`failed`（失败）。
- 子图抛出 `handoff_request` 后 `execution_status` **必须**置 `waiting_handoff`，**不得**置 `completed` 或 `failed`；人工 Resolve 回切 `conversation_mode=ai` 后，ResumeHandler 恢复原被中断子图时转回 `running`（见 `graph-spec.md` §3、§6.5）。
- `conversation_mode` **不在此 State 中**：它属于入口层轻量 `conversation_status` 存储（`handoff-spec.md` §2），图内不维护；`handoff` 字段是图内转人工信号标记，与 `conversation_mode` 分层。

## 2. 子图局部状态

- Read / Mutation / HighRisk 子图共享 `CustomerServiceState` 的 `messages` / `context`，各自编译为独立 `StateGraph`，作为总图节点嵌套。
- 子图只返回自身负责的字段更新，写回总图；不得擅自覆盖其他子图字段。

## 3. 字段变更纪律

- 新增字段必须在此登记，并同步更新 `graph-spec.md` 中读写该字段的节点清单。
- 禁止在子图内引入与总图重名的私有状态字段（避免 reducer 冲突）。
