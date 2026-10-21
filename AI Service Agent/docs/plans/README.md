# 执行计划（docs/plans/）

> **归档索引**：本目录为历史实现计划，正文一般不改写。现行行为以 `docs/specs/` 与 `docs/adr/` 为准。C 端主入口见 [ADR 0001](../adr/0001-widget-agentbot-form-contract.md)（自建 SSE 用户页已废止）。

- `2026-09-14-mvp-first-mile.md` — MVP 第一公里实现计划（Task 0–9，TDD，每任务可独立测试与 commit），对应 `docs/specs/mvp-first-mile.md`（历史里程碑）。
- `2026-09-15-p3-guard-mutation-highrisk.md` — P3 Guard / Mutation / HighRisk / Approval 实现计划（Task 1–8，TDD，每任务 commit）。
- `2026-09-15-p4-clarify-precheck-resume.md` — P4 Clarify / 预检规则增量 / 读侧边界 / resume final 提取 实现计划（Task 1–6，TDD，每任务 commit）。
- `2026-09-17-user-page-frontend.md` — ~~自建 SSE 用户页前端~~（历史；对应已废止 `user-page-spec.md`）。**勿再按此实现**；C 端见 ADR 0001。
- `2026-09-18-user-page-backend-integration.md` — ~~用户页 ↔ 后端契约~~（历史；对应已废止 `user-page-spec.md` §10/§11）。人工车道真机验收改走 `scripts/real_human_lane_smoke.py` 等。
- `2026-09-18-handoff-intent-gate.md` — P0 显式转人工入口检测（detector 前置拦截，不进 Router/Guard/Graph）+ 统一 `_emit_handoff` side-effect path（Task 1–5，TDD，每任务 commit）。
- `2026-09-18-readgraph-lightrag-rag.md` — ReadGraph 知识库 RAG 接入（`kb_search` 接真实 LightRAG-chengyuan 检索：`naive` / 关 rerank / 来源标注 / 确定性降级 / 真机冒烟）（Task 1–5，TDD，已完成）。
- 计划按 superpowers:writing-plans 规范编写；新功能请先改 specs/ADR，再开计划，勿直接以本目录旧计划为权威。
