# 状态规格（state-spec.md）

- 关联：`architecture.md`（总览）· `graph-spec.md`（消费方）

## 1. 总图状态 `CustomerServiceState`

```python
class CustomerServiceState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]   # 对话历史（唯一跨子图累积字段；真相源=Message Store，每次图调用水合的工作视图，见 handoff-spec.md §5.2）
    user_id: str
    session_id: str
    route_intent: str              # Router 唯一语义输出：稳定路由意图（不是 tool_name / target_graph）
    target_graph: Literal["read_graph", "mutation_graph", "high_risk_graph", "fallback"]
    execution_class: Literal["read_only", "standard_write", "high_risk"]
    allowed_tools: list[str]       # Policy 派生的候选工具边界
    requires_approval: bool        # Policy 派生的高风险审批标记
    confidence: float
    routing_backend: str | None   # Router 观测字段：jev / deepseek / qwen / …，不参与路由
    routing_fallback: bool        # Router 观测字段：是否使用 fallback 模型
    route_control: Literal["policy", "deepseek_fallback"] | None
                                  # 路由控制字段：唯一允许决定一次 DeepSeek fallback 的信号
    guard_status: Literal["pass", "fail"] | None   # Guard 并行检测结果（每轮覆盖，见 graph-spec.md §2）
    guard_reason: str | None       # Guard fail 时的规则包名（prompt_injection / approval_bypass / …）；pass 为 None
    context: dict                  # 业务上下文：订单、账户、工单等
    pending_approval: ApprovalRequest | None
    handoff: bool                  # 转人工信号标记（check_handoff 置位，见 graph-spec.md §6.5）
    handoff_reason: str            # 转人工触发源说明；由 check_handoff 确定性写入，供 interrupt(reason) 使用
    execution_status: Literal["idle", "routing", "clarifying", "running",
                              "waiting_confirmation", "waiting_approval",
                              "waiting_handoff", "completed", "failed"]
```

规则：

- `messages` 用 `add_messages` reducer，全图唯一累积通道；其余字段按需覆盖。
- `messages` 为入口层从 Message Store 水合的工作视图（`handoff-spec.md` §5.2）；图内只累积 / 工作，持久化由入口层完成，工具 / Router 内部消息不入 Store。
- `messages` 对账（P1 定案）：水合消息带稳定 id（user = `store-{row_id}`、assistant = 落库时的 langchain message id），每次图调用以完整水合历史为输入，经 `add_messages` 按 id 对账，使 `state.messages` 恒等于 Store 快照；图内自产消息（无 store id，如工具消息 / Router 结构化输出）作为工作历史随 checkpoint 累积，不入 Store（见 `handoff-spec.md` §5.2）。
- `__interrupt__` 由 LangGraph 自动维护，不放进业务字段；它是恢复判断的**权威依据**，`pending_approval` 为派生业务字段（供审批服务查询），两者保持一致。
- `execution_status` 为每个 turn 的执行阶段状态：`idle`（会话创建 / 无活动 turn）→ `routing`（Router 分类中）→ `running`（子图执行中）；`clarifying`（**HighRisk / tier_mismatch / Mutation Operation Clarify**：聊天补参或问清操作类型，普通轮非 interrupt；Mutation 业务字段缺参不再置此态，改走 Form Interrupt）、`waiting_confirmation`（Mutation 子图内 Form Interrupt 挂起 / 等待 Form Resume；**子图 `interrupt()` 未结束时该更新不并回父图 checkpoint，父图仍为 `routing`**）、`waiting_approval`（HighRisk 子图内等待人工审批；同样在子图挂起期间不并回父图）、`waiting_handoff`（因中途触发 Handoff 而暂停，既非正常完成也非失败）、`completed`（正常完成）、`failed`（失败）。
- **Form / 审批挂起的对外权威是 checkpointer `__interrupt__`（SSE 投影 `form_required` / `approval_required`）**，不是父图 `execution_status`。观测与车道判据不得要求父图出现 `waiting_confirmation` / `waiting_approval`。
- `context` 业务键（P4/P5 登记，D1/D3/B2b）：`clarify_missing`（**HighRisk** 本轮缺失字段列表；Mutation 业务缺参不再写此键）、`clarify_count`（澄清轮次，完整成功 select 清零、clarify / Mutation 内联 Operation Clarify 自增，>N=2 转 fallback）、`fallback_count`（同一会话 fallback 回复次数；最多 N=4 次，第 N+1 次 fallback 由 check_handoff 确定性转人工，触发后清零）、`precheck_data`（预检事实快照：订单状态，仅事实、无裁决）、`tier_clarify`（**HighRisk** select 选中已注册低档工具或无 tool_call 时的档不匹配标记；由 clarify 节点消费，完整 select 时清除。Mutation 不再写此键）、`cancellation_candidates`（cancel-order 缺单号时的可取消已付款订单候选，由当次 select 写入、clarify 消费；select 每次进入结果分支前清除，仅 cancel-order 澄清路径重新写入）、`form_session_id`（Mutation Form 同一表单会话标识，随 Form Interrupt `value` 投影；Cancel/成功写库后清除）、`form_errors`（校验/业务预检失败后供同一节点再次 Form Interrupt 的 `errors`；成功 Confirm 或 Cancel 时清除。权威挂起态仍是 checkpoint interrupt，不是「Form Pending」旁路）。（已移除 `operation_clarify` 上下文标记：Mutation Operation Clarify 由 Mutation Select（`mutation_select_llm`）内联发事件并写 messages，见 ADR 0004 / `CONTEXT.md`。）
- 子图抛出 `handoff_request` 后 `execution_status` **必须**置 `waiting_handoff`，**不得**置 `completed` 或 `failed`；人工 Resolve 回切 `conversation_mode=ai` 后由服务端主动 resume 完成被中断子图，最终由 `final` 节点置 `completed`（`running` 为 resume 执行中的中间态，不落最终态；见 `graph-spec.md` §3、§6.5）。
- **P0（2026-09-18）入口层显式转人工**：命中入口层 `detect_handoff_intent` 时不进入 LangGraph，该 turn 不产生图内 checkpoint，故 `execution_status` 不被置位（无图执行，`final` 节点不参与）；仅入口层 `conversation_status` 记 `conversation_mode=human`。图内动态转人工（子图抛 `handoff_request`）仍按上一段语义置 `waiting_handoff`，并由 resolve 主动 resume 至 `completed`。
- `conversation_mode` **不在此 State 中**：它属于入口层轻量 `conversation_status` 存储（`handoff-spec.md` §2），图内不维护；`handoff` 字段是图内转人工信号标记，与 `conversation_mode` 分层。
- `route_intent` 是 Router 的唯一语义输出（外加 `confidence` 与观测/控制字段）；`target_graph`、`execution_class`、`allowed_tools`、`requires_approval` 必须由 Policy Gate 确定性派生。
- `allowed_tools` 是候选工具边界，不是最终工具选择；最终工具调用仍由子图完成。
- State **不含**历史 Router 字段 `capability` / `risk_level`；工具注册表的 `min_risk_level`（见 `tool-spec.md`）是工具档位，与 State 无关，不得混用。

## 2. 子图局部状态

- Read / Mutation / HighRisk 子图共享 `CustomerServiceState` 的 `messages` / `context`，各自编译为独立 `StateGraph`，作为总图节点嵌套。
- 子图只返回自身负责的字段更新，写回总图；不得擅自覆盖其他子图字段。

## 3. 字段变更纪律

- 新增字段必须在此登记，并同步更新 `graph-spec.md` 中读写该字段的节点清单。
- 禁止在子图内引入与总图重名的私有状态字段（避免 reducer 冲突）。
- `routing_backend` / `routing_fallback` 是 Router 观测字段；Policy Gate 与子图不得将其作为路由依据。
- `route_control` 是路由控制字段：`deepseek_fallback` 允许 `router_join` 进入一次 DeepSeek；`policy` 直接进入 Policy Gate。该字段不属于业务状态，Router/Policy Gate 之外的节点不得修改。
