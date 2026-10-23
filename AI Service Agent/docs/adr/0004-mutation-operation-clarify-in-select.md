# Mutation Operation Clarify lives in Mutation Select

Mutation 上「要办哪种写操作」未定时，不再经入口 Hybrid Tool Resolution / 子图外 `operation_clarify` / 硬编码 `clarify` 节点；由 MutationGraph 内 **Mutation Select**（图内名 `mutation_select_llm`；与 HighRisk **分提示词 / 分节点 / 分模型工厂**）**同一次调用**在无 `tool_call` 时用响应 `content` 追问（空 content 回落默认句），发 `clarification_required`（`reason: operation_unclear`），普通轮 END，用户下一句从总图再进。tool 已定后的字段收集仍走 Form Interrupt。删除入口 Hybrid（含模糊伞与关键词映射）。**Mutation 删除 `tier_clarify` 固定澄清支路**：选中未绑定 / 白名单外工具 → `_reject("unknown_tool")`；固定话术澄清仅保留给 HighRisk。保留 Mutation 追问路径的 `clarify_max_turns`（超限固定 fallback）。HighRisk 澄清与 select 提示词本变更不动（后续再拆优）。同变更放宽 Policy「改/改下收货地址」归一化，避免白名单缺 `update_address`。术语见 `CONTEXT.md`（Mutation Select）。

选用「子图内一次 LLM 定 tool 或追问」而非入口 Hybrid + 多条模板澄清，是为消灭 Mutation 双（多）澄清路径、降低延迟，并保持 Form-Only Write。

**Status:** accepted（2026-10-23）

## Spec / glossary

- 术语：根目录 `CONTEXT.md`（Operation Clarify；Hybrid Tool Resolution 已移除）
- 实现时需同步：`docs/specs/mutation-form-interrupt-spec.md`、`graph-spec.md`、`state-spec.md`、`event-spec.md`

## Considered Options

- 澄清留在入口 Hybrid + 固定话术 — rejected：与「明确意图仍不出表单」体验冲突，且 Hybrid 高置信半边未接线
- 再挂 `clarify_llm` 第二节点 — rejected：增加延迟
- 用 Mutation Form 当操作类型菜单 — rejected：既有 glossary / Form 边界
- Mutation 保留 `tier_clarify` 固定话术 — rejected：又一条澄清路径；`bind_tools(tier_tools)` 下几乎不触发
- HighRisk 一并改 LLM 追问 / 同改 select 提示词 — deferred：本变更只改 Mutation

## Consequences

- 实现需删除 `hybrid_tool_resolve` / 入口 `operation_clarify` 边；Mutation 去掉模板 `clarify` 与 `tier_clarify` 路由
- Mutation `select` 使用独立提示词常量（可暂共享工厂，但提示词与节点绑定按 Mutation 优化；HighRisk 继续旧提示词）
- Eval / AgentBot 测试中依赖入口 Operation Clarify、Hybrid 或 Mutation `tier_clarify` 的用例要改写
- Router 误判 `order_mutation` 时，口语地址纠正与 select LLM 追问共同决定能否出表单；归一化补丁降低对追问的依赖

## Follow-on（grilled 2026-10-23，已实现）

Mutation Select 完全独立于 HighRisk select：`make_mutation_select_node` + `make_mutation_select_model`（`MUTATION_SELECT_*` 缺省回落 `SELECT_*`）+ 图内节点 `mutation_select_llm`；提示词按 Mutation 职责重写（含 Fake `MUTATION_SELECT_SYSTEM_TEMPLATE`）；动态 `{examples}` 保留；节点 Retry/Timeout 复用 `RETRY_LLM` 模板。允许共享无业务语义纯函数。HighRisk 本轮不动。术语见 `CONTEXT.md`（Mutation Select）。
