# P5 Select 工具风险对齐决策（2026-09-16）

- 状态：已审核通过（D1–D3 APPROVED，D4 OBSERVE ONLY）
- 关联：`docs/real-model-smoke.md` §7（真机漂移证据）· `docs/specs/tool-spec.md` §1/§4 ·
  `docs/specs/graph-spec.md` §6.2/§6.3/§9 · `docs/specs/state-spec.md` §1 · `docs/specs/event-spec.md` §2 ·
  `docs/decisions/2026-09-16-ctrl-0076-clarify-verification.md`

## 背景
- 真机冒烟（§7）两次运行：真实 ARK 对"退了吧"稳定选择只读工具 order_lookup；无 ToolMeta.schema →
  D1 参数完备性不触发 → Run 1 mutation 确认闸、Run 2 high_risk 对只读操作发起审批（不该发生的审批流）。
- 根因：只读工具无 schema + `for_risk_level` 分层把只读工具放进写图 select 允许集；select 的
  schema 校验与 D1 澄清整体包在 `if meta.schema is not None` 内。
- 写图为单工具显式 Workflow：select 锁定唯一工具 → 预检 → interrupt 确认/审批 → 执行；无"先查后写"
  多步循环，只读工具在写图 select 内无正当用例（查订单事实由确定性 precheck_fetch 节点提供）。

## 决策
- **D1（B1，采纳）**：只读工具补 select 侧 schema——`order_lookup`=`{order_id, request_id}`、
  `kb_search`=`{text, request_id}`（`request_id` 由 select 注入、非模型输出；`user_id` 为 InjectedState、
  不入 schema）。作用：注册表元数据完备（tool-spec §1 预检用）+ safety 预检对只读工具获得 args 白名单
  （纵深防御）。ReadGraph 消费工具函数签名，不读 ToolMeta.schema，零影响。
- **D2（B2b，采纳）**：写图 select 档内严格——允许集与系统提示仅含 `min_risk_level == tier` 的写工具；
  选中已注册低档工具 → `context["tier_clarify"]` → clarify 节点（普通轮，`clarification_required` +
  `reason:"tier_mismatch"`，`execution_status=clarifying`，计 `clarify_count`、>N=2 fallback）；
  未注册 / 高档工具仍 `unknown_tool` 拒绝。ToolNode 工具注入保持分层（tool-spec §1），select 是写图唯一
  工具选择闸。同步修订 tool-spec / graph-spec / state-spec / event-spec。
- **D3（采纳）**：离线回归（新增只读工具选中的 tier 澄清断言，fake 对齐真实 json_mode 路径）+ 真机 ARK
  探针复验"退了吧"（`clarification_required` 出现、`approval_required` 不出现），更新 `docs/real-model-smoke.md` §7。
- **D4（OBSERVE ONLY）**：Guard LLM 评估 / Approval Service SQLite→Postgres 迁移预研 / Memory 子计划，
  维持登记排队，不进入本计划实现。

## 影响评估
- 现有测试：写图 fake 均选写工具（refund_apply / order_cancel / account_update），档内严格不影响；
  eval master.jsonl 零改动（CTRL-0076 `expected_policy=clarify` 不变，runner scripted select 均写工具）；
  预期 138 + 6 新增用例全绿。
- 残余风险（D3 关联，维持登记）：写工具必填参数来源校验仍未实现（模型可能编造 order_id 导致 select 放行），
  不属于本计划范围。
