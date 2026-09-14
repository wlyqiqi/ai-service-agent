# Repository Guidelines

## 1. 项目目标
基于 LangGraph 构建 AI 客服系统：确定性工作流 + 智能体混合架构，按操作风险分层（Read / Mutation / HighRisk），配套完整 harness（FastAPI 网关、审批、事件、会话、观测）。

## 2. 技术栈
- Python 3.14（`.venv` 为 junction → `D:\常用软件\LangGragh\.venv`）；LangGraph 1.2.10、langchain-core 1.5.3、langchain-openai 1.4.1、langsmith 0.10.14；模型走火山方舟 ARK（`.env` 配置，OpenAI 兼容）。
- FastAPI 网关（**未安装**，使用前先加入 pyproject）；SQLite checkpointer；`langgraph` CLI 全局 0.4.31（venv 内无 CLI）。
- 环境基线与实测能力清单：`docs/project-audit.md`。

## 3. LangGraph 使用原则
- 以当前安装环境与类型定义为准，**禁止按旧教程假设 API**（例：`langgraph.prebuilt.create_react_agent` 已弃用，统一用 `langchain.agents.create_agent`）。
- 关键 API 可用性以 `docs/project-audit.md` B 节实测清单为准，不凭记忆推断。

## 4. FastAPI 与 LangGraph 的边界
- FastAPI 只负责 **Transport**（REST / SSE / WS、审批 HTTP 接口）；**业务编排必须留在 LangGraph**（Router、Policy、子图、中断恢复全部在图内）。
- **例外（入口层职责）**：`conversation_mode` 轻量判断与 AI / Human 路由属于 Conversation Runtime 入口层，在进入 LangGraph 之前完成；`conversation_mode == human` 时不触发 checkpointer / Graph / Guard / Router。权威定义见 `docs/specs/handoff-spec.md`。

## 5. Router 的职责（边界摘要）
- 只输出 `capability / risk_level / confidence` 结构化字段；**不得执行 tools、不得直接决定 target_graph、不得修改业务状态**。详见 `docs/specs/graph-spec.md`。

## 6. capability 与 risk_level 的区别（边界摘要）
- `capability` = 业务能力（faq / order / account / …），`risk_level` = 执行模式（read / mutation / high_risk），二者正交；子图由 Policy 按 (capability × risk_level) 决定。详见 `docs/specs/graph-spec.md`。

## 7. Policy Gate 的职责（边界摘要）
- `target_graph` 由**确定性代码**查映射表得出，不调 LLM；高风险操作必须经过确定性 Policy Gate。详见 `docs/specs/graph-spec.md`。

## 8. 子图边界（边界摘要）
- ReadGraph 可用 ReAct；MutationGraph 必须是显式 Workflow；HighRiskGraph 必须有人工 Approval。
- **子图之间不做内部转交**：每个新用户 turn 都重新经过 Router 判断意图与风险等级，不在子图内实现向其他子图跳转的逻辑。详见 `docs/specs/graph-spec.md`。

## 9. interrupt / resume 规则（边界摘要）
- 有挂起中断时新 turn 必须走 ResumeHandler（`Command(resume=...)`）恢复，不得重新 Router；批准 / 拒绝 / 过期语义见 `docs/specs/graph-spec.md` 与 `docs/specs/approval-spec.md`。

## 10. State 管理规则（边界摘要）
- `messages`（add_messages）是唯一跨子图累积通道；业务字段按 `docs/specs/state-spec.md` 定义，子图不得覆盖非自身字段。

## 11. Tool 设计规则（边界摘要）
- 工具必须声明 `side effect / risk / idempotency`；按 `min_risk_level` 在构建期注入工具集，ReadGraph 拿不到写工具。详见 `docs/specs/tool-spec.md`。

## 12. Approval 规则（边界摘要）
- HighRisk 必须先过安全预检再发起审批；审批通过 / 拒绝 / 过期后以 `Command(resume=...)` 恢复，操作幂等。详见 `docs/specs/approval-spec.md`。

## 13. Event Protocol 规则（边界摘要）
- 对外输出一律走 `docs/specs/event-spec.md` 定义的事件协议（该文件为唯一权威，不在本文件重复枚举）；新增事件类型须同步更新该文件。

## 14. Checkpointer 规则（边界摘要）
- 生产用 SQLite checkpointer（`AsyncSqliteSaver`），`thread_id = session_id`（**沿用旧定义**；`conversation_id` 与 `thread_id` / `session_id` 的映射关系仍未裁决，见 `docs/specs/handoff-spec.md` §5 未决问题）；`MemorySaver` 仅测试用。详见 `docs/specs/architecture.md` §4。

## 15. 测试规则（边界摘要）
- pytest（未安装，先登记 pyproject）；离线 fake model + stub 工具优先；interrupt 的 approve / reject / expired 三态恢复路径必测。详见 `docs/specs/architecture.md` §7。

## 16. Eval 规则
- 路由准确率、安全预检误拒率、handoff 准确率需有可重复运行的评估集（离线、确定性数据），评估结果不得依赖人工临时判断。

## 17. 错误处理规则（边界摘要）
- 节点级 RetryPolicy / TimeoutPolicy / error_handler；异常归一化为 `error` 事件且每个 turn 必发 `done`。详见 `docs/specs/architecture.md` §5。

## 18. 安全规则
- **LLM 负责理解，不负责最终权限控制**；权限与执行决策由确定性代码（Policy Gate / 安全预检）做出。
- a. Router 执行之前（或与之并行），必须存在**独立的恶意意图 / 情绪升级检测节点**，不依赖 Router 分类时顺带判断。
- b. **Handoff 触发必须是显式规则**（用户明确要求、投诉关键词命中、同一工具连续失败 N 次、情绪评分超阈值等），不允许 LLM 在生成回复时临时决定转人工。
- c. **Handoff 执行机制**：子图 / `check_handoff` 检测到转人工信号时**不自行创建 Handoff Record、不自行切换 `conversation_mode`**，统一抛出 `interrupt({"type": "handoff_request", ...})`，由外层（Conversation Runtime）接管：建 Record + 原子切换 mode + 发 `handoff_created`。AI 回复发送前必须实时校验 `conversation_mode == ai`，否则丢弃。详见 `docs/specs/handoff-spec.md`。
- 规则集见 `docs/specs/safety-spec.md`。

## 19. 依赖变更规则
- 依赖统一在 `pyproject.toml` 声明；新增依赖需说明理由并验证 Python 3.14 兼容；禁止直接 import 未登记包。环境基线见 `docs/project-audit.md`。

## 20. Codex 开发任务执行规则
- **不允许构建"万能 Agent"**；能力按 capability 拆分，子图职责边界不可模糊。
- 非 trivial 任务必须按 **Inspect → Plan → Implement → Test → Review → Regression** 执行。
- 未获要求不得修改无关模块；实现前先读对应 spec（阅读组合见 `docs/specs/README.md`）。

## 21. Harness 规则（边界摘要）
- 横切扩展点（事件、观测、审计、限流等）一律通过 harness hook **挂在工具执行前后**（挂在循环上，不写进循环里），禁止写进 agent / 工具执行循环内部；权威定义见 `docs/specs/architecture.md` §8.1。

## 22. conversation_mode 规则（边界摘要）
- 两态：`ai` / `human`。`POST /sessions` 时**显式写入 `ai`**，不依赖存储层隐式默认值；判断不设第三态兜底。
- `ai → human` 必须与创建 Handoff Record 处于**同一数据库事务**（原子操作）；`human → ai` 由人工 Resolve 触发，切换前必须校验该会话是否存在 pending 审批（审批归宿待决策，见 `docs/specs/handoff-spec.md` §5 未决问题）。
- 判断必须轻量：读独立 `conversation_status`（`conversation_id → conversation_mode`），禁止用完整 `checkpointer.aget()` 判断 mode；判断在入口层完成，进入任何 LangGraph 逻辑之前。详见 `docs/specs/handoff-spec.md`。
