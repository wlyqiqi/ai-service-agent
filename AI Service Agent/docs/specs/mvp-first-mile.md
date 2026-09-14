# MVP 第一公里实现范围（mvp-first-mile.md）

- 日期：2026-09-14
- 状态：实现范围 / 里程碑规格（brainstorming 定稿，下一步 writing-plans）
- 关联：`architecture.md`（架构总览）· `handoff-spec.md`（状态机 / 接管机制）· `graph-spec.md`（图内部）· `event-spec.md`（事件协议）· `state-spec.md`（State）

## 1. 目标与验收线

从空壳到一条**最小但真实**的端到端闭环，逐项验证刚定案的 6 项核心决策 + 1 个 handoff 最小验证：

1. 会话创建：`POST /sessions` → 生成 `thread_id`（uuid）、持久化 `conversation_id ↔ thread_id` 映射、**显式写入 `conversation_mode=ai`**（轻量 `conversation_status` 表）。
2. 消息入口：`POST /chat` → 入口层读 `conversation_status`（两态判断，不触 checkpointer）→ `ai` 才进图。
3. 图内部：入口 resume 调度（无挂起中断时新起 run）→ Router（`langchain.agents.create_agent` 新 API + 结构化输出）→ Policy 确定性映射 → **ReadGraph**（只读工具）→ Final Response。
4. 事件输出：SSE 归一化事件（`tool_call / token / error / done`）。
5. 发送前实时校验 `mode==ai`（防双重回复）。
6. 测试：pytest + 离线 fake model + stub 工具。
7. **handoff 最小验证（选项 B）**：显式"转人工" → `interrupt(handoff_request)` → 外层建 Record + 原子切 `mode=human` + `handoff_created` → Resolve 回切。

## 2. 范围

**纳入**
- 会话服务、轻量 `conversation_status`、`conversation_id ↔ thread_id` 映射。
- Router / Policy / ReadGraph / Fallback（含 `check_handoff`）与统一出口。
- handoff 最小闭环（创建 / 接管 / 人工车道占位 / resolve 回切）。
- SSE 事件流、入口错误归一化、SQLite 持久化、pytest。

**排除（后续增量）**
- Mutation / HighRisk / 审批（Approval Service）、Guard 节点、SSE/WS 长连接 mode 缓存、LangSmith/structlog/prometheus 完整观测、Store（用户记忆）、安全预检。

## 3. 组件与目录

```text
src/ai_service_agent/
  config.py               # pydantic-settings：模型端点/数据库路径/阈值
  main.py                 # CLI 入口（uv run ai-service-agent）
  harness/
    server.py             # FastAPI 应用：POST /sessions、POST /chat、POST /handoffs/{id}/resolve
    session.py            # 会话服务：thread_id 生成 + conversation_status 读写（SQLite 独立表）
    handoff.py            # Handoff Record + mode 原子切换 + resolve（MVP 同库独立表）
    streaming.py          # graph.astream → SSE 归一化事件
    errors.py             # 入口异常 → error 事件归一化
  graph/
    state.py              # CustomerServiceState（含 execution_status）
    builder.py            # 总图：Router / Policy / ReadGraph / Fallback / 统一出口（resume 由 harness 调度）
    router.py             # create_agent 新 API + response_format 结构化输出
    agents/read_agent.py  # ReadGraph：create_agent + 只读工具
    workflows/fallback.py # Fallback + check_handoff（interrupt(handoff_request)）
  tools/
    registry.py           # ToolRegistry + risk 注入
    kb.py / orders.py     # stub 只读工具（kb_search / order_lookup）
tests/                    # pytest
```

## 4. 数据流

### 4.1 建会话
`POST /sessions {user_id}` → 生成 `thread_id`（uuid）→ `conversation_status` 显式写 `mode=ai` → 持久化映射 → 返回 `{session_id}`（`conversation_id == session_id`，见 `handoff-spec.md` §5.1）。

### 4.2 AI 路径（mode=ai）
`POST /chat` → 入口读 `conversation_status`：`human` → 人工车道（§4.3）；`ai` → 入口 resume 调度（`aget_state()` 检测挂起中断，有则 `Command(resume=<唯一 schema>, update={messages})` 从中断点恢复，无则 `graph.ainvoke(config={"configurable": {"thread_id": ...}})` 新起 run）→ Router → Policy → ReadGraph → Final Response → SSE 事件（`tool_call / token / done`）；发送前实时复检 `mode==ai`，否则丢弃。

### 4.3 人工车道（mode=human）
入口直接写 Message Store（`conversation_message` 表，**唯一真相源**，见 `handoff-spec.md` §5.2）并返回人工占位响应，**不触发 checkpointer / Graph / Guard / Router**；AI 车道每个 turn 从 Store 水合 `state.messages` 工作视图。

### 4.4 handoff 最小闭环
Fallback 内 `check_handoff` 命中显式"转人工"规则 → 置 `execution_status=waiting_handoff` + `interrupt({"type": "handoff_request", "reason", "summary"})` → 外层（streaming 层观察 interrupt）建 `handoff_record` + **同一事务**切 `mode=human` → 发 `handoff_created`（含外层"已转人工"文案，发 token + 落 Message Store）。`POST /handoffs/{id}/resolve` → 校验 pending 审批（MVP 无审批，跳过占位）→ `mode=ai` → 发 `handoff_resolved` → **服务端主动 resume 完成被中断子图**（`Command(resume={"action": "handoff_resumed"})`，不等待用户下一条消息；见 `handoff-spec.md` §1.3）。

## 5. 依赖与运行

- `uv add`：`fastapi`、`uvicorn`、`sse-starlette`、`pydantic-settings`、`pytest`（`websockets` 后续；`langsmith`/`structlog` 观测后续）。
- 开发期模型：离线 `FakeMessagesListChatModel`（测试确定性）；`config` 保留 ARK 配置供真实运行。
- 数据库：单个 SQLite 文件，三张表——`conversation_status`（`conversation_id ↔ mode`）、`conversation_id ↔ thread_id` 映射、`handoff_record`；checkpoint 用 `AsyncSqliteSaver`（独立连接）。

## 6. 测试与验收清单

1. `POST /sessions` → 返回 id、`mode=ai`、映射持久化。
2. `POST /chat`（ai）→ SSE 含 `token…done`，不触发人工车道。
3. `POST /chat` 无效会话 → 归一化 `error`。
4. 显式"转人工" → `handoff_created` + `mode=human` + `execution_status=waiting_handoff`。
5. `mode=human` 时发消息 → 不触图、人工占位响应。
6. resolve → `handoff_resolved` + `mode=ai`。
7. 发送前校验：生成过程中切 human → 回复被丢弃。
8. Router / Policy 映射单测（fake model 确定性输出）。
9. 首次端到端：`uv run pytest` 全绿 + `uvicorn` 手动 curl 验证。

## 7. 未决标注

- ~~`conversation_message` 与 `state.messages` 唯一真相源~~：**已定案（2026-09-14）**——Message Store 为唯一权威，`state.messages` 为每次图调用的水合工作视图（见 `handoff-spec.md` §5.2 与 `docs/decisions/2026-09-14-message-store-truth-source.md`）。
