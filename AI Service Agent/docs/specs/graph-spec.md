# 图规格（graph-spec.md）

- 关联：`state-spec.md`（状态契约）· `tool-spec.md`（工具集）· `safety-spec.md`（预检与转人工）· `approval-spec.md`（审批服务）· `architecture.md`（总览）

## 1. 总图结构与入口条件边

```text
START
  |
  +- 条件边: Active Interrupt?（读 state["__interrupt__"]，权威依据）
  |    +- YES -> ResumeHandler
  |    +- NO  -> GuardNode ∥ RouterNode（并行，同一份输入，互不依赖）
  |                 |                |
  |                 +---(pass)-------+---> Policy 映射（capability x risk_level）-> 子图
  |                 +---(fail)------------> 安全收尾（丢弃 Router 并行结果）
  |                                                     |
  |                                                     v
  |                                         Read / Mutation / HighRisk / Fallback
  |
  END（Final Response 统一出口）
```

- 每个新 turn 先检查 `__interrupt__`；有挂起中断时语义是"回应上一个问题"，不重新走 Router。
- Final Response：子图结果写回 `messages`，由 harness 统一推送事件（见 `event-spec.md`）。

## 2. Guard 节点（并行检测）

- 与 Router **并行**执行，输入为同一份原始用户输入（`messages` + `user_id`），**不依赖 Router 的分类结果**，谁先完成不影响正确性。
- 只输出 `guard_status`（pass / fail），不参与 capability/risk_level 分类，不执行 tools，不修改业务状态。
- `fail` → 走安全收尾（见 `safety-spec.md` §1.5），Router 并行结果**不被消费**：实现上若调用可取消则取消，否则以"不消费其输出"兜底。
- 检测信号与阈值定义见 `safety-spec.md` §1.5。

## 3. ResumeHandler 节点

- 读取 `__interrupt__`，解析新消息为 `approve / reject / 其他`。
- resume payload 唯一 schema（审批 / 用户确认 / 超时共用，harness 直调与用户 turn 两条恢复入口均须遵守）：
  `Command(resume= { "action": "approve" | "reject" | "confirm" | "cancel", "note"?: str } | { "result": "expired" })`
- 中断过期：`Command(resume={"result": "expired"})`，子图按过期分支收尾。

## 4. Router 节点

- LLM 结构化输出：`capability`（faq / order / account / smalltalk / unknown）+ `risk_level` + `confidence`（0-1）。
- 低置信度（阈值可配置）→ `unknown` → 走 Fallback，不硬猜。
- 每轮执行；多轮中用户可切换意图。
- 仅写入自身分类字段（`capability` / `risk_level` / `confidence`），不得修改 `context` 等业务状态。
- Router 失败/超时 → 直接走 Fallback。

## 5. Policy 映射（纯确定性）

- `capability x risk_level -> 子图` 静态映射表，不调 LLM，可审计。
- 例：`order x read -> ReadGraph(order 只读工具)`；`account x mutation -> MutationGraph`；`order x high_risk -> HighRiskGraph`。
- 无法映射 → Concierge 兜底。

## 6. 子图

### 6.1 ReadGraph

- `create_agent` + 只读工具集（知识库检索、订单查询等）+ ToolNode + `tools_condition`。
- 无 interrupt；工具结果直接汇入回答。

### 6.2 MutationGraph（workflow）

- 确定性流程：**安全预检**（`safety-spec.md`）→ `interrupt()` 用户确认（含影响摘要）→ 执行写操作工具 → 结果。
- 用户确认对应 `Command(resume={"action":"confirm"|"cancel"})`（schema 见 §3）。
- 用户拒绝 → 收尾并记录，不执行。

### 6.3 HighRiskGraph

- **安全预检** → `interrupt()` 发起人工审批（见 `approval-spec.md`）→ 人工 approve/reject 后 `Command(resume=...)` 恢复 → 执行/拒绝收尾。
- 超时由下一 turn 惰性检查处理为过期。

### 6.4 Fallback / Concierge

- 兜底 agent：解释边界、收集更多信息、按 `safety-spec.md` 的 handoff 规则触发转人工（由 `check_handoff` 确定性节点执行，`handoff=True`）。

## 7. 节点级工程参数（横切，见 architecture.md §5）

- 模型/工具节点配置 `RetryPolicy` 与 `TimeoutPolicy`；工具节点配置 `error_handler`。

## 8. 变更纪律

- 改路由/子图行为：先改本文件，再实现。
- 新增子图：在此登记 Policy 映射条目与所需状态字段（回写 `state-spec.md`）。

## 9. 节点-字段读写表

| 节点/子图 | 读 | 写 |
|---|---|---|
| GuardNode | `messages`（原始输入）、`user_id` | `guard_status` |
| Router | `messages`、`user_id` | `capability`、`risk_level`、`confidence` |
| ResumeHandler | `__interrupt__`、`messages` | 仅经 resume 更新，不写业务字段 |
| ReadGraph | `messages`、`context` | `messages` |
| MutationGraph | `messages`、`context`、`capability`、`risk_level` | `messages`、`context`（确认结果） |
| HighRiskGraph | `messages`、`context`、`capability`、`risk_level` | `messages`、`context`、`pending_approval` |
| Fallback / Concierge | `messages`、`context` | `messages`、`handoff` |
| check_handoff | `context`（工具失败计数）、`messages` | `handoff` |
