# 图规格（graph-spec.md）

- 关联：`state-spec.md`（状态契约）· `tool-spec.md`（工具集）· `safety-spec.md`（预检与转人工）· `approval-spec.md`（审批服务）· `architecture.md`（总览）

## 0. 节点命名约定（LLM 节点标记）

- 图内**包含 LLM 推理的节点，节点 ID 一律带 `_llm` 后缀**，便于在 LangGraph Studio / 图可视化中直接辨识（本节为唯一权威约定）。
- 当前 `_llm` 节点清单（v0.1.0）：
  - 顶层：`router_llm`（json_mode 结构化分类）
  - ReadGraph：`agent_llm`（ReAct agent，`create_agent`）
  - MutationGraph / HighRiskGraph：`select_llm`（json_mode 工具 + 参数提取）
- 确定性节点（关键词 / 模板 / 预检 / 中断 / 收尾）**不得**使用 `_llm` 后缀。
- `guard` 节点当前为实现**确定性 v1**（关键词/正则），非 `_llm` 节点；若未来升级为 LLM Guard，须改名并同步本清单。
- 新增任何 LLM 节点时必须遵循本约定，并在本节登记。

## 1. 总图结构与入口 resume 调度

```text
入口（Conversation Runtime / harness，每次图调用前）
  |
  +- aget_state() 读挂起中断（权威依据 = checkpointer 的 __interrupt__）
  |    +- YES -> graph.ainvoke(Command(resume=<唯一 schema>, update={"messages": 水合历史}))
  |    |          = 从中断点继续被中断子图（天然不重走 Router，见 §3 实测依据）
  |    +- NO  -> graph.ainvoke({"messages": 水合历史}) 新起 run
  |                  |
  |                  v
  |        GuardNode ∥ RouterNode（并行，同一份输入，互不依赖）[P3 起]
  |                 |                |
  |                 +---(pass)-------+---> Policy 映射（capability x risk_level）-> 子图
  |                 +---(fail)------------> 安全收尾（丢弃 Router 并行结果）
  |                                                     |
  |                                                     v
  |                                         Read / Mutation / HighRisk / Fallback
  |                                                      |
  |                    +---------------------------------+  任意子图检测到转人工信号
  |                    v
  |        interrupt(handoff_request) -> 外层接管（建 Record + 原子切 mode + 发 handoff_created）
  |
  +- 正常完成 -> END（Final Response 统一出口）
```

- **入口 resume 调度（实测修正）**：LangGraph 1.2.11 中，带输入的新调用在图内节点 / START 条件边**读不到 `state["__interrupt__"]`**，且会弃置挂起中断并新起 run；恢复只能由调用方以 `Command(resume=...)` 在 invoke 层发起（实测依据见 `docs/decisions/2026-09-14-checkpointer-resume-handoff.md`）。因此"有挂起中断时新 turn 必须走 ResumeHandler"由 harness 在每次图调用前经 `aget_state()` 判定实现；恢复语义 = 回应上一个问题，不重新走 Router。
- 子图检测到需要转人工的信号时，**不自行创建 Handoff Record、不自行切换 `conversation_mode`**，统一抛出 `interrupt({"type": "handoff_request", "reason": ..., "summary": ...})`；外层（Conversation Runtime）负责接收信号 → 创建 Handoff Record → 原子切换 `conversation_mode` → 发出 `handoff_created` 事件。子图不直接调用任何 Handoff API（机制与事件见 `handoff-spec.md` §3 / `event-spec.md`）。
- 抛出 `handoff_request` 后 `execution_status` 置 `waiting_handoff`（见 `state-spec.md` §1），不得置 `completed` / `failed`。
- Final Response：子图结果写回 `messages`，由 harness 统一推送事件（见 `event-spec.md`）。

## 2. Guard 节点（并行检测）

- 与 Router **并行**执行，输入为同一份原始用户输入（`messages` + `user_id`），**不依赖 Router 的分类结果**，谁先完成不影响正确性。
- 只输出 `guard_status`（pass / fail），不参与 capability/risk_level 分类，不执行 tools，不修改业务状态。
- `fail` → 走安全收尾（见 `safety-spec.md` §1.5），Router 并行结果**不被消费**：实现上若调用可取消则取消，否则以"不消费其输出"兜底。
- 检测信号与阈值定义见 `safety-spec.md` §1.5。
- **P3 实现形态（D2 定案）**：确定性规则 v1（关键词/启发式，阈值可配置，无模型 key，离线可测）；**v1 是过渡实现、非 Guard 终态能力**——无法有效覆盖语义层面的恶意输入/越权诱导（如同义替换、间接暗示等绕过关键词匹配的攻击），后续须评估升级为 LLM Guard 或语义层判断（升级评估登记见 `docs/decisions/2026-09-15-p3-design-decisions.md` §2）。

## 3. Resume 调度（实测修正：入口层调度 + 图内续接）

- **LangGraph 1.2.11 实测**：图内节点与 START 条件边无法读取 `__interrupt__`（带输入的新调用读不到、且会弃置挂起中断）；resume 必须由调用方（harness / 审批端点）以 `Command(resume=...)` 在 invoke 层发起。恢复 = 从挂起中断点继续原被中断子图，**天然不重走 Router**（实测：resume turn 模型调用次数不增）。"ResumeHandler 节点"不作为图内路由节点存在；语义由两部分承担：
  1. **入口层调度（harness）**：每次图调用前 `aget_state()` 读挂起中断，构造 resume payload（审批 / 确认 / 超时 / handoff 共用唯一 schema，见下），以 `Command(resume=payload, update={"messages": <水合历史>})` 发起；无挂起中断则以普通输入新起 run。
  2. **图内续接（被中断节点）**：`interrupt()` 返回 resume payload，由发起 interrupt 的节点（或其子图续接逻辑）解析并完成状态转移；决策一律在图内，不允许挪到 harness 组件。
- resume payload 唯一 schema（审批 / 用户确认 / 超时 / handoff 共用，harness 直调与用户 turn 两条恢复入口均须遵守）：
  `Command(resume= { "action": "approve" | "reject" | "confirm" | "cancel" | "handoff_resumed", "note"?: str } | { "result": "expired" })`
- 中断过期：`Command(resume={"result": "expired"})`，子图按过期分支收尾。
- `handoff_request` 中断的恢复：人工 Resolve 回切 `conversation_mode=ai` 后，**由服务端在 resolve 动作本身触发时主动完成 resume**（`Command(resume={"action": "handoff_resumed", "note"?: ...})`，不等待用户下一条消息；见 `handoff-spec.md` §1.3）。被中断子图（ReadGraph / MutationGraph / HighRiskGraph / Fallback，取决于中断来源）从中断点继续至完成，才汇入 Final Response；不重新走 Router。用户 resolve 后发送的第一条消息直接进入正常 Router 流程；若主动 resume 异常未完成，入口层 resume 调度在下一用户消息时兜底恢复。恢复执行后的状态转移决策必须在图内完成（同 §3 原则，见 `approval-spec.md` §3.1）。

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
- 无**业务** interrupt（不发起确认 / 审批）；检测到需要转人工的信号时同样经 `interrupt(handoff_request)` 退出（见 §6.5）。
- 工具结果直接汇入回答。

### 6.2 MutationGraph（workflow）

- 确定性流程：**select（参数完备性）** → 不完整则 **clarify（普通轮，非 interrupt，D1）**；完整 → **安全预检**（`safety-spec.md`；含业务规则，D3：`precheck_fetch` 提供订单状态事实 → `context["precheck_data"]`，`safety_check` 确定性裁决）→ `interrupt()` 用户确认（含影响摘要）→ 执行写操作工具 → 结果。
- 参数不完整判定（D1）：必填业务字段（不含 `request_id`）缺失/为空串，或（全可选工具）全部业务字段为空 → 进入 clarify 节点：确定性模板文案（字段→中文 label）+ `execution_status=clarifying` + 发 `clarification_required`；`context["clarify_count"]` 自增，**完整成功 select 时清零**，超过 N=2 → 确定性 fallback 文案 + `completed`。解析失败 / 工具不在档 / schema 校验错误仍为拒绝（`safety_rejected`），不走 clarify。
- 写图 select 允许集为档内严格（P5 B2b）：只读工具不可选；选中已注册低档工具 → `tier_clarify` →
  clarify 节点（普通轮，`clarification_required` + `reason:"tier_mismatch"`，`execution_status=clarifying`）。
- 用户确认对应 `Command(resume={"action":"confirm"|"cancel"})`（schema 见 §3）。
- 用户拒绝 → 收尾并记录，不执行。
- **恢复后、执行前最小不变量 recheck（D6 定案）**：确认/取消恢复后，执行写工具前复核 `request_id` 存在、参数未变、`context` 非陈旧；不重跑完整预检、不重新调 LLM。确认结果写入 `context`（graph-spec §9），不新增 State 字段。

### 6.3 HighRiskGraph

- **select（参数完备性，同 §6.2 clarify 机制）** → **安全预检**（含业务规则，D3）→ `interrupt()` 发起人工审批（见 `approval-spec.md`）→ 人工 approve/reject 后 `Command(resume=...)` 恢复 → 执行/拒绝收尾。
- **档内严格（P5 B2b）**：写图 select 只允许 `min_risk_level == tier` 的写工具；只读工具不可选，
  选中已注册低档工具 → tier_mismatch 澄清（同 §6.2）。
- 超时由下一 turn 惰性检查处理为过期。
- **审批承接（D7/D8 定案）**：由 Approval Service（SQLite 轻量存储 + 前置校验端点，见 `approval-spec.md` §3）承接；同一 `approval_id` 重复 decision 天然幂等；恢复后、执行前做最小不变量 recheck（`request_id` 存在、参数未变、`context` 非陈旧）。

### 6.4 Fallback / Concierge

- 兜底 agent：解释边界、收集更多信息；`check_handoff` 确定性节点按 `safety-spec.md` §2 规则检测转人工信号，命中后经 `interrupt(handoff_request)` 抛出（见 §6.5），由外层接管；Fallback 自身不直接创建 Handoff / 切换 mode。
- **节点顺序（P1 定案）**：`START -> check_handoff`，条件边 `handoff? -> pause | else -> explain`；`explain` 只服务非 handoff 兜底回复。命中 handoff 时**不在图内生成"已转人工"文案**——该文案由外层在 `handoff_created` 时发 token + 落 Message Store（见 `handoff-spec.md` §5.2 / `event-spec.md`），避免被中断 super-step 在 resume 时重放重复文本（实测依据见 `docs/decisions/2026-09-14-checkpointer-resume-handoff.md`）。

### 6.5 子图中途触发 Handoff（统一机制）

- ReadGraph / MutationGraph / HighRiskGraph 检测到需要转人工的信号时，不自行创建 Handoff Record，也不自行决定 `conversation_mode` 切换；统一抛出一次 interrupt，resume / 退出信号类型为：
  `interrupt({"type": "handoff_request", "reason": ..., "summary": ...})`
- 外层（Conversation Runtime 或等价的入口逻辑）统一负责：接收该信号 → 创建 Handoff Record → 原子切换 `conversation_mode` → 发出 `handoff_created` 事件。子图自身不直接调用 Handoff 相关的任何 API。
- 抛出后 `execution_status` 置 `waiting_handoff`（见 `state-spec.md` §1），不得置 `completed` / `failed`。
- 触发信号来源为 `check_handoff` 等确定性规则（见 `safety-spec.md` §2）；LLM 无权临时决定转人工。

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
| 入口 resume 调度（harness） | checkpointer `__interrupt__`（`aget_state`）、`messages` | 以 `Command(resume=..., update={messages})` 发起，不写业务字段 |
| ReadGraph | `messages`、`context` | `messages` |
| MutationGraph | `messages`、`context`、`capability`、`risk_level` | `messages`、`context`（确认结果） |
| HighRiskGraph | `messages`、`context`、`capability`、`risk_level` | `messages`、`context`、`pending_approval` |
| select（mutation/high_risk） | `messages`、`context`（`clarify_count`） | `context`（`pending_tool` 或 `clarify_missing` / `tier_clarify`；完整 select 时清零 `clarify_count`） |
| clarify | `context`（`clarify_missing` / `tier_clarify`、`clarify_count`） | `messages`（确定性模板文案）、`context`（`clarify_count` 自增）、`execution_status=clarifying`，并发出 `clarification_required` |
| precheck_fetch（mutation/high_risk） | `context`（`pending_tool`） | `context`（`precheck_data`：仅事实，订单状态快照；不做授权/裁决） |
| Fallback / Concierge | `messages`、`context` | `messages` |
| check_handoff | `context`（工具失败计数）、`messages` | `handoff`（置位）、`execution_status=waiting_handoff`，并抛出 `interrupt(handoff_request)` |

- `execution_status` 由入口与图内节点按 turn 阶段维护（取值见 `state-spec.md` §1）；子图抛出 `handoff_request` 时置 `waiting_handoff`，不得置 `completed` / `failed`。

