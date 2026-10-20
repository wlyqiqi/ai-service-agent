# AI Service Agent

基于 LangGraph 的 AI 客服系统：**确定性工作流 + 智能体混合架构**，按操作风险分层（Read / Mutation / HighRisk），配套完整 harness（FastAPI 网关、审批、事件、会话、观测）。

> 里程碑：`v0.1.0`（P0–P5 全量落地）· 当前 693 passed / 2 skipped（`pytest tests --ignore=tests/evals`）· `langgraph validate` valid · 真机冒烟通过

## 架构概览

```
FastAPI Transport（REST / SSE，只做 Transport）
  └─ Conversation Runtime（conversation_mode 入口层判断：ai / human）
       └─ LangGraph CustomerServiceGraph
            ├─ Guard（确定性 v1：恶意意图 / 情绪升级检测，先于 Router）
            ├─ Router（json_mode 结构化输出：capability × risk_level，不执行工具）
            ├─ Policy Gate（确定性映射 target_graph，不调 LLM）
            ├─ Select + Safety Precheck（写操作参数澄清 / 预检 / recheck）
            ├─ 子图：ReadGraph(ReAct) / MutationGraph(显式 Workflow) / HighRiskGraph(人工审批)
            └─ Checkpointer（SQLite，thread_id）· interrupt / resume
Harness：Approval Service · Handoff Service · Message Store · Conversation Status Store · 事件协议
```

## 技术栈

- Python 3.14 · LangGraph 1.2.10 · langchain-core 1.5.3 · langchain-openai 1.4.1
- 模型：火山方舟 ARK（OpenAI 兼容，`.env` 配置）
- FastAPI + SSE（sse-starlette）· SQLite checkpointer（`AsyncSqliteSaver`）· pytest / httpx

## 快速开始

```powershell
# 1) 安装依赖（含 dev 组）
uv sync --group dev

# 2) 配置环境变量
copy .env.example .env   # 填入 ARK_API_KEY / ARK_BASE_URL / ARK_MODEL

# 3) 启动服务（127.0.0.1:8000；配置 fake_model=true 时使用离线 fake model）
uv run ai-service-agent

# 4) LangGraph Studio（默认 checkout/pull main；功能分支加 -Branch <name>）
.\scripts\start_studio.ps1
#    -Tracing      让 Studio 看到 run（进程级覆盖 .env，不改文件）
#    -WithApp      同时启动 FastAPI runtime :8000
#    -OpenBrowser  启动后自动拉起浏览器
#    -Port 2025    端口被占用时换一个（脚本会先检测占用并报出 PID）

# 5) 真实模型 SSE 冒烟（FAQ / Mutation / HighRisk / Handoff / Clarify 五场景）
uv run python scripts/real_model_http_smoke.py
```

## 测试与评估

```powershell
# 单元 / 集成测试（含 interrupt 三态恢复、Guard、Select 档位、ToolRegistry 注入）
uv run pytest

# 离线确定性评估（routing / policy / guard / handoff 四维，0 依赖真实 API key）
uv run python -m ai_service_agent.evals --data tests/evals/control --out eval-report.json

# 图结构校验（如环境内置 langgraph CLI）
langgraph validate
```

## 真机冒烟场景

详见 `docs/real-model-smoke.md`（真实 ARK 模型，SSE 事件流证据）：

| 场景 | 输入示例 | 预期链路 |
|---|---|---|
| FAQ / Read | 查订单状态 | SSE 完整事件流 → `done` |
| Mutation | 把收货地址改成北京市朝阳区 | `form_required`（Form Interrupt）→ Form Resume confirm → 工具只执行一次 → `done`（见 `event-spec.md` §2.2） |
| HighRisk | 订单10086申请退款 | `approval_required` → `/approvals/{id}/approve` → resume → 工具只执行一次 → `done` |
| Handoff | 转人工 | `handoff_created` → `conversation_mode=human` → `/handoffs/{id}/resolve` → AI 恢复接管 → `done` |
| Clarify | 退了吧（CTRL-0076） | `clarification_required`（缺订单号）→ 补参后下一轮进入 HighRisk |

## 文档导航

- 规格：`docs/specs/`（architecture / graph / state / event / safety / approval / tool / handoff / mutation-form-interrupt）· 术语：`CONTEXT.md`
- 决策记录：`docs/decisions/`（P1–P5 各阶段定案 + Guard / 0076 评估）
- 实现计划：`docs/plans/`（2026-09-14 起各阶段 TDD 计划）
- 审计与实测：`docs/project-audit.md`（环境基线）· `docs/real-model-smoke.md`（真机证据）
- 评估数据集：`tests/evals/control/`（master.jsonl + 分维子集）

## 设计要点

- 统一使用 `langchain.agents.create_agent`，禁止 `langgraph.prebuilt.create_react_agent`。
- FastAPI 只做 Transport，业务编排全部留在图内；入口层仅做 `conversation_mode` 轻量判断。
- LLM 只负责理解，权限与执行决策由确定性代码做出（Guard / Policy Gate / Safety Precheck / 属主校验）。
- `conversation_mode` 两态（`ai`/`human`），handoff 建记录 + 切 mode 同事务原子完成；`thread_id` 为独立 checkpoint 标识。
- Guard v1 为过渡实现：字面 adversarial 样本 5/5 拦截；语义层改写（同义替换 / 间接暗示）漏判已登记为已知风险（`docs/decisions/2026-09-15-p4-guard-llm-eval.md`）。
