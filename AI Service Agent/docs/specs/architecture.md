# 架构规格（architecture.md）

- 日期：2026-09-11
- 关联：`README.md`（索引）· 子规格：`state-spec.md` / `graph-spec.md` / `tool-spec.md` / `approval-spec.md` / `event-spec.md` / `safety-spec.md`
- 环境基线：`docs/project-audit.md`（LangGraph 1.2.10 实测能力清单与兼容风险）

## 1. 背景与目标

基于 LangGraph 的 AI 客服系统，采用**确定性工作流 + 智能体混合架构**：

- 可预测、可审计：业务关键路径用确定性 workflow，只有需要自由度的地方才用 agent。
- 按操作风险分层：读操作（Read）放开、变更操作（Mutation）用户确认、高风险操作（HighRisk）人工审批。
- 完善的 harness：FastAPI 网关、会话持久化、归一化事件流、审批服务、安全预检、可观测性、错误兜底。
- 多轮对话：每个 turn 重新路由；挂起的中断优先处理（Resume）。

## 2. 架构总览

```text
FastAPI (REST + SSE/WS, 仅传输层)
   |
   v
Conversation Runtime（入口层，轻量 conversation_status）
   |  conversation_mode == human ? -> 人工车道（写 Message Store + 通知坐席，不触图）
   |  conversation_mode == ai      -> 进入 CustomerServiceGraph
   |  接收子图 interrupt(handoff_request) -> 建 Handoff Record + 原子切 mode=human + 发 handoff_created
   |  人工 Resolve -> mode=ai + 发 handoff_resolved
   |  发送 AI 回复前最终校验 mode == ai（实时值，非生成时刻值）
   v
CustomerServiceGraph (LangGraph 总图, SQLite Checkpointer)
   |
   +- 入口 resume 调度（每次图调用前 aget_state 检测挂起中断；P1 定案，
   |     实测依据见 docs/decisions/2026-09-14-checkpointer-resume-handoff.md）
   |    +- 有挂起中断 -> Command(resume=<唯一 schema>, update={messages}) 从断点恢复子图（不重走 Router）
   |    +- 无挂起中断 -> 新 run: Router
   |
   +- Router 节点 (LLM 结构化输出: capability + risk_level + confidence)
   |         |
   |         v  Policy 纯确定性映射 (capability x risk_level -> 子图)
   |
   +- read      -> ReadGraph      (ReAct agent + 只读工具, 无业务 interrupt)
   +- mutation  -> MutationGraph  (安全预检 -> interrupt 用户确认 -> 执行)
   +- high_risk -> HighRiskGraph  (安全预检 -> interrupt 人工审批 -> 执行/拒绝)
   +- fallback  -> Concierge 兜底 agent
   |
   +- 任意子图检测到转人工信号 -> interrupt(handoff_request) -> 外层接管（见上）
   |
   v
Final Response (写回 messages, 统一出口 + 归一化事件)
```

业务工具为共享层（ERP/CRM/API 客户端），按 `risk_level` 绑定可调用集合。详见 `graph-spec.md`、`tool-spec.md`、`safety-spec.md`。

顶层会话流程（`conversation_mode` 判断 → AI / 人工双车道 → 循环回入口）见 `docs/diagrams/handoff-overview.svg`；AI 路径内部结构见 `graph-spec.md`。入口层职责与 `conversation_mode` 状态机见 `handoff-spec.md`。

## 3. 技术选型（含实测依据）

| 选型 | 依据（审计报告 B 节实测） |
|---|---|
| LangGraph 1.2.10 | StateGraph / interrupt / Command / 子图 / 7 种 stream_mode / Store 均实测可用 |
| Agent 构建：`langchain.agents.create_agent` | `langgraph.prebuilt.create_react_agent` 已弃用（`LangGraphDeprecatedSinceV10`）；新 API 签名实测确认（`system_prompt`、`tools` 可空、`middleware`） |
| 结构化输出：`response_format`（pydantic schema） | `create_agent` 支持；用于 Router 的 capability/risk_level/confidence 输出 |
| Checkpointer：`AsyncSqliteSaver`（`langgraph.checkpoint.sqlite.aio`） | 异步路径；`MemorySaver` 仅测试用 |
| Store：`InMemoryStore` 起步 | 用户记忆/画像；接口按 `BaseStore` 抽象，可换 SQLite/Postgres |
| 网关：FastAPI（需 `uv add fastapi`） | 当前环境未装，见审计 G7 |
| 事件传输：SSE + WebSocket | `sse-starlette` / `websockets` 已装 |
| 模型：ARK（OpenAI 兼容） | `.env` 已配；Router 与业务 agent 模型配置化 |
| 离线测试：`FakeMessagesListChatModel` | langchain-core 自带，测试无需联网 |

## 4. 会话与持久化（横切）

- **会话标识定案**：`conversation_id == session_id`（业务 / 契约层会话标识，面向用户与坐席）；`thread_id` 为 LangGraph checkpoint 内部标识，与 `conversation_id` 一对一映射但**不是同一字段**（见 `handoff-spec.md` §5.1）。SQLite checkpoint（异步 `AsyncSqliteSaver`）按 `thread_id` 持久化每次运行的完整状态。
- `thread_id` 在会话创建时生成（如 uuid）并持久化 `conversation_id ↔ thread_id` 映射；同一业务会话需重置图执行状态时可更换 `thread_id` 而保留 `conversation_id`。
- **resume 调度（P1 定案）**：有挂起中断时由入口层（harness）经 `aget_state()` 检测并以 `Command(resume=...)` 恢复（LangGraph 1.2.11 实测：图内节点读不到 `__interrupt__`，见 `graph-spec.md` §3 / 决策记录）；`state.messages` 每次图调用以 Store 水合历史（稳定 id）经 `add_messages` 对账（见 `handoff-spec.md` §5.2）。
- **轻量 `conversation_status` 存储（新增）**：独立于完整 checkpointer 状态（独立表或 Redis key），只存 `conversation_id → conversation_mode`（`ai` / `human` 两态）。`conversation_mode` 判断**禁止**走 `checkpointer.aget()` 读完整 State，必须读该轻量存储。
- `POST /sessions` 创建会话时**显式写入 `conversation_mode = "ai"`**，不依赖存储层隐式默认值；入口判断为两态：`mode == "human" ? 进入人工车道 : 进入 AI 路径`。
- 长连接（SSE / WebSocket）场景可将 `conversation_mode` 缓存在连接级内存，仅在 `handoff_created` / `handoff_resolved` 事件发生时主动更新，不要求每条用户消息重新查库（见 `handoff-spec.md` §2）。
- **Message Store 唯一真相源（2026-09-14 定案）**：会话 user / assistant 轮次以 `conversation_message` 表为权威；入口层（Conversation Runtime）每个 AI turn 先持久化用户消息、再从 Store 水合 `state.messages` 工作视图输入图，正常发送（发送前复检 `mode==ai`）后才持久化 assistant 回复；人工车道只写 Store。工具 / Router 内部消息不入 Store（见 `handoff-spec.md` §5.2、`docs/decisions/2026-09-14-message-store-truth-source.md`）。
- 会话管理：创建 / 续期 / 历史查询（`get_state` / `aget_state`）。
- Store（用户记忆）：`InMemoryStore` 起步，命名空间 `("users", user_id)`，接口按 `BaseStore` 抽象。
- **传输契约（输入侧）**：`POST /chat { user_id, session_id?, message, meta? }` → 事件流；`POST /sessions { user_id }` → `{ session_id }`。FastAPI 仅做传输与会话映射，不含业务编排（输出侧契约见 `event-spec.md`）。

> 未决标注：顶层图中人工车道"写入 Message Store"与图内 `state.messages` 谁是唯一真相源仍未裁决（见 `handoff-spec.md` §5.2 未决问题），本节不给出结论。

## 5. 错误处理 / 重试 / 超时（横切）

- 节点级 `RetryPolicy`（指数退避 + jitter + 可配置 `retry_on`）。
- `TimeoutPolicy`（`run_timeout` + `idle_timeout`）防模型/工具悬挂。
- 节点 `error_handler` 将异常归一化为可回复的 `error` 事件，不让图死掉。
- Router 失败 → 直接走 Fallback。

### Async Node Timeout

异步 LLM 边界节点（Router、Select、ReadGraph、Fallback）使用 `TimeoutPolicy(run_timeout=...)`，且按节点单独配置，不使用 `set_node_defaults(timeout=...)`（后者可能影响隐式同步节点并导致编译失败）。`NodeTimeoutError` 是瞬态错误，由对应 `RetryPolicy` 控制。超时范围是：**Select 覆盖完整 Select 尝试，包括节点内的只读候选 DB 查询；ReadGraph 覆盖完整 ReAct 尝试，包括该尝试内嵌套执行的只读工具工作**。Mutation / HighRisk 写工具执行不得纳入节点超时，直到副作用补偿机制明确。

### Model 节点控制（工厂参数，非 before_model hook）

- **双层超时**：`ChatOpenAI(timeout=...)`（HTTP）必须 **小于** 对应节点 `TimeoutPolicy`（Router：model 5s &lt; node 6s；Select/Read/Fallback：model 10s &lt; node 12s）。SDK `max_retries=0`，避免与图级 `RETRY_LLM` 叠乘。
- **输出 token**：分档 `max_tokens`——Router 256 / Select 512 / Read 1024 / Fallback 512（设在模型实例上）。
- **输入 token**：Context harness（`*_TOTAL_INPUT_MAX_TOKENS` + `trim_messages` / `ContextBudgetMiddleware`）控制送入模型的上下文；与输出 `max_tokens` 分责。
- **temperature**：Router/Select=0；Read/Fallback=0.3。`enable_thinking=False` 全档关闭。

## 6. 可观测性（横切）

- LangSmith tracing（`LANGSMITH_*` 环境变量，补入 `.env.example`）。
- `structlog` 结构化日志：每个 turn 记录耗时 / token / 路由结果 / 是否转人工。
- 可选：prometheus 指标（依赖已装）。

## 7. 测试策略（横切）

- **单测**（fake model，离线）：Router 分类、Policy 映射、工具注册表权限、安全预检、Mutation/HighRisk 校验。
- **中断测试**：interrupt → approve / reject / 过期 三条恢复路径。
- **e2e**：全图一 turn 完整流（fake model + stub 工具）。
- 审批服务：接口测试 + 模拟审批开关。

## 8. 目录结构（横切）

```text
src/ai_service_agent/
  config.py               # pydantic-settings
  main.py                 # CLI
  harness/                # server / events / session / approvals / streaming / errors / tracing
  evals/                  # offline eval runner（AGENTS.md §16）
  graph/                  # state / builder / router / agents/ / workflows/
  tools/                  # registry / kb / orders
  memory/                 # store
tests/                    # unit / e2e
docs/                     # project-audit.md + specs/
```

完整结构见 `docs/project-audit.md` E 节。

## 8.1 Harness 层职责与 hook 扩展点（横切）

- 原则（不可违反）：**挂在循环上，不写进循环里** — 扩展逻辑一律通过 harness hook 注入，不侵入 LangGraph 主循环（agent / 工具执行循环）。
- hook 挂载点：工具执行**前 / 后**（pre/post tool hook）；harness 层提供 hook 作为扩展点，承载横切逻辑：归一化事件（`tool_call` / `tool_result` 等，见 `event-spec.md`）、观测（§6）、审计、限流等。
- 子图与工具自身不得内联这些横切逻辑；工具只做业务动作。实现落点在 `harness/`（server / events / session / approvals / streaming / errors / tracing）。
- hook 职责边界：
  - **State 只读**：hook 可读 LangGraph State / 事件载荷用于观测与审计，**不得修改 State 与业务字段**；任何状态变更必须由图内节点显式完成。
  - **允许阻断**：pre-hook 可阻断工具执行（限流、策略拦截等）；阻断时工具不执行，必须产出归一化事件（`error` 或对应类型），不得静默吞掉。
  - **观测数据去向**：结构化日志（structlog）→ 日志后端；tracing → LangSmith；指标 → prometheus；对外事件 → `event-spec.md` 归一化协议。观测数据不写入 LangGraph State / 会话消息。
- handoff 生命周期事件（`handoff_created` / `handoff_resolved`）由 Conversation Runtime 在接管 / Resolve 时经 harness 事件通道发出（事件契约见 `event-spec.md`；接管机制见 `handoff-spec.md`）。

## 9. 假设与范围外（横切）

**假设（v1）**
- ERP/CRM/API：工具协议接口 + stub 实现（可跑通全链路），真实系统按同接口替换。
- 人工审批：真实可用接口 + 最小人工端（CLI/简单页），测试可用模拟。
- 渠道：先做单 Web 通道（REST/SSE/WS），多渠道后续抽象。
- 模型：单模型配置起步，不做模型路由。

**范围外**
- 多租户 / 复杂鉴权 / 高可用集群 / 队列削峰 / 知识库自动更新管道。

## 10. 风险与缓解（摘要，详见审计 G 节）

- Agent API 弃用 → 统一用 `langchain.agents.create_agent`，禁止新代码用 `prebuilt.create_react_agent`。
- FastAPI / pytest 未装 → 开发第一步安装并加入 pyproject。
- CLI 全局/venv 分离 → 文档与 CI 显式调用全局 `langgraph`。
- ARK Coding 端点特性 → 生产前复核端点/模型额度与审核策略。

## 11. 状态机管理全景（横切）

整个方案由多个**独立、分层管理**的状态机构成，遵循「谁存储、谁决策、谁转移」分层原则：

| 状态机 | 取值 | 存储位置 | 管理位置 | 转移触发 |
|---|---|---|---|---|
| `conversation_mode` | `ai` / `human` | 轻量 `conversation_status`（独立表/Redis key，`conversation_id → mode`） | **入口层**（Conversation Runtime），不进 LangGraph | `ai→human`：收 `interrupt(handoff_request)` 后与建 Handoff Record **同一事务**原子切换；`human→ai`：人工 Resolve（前置校验 pending 审批，归宿=继续挂起，见 `handoff-spec.md` §1.3） |
| `execution_status` | 9 值（`idle…waiting_handoff…failed`） | LangGraph **State 内字段**，随 checkpoint 持久化 | **图内**（Router / 子图 / ResumeHandler 按 turn 阶段置位） | `check_handoff` 抛 `handoff_request` 前置 `waiting_handoff`；Resolve 后 ResumeHandler 恢复转 `running` |
| 审批状态 | `pending / approved / rejected / expired` | Approval Service（轻量状态存储，同 `conversation_status` 定位） | 状态存储在外置服务（惰性过期）；**业务决策在图内** | 图发起 interrupt；HTTP 端点前置校验后 `Command(resume=...)` 恢复（见 `approval-spec.md` §3） |
| Handoff 生命周期 | `created → 接管中 → resolved` | `handoff_record` 表 | **外层**创建 / resolve，与 mode 切换同事务 | 子图 `interrupt(handoff_request)` → 外层建 Record + 切 mode + `handoff_created`；`POST /handoffs/{id}/resolve` → `handoff_resolved` |

- **入口闸门**（`conversation_mode`）只回答"这条消息 AI 能不能碰"，不关心图内进度；判断必须轻量（见 `handoff-spec.md` §2）。
- **图内进度**（`execution_status`）回答"本 turn 走到哪一步"，随 checkpoint 落库；转人工时置 `waiting_handoff`，与 `mode` **分层但语义一致**（分属 checkpoint 与外层表，非同一事务）。
- **审批状态**只由服务持久化，业务决策全在图内；MVP 第一公里未纳入（见 `mvp-first-mile.md` §2）。
- **Handoff Record** 创建与 `mode` 切换为**同一事务**（`handoff-spec.md` §1.2 的原子性）。
- 全景观图：`docs/diagrams/state-machines.html`。

## 12. Order 数据边界（2026-10-10）

- 订单领域通过 `OrderRepository` 暴露，接口包含读事实（`get_order_snapshot` / `list_actionable_orders`）与高风险命令（`cancel_order` / `refund_apply`）。
- `SQLiteOrderRepository` 是唯一知道 SQLite schema、连接方式和事务边界的 adapter；`GraphContext.order_repository` 是图内唯一订单依赖入口。
- HighRisk / Mutation 节点通过 LangGraph `Runtime[GraphContext]` 使用 repository；高风险写工具通过 repository 工厂构造。
- 后续接入 Postgres、HTTP API 或 ERP 时只新增 adapter 并在组装层注入，不改 select / safety / approval / graph 业务逻辑。

