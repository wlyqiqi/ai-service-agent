# AI Service Agent — 项目审计报告

- 审计日期：2026-09-11
- 审计角色：架构工程师（只读审计，未修改任何业务代码）
- 审计方法：以**当前安装环境**为准（包内源码 + 类型定义 + 运行期冒烟验证），不以旧教程推断 API。Context7 MCP 在本会话不可用，故以安装包源码/签名作为权威依据，并用内存图做了真实运行验证。

---

## A. 当前项目技术环境

| 项 | 值 |
|---|---|
| Python | 3.14.5（`.venv` 为 Junction → `D:\常用软件\LangGragh\.venv`；系统 Python 在 `D:\常用软件\python`） |
| 包管理/构建 | `pyproject.toml`（`requires-python = ">=3.14"`，`uv_build` 后端，`[project.scripts] ai-service-agent`） |
| langgraph | **1.2.10**（`importlib.metadata` 确认；`langgraph.__version__` 属性不存在） |
| langgraph-prebuilt | 1.1.0 |
| langgraph-checkpoint | 4.1.1 |
| langgraph-checkpoint-sqlite | 3.1.1 |
| langgraph-sdk | 0.4.2（客户端库） |
| langgraph-runtime-inmem | 0.31.2（本地 dev server 运行时） |
| langchain | 1.3.14 |
| langchain-core | 1.5.3 |
| langchain-openai | 1.4.1（ARK 走 OpenAI 兼容协议） |
| langchain-community / -classic / -text-splitters / -tavily | 0.4.2 / 1.0.8 / 1.1.2 / 0.2.18 |
| langsmith | 0.10.14 |
| openai | 2.52.0 |
| pydantic / pydantic-settings | 2.13.4 / 2.14.2 |
| sqlite-vec | 0.1.9（可用于本地向量检索） |
| trustcall | 0.0.39（可用于用户记忆抽取） |
| 观测/日志 | structlog 25.5.0、prometheus_client、opentelemetry-*（已装） |
| 服务类 | starlette 1.3.1、sse-starlette 3.3.4、uvicorn 0.52.0、websockets 15.0.1、httpx 0.28.1 |
| **FastAPI** | ❌ **未安装** |
| **pytest** | ❌ **未安装** |
| langgraph CLI | ✅ 全局安装：`D:\常用软件\python\Scripts\langgraph.exe`，**0.4.31**（命令：dev / up / build / validate / dockerfile / new / deploy*） |
| `.env` | `ARK_API_KEY` / `ARK_BASE_URL` / `ARK_MODEL` 均已配置；端点 `https://ark.cn-beijing.volces.com/api/coding/v3`（火山 Coding Plan 端点），模型 `deepseek-v4-flash` |
| `langgraph.json` | ✅ `langgraph validate` 通过：`Configuration file ... is valid. (1 graph found)` |

---

## B. 当前 LangGraph 能力清单（1.2.10 实测）

> 以下均经「导入 + 签名检查 + 运行验证」三重确认。

| 能力 | 状态 | 实测结论 |
|---|---|---|
| `StateGraph` | ✅ | `StateGraph(state_schema, context_schema=None, *, input_schema, output_schema)`；State 为自定义 `TypedDict` + `Annotated` reducer（或 pydantic） |
| `State` | ⚠️ | **`langgraph.types` 不再导出 `State`**。老教程的 `from langgraph.types import State` 会 ImportError；用自定义 TypedDict 即可 |
| 内建 `MessagesState` | ✅ | `{'messages': Annotated[list[AnyMessage], add_messages]}`；`add_messages` 从 `langgraph.graph` 导入 |
| Node | ✅ | `add_node(node, action, *, defer, metadata, input_schema, retry_policy, cache_policy, error_handler, destinations, timeout)`；支持函数 / Runnable / StateGraph / **编译后的子图** |
| Edge / `add_sequence` | ✅ | `START`/`END` 哨兵；`add_sequence` 存在 |
| Conditional Edge | ✅ | `add_conditional_edges(source, path, path_map)`；path 可为函数/Runnable；亦支持 `Command(goto=...)` 动态路由 |
| Subgraph | ✅ | **实测**：编译子图作为父图节点运行成功，状态共享 |
| `interrupt` | ✅ | `interrupt(value) -> Any`；**实测**：中断后 state 出现 `__interrupt__`（`Interrupt(value, id)`） |
| `Command` | ✅ | 字段 `graph / update / resume / goto`；**实测** `Command(resume=...)` 从断点恢复成功 |
| Checkpointer | ✅ | `compile(checkpointer=...)`；`MemorySaver` 实测通过；`SqliteSaver`（`langgraph.checkpoint.sqlite`，同步）与 **`AsyncSqliteSaver`（`langgraph.checkpoint.sqlite.aio`，异步）** 均可导入（注意：**异步版不在顶层**） |
| Streaming | ✅ | `stream/astream/astream_events`；`stream_mode` 为 `Literal['values','updates','checkpoints','tasks','debug','messages','custom']`（7 种，`Literal` 非枚举）；多模式列表实测通过；`messages` 模式依赖模型流式事件（ChatOpenAI 支持） |
| Store | ✅ | `compile(store=...)`；`InMemoryStore`（`put/get/search/list_namespaces` + async + TTL）**实测通过**；`BaseStore` 抽象存在；工具可注入 `InjectedStore` |
| ReAct Agent 构建 | ⚠️ 两条路径 | ① `langgraph.prebuilt.create_react_agent`（1.2.10 仍可用，但运行即报 `LangGraphDeprecatedSinceV10`：迁移到 `langchain.agents`）；② **`langchain.agents.create_agent`**（langchain 1.3.14 新 API，签名：`(model, tools=None, *, system_prompt, middleware, response_format, state_schema, context_schema, checkpointer, store, interrupt_before/after, debug, name, cache, transformers)`） |
| `ToolNode` | ✅ | `ToolNode(tools, *, name='tools', handle_tool_errors, messages_key, ...)`；**图内运行实测通过**（Human→AI(tool_call)→ToolMessage→AI 终答）；⚠️ 1.2.x 中**脱离图直调需要运行时 config**（`ToolRuntime`），否则报 `Missing required config key` |
| `tools_condition` | ✅ | `(state, messages_key='messages') -> 'tools' | '__end__'` |
| `Runtime` 注入 | ✅ | `langgraph.runtime.Runtime`（context/store/stream_writer/heartbeat/control）+ `get_runtime()` 新模式 |
| 重试/超时 | ✅ | `RetryPolicy(initial_interval, backoff_factor, max_interval, max_attempts, jitter, retry_on)`；`TimeoutPolicy(run_timeout, idle_timeout, refresh_on)`；节点级 `retry_policy/timeout/error_handler` |
| `Send` | ✅ | `langgraph.types.Send` 可用（map-reduce 扇出） |
| 本地 dev server | ✅ | 全局 CLI 0.4.31 `langgraph dev/up`；`langgraph validate` 已通过；venv 内无 CLI（需用全局的） |

---

## C. 当前脚手架已经提供什么

- `pyproject.toml`：核心依赖已齐（langgraph / langchain / checkpoint-sqlite / langsmith），入口 `ai-service-agent = "ai_service_agent:main"`。
- `langgraph.json`：`{"dependencies": ["."], "graphs": {"agent": "./agent.py:app"}, "env": "./.env"}` —— **CLI 校验通过**，平台/dev 模式可发现图。
- `agent.py`：最小 ReAct 骨架 `create_react_agent(model, tools=[])`，可从 `.env` 加载 ARK 配置。⚠️ 用的是已弃用导入路径。
- `src/ai_service_agent/__init__.py`：`main()` 仅打印 Hello。
- `.env` / `.env.example` / `.gitignore` / `.python-version` / 空 `README.md`。
- 环境里的"隐藏资产"：sqlite-vec、trustcall、structlog、opentelemetry、sse-starlette、langgraph-sdk、fake chat model（离线测试可用）。

---

## D. 当前还缺少什么

1. **项目文档**：AGENTS.md、设计 spec、README 内容（全空）。
2. **业务图**：Router（capability + risk_level 分类）、Read/Mutation/HighRisk 三子图、ResumeHandler、Fallback——目前只有空 ReAct。
3. **Harness**：FastAPI 网关（**fastapi 未安装**）、会话管理（SqliteSaver 接线）、归一化事件协议、审批服务（approvals API + 超时）。
4. **工具层**：工具注册表 + 按 risk_level 的权限绑定、知识库检索、订单/ERP stub。
5. **内存/持久化**：Store（用户记忆）未接线。
6. **可观测性**：`.env.example` 无 LANGSMITH_* 配置；structlog/opentelemetry 未接线。
7. **测试**：pytest 未安装，无任何测试。
8. **清理**：`.langgraph_api/`、`__pycache__/` 残留（gitignore 已覆盖 pycache，但 `.langgraph_api` 未在 `.gitignore`）。

---

## E. 根据目标架构，推荐的项目目录结构

```text
src/ai_service_agent/
  __init__.py
  config.py               # pydantic-settings：ARK_* / LANGSMITH_* / APP_*（会话、审批超时等）
  main.py                 # CLI：serve / validate / dev（复用全局 langgraph CLI 说明）
  harness/                # 运行时 harness 层
    server.py             # FastAPI 网关：REST + SSE/WS 流式（需安装 fastapi）
    events.py             # 归一化事件协议：message / tool_call / approval_required / handoff / done / error
    session.py            # thread_id 管理 + SqliteSaver 接线（AsyncSqliteSaver 供 async 路径）
    approvals.py          # 审批服务：POST /approvals/{id}/approve|reject + 超时过期
    streaming.py          # graph astream → SSE/WS 适配器
    errors.py             # 错误分类 + RetryPolicy / TimeoutPolicy 模板
    tracing.py            # LangSmith tracing + structlog 结构化日志
  graph/                  # 业务图
    state.py              # CustomerServiceState（TypedDict + reducers）
    builder.py            # 总图组装入口（langgraph.json 指向这里）
    router.py             # Router 节点（capability + risk_level 结构化输出）+ 条件边 + ResumeHandler
    agents/               # agent 节点（用 langchain.agents.create_agent）
      faq.py              # FAQ/RAG agent（只读工具）
      service.py          # 业务 agent（读写工具）
      concierge.py        # 兜底 agent
    workflows/            # workflow 子图（确定性流程）
      mutation.py         # MutationGraph：校验 → interrupt 确认 → 执行
      highrisk.py         # HighRiskGraph：interrupt → 人工审批 → 执行
      ticket.py           # 工单流转（可选）
  tools/
    registry.py           # 工具注册 + risk_level → 可用工具集映射
    kb.py                 # 知识库检索（sqlite-vec / 关键词兜底）
    orders.py             # ERP/订单客户端（协议接口 + stub 实现）
  memory/
    store.py              # Store 封装（InMemoryStore 起步，可换 SQLite/Postgres）
tests/
  unit/                   # router / tools / mutation / highrisk（fake model，离线）
  e2e/                    # 全图 + resume/审批恢复路径
docs/
  project-audit.md        # 本报告
  specs/                  # 规格文档（README.md 索引 + 拆分 spec）
langgraph.json            # graphs 改为 "./src/ai_service_agent/graph/builder.py:app"
agent.py                  # 删除或保留为兼容别名
```

---

## F. 需要建立哪些项目级文档

| 文档 | 用途 |
|---|---|
| `AGENTS.md` | 项目根协作规范：架构约定、导入约定（新 create_agent vs 旧 prebuilt）、测试/验证命令 |
| `docs/specs/` | 规格文档目录：`README.md`（索引与阅读组合）、`architecture.md`、`state-spec.md`、`graph-spec.md`、`tool-spec.md`、`approval-spec.md`、`event-spec.md`、`safety-spec.md` |
| `README.md` | 快速开始：环境、`langgraph dev`、测试、目录导航 |
| `docs/project-audit.md` | 本报告（环境基线 + 能力清单 + 风险） |
| （可选）`docs/adr/` | 关键决策记录：如"Agent 构建 API 选型""FastAPI 网关 vs 平台 server" |

---

## G. 发现的版本兼容风险

1. **`create_react_agent` 已弃用（高优先级）**：`langgraph.prebuilt.create_react_agent` 运行即警告 `LangGraphDeprecatedSinceV10`，V2.0 移除；迁移目标是 `langchain.agents.create_agent`（签名不同：`system_prompt` 替代 `prompt`、`tools` 可空、新增 `middleware`）。现有 `agent.py` 用旧路径，需尽早统一到一个入口。
2. **`langgraph.types.State` 不存在**：老教程导入会 ImportError；State = 自定义 TypedDict。
3. **ToolNode 不能裸调用**：1.2.x 直调 `ToolNode().invoke(messages)` 报 `Missing required config key`（需要 `ToolRuntime`）；必须放图内运行。
4. **`AsyncSqliteSaver` 路径变化**：在 `langgraph.checkpoint.sqlite.aio`，顶层没有。
5. **`langgraph.pregel.types` 已弃用**：`StreamMode` 等应从 `langgraph.types` 导入（且为 `Literal`，不是枚举）。
6. **CLI 全局 / venv 分离**：venv 内无 `langgraph` 命令；CI/文档需明确调用全局 `langgraph`（0.4.31）。`langgraph dev` 向 venv 注入运行时依赖时注意版本一致性。
7. **FastAPI / pytest 缺失**：目标架构要求 FastAPI 网关，需 `uv add fastapi`；测试需加 `pytest`（dev 依赖）。
8. **Python 3.14 很新**：依赖均装成功，但生态新库需验证 3.14 兼容（如个别 C 扩展）；升级大版本前先 `pip check`。
9. **ARK 端点属性**：`.env` 用的是 Coding Plan 端点（`/api/coding/v3`）与 `deepseek-v4-flash`；该端点面向编码场景，生产客服建议确认标准 ARK 端点与模型额度/审核策略。
10. **残留目录**：`.langgraph_api/` 未纳入 `.gitignore`，避免误提交。
11. **Studio 父图中子图节点无法下钻（根因已定位，2026-09-27）**：子图用 `set_node_defaults(error_handler=...)` 做节点级容错，静态图中 `__default_error_handler__` 节点与容错节点（如 `finalize_failed`，入边来自运行时 `Command(goto=...)`）均为孤立节点（入度/出度为 0）。`langchain-core` 的 `Graph.first_node()/last_node()` 因候选不唯一返回 `None`，`langgraph/pregel/_draw.py` 的 xray 替换守卫**静默跳过**子图内联，Studio 拿到的 `GET /assistants/{id}/graph?xray=true` 中子图仅剩 `{id, name}`。**缓解**：生产 `langgraph.json` 只注册根 `agent`，避免绕过 Policy Gate；调试用 `langgraph.studio.json` 单独注册四个子图（`harness/studio_graphs.py`，构建参数与 `make_ai_graph` 一致）。Studio 可使用 `langgraph dev -c langgraph.studio.json` 后在图选择器中单独查看各子图；manifest 变更需重启 `langgraph dev`。彻底修复需上游放宽 `_draw.py` 守卫或重构子图容错接法（动 spec §17 语义，暂不做）。

---

## H. 建议的最小开发顺序

1. **文档先行**：`AGENTS.md` + 设计 spec（确认架构与 API 选型：`langchain.agents.create_agent`、`interrupt`/`Command`、SqliteSaver、FastAPI 网关）。
2. **骨架**：`config.py`、`state.py`、`builder.py`（空总图 + Router stub）、改 `langgraph.json` 指向 builder，`langgraph validate` 通过。
3. **Router**：结构化输出分类节点（capability + risk_level）+ 条件边 + 低置信度兜底；fake model 单测。
4. **ReadGraph**：`create_agent` + 只读工具 + 工具注册表（risk 绑定）。
5. **MutationGraph**：`interrupt()` 确认 + `Command(resume=...)` + SqliteSaver 持久化；e2e 覆盖恢复路径。
6. **HighRiskGraph**：`interrupt()` 人工审批 + approvals 服务（approve/reject/超时）。
7. **Harness**：安装 fastapi，REST + SSE/WS 流式，会话管理，归一化事件。
8. **观测与健壮性**：LangSmith tracing、structlog、RetryPolicy/TimeoutPolicy/error_handler。
9. **测试补齐**：pytest（dev 依赖）+ 单元/端到端测试矩阵。
10. **交付态**：README、`langgraph up`/`build`（全局 CLI）验证、清理残留目录。

---

## 附录：验证记录（关键证据）

- `langgraph validate` → `Configuration file D:\AI_Project\AI Service Agent\langgraph.json is valid. (1 graph found)`
- 子图 + interrupt 冒烟：编译子图作为父图节点；`invoke` 后 state 出现 `__interrupt__`；`Command(resume=5)` 后 state 更新为期望值；`get_state` 正常。
- Streaming 冒烟：`stream_mode='updates'` 与多模式列表均正常输出；7 种 stream_mode 均在 `Literal` 类型中确认。
- ToolNode 冒烟：图内 ReAct 循环产出 `[HumanMessage, AIMessage(tool_call), ToolMessage, AIMessage(final)]`；离线 fake model 可驱动全链路（`langchain_core.language_models.fake_chat_models` 可用，测试无需联网）。
- Store 冒烟：`InMemoryStore.put/get/search` 通过。
- 弃用警告实证：`create_react_agent` 运行输出 `LangGraphDeprecatedSinceV10: create_react_agent has been moved to langchain.agents ...`
