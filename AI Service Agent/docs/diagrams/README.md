# 图表索引（docs/diagrams）

本目录存放 AI Service Agent 项目的框架图、流程图与时序图，均为 Mermaid 源码（.md 内含代码块），可在支持 Mermaid 的编辑器 / GitHub / 文档工具中直接渲染。

> 图以 spec 为唯一权威：若图与 `docs/specs/` 冲突，以 spec 为准，并请同步修正本目录文件。

## 文件清单

| 文件 | 类型 | 内容 |
|---|---|---|
| `handoff-overview.svg` | 顶层图 | conversation_mode 入口判断 → AI / 人工双车道 → 循环回入口（高层会话流程 / 状态机视图） |
| `graph-overview.md` | flowchart | AI 路径内部总图：入口条件边 / Guard∥Router / Policy / 四个子图 / handoff 退出 / 发送前重校验（下层细节图） |
| `customer_service_final_architecture.svg` | SVG | 与 `graph-overview.md` 等价的下层细节图渲染版 |
| `state-machines.html` | 层叠 + 状态机 | 状态机管理全景：4 个状态机的存储 / 管理位置 / 转移触发（对应 `architecture.md` §11） |
| `interrupt-resume-sequence.md` | sequenceDiagram | 多轮对话中 interrupt → 审批/确认 → Command(resume) 恢复的时序 + Handoff 接管时序 |

## 维护规则

- 新增图表：按业务主题命名（如 `mutation-workflow.md`、`approval-flow.md`），并在本表登记。
- 修改图表：先改对应 spec（`docs/specs/`），再改图，保持二者一致。
- 不使用本目录承载业务定义，业务定义一律在 `docs/specs/`。
- 层级约定：`handoff-overview.svg` 为顶层图（会话流程 / 状态机）；`graph-overview.md` / `customer_service_final_architecture.svg` 为其 AI 路径的内部细节图。
