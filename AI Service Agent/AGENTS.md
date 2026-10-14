# Repository Guidelines

## 1. 项目目标

基于 LangGraph 的 AI 客服 harness：确定性工作流 + 智能体混合架构，按操作风险分层（Read / Mutation / HighRisk），配套 FastAPI 网关、审批、事件、会话与观测。

## 2. 环境与基线

- 运行时与依赖以 **`pyproject.toml`** 与 **`.python-version`** 为准；本地 venv 见仓库布局。
- 已装能力与 API 实测清单：改图 / 换 API 前读 **`docs/project-audit.md`**（尤其 B 节）。
- 模型与密钥走 **`.env`**（OpenAI 兼容；具体供应商见环境文件，不在此复述）。

## 3. 框架优先

- **Framework-first**：图编排、interrupt / resume、工具执行、checkpointer、预置 agent / 节点——先采用 LangGraph / LangChain **官方组件或推荐路径**（如 `interrupt` + `Command(resume=...)`、`ToolNode`、`langchain.agents.create_agent`）。
- **Docs via Context7**：API、版本差异或官方推荐写法不确定时，先用 **Context7 MCP** 查当前 `langgraph` / `langchain` 文档，再编码。文档与本机实测冲突时，以 **已安装环境**（`docs/project-audit.md`）为准并注明差异。
- 仅当官方组件盖不住已定规格时才写适配层，并写清：**官方缺口、不能用的原因、自写边界**。

## 4. Transport vs Graph

- **FastAPI = Transport**（REST / SSE / WS、审批 HTTP）。
- **业务编排留在 LangGraph**（Router、Policy、子图、中断恢复）。
- **入口层例外**：`conversation_mode` 轻量判断与 AI / Human 路由在进图之前完成；`human` 模式不触发 checkpointer / Graph / Guard / Router。细节：`docs/specs/handoff-spec.md`。

## 5. Spec 阅读纪律

实现前按任务读规格；**权威在 specs，不在本文件摘要**。任务 → 文件组合见 **`docs/specs/README.md`**。领域用词见根目录 **`CONTEXT.md`**。

| 改动主题 | 先读 |
|---|---|
| 总图 / Router / Policy / 子图 / resume | `docs/specs/graph-spec.md` |
| State 字段 | `docs/specs/state-spec.md` |
| 工具与 risk 注入 | `docs/specs/tool-spec.md` |
| 审批 | `docs/specs/approval-spec.md` |
| 对外事件（SSE/WS） | `docs/specs/event-spec.md` |
| Mutation Form / Form Interrupt | `docs/specs/mutation-form-interrupt-spec.md` + `CONTEXT.md` |
| 安全预检 / handoff 触发 | `docs/specs/safety-spec.md` |
| conversation_mode / Handoff 生命周期 | `docs/specs/handoff-spec.md` |
| 持久化 / 错误 / harness hook / 测试基线 | `docs/specs/architecture.md` |
| C 端入口（Widget / AgentBot） | `docs/adr/0001-widget-agentbot-form-contract.md`（`user-page-spec.md` 已废止） |
| 架构决策留痕 | `docs/decisions/`（新决策优先 `docs/adr/`，见 `docs/agents/domain.md`） |

## 6. 常驻硬约束

- **Deterministic authority**：LLM 负责理解；权限与执行决策由确定性代码（Policy Gate / 安全预检）做出。
- **Capability-scoped**：按 capability 拆分子图与工具集；不做「万能 Agent」。
- **Harness hooks**：横切（事件、观测、审计、限流）挂在工具执行前后，不写进 agent / 工具循环内部（`architecture.md` §8.1）。
- **Eval**：路由 / 安全误拒 / handoff 等指标用可重复离线集，不靠人工临时判断。

## 7. 开发节奏

非 trivial 任务按 **Inspect → Plan → Implement → Test → Review → Regression**。未要求不改无关模块；实现前先完成 §5 的阅读组合。

## 8. 依赖变更

新依赖只进 **`pyproject.toml`**，说明理由并确认与当前 Python 兼容；不直接 import 未登记包。

## Agent skills

### Issue tracker

Issues live in this repo's GitHub Issues (`gh` CLI). See `docs/agents/issue-tracker.md`.

### Triage labels

Canonical roles map 1:1 to tracker labels (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: root `CONTEXT.md` + `docs/adr/`. See `docs/agents/domain.md`.
