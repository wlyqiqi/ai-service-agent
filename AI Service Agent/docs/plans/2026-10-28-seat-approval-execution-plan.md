# 坐席端审批卡 · 执行计划（可交接）

> **交接说明**：本文是给执行 agent 的施工单。按 §4 的任务顺序做，每个任务有独立验收；§8「不要做的事」是硬约束，不要自行扩大范围。
> 设计依据：`docs/plans/2026-10-28-seat-approval-card.md`（信息架构/文案/状态矩阵）· 视觉稿 `docs/ops/chatwoot/approval-card-mockup.html` · 决策 `docs/adr/0006-seat-approval-card-hitl.md`
> 现状证据：`docs/ops/chatwoot/approval-before.png`（坐席端实拍）

---

## 0. 目标与非目标

**目标**：坐席在 Chatwoot 会话里看到一张可读、可决策的高风险审批卡；同一条审批只出现一次；决策后卡片与客户侧文案都有明确交代。

**非目标**：不改 Chatwoot 镜像、不引入新依赖、不重构 HighRisk 图、不动审批决策语义（决策仍在图内）、不做客户侧 UI 改动。

---

## 1. 开工前必读

| 文件 | 为什么读 |
|---|---|
| `src/ai_service_agent/graph/workflows/high_risk_agent.py` | `prepare_approval` / `approval_pause` / `finalize`，审批中断与收尾 |
| `src/ai_service_agent/harness/approval_side_channel.py` | 投影、解析、决策链（`send_approval_form` / `decide`） |
| `src/ai_service_agent/harness/approvals.py` | `approval_record` 表结构与 `insert_if_absent` |
| `src/ai_service_agent/graph/workflows/copy.py` | `approval_summary` / `approval_*_text` |
| `src/ai_service_agent/graph/approval.py` | `resolve_approval_outcome`（resume → approved/rejected/expired） |
| `src/ai_service_agent/harness/server.py` | `POST /approvals/{id}/approve|reject`、`_seat_actor`、`_seat_http` |
| `src/ai_service_agent/domain/orders.py` | `OrderSnapshot.to_precheck_data()`、`get_user_profile()` |
| `tests/test_chatwoot_approval_form.py` | 会被改动的断言 |

---

## 2. 复用清单（先看有没有官方轮子）

| 环节 | 官方 | 本项目用法 |
|---|---|---|
| 暂停/恢复 | `langgraph.types.interrupt()` / `Command(resume=)` | **已在用**，不改 |
| 审批载荷形状 | `HITLRequest{action_requests, review_configs}`（`HumanInTheLoopMiddleware`） | **采用同名字段**，不 import 私有模块（见 ADR-0006） |
| 决策词汇 | `approve` / `edit` / `reject(message)` / `respond` | **采用**；一期只开 `approve` / `reject`，`edit` 留二期 |
| 工具级审批中间件 | `HumanInTheLoopMiddleware` | **不用**（只挂 `create_agent`；`reject` 产 `ToolMessage(status="error")` 会与 `finalize` 冲突） |
| 审批 UI | 无内嵌组件；Agent Inbox 为独立应用 | **自绘卡片**；Agent Inbox 仅作可选演示（T10） |

结论：真正需要自己写的只有 **HTML 卡片渲染** 与 **签名决策链接** 两块，其余全部复用。

---

## 3. 契约（先定死，再动手）

### 3.1 interrupt 载荷

```python
interrupt({
    "action_requests": [
        {"name": tool, "args": args, "description": "<markdown 摘要，可读的一句话>"}
    ],
    "review_configs": [
        {"action_name": tool, "allowed_decisions": ["approve", "reject"]}
    ],
    # 业务扩展：官方没有，但卡片与决策页需要
    "x_approval": {
        "approval_id": "...", "expires_at": "...", "facts": {...}
    },
})
```

### 3.2 resume 载荷（两种都要支持，兼容期 2 个版本）

```python
{"decisions": [{"type": "approve"}]}
{"decisions": [{"type": "reject", "message": "商品已发货，需走退货流程"}]}
# 旧形状继续可用（现有测试与 Chatwoot webhook 路径在用）
{"action": "approve"} / {"action": "reject", "note": "..."}
```

### 3.3 决策链接

```
t = base64url(json({"a": approval_id, "c": conversation_id, "s": seat_id, "e": exp_epoch}))
    + "." + hmac_sha256(secret, payload_b64)
secret = settings.approval_link_secret          # 新增可选配置 APPROVAL_LINK_SECRET
      或 HMAC(settings.seat_approval_secret, b"approval-link")   # 缺省派生，避免新增必填 env
TTL = 15 分钟；一次性由 approval_record.status 保证
```

| 方法 | 路径 | 行为 |
|---|---|---|
| GET | `/approvals/{id}/decide?t=` | 返回确认页 HTML（暖橙，复用卡片设计语言）；token 无效 400 / 过期 410 / 已处理 409（渲染成静态提示页，不抛 JSON） |
| POST | `/approvals/{id}/decide` | body `{"t","action","note"}` → 校验 token → `SeatActor(payload["s"])` → `seat.decide(...)` → `{"approval_id","status"}` |

现有 `POST /approvals/{id}/approve|reject`（header 工牌）**保留不动**，两条入口共用 `ApprovalSideChannel.decide`，幂等由 `store.precheck` 保证。

---

## 4. 任务

| # | 任务 | 主要文件 | 预估 | 依赖 |
|---|---|---|---|---|
| T1 | 结构化审批事实 | `copy.py` | 0.5d | — |
| T2 | interrupt 载荷对齐 HITL | `high_risk_agent.py`、`graph/approval.py`（`resolve_approval_outcome`） | 0.5d | T1 |
| T3 | 存储加列 + 投影幂等基础 | `approvals.py` | 0.25d | — |
| T4 | 卡片 HTML 渲染 | `harness/approval_card.py`(新) | 0.5d | T1 |
| T5 | 投影改卡片 + 幂等 | `approval_side_channel.py` | 0.25d | T3,T4 |
| T6 | 签名链接 + 决策页 | `approval_card.py`、`server.py` | 0.75d | T5 |
| T7 | 决策后时间线私备注 | `approval_side_channel.py` | 0.25d | T6 |
| T8 | 客户侧文案对齐 | `copy.py` | 0.1d | — |
| T9 | （可选）注入式真按钮 | `docs/ops/chatwoot/seat-approval.*`、`server.py`(CORS) | 1.5d | T6 |
| T10 | （可选）Agent Inbox 演示面板 | 无代码 | 0.5d | — |

### T1 · 结构化审批事实

新增 `approval_facts(tool, args, facts, user_id, profile=None) -> dict`（`copy.py`）：

```python
{
  "tool": "refund_apply", "action_label": "退款",
  "order_id": "ORD-2026-10059",
  "amount": 240.31, "currency": "CNY",
  "order_total": 240.31, "refunded": 0.0,
  "product_summary": "澄元智能破壁机 Pro Max 01 ×1" | None,
  "order_status": "paid", "order_status_label": "已付款 · 未发货",
  "customer": {"name": "张伟", "phone_tail": "6375"} | None,
  "requested_at": "...", "expires_at": "...",
  "customer_quote": "不想要了" | None,
}
```

- `approval_summary()` **保留**（审计/日志），不要删；两者共用同一份判定（`cancel_order` 需 `paid` 快照、`refund_apply` 允许部分金额）。
- 客户信息：`get_user_profile()` 目前只返回 `{user_id, phone, address}`；`customers` 表还有 `name` / `tier`。在 `domain/orders.py` 的查询里补 `name`（可选 `tier`，卡片可显示会员等级）。
  注意 `customers.phone` 数据带尾随空格（实测 `'15198775666 '`），取尾号前先 `strip()`，再取后 4 位。
- `product_summary` 为默认值 `"商品信息不可用"` 时视为缺失 → `None`（根因是快照构造没查商品，另开单，别在本 PR 修）。

**验收**：单测覆盖 cancel_order 缺快照（返回 `(None, "cancel_order_facts_unavailable")` 语义不变）、refund_apply 部分金额、update_address 通用分支；`approval_facts` 输出无 `"不可用"` 字样。

### T2 · interrupt 载荷对齐 HITL

`approval_pause` 改为 §3.1 结构；`x_approval.facts` 来自 T1。`graph/approval.py::resolve_approval_outcome(resume)` 支持 §3.2 双形状（优先 `decisions`，回落 `action`；`result="expired"` 优先级不变）。

**验收**：`pytest tests/test_approval_side_channel.py tests/test_approval_resume_gates.py` 通过；新增用例断言两种 resume 形状得到同一 outcome。

### T3 · 存储加列

`approval_record` 增 `payload_json TEXT`、`projected_at TEXT`，用幂等迁移（`PRAGMA table_info` 判断后再 `ALTER TABLE`，`CREATE TABLE IF NOT EXISTS` 保持）。`insert_if_absent` 落 `payload_json`；新增 `mark_projected(approval_id)`。

**验收**：对已有 `data/ai_service.db` 跑一次不报错、重复跑不报错；`test_approvals.py` 增迁移幂等用例。

### T4 · 卡片 HTML 渲染

`harness/approval_card.py`：

```python
def render_approval_card(facts: dict, *, approval_id: str, expires_at: str,
                         base_url: str, token: str) -> str: ...
def sign_decision_token(...) -> str: ...
def verify_decision_token(token: str, approval_id: str) -> dict: ...  # 失败抛 TokenInvalid/TokenExpired
```

要求：

- 全部字段 `html.escape`；只输出 `div/span/a/dl/dt/dd/h3/p` + 内联 `style`（Chatwoot 会过 DOMPurify，`script`/`on*` 会被剥，`style`/`class`/`href` 可用）。
- 视觉按 `docs/ops/chatwoot/approval-card-mockup.html` 的 token 落地（`#C0562B` / `#FFFDF8` / `#E6D8C6` / 424px / 12px 圆角 / 标题 17px-650 / 事实 12.5px / 按钮 38px）。
- 缺值兜底：商品 `商品信息暂缺，查看订单详情`；客户缺失显示 `客户信息暂缺`；**不要**出现 `None`、`u0034`、ISO 微秒串。
- 时间：`Asia/Shanghai`，格式 `今天 17:49`；剩余时间阶段一渲染静态 `30 分钟内有效`（无实时倒计时，见风险 R2）。
- DOM 文本顺序必须线性可读（会话列表/通知栏会 `getPlainText` 拉平）：抬头 → 标题 → 事实 → 原话 → 动作。

**验收**：新增 `tests/test_approval_card.py`：快照（含完整数据）、XSS 字段（`<script>` / `"` / `&`）转义、缺字段兜底、token 签名/篡改/过期。

### T5 · 投影改卡片 + 幂等

- `send_approval_form` → `send_approval_card`（`content_type="form"` 保留，`items` 保留但不再承载决策语义）；`approval_form_title_from_payload` 改名为 `render_approval_card`（旧函数删除，同步改测试）。
- `project()`：`record["projected_at"]` 非空直接返回；发送成功后 `mark_projected`。

**验收**：同一 `approval_id` 连续 `project()` 两次只发 1 条（`AsyncMock` 断言 `await_count == 1`）；修掉实测的 3 连发。

### T6 · 签名链接 + 决策页

- `GET /approvals/{id}/decide?t=`：HTML 确认页（复用 T4 的样式常量，抽 `CARD_CSS` 复用），显示金额/订单/客户 + 「批准并退款」/「拒绝」+ 拒绝理由输入（必填，前端 + 后端都校验）。
- `POST /approvals/{id}/decide`：校验 token → `SeatActor(payload["s"])` → `seat.decide(...)`；错误映射沿用 `_seat_http`（404/409/410），token 问题用 400。
- **不要**给 `GET` 加 CSRF 风险动作：GET 只渲染页面，决策只走 POST。

**验收**：`tests/test_approval_decide_link.py`（新）：正常批准/拒绝、token 篡改 400、过期 410、重复提交 409、拒绝缺理由 422。

### T7 · 决策后时间线

`ApprovalSideChannel.decide` 成功（或失败）后，向同一会话补发一条**短私备注**：`已批准 · 王琦 · 17:52`（失败：`执行失败 · 处理编号 8f3a1c2b`）。不要重复发（以 `decided_at` 是否已写为界）。

**验收**：`test_approval_side_channel.py` 断言决策后私备注恰好 1 条，重复决策不再发。

### T8 · 客户侧文案

`approval_approved_text` 补「预计 1–3 个工作日到账」，与卡片结果句一致。只改这一条，其余文案不动。

### T9 · （可选）注入式真按钮

- `docs/ops/chatwoot/seat-approval.css|js` → 挂进 `AI客服Demo/harness/chatwoot`（compose 只读挂载 + `z2/vueapp.html.erb` 覆盖 `app/views/layouts/vueapp.html.erb`）。
- JS 用 `MutationObserver` 把卡片里的 `a[data-approval-action]` 换成按钮 → `fetch` POST §3.3 → 就地替换为结果态；失败回退成链接。
- **runtime 需加 CORS**（现在没有任何 `CORSMiddleware`）：只允许 Chatwoot origin + `POST`。
- 同时隐藏 `FormBubble` 的「无响应」占位、把私备注底色改浅。

### T10 · （可选）Agent Inbox 演示

`langgraph dev` + 自建 Agent Inbox，连 `langgraph.json` 暴露的 `harness/graph.py:app`，仅作内部演示/调试。不接进产品路径。

---

## 5. PR 切分

| PR | 内容 | 说明 |
|---|---|---|
| PR0 | `docs/adr/0006-*` + 本计划 + 设计稿/截图 | 纯文档，先把设计基线入库；工作区当前这些文件均未提交 |
| PR1 | T1 + T2 + T3 | 数据与契约（含迁移、单测），不改变 UI 表现 |
| PR2 | T4 + T5 | 卡片渲染与投影（线上可见变化） |
| PR3 | T6 + T7 + T8 | 决策链路与文案 |
| PR4 | T9 | 注入增强（可选，独立 PR 便于回滚） |

流程（项目惯例）：新分支（建议 `feat/seat-approval-card`）→ 小步 commit → PR → CI `offline-tests` → **停在 PR，不自动合并**，汇报链接与结论等用户确认。

> 注意：当前工作区有大量未跟踪文件（`docs/`、`tmp/`、`_diag_*.py` 等）。PR0 只 `git add` 本计划涉及的文档与截图，不要把 `_diag_*`、`tmp/`、`.deepeval/` 一起提交。

---

## 6. 验收

**自动**

```bash
pytest tests --ignore=tests/evals
```

注意：本机可能得 `EXIT=1` 且 summary 被钩子输出吞掉，核对方式见 `.workbuddy-ai/memory/MEMORY.md`「全量验收不要只看退出码」。

**手工（真实链路）**

1. `AI_SERVICE_PORT=8766 .venv/Scripts/python.exe -m ai_service_agent.main`
2. Widget 用演示账号登录（`+65-89136375` / `123456`），发「我要退款」并确认表单
3. 坐席端 `/app/accounts/3/inbox-view/conversation/<id>`：卡片信息齐全、无「无响应」、按钮可点
4. 点批准 → 二次确认 → 卡片变结果态 → 客户收到到账时效话术
5. 重复触发同一审批 → 只出现 1 条
6. 改 `expires_at` 到过去 → 卡片与客户侧都显示过期

---

## 7. 风险与回滚

| # | 风险 | 处置 |
|---|---|---|
| R1 | HTML 被 DOMPurify 剥样式 | 只用 `style` 内联 + 白名单标签；上 PR2 前先在真实坐席端截图确认 |
| R2 | 卡片无实时倒计时 | 阶段一文案写「30 分钟内有效」，不写「剩 28:41」；阶段二由 JS 接管 |
| R3 | 会话列表预览被拉平成一长串 | DOM 文本顺序线性可读；`getPlainText` 后前 100 字仍是「高风险审批 · 退款 ¥240.31 · 张伟」 |
| R4 | 签名链接被转发 | TTL 15 分钟 + 一次性（`status`）+ `decided_by` 记座位 |
| R5 | 双入口（webhook 表单 / 链接）并发 | 共用 `seat.decide`，`store.precheck` 锁内预检已保证只有一个成功 |
| R6 | Chatwoot 升级改 `FormBubble` 结构 | 卡片用内联样式，不依赖 class；升级回归清单加一条 |

回滚：PR2/PR3 各自可独立 revert；`approval_record` 新增两列可空，回滚代码后旧逻辑照常工作（`SELECT` 显式列名，不受影响）。

---

## 8. 不要做的事

- ❌ 不改 Chatwoot 镜像、不 fork、不重打镜像
- ❌ 不把 `highRiskGraph` 改成 `create_agent` + `HumanInTheLoopMiddleware`（ADR-0006）
- ❌ 不把审批发给客户（必须 `private=true`）
- ❌ 不动 `ApprovalService` 的决策语义（审批仍在图内，存储只做持久化）
- ❌ 不引入新 Python/npm 依赖
- ❌ 不改 `product_summary` 的数据侧缺失问题（另开单）
- ❌ 不自动合并 PR
