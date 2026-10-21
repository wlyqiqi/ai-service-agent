# 图表索引（docs/diagrams）

本目录存放 AI Service Agent 项目的框架图、流程图与时序图。Mermaid 源码可在 GitHub / 编辑器中直接渲染；Archify 产物为可交互 HTML。

> 图以 spec 为唯一权威：若图与 `docs/specs/` 冲突，以 spec 为准，并请同步修正本目录文件。

## 文件清单

| 文件 | 类型 | 内容 |
|---|---|---|
| `handoff-overview.svg` | 顶层图 | conversation_mode 入口判断 → AI / 人工双车道 → 循环回入口 |
| `graph-overview.md` | Mermaid flowchart | AI 路径内部总图：入口 resume / Guard∥Router / Policy(`route_intent`) / 子图 / handoff / 发送前重校验 |
| `interrupt-resume-sequence.md` | Mermaid sequenceDiagram | 审批 / Form Interrupt resume 与 Handoff 时序 |
| `state-machines.html` | 层叠 + 状态机 | 状态机管理全景（对应 `architecture.md` §11） |
| `架构图/archify-architecture.html` | Archify | 组件架构渲染版（JSON 同源） |
| `时序图/archify-sequence-*.html` | Archify | 请求 / 客户时序渲染版 |
| `状态机/archify-lifecycle-*.html` | Archify | mode / execution / approval 生命周期 |

已删除的过时产物（勿再引用）：`customer_service_final_architecture.svg`、`graph_overview.mmd`（旧 Mutation confirm_pause 导出）。

## 维护规则

- 新增图表：按业务主题命名，并在本表登记。
- 修改图表：先改对应 spec（`docs/specs/`），再改 Mermaid / Archify JSON，再 `archify deliver` 刷新 HTML。
- 不使用本目录承载业务定义，业务定义一律在 `docs/specs/`。
- Router 契约：唯一语义输出为 `route_intent` + `confidence`；勿再画 `capability × risk_level`。
