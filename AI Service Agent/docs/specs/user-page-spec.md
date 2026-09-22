# 用户页面规格（user-page-spec.md）

- 日期：2026-09-17
- 状态：brainstorming 定稿（待实现计划）
- 关联：`event-spec.md`（事件协议）· `handoff-spec.md`（conversation_mode / 人工车道）· `architecture.md`（Transport 边界）· `approval-spec.md`（审批）· `tool-spec.md`（工具语义）

## 1. 目标与范围

- 目标：为智能家电品牌提供**桌面端 Web 用户页面**：登录后与 AI 客服对话、在对话内完成业务操作（查订单 / 改地址 / 取消订单 / 申请售后等）、需要时转人工客服。
- 范围：**只做用户页面前端（Transport 层）**；不新增 / 修改 LangGraph 图内逻辑；不修改后端事件协议（以 `event-spec.md` 为准）；认证后端与具体 API 路径留占位，由后续后端任务补齐。
- 交付物：单文件 HTML（`web/index.html`），无构建工具，FastAPI 静态托管或本地静态服务器预览。

## 2. 设计定案（brainstorming 结论）

| # | 决策点 | 定案 |
|---|---|---|
| 1 | 渠道 | 桌面 Web 为主（品牌官网 / 独立页面） |
| 2 | 登录 | 需要登录（账号体系），页面内登录 / 会话管理；MVP 认证后端为 SQLite + JWT |
| 3 | 业务操作 | 纯对话驱动：所有业务操作在对话框内完成，快捷指令 chips 把指令带入输入框 |
| 4 | 转人工 | 简单承接：对话窗继续、轮询刷新坐席回复、仅标注「人工接待中」 |
| 5 | 视觉 | C · 干净商务（`#f6f8fb` / `#2563eb` / `#1f2937`） |
| 6 | 登录实现 | MVP 登录使用 SQLite + JWT：`POST /auth/login` 提交 `{phone, password}`；成功返回 JWT 与用户 ID。生产认证后续替换为 Supabase，前端 Bearer 契约保持不变。会话过期回登录视图 |
| 7 | 技术栈 | 单文件 HTML（内嵌 CSS/JS），hash 路由两个视图 |
| 8 | 布局 | 1 · 居中单栏对话（约 860px），无左栏 |

## 3. 信息架构与布局

### 3.1 顶栏（全宽，约 56px）

- 左：品牌「智家」Logo + “AI 客服中心”。
- 右：用户区（头像 + 用户名 + 下拉：个人资料 / 退出登录）。登录、登出、会话过期入口都在这里。

### 3.2 对话窗（居中，约 860px，白卡片）

- 会话头部：客服头像 + “智家客服中心” + 状态标签（`AI 在线` / `人工接待中`）。
- 快捷指令条（头部下方 chips）：新会话 / 查订单 / 改地址 / 申请售后 / 转人工——点击把指令带入输入框并发送，保持“纯对话驱动”。“新会话”= 发送“重新开始”指令，不新建会话、不清历史（`conversation_id` 保持不变）。
- 消息流：用户 / 客服气泡 + 事件卡片（见 §5）。
- 输入区：输入框 + 发送按钮；转人工后输入框保留（用户消息直达坐席），仅显示“当前由人工客服接待”轻提示。

### 3.3 登录视图

- 居中登录卡片：账号 + 密码；成功 → 对话视图；失败 → 提示错误。

## 4. 视图与路由

- 单文件内两个视图，hash 路由：`#/login`、`#/chat`。
- 鉴权守卫：无 token 访问 `#/chat` → 重定向 `#/login`。
- 会话过期（401）→ 提示 → 回 `#/login`；重新登录后恢复原会话（`conversation_id` 不变）。

## 5. 对话流交互卡片（事件 → UI 映射）

前端逐事件渲染，不需要维护图内部状态（事件可独立消费，遵循 `event-spec.md` §4）。

| 事件 | 前端表现 |
|---|---|
| `token` | AI 气泡流式文本（打字机效果） |
| `tool_call` / `tool_result` | 工具执行小卡片（图标 + 名称 + 结果摘要，如“正在查询订单… ✓”），可折叠 |
| `clarification_required` | 澄清卡片：列出缺失字段（如“请提供订单号”）+ 快捷补全选项 |
| `confirmation_required` | 确认卡片：操作影响摘要 + `[确认]` `[取消]`（Mutation） |
| `confirmation_updated` | 确认卡片状态刷新（已确认 / 已取消） |
| `approval_required` | 审批卡片：摘要 + “审批中”状态 + 到期时间（HighRisk） |
| `approval_updated` | 审批卡片状态刷新（通过 / 拒绝 / 过期） |
| `safety_rejected` | 拒绝提示条（规则 + 原因，中性样式，不打断对话） |
| `handoff_created` | 人工接管横幅（“已转人工客服”）+ 头部标签变「人工接待中」 |
| `handoff_resolved` | 横幅收起为历史消息，标签回「AI 在线」 |
| `error` | 错误条（code + message），对话不中断 |
| `done` | 本 turn 结束，停掉输入中的加载指示 |

UX 规则：

- 确认 / 审批按钮带 pending 态，防重复点击；`request_id` 幂等由后端保证，不暴露给用户。
- 人工车道：轮询刷新坐席回复，新消息自动滚动到底。
- AI 回复的 `mode == ai` 实时校验由后端强制（`handoff-spec.md` §1.4），前端只负责展示，不做兜底逻辑。

## 6. 前端状态与数据流

### 6.1 store

- `view`: `login` | `chat`
- `auth`: `{ token, user, expired }`（token 存 sessionStorage）
- `conversation`: `{ conversation_id, mode: 'ai' | 'human' }`
- `messages[]`: `{ id, role: user|assistant|system, kind: text|tool|clarify|confirm|approval|handoff|error, content, meta }`
- `conn`: `{ status: idle|streaming|reconnecting, lastError }`
- `pendingAction`: 当前未决的确认 / 审批卡片（按钮 pending 态用）

### 6.2 四条链路

1. **登录**：`POST /auth/login` → JWT（`Authorization: Bearer <token>`）→ `POST /sessions`（后端显式写 `mode=ai`，返回 `conversation_id`）→ `#/chat` → `GET /conversations/{id}/messages` 载入历史。
2. **AI 对话**：用户发消息 → `POST /chat`（SSE）→ 事件逐个渲染（§5 映射）→ `done` 结束。
3. **人工车道**：`mode == human` 时发消息 → 写 Message Store（同步给坐席）→ 回复靠轮询 `GET /conversations/{id}/messages` 刷出；收到 `handoff_resolved` → 标签回「AI 在线」。
4. **动作接口**：确认 / 取消、审批通过 / 拒绝、澄清补全走独立 HTTP 动作（占位）；前端只调 api 层占位函数，不感知 resume 细节；返回 `confirmation_updated` / `approval_updated` 刷新卡片。

### 6.3 断线与恢复

- SSE 断开 → `reconnecting` 状态 + 输入区禁用，3 秒退避重连。
- 轮询失败指数退避。
- 页面刷新后从历史消息重建视图（Message Store 为唯一真相源）。

## 7. 错误处理与边界

- api 层所有调用失败 → `conn.lastError` + 错误条，对话不中断。
- 前端不做 mode / 权限 / 安全兜底判断，以后端确定性逻辑为准。
- 不向用户暴露 `request_id`、`thread_id` 等内部标识。

## 8. 实现结构

- `web/index.html` 单文件内部分层：
  - `api`（Transport 层）：`login / createSession / sendMessage(SSE) / getMessages(轮询) / confirm / approve`；login / createSession / sendMessage / getMessages 已对接真实后端（§10.1），confirm / approve 仍为占位（§10.2）。
  - `events` 层：SSE 事件 → store 更新。
  - `render` 层：store → DOM 纯函数。
  - `router`：hash 切视图 + 鉴权守卫。
- Mock 模式：URL 参数或 localStorage `mock=true`——用离线 fake 事件流覆盖 `event-spec.md` 全部事件类型，不依赖后端 / 模型即可预览与验收 UI；后端就绪后只切 api 层实现。

## 9. 验证

- Mock 手测清单：登录 → 对话 → 澄清 → 确认 → 审批 → `safety_rejected` → 转人工 → 轮询坐席回复 → `handoff_resolved` → `error` → 断线重连。
- 刷新页面会话不丢（历史从 Message Store 重建）。
- 浏览器控制台无报错。

## 10. 接口契约（对齐状态 2026-09-18）

### 10.1 已对齐（页面 api 层 ↔ 真实后端）

| 函数 | 接口 | 说明 |
|---|---|---|
| `login` | `POST /auth/login` | 提交 `{phone, password}`，返回 `{token, user:{id,name}}`；MVP 登录使用 SQLite + JWT，生产认证后续替换为 Supabase，前端 Bearer 契约保持不变 |
| `createSession` | `POST /sessions` `{user_id}` | 返回 `{session_id, mode}`（显式 `mode=ai`）；页面读 `session_id` |
| `sendMessage` | `POST /chat` `{session_id, message}`（SSE） | 事件流逐条渲染 |
| `getMessages` | `GET /conversations/{id}/messages` | 返回 `{items, mode}`；页面归一化为 `{messages, mode}` |

### 10.2 仍为占位（D5：AI 车道动作，后续任务对接）

| 函数 | 真实后端契约 | 说明 |
|---|---|---|
| `confirm` / `clarify` | 经 `/chat` resume（`resolve_resume_payload`） | 页面动作接口待对齐；不属于「用户 ↔ 坐席」链路 |
| `approve` / `reject` | `POST /approvals/{id}/approve` 或 `/reject` | 页面 approve 端点待对齐 |

## 11. 变更纪律

- 新增事件类型：同步更新 `event-spec.md` 与本文件 §5 映射表。
- 改页面结构 / 路由 / 数据流：更新本文件。
- 对接真实后端后：移除 §10 占位标注并更新对应契约。
