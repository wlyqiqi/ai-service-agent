# 规格文档索引（docs/specs/）

按**渐进式披露**原则拆分：实现某个模块时只读相关文件，不整包加载，避免 token 浪费与读岔。

## 文件地图

| 文件 | 内容 | 谁该读 |
|---|---|---|
| `architecture.md` | 背景目标、架构总览、技术选型 + 横切关注点（持久化/错误/观测/harness hook/测试/目录/假设/风险） | 所有人（起点） |
| `state-spec.md` | CustomerServiceState 状态设计 | 改动 State 的人 |
| `graph-spec.md` | 总图：入口 resume 调度/Router/Policy/Read/Mutation/HighRisk/Fallback | 改图的人 |
| `tool-spec.md` | 工具注册表与 risk_level 权限模型 | 加工具的人 |
| `approval-spec.md` | 审批服务（Approval Service） | 做审批的人 |
| `event-spec.md` | 归一化事件协议（SSE/WS）；含 `form_required` / `form_rejected`（Form Interrupt 投影） | 前后端对接的人 |
| `mutation-form-interrupt-spec.md` | Mutation Form / Form Interrupt / Form Resume 产品与实现决策 | 改 Mutation 写路径 / 表单事件的人 |
| `safety-spec.md` | 安全预检 + handoff 触发规则 | 安全相关改动 |
| `handoff-spec.md` | conversation_mode 状态机 + Handoff 生命周期 + 轻量判断 | 做会话入口 / handoff 的人 |
| `mvp-first-mile.md` | MVP 第一公里实现范围与里程碑（brainstorming 定稿） | 做 MVP 实现 / 执行计划的人 |
| `user-page-spec.md` | 用户侧 AI 客服页面（布局 / 组件 / 事件映射 / 数据流） | 做用户页面的人 |

## 任务 → 阅读组合

| 任务 | 需要读 |
|---|---|
| 实现 ReadGraph | `graph-spec.md` + `state-spec.md` + `tool-spec.md` |
| 实现 MutationGraph | `graph-spec.md` + `state-spec.md` + `safety-spec.md` + `event-spec.md` |
| 实现 Mutation Form（Form Interrupt） | `mutation-form-interrupt-spec.md` + `event-spec.md` §2.2 + `graph-spec.md` + `CONTEXT.md`（根目录术语） |
| 实现 HighRiskGraph | `graph-spec.md` + `state-spec.md` + `approval-spec.md` + `safety-spec.md` + `event-spec.md` |
| 实现审批服务 | `approval-spec.md` + `event-spec.md` |
| 加一个新工具 | `tool-spec.md` + `safety-spec.md` |
| 接前端流式 | `event-spec.md` + `architecture.md`（可观测性章节） |
| 会话/记忆持久化 | `architecture.md`（持久化章节）+ `state-spec.md` |
| 会话入口 / conversation_mode 判断 | `handoff-spec.md` + `architecture.md` + `event-spec.md` |
| 做用户页面（前端） | `user-page-spec.md` + `event-spec.md` |

- 决策记录见 `docs/decisions/`（架构决策留痕）。

## 变更纪律

- 一次 PR 尽量只动与主题相关的 spec 文件，保持 diff 聚焦。
- 改 State/事件/接口契约时，同步更新对应 spec 并在此索引登记。
