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
- `messages` 对账（P1 定案）：水合消息带稳定 id（user = `store-{row_id}`、assistant = 落库时的 langchain message id），每次图调用以完整水合历史为输入，经 `add_messages` 按 id 对账，使 `state.messages` 恒等于 Store 快照；图内自产消息（无 store id，如工具消息 / Router 结构化输出）作为工作历史随 checkpoint 累积，不入 Store（见 `handoff-spec.md` §5.2）。
- `__interrupt__` 由 LangGraph 自动维护，不放进业务字段；它是恢复判断的**权威依据**，`pending_approval` 为派生业务字段（供审批服务查询），两者保持一致。
- `execution_status` 为每个 turn 的执行阶段状态：`idle`（会话创建 / 无活动 turn）→ `routing`（Router 分类中）→ `running`（子图执行中）；`clarifying`（**P4 起由 clarify 节点产出**：select 参数不完整，普通轮非 interrupt，D1）、`waiting_confirmation`（Mutation 等待用户确认）、`waiting_approval`（HighRisk 等待人工审批）、`waiting_handoff`（因中途触发 Handoff 而暂停，既非正常完成也非失败）、`completed`（正常完成）、`failed`（失败）。
- `context` 业务键（P4/P5 登记，D1/D3/B2b）：`clarify_missing`（本轮缺失字段列表）、`clarify_count`（澄清轮次，完整成功 select 清零、clarify 自增，>N=2 转 fallback）、`precheck_data`（预检事实快照：订单状态，仅事实、无裁决）、`tier_clarify`（写图 select 选中已注册低档工具时的意图-档不匹配标记，值为工具名；由 clarify 节点消费，完整 select 时清除）、`cancellation_candidates`（cancel-order 缺单号时的可取消已付款订单候选，由当次 select 写入、clarify 消费；select 每次进入结果分支前清除，仅 cancel-order 澄清路径重新写入）。
- 子图抛出 `handoff_request` 后 `execution_status` **必须**置 `waiting_handoff`，**不得**置 `completed` 或 `failed`；人工 Resolve 回切 `conversation_mode=ai` 后由服务端主动 resume 完成被中断子图，最终由 `final` 节点置 `completed`（`running` 为 resume 执行中的中间态，不落最终态；见 `graph-spec.md` §3、§6.5）。
- **P0（2026-09-18）入口层显式转人工**：命中入口层 `detect_handoff_intent` 时不进入 LangGraph，该 turn 不产生图内 checkpoint，故 `execution_status` 不被置位（无图执行，`final` 节点不参与）；仅入口层 `conversation_status` 记 `conversation_mode=human`。图内动态转人工（子图抛 `handoff_request`）仍按上一段语义置 `waiting_handoff`，并由 resolve 主动 resume 至 `completed`。
- `conversation_mode` **不在此 State 中**：它属于入口层轻量 `conversation_status` 存储（`handoff-spec.md` §2），图内不维护；`handoff` 字段是图内转人工信号标记，与 `conversation_mode` 分层。

## 2. 子图局部状态

- Read / Mutation / HighRisk 子图共享 `CustomerServiceState` 的 `messages` / `context`，各自编译为独立 `StateGraph`，作为总图节点嵌套。
- 子图只返回自身负责的字段更新，写回总图；不得擅自覆盖其他子图字段。

## 3. 字段变更纪律

- 新增字段必须在此登记，并同步更新 `graph-spec.md` 中读写该字段的节点清单。
- 禁止在子图内引入与总图重名的私有状态字段（避免 reducer 冲突）。
