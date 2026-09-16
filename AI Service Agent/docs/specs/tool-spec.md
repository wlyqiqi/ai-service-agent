# 工具规格（tool-spec.md）

- 关联：`safety-spec.md`（预检）· `graph-spec.md`（消费方）· `architecture.md`（假设：ERP stub）

## 1. 工具注册表 `ToolRegistry`

- 统一注册入口；每个工具声明元数据：

```python
class ToolMeta:
    name: str
    description: str
    min_risk_level: Literal["read", "mutation", "high_risk"]   # 该工具最低所需风险等级
    schema: type[BaseModel] | None                             # 参数 schema（预检用）
    idempotent: bool = False
```

- 子图构建时按 `risk_level` 注入允许的工具集：
  - ReadGraph：仅 `min_risk_level == "read"` 的工具。
  - MutationGraph：`read` + `mutation`。
  - HighRiskGraph：`read` + `mutation` + `high_risk`。
- ReadGraph 永远拿不到写工具（构建期过滤，而非运行期拦截）。
- 写图 select 节点允许集为**档内严格**（P5 B2b）：仅 `min_risk_level == tier` 的写工具可被选中；
  只读工具不进入写图 select 允许集与系统提示，若模型仍选中已注册低档工具 → `tier_clarify` 澄清
  （普通轮，见 graph-spec.md §6.2/§6.3），未知工具仍拒绝。ToolNode 工具注入保持分层。

## 2. 工具实现约定

- 工具函数签名：参数带类型注解与描述；通过 `InjectedState` / `InjectedStore` / `ToolRuntime` 访问会话/记忆/运行时。
- 返回值必须是 JSON 可序列化结构；工具内部异常由 `handle_tool_errors` 归一化，不得裸抛到图外。
- 写工具必须支持幂等键（`idempotent=True` + 参数带 `request_id`），防重放。
- 工具自身不承载横切逻辑（事件、观测、审计、限流等）：由 harness hook 在工具执行前后统一挂接（见 `architecture.md` §8.1）。

## 3. 与安全预检的衔接

- 写/高险工具执行前必须经过 `safety-spec.md` 的预检节点（参数白名单、权限、业务规则、数据边界）。
- 工具自身不得绕过预检（预检是图内强制节点，非工具内自检）。

## 4. v1 工具清单

| 工具 | 类型 | min_risk_level | 说明 |
|---|---|---|---|
| `kb_search` | 知识库检索 | read | sqlite-vec / 关键词兜底 |
| `order_lookup` | 订单查询 | read | stub 客户端；**P4（D4）属主校验**：`user_id` 经 `langgraph.prebuilt.InjectedState` 注入，仅返回本人订单，越权拒绝 |
| `account_update` | 账户变更 | mutation | 幂等 |
| `order_cancel` | 订单取消 | high_risk | 需人工审批 |
| `refund_apply` | 退款申请 | high_risk | 需人工审批 |

- **P3 实现（D5 定案）**：`account_update` / `order_cancel` / `refund_apply` 以 stub 落地（内存 fake store，不引入真实外部系统依赖），`idempotent=True` + 参数带 `request_id`（防重放，见 §2）；按 `min_risk_level` 档注册进 ToolRegistry，`for_risk_level` 分层注入不变。
- **P4 实现（D3/D4 定案）**：`orders.py` 升级为最小订单状态模型（`order_id -> {user_id, status, signed_at, cancelled_at}`，含确定性种子数据供离线 eval/测试）；`order_lookup` 承担读侧属主校验（D4）；订单状态事实由 `precheck_fetch` 节点提供给预检（D3，见 safety-spec.md §1.2）。
- **P5（B1）**：只读工具补 select 侧 schema（`kb_search`=`{text, request_id}`、
  `order_lookup`=`{order_id, request_id}`；`request_id` 由 select 注入，`user_id` 经 InjectedState 注入不入 schema），
  注册表元数据完备（预检用）。

- 转人工**不是工具**：由 `check_handoff` 确定性节点检测信号并经 `interrupt(handoff_request)` 抛出（见 `safety-spec.md` §2、`graph-spec.md` §6.5），不作为工具注册、不进入任何 agent 工具集；Handoff Record 创建与 `conversation_mode` 切换由外层（Conversation Runtime）完成，子图不直接调用任何 Handoff API。

## 5. 变更纪律

- 新增/修改工具：更新本文件 + `safety-spec.md`（若涉及预检规则）+ 工具对应测试。
