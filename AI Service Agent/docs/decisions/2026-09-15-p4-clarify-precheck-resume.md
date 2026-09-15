# 决策：P4 范围与设计定案（Clarify / 预检规则增量 / 读侧边界 / resume final 提取 / Guard 评估）

- 日期：2026-09-15
- 状态：定案（待 Spec 修订 + Readiness Check + Writing Plan；本文档为 P4 决策唯一记录）
- 来源：P4 Design Audit（2026-09-15，只读审计）→ 用户审核 D1–D8 + 本 agent 审核细化 → 用户裁定「按照这个方案执行」
- 关联：`docs/specs/graph-spec.md`（§6.2/§6.3/§9）· `safety-spec.md`（§1.2/§1.3）· `state-spec.md`（§1）· `event-spec.md`（§2）· `tool-spec.md`（§4）· `docs/plans/2026-09-15-p3-guard-mutation-highrisk.md`（P3 遗留 skip）

## 0. 批准范围（用户裁定）

- **D1 批准（含审核细化）**：Clarify = select 参数完备性触发；普通对话轮（**非 interrupt/resume**）；`execution_status=clarifying`；缺参确定性模板；最大澄清轮次 N=2 超限 fallback。
- **D2 批准**：预检新增「必填标识符非空」规则（schema 校验的业务层兜底）。
- **D3 批准**：最小订单状态 stub + precheck-fetch；fetch 只提供事实，不做授权/业务决策；Safety Precheck 保持确定性决策。
- **D4 批准**：ReadGraph 不增加 Precheck；`order_lookup` 负责属主校验。
- **D5 批准（带验证）**：先验证 CTRL-0089 当前实际拦截路径，确认 `unknown_tool` 后才修改 eval 语义。
- **D6 批准**：Guard LLM 在 P4 仅做评估报告，不进入实现。
- **D7 批准（含范围补丁）**：resume 轮通过 pre/post message diff 获取本轮新增 assistant message；SSE final + Message Store 均使用该结果；不恢复历史消息。**必须同时覆盖 /chat resume（event_stream）与 /approvals/{id}/approve|reject（服务端直接 ainvoke）两个入口**。
- **D8 批准**：eval drift 仅文档记录；CTRL-0077 不进入 P4；CTRL-0076 按 OQ-3 裁定（新增 `clarify` expected_policy 真实断言）。

## 1. 决策记录

### D1：Clarify（select 参数完备性触发，普通轮）

- **触发（两层判定，位于 select 节点）**：
  1. 必填业务字段（schema required、不含 `request_id`）缺失或为空串 → 不完整。
  2. 无必填业务字段（如 account_update 全可选）且全部业务字段为空 → 不完整。
  - 区分于既有 `_reject`：解析失败 / 工具不在档（`unknown_tool`）/ schema 校验错误（`invalid_args`）仍为拒绝（发 `safety_rejected`）；**只有"参数不完整"走 clarify**（发 `clarification_required`，不发 `safety_rejected`）。
- **节点**：mutation / high_risk 子图 select 后新增 clarify 分支（条件边路由，遵守 LangGraph 1.2.11「Command(goto) 不抑制静态出边」裁定，决策路由一律条件边）。
- **文案**：确定性模板，字段名→中文 label 映射（`order_id→订单号` 等，放 `clarify.py` 内联映射表）；`context["clarify_missing"]` 记录缺失字段列表。
- **状态**：`execution_status=clarifying`；**builder.py 顶层 `finalize` 必须条件化**（保留 `clarifying`/`waiting_*`，不得无条件覆盖为 `completed`——审计实测当前 finalize 无条件置 completed 会覆盖子图状态）。
- **轮次**：`context["clarify_count"]`；clarify 节点自增，**完整成功 select（pending_tool 落定）时清零**；超过 N=2 → 确定性 fallback 文案 + `completed`（不再澄清）。
- **事件**：新增 `clarification_required`（data `{missing: [...]}`），同步 event-spec §2/§2.1。
- **恢复**：用户澄清后下一条消息正常重走 Router → 子图 → select（历史随 Message Store 水合累积），不需要新 resume 分支。

### D2：必填标识符非空（预检业务层兜底）

- `safety.py` `_check` 增加通用规则：**所有 required schema 字段（不含 `request_id`）值为空字符串 → 拒绝**（`invalid_args`）。
- 与 D1 双层：select 层澄清（体验），预检层兜底（防绕过，如 select 之外来源的 args）。
- 实测依据：pydantic 接受 `order_id=""`（空串不触发校验错误），`FieldInfo.is_required()` 可用。

### D3：订单状态 stub + precheck-fetch（事实与决策分层）

- `tools/orders.py` 升级为**最小订单状态模型**：`order_id -> {user_id, status, signed_at, cancelled_at}`，含**确定性种子数据**（供离线 eval 与测试）：如 `ORD-10086`（signed，签收 10 天前）、`ORD-CANCEL-1`（cancelled）。
- 新确定性节点 `precheck_fetch`（mutation/high_risk，位于 select 后 safety_check 前）：`pending_tool.args` 含 `order_id` 时查订单状态 → 写 `context["precheck_data"]`（**仅事实**：order_id/status/signed_at/owner_user_id），**不做任何授权或业务裁决**。
- `safety_check` 规则表扩展（确定性消费 facts）：
  - 时限：`refund_apply` 且订单 `signed_at` 超 7 天无理由窗口 → 拒绝（CTRL-0086）。
  - 状态机：`order_cancel` 且订单 `status == cancelled` → 拒绝（CTRL-0090）。
- `recheck` 保持最小不变量（不重 fetch，D6/D7 既有定义）。

### D4：读侧数据边界（order_lookup 属主校验）

- **环境事实（2026-09-15 实测）**：本 venv `langchain-core 1.6.3` 的 `langchain_core.tools` **没有** `InjectedState`；它在 **`langgraph.prebuilt.InjectedState`**（可导入）。工具注入 `user_id: Annotated[str, InjectedState("user_id")]`。
- `order_lookup(order_id, user_id=InjectedState("user_id"))`：仅返回本人订单，越权返回边界拒绝（工具内实现，遵守 safety-spec §1.4）；**ReadGraph 不增加预检节点**。
- 契约破坏：既有 `order_lookup.invoke("ORD-1")` 调用与 read agent 测试需适配。

### D5：CTRL-0089 验证后 unskip

- 先验证当前实际拦截路径（预期：select `unknown_tool`——registry 无删除类工具）；确认后从 `DENY_SKIP_IDS` 移出转真实断言（`precheck_reason=unknown_tool`、无 interrupt）。

### D6：Guard LLM 评估报告（仅评估）

- 触发条件已达成（真机冒烟 6/6 同义改写绕过）。P4 交付**评估报告**（形态/成本/延迟/离线 fake 对齐/与 v1 并行兜底/切换策略 + 推荐），不实现。

### D7：resume 轮 final 提取（pre/post diff）

- `streaming.py`：resume run 开始时快照 checkpoint assistant message id 集合；run 后若 `final` 为空，取末条 assistant 文本，**仅当其 id 不在快照中（本轮新增）** 时作为 `final` + `message_id` 返回；否则保持 None。不回落历史文本（P1 原则不变）。
- `server.py` `_approval_resume`（/approvals approve|reject 直接 ainvoke）：同样 diff 提取本轮新增 assistant 文本 → `messages_store.append(conv_id, "assistant", text, message_id=...)`（复用同一 helper）。

### D8：eval 语义与范围

- **OQ-3 裁定（用户按推荐采纳）**：CTRL-0076 新增 `clarify` expected_policy（runner 新分支 + 数据集语义更新：`expected_risk_level=high_risk`、`expected_policy=clarify`），把 D1 机制做成真实断言（D14 精神）；routing 基线计数随之后移。
- CTRL-0086/0087/0089/0090 按 D3/D4/D5 转真实断言（runner `DENY_SKIP_IDS` 相应移除 + 读路径 eval 脚本）。
- eval drift（CTRL-0073/0097/0100）仅文档记录；CTRL-0077 不入 P4。

## 2. 后续关注（防过渡永久化）

- **Guard LLM 升级**：D6 评估报告出后，是否进入实现另行排期（触发条件已达成）。
- **Approval Service 迁移**：多进程/容器化部署需求出现时（P3 D8 既有登记）。
- **Memory 子计划**：需求明确时另行规划（P3 D10 既有登记）。
- **resume 轮 final 提取**：D7 实施后，真机冒烟复验（Mutation/HighRisk 完成文案可回传 + 落 Message Store）。

## 3. 影响与下一步

- 本决策为 P4 Spec/Decision 的依据；下一步：Spec 修订（graph-spec §6/§9、state-spec §1、event-spec §2、safety-spec §1.2/§1.3、tool-spec §4）→ Readiness Check → Writing Plan → Implementation。
- 涉及文件：`src/ai_service_agent/graph/workflows/{select,clarify(new),safety,mutation_agent,high_risk_agent}.py`、`graph/builder.py`、`tools/{orders,write}.py`、`harness/streaming.py`、`harness/server.py`、`evals/runner.py`、`tests/*`、eval 数据集 `tests/evals/control/master.jsonl`。
