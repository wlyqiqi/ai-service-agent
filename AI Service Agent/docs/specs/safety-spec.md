# 安全规格（safety-spec.md）

- 状态：**新增**（补原设计两个缺口：安全预检、handoff 触发规则）
- 关联：`tool-spec.md`（工具元数据）· `graph-spec.md`（预检节点位置）· `approval-spec.md`（审批 gate）· `event-spec.md`（safety_rejected / handoff_created / handoff_resolved）· `architecture.md`（错误处理）

## 1. 安全预检（Safety Pre-check）

### 1.1 定位

- 图内**确定性节点** `safety_check`，位于 MutationGraph / HighRiskGraph 的写操作执行**之前**，作为第一道 gate。
- 顺序固定：**安全预检 →（Mutation）用户确认 /（HighRisk）人工审批 → 执行**。预检先于 interrupt，避免让用户/审批人对非法操作做无谓确认。
- 预检失败 → 拒绝并收尾，**不进入 interrupt**，也不调用工具。

### 1.2 检查项（规则表，可配置）

| 规则 | 说明 | 示例 |
|---|---|---|
| 参数 schema 校验 | 工具 `schema` 白名单校验 | 订单号格式、金额范围 |
| 权限校验 | 用户身份 + 工具 `min_risk_level` 匹配 | 未登录不能改账户 |
| 数据边界 | 越权/跨租户访问拦截 | 只能查自己的订单 |
| 业务规则 | 频次/额度/状态机约束 | 已取消订单不可再取消（CTRL-0090）；单日退款次数上限 |
| 必填标识符非空（D2，P4） | required schema 字段值不得为空串（`request_id` 除外） | `order_id=""` → 拒绝 |
| 退款时限（D3，P4） | 无理由退款须在签收 7 天窗口内 | 签收超 7 天 → 拒绝（CTRL-0086） |
| 敏感操作复核 | 破坏性关键词/高危参数二次确认要求 | 批量删除、大额转账 |

- **v1 范围（D4 定案，P3）**：先实现「参数 schema 白名单 + 权限（`min_risk_level` 匹配 + `user_id` 登录态）+ 数据边界」。
- **P4 增量（D2/D3 定案）**：业务规则 v1 落地「必填标识符非空 + 退款时限 + 取消状态机」（事实来自 `precheck_fetch` 写入的 `context["precheck_data"]`，仅事实；裁决仍在 `safety_check` 确定性完成）；「敏感操作复核」与其余频次/额度规则仍留后续。
- **参数不完整 ≠ 拒绝**：select 层判定缺参/空参时走 clarify（普通轮，`execution_status=clarifying`，发 `clarification_required`，见 graph-spec.md §6.2/§6.3）；`safety_rejected` 仅用于真正的拒绝（解析失败/未知工具/schema 错误/预检规则失败）。

### 1.3 结果与事件

- `pass` → 继续流程。
- `fail` → 写回 `messages`（拒绝原因）+ 发 `safety_rejected` 事件 + 收尾；可配置是否走 Fallback 解释。

### 1.4 纪律

- 预检是图内强制节点，工具自身不得绕过；Read 工具不需要预检（只读），但数据边界规则仍由工具实现保证。

### 1.5 Guard 检测节点（并行于 Router）

- 独立检测**原始输入**的恶意意图与情绪升级信号，不依赖 Router 分类结果（图位置见 `graph-spec.md` §2）。

| 信号 | 示例 |
|---|---|
| 恶意 / 越权意图 | Prompt 注入、诱导越权、提权、威胁 |
| 情绪升级 | 愤怒/投诉高频词连续出现、情绪评分超阈值 |

- `fail` → 发 `safety_rejected` 事件（rule=`guard`）+ 安全收尾，Router 并行结果不消费；可配置转 handoff 规则。
- `pass` → 正常进入 Policy。
- **P3 实现形态（D2 定案）**：确定性规则 v1（关键词/启发式，阈值可配置，无模型 key，离线可测）；**v1 是过渡实现、非 Guard 终态能力**——无法有效覆盖语义层面的恶意输入/越权诱导（如同义替换、间接暗示等绕过关键词匹配的攻击），后续须评估升级为 LLM Guard 或语义层判断（升级评估登记见 `docs/decisions/2026-09-15-p3-design-decisions.md` §2）。
- 阈值全部可配置。

## 2. Handoff 触发规则（转人工）

### 2.1 触发源

| 类别 | 规则 | 可配置项 |
|---|---|---|
| 显式请求 | 用户明确要求"转人工/人工客服/找真人"等意图 | 关键词/意图列表 |
| 连续失败（路由级） | 连续 N 次 `unknown` 或低置信度路由 | N（默认 3） |
| 连续失败（工具级） | 同一工具连续失败 N 次 | N（默认 3），计数存 `context` |
| 重复未解决 | 同一诉求重复提出超过 X 次仍未解决 | X（默认 3） |
| 情绪信号 | 负向情绪（愤怒/投诉高频词）且连续出现 | 检测开关 |
| 审批被拒后坚持 | HighRisk 审批被拒后用户仍要求执行 | 开关（默认开） |
| 策略上限 | 单会话时长 / token / turn 数超限 | 阈值 |

### 2.2 流程

```text
规则命中 -> check_handoff 置位 handoff、execution_status=waiting_handoff
       -> 抛出 interrupt(handoff_request)（reason / summary，见 graph-spec.md §6.5）
       -> 外层接管：创建 Handoff Record + 原子切换 conversation_mode=human + 发 handoff_created
       -> 通知用户（含工单号/排队说明）-> 本 turn 结束
```

- 触发判定节点：`check_handoff`（确定性规则）检测信号并置位 `handoff` / `execution_status=waiting_handoff`，随后抛出 `interrupt(handoff_request)`；工具级失败计数在 `context` 中维护。Concierge / 审批拒绝分支可复用同一机制置位转人工信号。
- 转人工后 `conversation_mode=human`，后续用户消息在入口直接进入**人工车道**（写 Message Store + 通知坐席，见 `handoff-spec.md` §1），不触发 checkpointer / Graph / Guard / Router；人工 Resolve 回切 mode=ai 后恢复 AI 路径。

### 2.3 边界

- **审批拒绝 ≠ 自动转人工**：拒绝后按 2.1 规则判定（默认用户坚持才转）。
- 预检失败不直接转人工，按 1.3 收尾；用户再次追问同一操作时才累计计数。
- 转人工信号由确定性节点 `check_handoff` 检测并经 `interrupt(handoff_request)` 抛出，**不作为工具注册**、不进入任何 agent 工具集，LLM 无权选择转人工；Handoff Record 创建与 `conversation_mode` 切换由外层（Conversation Runtime）统一完成，子图不直接调用任何 Handoff API（见 `graph-spec.md` §6.5、`handoff-spec.md` §3）。

## 3. 变更纪律

- 改预检规则 / handoff 阈值：改本文件 + 对应测试（安全预检测试、handoff 规则测试）。
- 新增高危操作类型：同时更新 `tool-spec.md` 工具表与本文件规则表。
