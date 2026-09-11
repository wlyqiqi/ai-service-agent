# 规格文档索引（docs/specs/）

按**渐进式披露**原则拆分：实现某个模块时只读相关文件，不整包加载，避免 token 浪费与读岔。

## 文件地图

| 文件 | 内容 | 谁该读 |
|---|---|---|
| `architecture.md` | 背景目标、架构总览、技术选型 + 横切关注点（持久化/错误/观测/测试/目录/假设/风险） | 所有人（起点） |
| `state-spec.md` | CustomerServiceState 状态设计 | 改动 State 的人 |
| `graph-spec.md` | 总图：入口条件边/ResumeHandler/Router/Policy/Read/Mutation/HighRisk/Fallback | 改图的人 |
| `tool-spec.md` | 工具注册表与 risk_level 权限模型 | 加工具的人 |
| `approval-spec.md` | 审批服务（Approval Service） | 做审批的人 |
| `event-spec.md` | 归一化事件协议（SSE/WS） | 前后端对接的人 |
| `safety-spec.md` | 安全预检 + handoff 触发规则 | 安全相关改动 |

## 任务 → 阅读组合

| 任务 | 需要读 |
|---|---|
| 实现 ReadGraph | `graph-spec.md` + `state-spec.md` + `tool-spec.md` |
| 实现 MutationGraph | `graph-spec.md` + `state-spec.md` + `safety-spec.md` + `event-spec.md` |
| 实现 HighRiskGraph | `graph-spec.md` + `state-spec.md` + `approval-spec.md` + `safety-spec.md` + `event-spec.md` |
| 实现审批服务 | `approval-spec.md` + `event-spec.md` |
| 加一个新工具 | `tool-spec.md` + `safety-spec.md` |
| 接前端流式 | `event-spec.md` + `architecture.md`（可观测性章节） |
| 会话/记忆持久化 | `architecture.md`（持久化章节）+ `state-spec.md` |

- 决策记录见 `docs/decisions/`（架构决策留痕）。

## 变更纪律

- 一次 PR 尽量只动与主题相关的 spec 文件，保持 diff 聚焦。
- 改 State/事件/接口契约时，同步更新对应 spec 并在此索引登记。
