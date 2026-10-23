# HighRisk Select isolation (P0)

HighRiskGraph 的工具选择与 Mutation Select 对称隔离：图内节点 `high_risk_select_llm`、`make_high_risk_select_node`（无 `tier`）、`make_high_risk_select_model`（`HIGH_RISK_SELECT_*` 缺省回落 `SELECT_*` / `LLM_*`）、专属 Fake/bind_tools 提示词（仅 cancel/refund）。删除 `make_select_node` / 通用 `select_llm` 命名，`select_model` 入参硬切为 `high_risk_select_model`。P0 **不**改变 clarify / Form / 审批拓扑，无 tool_call 仍走 `tier_clarify`（不内联 Operation Clarify）。术语见 `CONTEXT.md`（HighRisk Select）。P1（澄清策略）另议。

**Status:** accepted（grilled 2026-10-23）

## Spec / glossary

- 术语：根目录 `CONTEXT.md`（HighRisk Select）
- 实现时需同步：`docs/specs/graph-spec.md`、`state-spec.md`、`event-spec.md`；ADR 0004 可互链本决策

## Considered Options

- 只换提示词、保留 `select_llm` / `make_select_node(tier=)` — rejected：与 Mutation Select 对称性及 glossary 冲突
- P0 一并做 Mutation 式内联口语追问 — deferred：属 P1，动澄清/事件语义
- 保留 `select_model` / `make_select_model` 别名 — rejected：硬切去掉 overload

## Consequences

- harness / `build_graph` / Studio / 测例中 HighRisk 选择路径全部改名
- 环境变量 `SELECT_*` 仅作 `HIGH_RISK_SELECT_*` 缺省回落源，不再表示「双档共用 select」
