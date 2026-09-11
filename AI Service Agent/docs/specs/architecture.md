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
CustomerServiceGraph (LangGraph 总图, SQLite Checkpointer)
   |
   +- 入口条件边: Active Interrupt?
   |    +- YES -> ResumeHandler  (Command(resume=...) 从断点恢复子图)
   |    +- NO  -> Router
   |
   +- Router 节点 (LLM 结构化输出: capability + risk_level + confidence)
   |         |
   |         v  Policy 纯确定性映射 (capability x risk_level -> 子图)
   |
   +- read      -> ReadGraph      (ReAct agent + 只读工具, 无 interrupt)
   +- mutation  -> MutationGraph  (安全预检 -> interrupt 用户确认 -> 执行)
   +- high_risk -> HighRiskGraph  (安全预检 -> interrupt 人工审批 -> 执行/拒绝)
   +- fallback  -> Concierge 兜底 agent
   |
   v
Final Response (写回 messages, 统一出口 + 归一化事件)
```

业务工具为共享层（ERP/CRM/API 客户端），按 `risk_level` 绑定可调用集合。详见 `graph-spec.md`、`tool-spec.md`、`safety-spec.md`。

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

- `thread_id = session_id`，SQLite checkpoint（异步 `AsyncSqliteSaver`）持久化每次运行的完整状态。
- 会话管理：创建 / 续期 / 历史查询（`get_state` / `aget_state`）。
- Store（用户记忆）：`InMemoryStore` 起步，命名空间 `("users", user_id)`，接口按 `BaseStore` 抽象。
- **传输契约（输入侧）**：`POST /chat { user_id, session_id?, message, meta? }` → 事件流；`POST /sessions { user_id }` → `{ session_id }`。FastAPI 仅做传输与会话映射，不含业务编排（输出侧契约见 `event-spec.md`）。

## 5. 错误处理 / 重试 / 超时（横切）

- 节点级 `RetryPolicy`（指数退避 + jitter + 可配置 `retry_on`）。
- `TimeoutPolicy`（`run_timeout` + `idle_timeout`）防模型/工具悬挂。
- 节点 `error_handler` 将异常归一化为可回复的 `error` 事件，不让图死掉。
- Router 失败 → 直接走 Fallback。

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
  graph/                  # state / builder / router / agents/ / workflows/
  tools/                  # registry / kb / orders
  memory/                 # store
tests/                    # unit / e2e
docs/                     # project-audit.md + specs/
```

完整结构见 `docs/project-audit.md` E 节。

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
