# LangGraph HITL 测试方案（2026-10-14）

- 目标：把 [LangGraph 官方 Testing](https://docs.langchain.com/oss/python/langgraph/test) 的三种模式映射到本仓库分层，补齐 Mutation Form / HighRisk 等 HITL 缺口，且不重写既有 Fake 图测。
- 规格锚点：`docs/specs/graph-spec.md`、`docs/specs/state-spec.md`、`docs/specs/mutation-form-interrupt-spec.md`；挂起权威信号 = `__interrupt__`（PR #40 / option A，父图可为 `routing`）。
- 相关 scratch：`.scratch/mutation-form-interrupt/issues/`（尤其 `#04` form_rejected / 挂起纯文本）。

## 非目标

- 不重写现有 Fake + `MemorySaver` 图测骨架
- 不把真模或 L3 smoke 塞进 pytest
- 本文件只定方案与 backlog；实现另开执行轮次

## 背景

原型跑通后补测。官方推荐三类模式：

1. **整图** — `MemorySaver` + `thread_id` + `invoke` / `Command(resume=...)`
2. **单节点** — `compiled.nodes[name].invoke(...)`（绕过 checkpointer）
3. **局部路径** — `update_state(..., as_node=...)` + `interrupt_after` / `interrupt_before`

本仓库 HITL 主路径（Mutation form、HighRisk approval）已按模式 1 覆盖；模式 2/3 与若干 form 再挂起同构用例仍缺。

## 分层映射

| 层 | 官方对应 | 本仓库落点 | 判据 |
|---|---|---|---|
| L1 确定性单元 | 节点 / 纯函数 | `test_select` / `test_operation_clarify` / `test_form_hang_parent_status` 等 | 无 LLM、无整图 |
| L2 整图 HITL | MemorySaver + thread_id + Command | `tests/test_mutation_graph.py`、`tests/test_high_risk_graph.py` | `__interrupt__` / resume 后 state |
| L3 局部路径 | `nodes[].invoke` / `update_state(as_node=)` | Mutation / HighRisk 子图（待补） | 单节点或 prepare→interrupt→recheck |
| L4 Transport / 真模 | Studio + 线上形态 | `test_server` / `test_streaming`；`scripts/real_smoke_lanes.py` + `evals/lane_criteria.py` | SSE 契约；真链路 smoke |

职责边界：pytest = 图与 HITL 正确性；control eval = 路由/策略语料；L3 smoke = 生产形态 HTTP+SSE。三者共享语义，不共用 runner。

## 现状 vs 缺口

- **已强：** L2 Mutation confirm/cancel/incomplete form；HighRisk approve/reject/expired；`lane_criteria` 与父图 `routing` 挂起语义。
- **偏 Transport：** `form_rejected` 同 session 再挂起、挂起中纯文本拒写、vague → operation_clarify → form 多在 `tests/test_server.py`；图层缺同构。
- **未用官方模式 2/3：** `graph.nodes[...]` 目前多用于 timeout/metadata 检查，几乎不 `invoke`；测试中无 `update_state(as_node=...)`。

## Backlog（实现轮次）

优先级：P0 → P1 → P2；L4 现有 server/SSE/smoke 保留，不替代图层补测。

### P0 — `tests/test_mutation_graph.py`（MemorySaver 整图）

- [x] Confirm → recheck 失败 → 同 `form_session_id` 再 interrupt，payload 含 `errors`（`test_mutation_confirm_recheck_failure_reissues_form_interrupt`）
- [x] Form 挂起且未 Resume：无写工具；interrupt 仍在（`test_mutation_form_pending_without_resume_keeps_interrupt_no_write`）

### P1 — 父图多轮与投影

- [x] 父图两轮：模糊意图 → `operation_clarify` 且无 `__interrupt__` → 下一轮具体意图 → form interrupt（`test_vague_mutation_operation_clarify_then_form_interrupt_graph`）
- [x] `harness/form_events.project_form_interrupt`：无 `errors` → `form_required`；有 `errors` → `form_rejected`（`tests/test_form_events.py`）

### P2 — 局部路径与回归

- [x] `update_state(as_node="prepare_form")` → form_interrupt：schema（confirm/cancel + 业务字段）与 Cancel 清 `form_session_id`（`test_mutation_partial_prepare_form_to_interrupt_schema_and_cancel`；`make_mutation_graph(checkpointer=...)`）
- [x] 回归：happy form 路径不出现 chat `confirmation_required`（`test_mutation_happy_form_path_emits_no_confirmation_required`）

## 约定

| 项 | 做法 |
|---|---|
| Checkpointer | pytest 仅 `MemorySaver`；生产 `AsyncSqliteSaver`（见 `architecture.md`） |
| Fake 契约 | `src/ai_service_agent/testing.py` 的 router/select JSON，不混 ToolStrategy 文案 |
| 挂起判据 | 读 `__interrupt__` / SSE；不要求父图 `waiting_confirmation` |
| 线程 | HITL 用例一律显式 `thread_id` |
| Studio | 改图时用 `langgraph dev` 手测；不替代 pytest |

## 验收（本方案文档）

- [x] 本文件落在 `docs/plans/`
- [x] `docs/plans/README.md` 已登记索引
- [x] 实现轮次：P0–P2 已落地；`uv run pytest tests/test_mutation_graph.py tests/test_form_events.py -q` 全绿
