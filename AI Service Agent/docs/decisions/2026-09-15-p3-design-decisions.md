# 决策：P3 范围与设计定案（Guard / 权限 / Memory / Mutation / HighRisk / Handoff / Hook / State / Eval）

- 日期：2026-09-15
- 状态：定案（待 Spec 修订 + Readiness Check + Writing Plan；本文档为 P3 决策唯一记录）
- 来源：P3 Design Audit（2026-09-15，只读审计）→ 用户审核 D1–D14 → 修正/补充说明后定稿
- 关联：`docs/specs/graph-spec.md`（§1/§2/§5/§6）· `safety-spec.md`（§1/§2）· `approval-spec.md` · `handoff-spec.md`（§1.3/§2）· `state-spec.md`（§1）· `tool-spec.md`（§1/§2/§4）· `architecture.md`（§4/§8.1/§11）· `docs/plans/2026-09-14-p2-tools-and-evals.md`（OQ-3/OQ-4）

## 0. 批准范围（用户裁定）

- **批准**：D1、D3–D7、D10–D12、D14。
- **批准（带补充说明）**：D2（Guard 确定性 v1 为过渡，非终态）、D8（Approval Service SQLite，须登记迁移触发条件）。
- **裁定**：D9（pending-approval 检查为只读信息项，不影响 resolve 分支）。
- **措辞修正**：D13（9 条 skip 覆盖 §2.1 全部未实现规则，情绪类仅 2 条；"情绪评分超阈值"规则采纳方案A）。

## 1. 决策记录

### D1：graph-spec 标注修正 + Guard 占位定性

- 修正 `docs/specs/graph-spec.md` §1 总图标注：`GuardNode ∥ RouterNode [P2 起]` → `[P3 起]`（纯文档修正；P2 实际未启用 Guard）。
- 定性：P2 eval 的 `skip(guard_pending)` 是 **MVP"推迟 Guard 节点"决策框架内的占位设计**（可随时启用的覆盖率占位），**不是推翻该决策，也不是对 P3 启用的预授权**；本决策 D2 才是启用 Guard 的依据。

### D2：Guard 采用确定性规则 v1（过渡实现，非终态）

- P3 启用 Guard 节点（并行于 Router，输出 `guard_status`，规则基，无模型 key，离线可测）。
- **v1 过渡声明（必须写进实现与测试注释）**：v1 是**过渡方案，不是 Guard 的终态能力**；确定性关键词/启发式**无法有效覆盖语义层面的恶意输入/越权诱导**（如同义替换、间接暗示等绕过关键词匹配的攻击）。后续必须评估升级为 **LLM Guard 或其它语义层判断方式**，升级评估登记为后续关注项，**防止 v1 事实上永久化**。

### D3：eval 主键改为 expected_policy，拆分 Guard 与安全预检

- runner 由 `expected_graph` 主键改为 `expected_policy` 主键。
- 5 条 `guard_reject`（adversarial，CTRL-0096–0100）→ Guard 断言（`safety_rejected rule=guard`、Router 输出不被消费、不触工具）。
- 9 条 `deny`（policy_violation 5 + tool_permission 4）→ 安全预检断言（`safety_rejected`、不执行、不 interrupt）。

### D4：安全预检 = 确定性节点 + v1 规则集

- 图内确定性节点 `safety_check`，位于 Mutation/HighRisk 执行前（safety-spec §1.1，先预检后 interrupt）。
- v1 规则集：参数 schema 白名单（`ToolMeta.schema`）+ `min_risk_level` 匹配 + `user_id` 登录态 + 数据边界（跨租户拦截）。发 `safety_rejected` 事件并收尾。
- 完整业务规则表（频次/额度/状态机约束、敏感复核）留待后续增量，不在 P3 强制。

### D5：新增 3 个写工具 stub

- `account_update`（mutation）/ `order_cancel`（high_risk）/ `refund_apply`（high_risk），按 tool-spec §1 声明 `min_risk_level` 与 `schema`，`idempotent=True` + 参数带 `request_id`（tool-spec §2）。
- stub 形态：内存 fake store（同 Read 工具 stub 深度，不引入真实外部系统依赖）；按 risk 档注册进 ToolRegistry，`for_risk_level` 分层注入不变。

### D6：MutationGraph = 预检 → 确认 → 恢复 → recheck → 执行

- 显式 workflow（非 ReAct）：`safety_check` → `interrupt`（`confirmation_required` 事件，含影响摘要）→ resume `{"action":"confirm"|"cancel"}` → **最小不变量 recheck**（request_id 存在、参数未变、context 非陈旧）→ 执行写工具 / 拒绝收尾。
- 确认结果写入 `context`（graph-spec §9），不新增 State 字段。
- 复用唯一 resume schema（graph-spec §3 / approval-spec §3.3），不设新分支。

### D7：HighRiskGraph = 预检 → 审批 → 恢复 → recheck → 执行

- `safety_check` → `interrupt`（`approval_required` 事件）→ Approval Service → resume `{"action":"approve"|"reject","note"?}` / `{"result":"expired"}` → 最小不变量 recheck → 执行/拒绝/过期收尾。
- 幂等：同一 `approval_id` 重复 decision 天然幂等（approval-spec §2）；服务端每 thread 锁（P1 既有）防双重 resume；补并发双 resume 测试。

### D8：Approval Service = SQLite 轻量存储（P3 实现）+ 迁移触发条件

- SQLite 表 + FastAPI 端点（`POST /approvals/{id}/approve|reject`、`GET /approvals/{id}`、`GET /approvals?status=pending`），只做前置有效性检查 + 转发；业务决策在图内；`simulate_approval` 测试开关走同一路径。
- **迁移触发条件（登记，类比 checkpointer SQLite→Postgres）**：出现**多进程 / 容器化部署**需求时，将 Approval Service 迁移至独立存储（如 Postgres），并同步评估线程锁与 checkpointer 的迁移；避免"先用着，后面才想起要迁移"。

### D9：resolve 的 pending-approval 检查 = 只读信息项（裁定）

- `POST /handoffs/{id}/resolve` 对 pending-approval 的检查是**一次只读查询，仅用于日志记录 / 坐席展示"该会话存在未处理审批"**，**不影响 resolve 端点本身的执行分支**——resolve 无论审批状态如何都正常完成：handoff record 标记 `resolved`、`conversation_mode` 切回 `ai`。
- 审批的处理完全交由 **Approval Service 自身状态机**与**入口层 resume 调度**在 AI 重新接管后自然恢复；Handoff 逻辑对审批**不做任何判断或干预**。
- Approval 与 Handoff 是两个**完全独立的状态机**；此次检查**不构成功能合并**，只是消除"人工接管期间系统对仍存在的挂起审批一无所知"这一信息盲区。

### D10：Memory 不进 P3

- Store（用户记忆，`("users", user_id)` 命名空间，`InMemoryStore`→`BaseStore`）不在 P3 范围；如需，另行子计划（水合节点 + 写回节点 + InjectedStore 接线）。
- Memory Retrieve/Write/Summary 不得做成 Hook（hook 对 State 只读；记忆是业务能力，属图内节点/工具）。

### D11：沿用 astream 事件派生，不重建 hook 层

- P3 沿用现有 `graph.astream` updates 派生事件（streaming.py）；仅当需要限流/审计时才加薄 before/after tool hook。
- Guard / 安全预检 / Policy / resume 调度 / mode 切换与 handoff Record 创建 / 审批语义解读、State 变更——**一律不得藏在 hook 中**，必须留在图内节点或 Runtime。

### D12：State 字段补齐（spec 对齐，不新增额外字段）

- `CustomerServiceState` 补 `guard_status: Literal["pass","fail"] | None` 与 `pending_approval: ApprovalRequest | None`（state-spec §1 已定义）。
- 确认结果入 `context`；`request_id` 入工具参数/context；不引入其他新字段。

### D13：handoff 规则扩展不实现（方案A），9 条保持 skip

- **情绪评分触发规则（此前方案A/B 讨论）采纳方案A（不实现）**，`skip(handoff_rule_pending)` 保持。
- 措辞修正（事实核实）：9 条 `handoff_rule_pending` **并非全部对应情绪评分**，而是覆盖 safety-spec §2.1 全部未实现规则：
  - 情绪类 2 条：`CTRL-0063`（投诉关键词+点名主管）、`CTRL-0066`（负向情绪高频词连续出现）。
  - 其余 7 条：`CTRL-0062`（显式人工意图变体）、`CTRL-0064`（路由连续失败 N=3）、`CTRL-0065`（重复未解决 X=3）、`CTRL-0067`（审批被拒后坚持）、`CTRL-0068`（策略上限）、`CTRL-0069`（工具级连续失败 N=3）、`CTRL-0070`（安全敏感信号→Guard 转人工）。
- 1 条真实命中（`CTRL-0061`）保留为 pass 断言。

### D14：eval skip → 真实断言

- runner 按 D2/D3/D4/D6/D7 转化：`guard_pending`→Guard/预检断言、`mutation_pending`→MutationGraph 断言、`high_risk_pending`→HighRiskGraph 断言；routing 维度转端到端（含 interrupt 恢复路径）。
- `handoff_rule_pending`（9 条）按 D13 保持 skip。

## 2. 后续关注（防止"过渡"永久化）

- **Guard 升级评估**：D2 v1 上线后登记评估 LLM Guard / 语义层判断（覆盖同义替换、间接暗示等绕过）；触发条件：v1 误判率超阈值或出现语义绕过案例。
- **Approval Service 迁移评估**：D8 迁移触发条件 = 多进程/容器化部署需求出现时。
- **Memory 子计划**：D10 若需求明确，另行规划。

## 3. 影响与下一步

- 本决策为 P3 Spec/Decision 的依据；下一步：Spec 修订（graph-spec §1/§2/§6、state-spec §1、safety-spec、approval-spec、tool-spec §4、event-spec 如需）→ Readiness Check → Writing Plan → Implementation。
- 涉及文件：`docs/specs/*`、`src/ai_service_agent/graph/{state,builder}.py`、`graph/agents/{mutation_agent,high_risk_agent}.py`（新增）、`graph/workflows/guard.py`（新增）、`tools/`（3 写工具）、`harness/approvals.py`（新增）、`evals/runner.py`。
