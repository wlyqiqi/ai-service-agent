# ADR 2026-09-11：spec 冲突审计与修复决策

- 状态：已决策并执行（Applied）
- 涉及：`AGENTS.md` + `docs/specs/*`（architecture / state / graph / tool / safety / approval / event）+ `docs/project-audit.md`
- 治理原则：AGENTS.md 只写不可违反的边界摘要；spec 是业务定义唯一权威；同一件事只在一处定义。

## 一、审计背景

对 8 份文档按 8 个检查点交叉审计（State 字段 / Graph 输入输出 / capability 与 risk_level / approval 流程 / interrupt 与 resume / Tool 权限 / FastAPI 边界 / Event schema）。结论：无结构性架构冲突；发现 2 处硬缺口、1 处工具权限冲突、7 处命名与枚举类问题。

## 二、一致性快照（本次审计确认一致、未改动，作后续审计基线）

- `risk_level` 三值（read / mutation / high_risk）在 state / graph / tool / AGENTS.md 完全一致。
- 审批生命周期（pending → approved / rejected / expired）、"预检先于审批"顺序、过期惰性检查、幂等：graph / approval / safety 三方一致。
- interrupt / resume 入口语义：`__interrupt__` 为权威依据、挂起时新 turn 走 ResumeHandler 不重走 Router。
- FastAPI 只做 Transport、业务编排留在 LangGraph。
- 子图边界：Read=ReAct、Mutation=显式 Workflow、HighRisk=人工 Approval、子图间不做内部转交（每 turn 重走 Router）。

## 三、决策记录

| 编号 | 问题 | 决策（含理由） |
|---|---|---|
| P1 | Guard 节点缺失 | **并行**：Guard ∥ Router 对同一份原始输入并行检测，互不依赖；`fail` 时丢弃（不消费）Router 并行结果，可取消则取消。理由：不增加串行 LLM 延迟；正确性不依赖顺序。 |
| P2 | 工具级失败触发缺失 | 批准：safety-spec §2.1 拆"路由级 / 工具级"两行，工具级计数存 `context`。 |
| P3 | escalate_human 权限泄漏 | **移出工具表**，改为 `check_handoff` 图内确定性节点直接执行；**不用** `user_facing` 标志位。理由：约束要机械化；标志位可被注入规则重构绕过，移出工具表从根上消除风险类别。 |
| P4 | resume payload 双形状 | 统一为单一 schema（graph-spec §3 权威定义）：`Command(resume= { "action": "approve"|"reject"|"confirm"|"cancel", "note"?: str } | { "result": "expired" })`；approval / mutation 均引用。 |
| P5 | Mutation 确认事件缺失 | 补 `confirmation_required` / `confirmation_updated` 事件（event-spec §2）。 |
| P6 | Transport 输入契约缺失 | architecture §4 补 `POST /chat`、`POST /sessions` 输入契约。 |
| P7 | intent / capability 命名 | state-spec 字段统一改名 `capability`。 |
| P8 | Router 写状态歧义 | graph-spec 澄清：Router 仅写自身分类字段（capability / risk_level / confidence），不得修改 `context` 等业务状态。 |
| P9 | capability=ticket 悬空 | **方案 A**：从 capability 列表移除；待真实数据证明工单需求值得单独路由时再补方案 B（新增 ticket 子图）。 |
| P10 | 节点-字段读写表缺失 | **方案 A**：graph-spec 新建 §9 读写表，不弱化措辞；日后排查 State 被谁污染时有据可依。 |
| P11 | AGENTS.md 事件枚举重复 | 删除内联枚举，指向 event-spec.md 为唯一权威。教训：规则文件自身违反单一来源原则，改 AGENTS.md 时需自查自我矛盾。 |

## 四、执行结果

- 已修改：`AGENTS.md`、`docs/specs/architecture.md`、`state-spec.md`、`graph-spec.md`、`tool-spec.md`、`safety-spec.md`、`approval-spec.md`、`event-spec.md`、`docs/specs/README.md`；新增本记录。
- 新增状态字段：`guard_status`（Guard 并行检测结果，每轮覆盖）。
- 转人工不再作为工具存在；`escalate_human` 从工具表移除，由 `check_handoff` 确定性节点执行。

## 五、后续关注

- P9：若真实数据表明"工单类需求量大到值得单独路由"，再评估新增 ticket 子图。
- Guard 取消语义以"不消费 Router 输出"兜底；若 LangGraph 后续提供节点级取消能力再升级实现。
- 下次审计时以本记录"一致性快照"为基线对比，确认上述约定未被破坏。
