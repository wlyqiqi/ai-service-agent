# 决策：Checkpointer 持久化 + Handoff 中断恢复（P1）

- 日期：2026-09-14
- 状态：定案（spec 修订已同步：`graph-spec.md` §1/§3/§6.4/§9、`handoff-spec.md` §1.3/§3/§5.2、`state-spec.md` §1、`event-spec.md`、`approval-spec.md` §3.3、`architecture.md` §2/§4、`mvp-first-mile.md` §4）
- 关联：`docs/specs/README.md` · `docs/decisions/2026-09-14-message-store-truth-source.md`（消息真相源）· `docs/decisions/2026-09-14-handoff-decisions.md`（ID 映射 / pending 归宿）

## 背景

P1 目标：`AsyncSqliteSaver` 按 `thread_id` 持久化、handoff 中断的恢复、true 多轮，同时保持 Message Store 唯一真相源（`handoff-spec.md` §5.2）。

## 实测依据（LangGraph 1.2.11，本项目 venv）

1. **图内节点 / START 条件边读不到 `__interrupt__`**：带输入的新调用在图内任何节点与入口条件边中都看不到挂起中断（`state.get("__interrupt__")` 恒为 `None`）。
2. **带输入调用会弃置挂起中断**：有挂起中断时若以普通输入新起 run，会从 START 重跑并产生**新的** interrupt，旧中断被覆盖——即"不恢复"语义不存在。
3. **恢复只能由调用方发起**：`graph.ainvoke/astream(Command(resume=..., update={...}), config)` 从挂起中断点继续；`interrupt()` 返回 resume payload；`update` 与中断点续接在同一流程生效。恢复 turn 模型调用次数不增（Router 不重跑）。
4. **嵌套子图中断的 checkpoint 是"陈旧"的**：子图内已完成节点的写未落 checkpoint（pending），resume 时会重放（fallback 的 explain 文本会再流一次并留一条重复消息）。
5. **`add_messages` 按 message id 对账**：给水合消息分配稳定 id，可令 `state.messages` 与输入（Store 快照）一致，不产生重复。

## 决策

### D1：resume 由入口层（harness）调度，而非图内 ResumeHandler 节点

- 每次图调用前由 harness 经 `graph.aget_state(config)` 读取挂起中断（权威依据 = checkpointer `__interrupt__`）；有则 `Command(resume=<唯一 schema>, update={"messages": 水合历史})`，无则普通输入新起 run。
- "不重新走 Router"由 `Command(resume=...)` 机制天然保证（实测 3）；图内语义由被中断节点续接承担，状态转移决策一律在图内。
- **原因**：实测 1/2 证明"入口条件边 → ResumeHandler 节点"在 LangGraph 1.2.11 不可实现；spec 相应修订（`graph-spec.md` §1/§3）。

### D2：resolve 时服务端主动完成 handoff resume

- `POST /handoffs/{id}/resolve` 在回切 `conversation_mode=ai` 后，由服务端主动执行 `graph.ainvoke(Command(resume={"action": "handoff_resumed"}, update={"messages": 水合历史}))`，**不等待用户下一条消息**。
- 用户 resolve 后发送的第一条消息直接进入正常 Router 流程并得到回复（避免"第一句话无人应答"的体验缺口）。
- 若该主动 resume 异常未完成，入口层 resume 调度在下一用户消息时兜底恢复（决策前行为作为降级路径保留）。
- 无挂起中断时（如手工构造的 handoff Record）跳过 resume，resolve 只回切 mode。

### D3：Fallback 重排 + "已转人工"文案移外层

- Fallback 节点顺序改为 `START -> check_handoff`，条件边 `handoff? -> pause | else -> explain`；`explain` 只服务非 handoff 兜底回复。
- 命中 handoff 时图内**不生成**"已转人工"文案；该文案由外层在 `handoff_created` 时发 token + 落 Message Store。
- **原因**：实测 4——消除被中断 super-step 在 resume 时的文本重放与重复消息。

### D4：消息对账（message_id 列 + 稳定 id）

- `conversation_message` 增 `message_id` 列（langchain message id）；assistant 消息落库时由 harness 从流式输出记录该 id（`done.message_id`）。
- `hydrate()`：user → `HumanMessage(id=f"store-{row_id}")`；assistant → 复用 `message_id`（缺失则 `store-{row_id}`）。
- 每次图调用输入 = 完整水合历史，经 `add_messages` 按 id 对账（实测 5），`state.messages` 恒等于 Store 快照。

### D5：`handoff_resumed` 纳入唯一 resume schema

- resume schema 唯一（`graph-spec.md` §3 / `approval-spec.md` §3.3）：`Command(resume={"action": "approve"|"reject"|"confirm"|"cancel"|"handoff_resumed", "note"?: str} | {"result": "expired"})`。
- `handoff_resumed` 与审批 / 用户确认 / 超时共用同一 schema，不设游离分支（P4 审批实现沿用同一 schema 与两条恢复入口）。

### D6：Checkpointer 生命周期与配置

- 每请求 `AsyncSqliteSaver.from_conn_string(db_path)`（同库独立连接；实测跨请求持久化与并发连接可用），MVP 不引入池化。
- 图调用 config 一律 `{"configurable": {"thread_id": <thread_id>}}`（裸 `thread_id` 键不生效）。
- `MemorySaver` 仅测试用（`AGENTS.md` §15）。

## 影响

- `event-spec.md`：`done` 增加可选 `message_id`；resolve 主动 resume 不产生 SSE。
- `state-spec.md`：`messages` 对账规则、resume 后 `execution_status` 由 `final` 置 `completed`。
- `handoff-spec.md` §1.3：resolve 触发主动 resume；§5.2：对账机制。
- 后续 P3/P4（Mutation / HighRisk 确认与审批）复用同一 resume schema 与入口调度，不新增分支。
