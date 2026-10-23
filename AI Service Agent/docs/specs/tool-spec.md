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
  只读工具不进入写图 select 允许集与系统提示。HighRisk 若仍选中已注册低档工具 → `tier_clarify` 澄清
  （普通轮，见 graph-spec.md §6.3）；Mutation 选中未绑定/低档工具 → `unknown_tool`（ADR 0004）。ToolNode 工具注入保持分层。

## Allowed Tools（Router v2）

- Router v2 不选择工具。
- Policy Gate 根据 `route_intent` 输出 `allowed_tools`。
- `allowed_tools` 是候选工具边界，子图仍负责最终工具选择与参数提取。
- 子图模型可见工具集必须与 `allowed_tools` 相交。
- ToolNode 执行前必须拒绝不在 `allowed_tools` 内的工具调用。
- 现有 risk-tier 校验、schema 校验、业务预检、审批和 recheck 全部保留。

## 2. 工具实现约定

- 工具函数签名：参数带类型注解与描述；通过 `InjectedState` / `InjectedStore` / `ToolRuntime` 访问会话/记忆/运行时。
- 返回值必须是 JSON 可序列化结构；工具内部异常由 `handle_tool_errors` 归一化，不得裸抛到图外。
- 写工具必须支持幂等键（`idempotent=True` + 参数带 `request_id`），防重放。
- 工具自身不承载横切逻辑（事件、观测、审计、限流等）：由 harness hook 在工具执行前后统一挂接（见 `architecture.md` §8.1）。
- **订单边界（2026-10-10）**：订单读写工具不直接打开数据库连接；实现通过 `OrderRepository` 访问订单事实与命令。高风险写工具由 `make_cancel_order_tool(order_repository)` / `make_refund_apply_tool(order_repository)` 工厂构造；SQLite 只保留在 `SQLiteOrderRepository` adapter 内，后续可替换为 Postgres / HTTP / ERP adapter。

## 3. 与安全预检的衔接

- 写/高险工具执行前必须经过 `safety-spec.md` 的预检节点（参数白名单、权限、业务规则、数据边界）。
- 工具自身不得绕过预检（预检是图内强制节点，非工具内自检）。

## 4. v1 工具清单

| 工具 | 类型 | min_risk_level | 说明 |
|---|---|---|---|
| `kb_search` | 知识库检索 | read | 真实 LightRAG-chengyuan 检索（read 档）：`only_need_context` / `include_references` / `naive` / `top_k=5` / `chunk_top_k=5`（请求显式传，不吃服务端 CHUNK_TOP_K 默认）/ 关 rerank；返回上下文 + 参考来源；不可达 / 无结果返回中性检索信号（无成品客服话术、无转人工引导，话术由 ReadGraph 提示词负责）；引用来源在回复文本内标注（event-spec 不变） |
| `order_lookup` | 订单查询 | read | **数据源替换（2026-09-19，同 `kb_search` stub→真实检索模式）**：内部实现从写死 stub 改为查询模拟电商库 `data/mock_ecommerce.db`（`scripts/seed_mock_db.py` 确定性生成，配置 `mock_ecommerce_db_path`，只读连接，库缺失/损坏确定性降级）；解析顺序 = `ORDER_STORE` 评测夹具（相对时间语义，CTRL-0086/0090 依赖，永远优先）→ 模拟电商库 → `not_found`；ToolMeta.schema 与对外接口/返回字段不变；**P4（D4）属主校验**不变：`user_id` 经 `langgraph.prebuilt.InjectedState` 注入，仅返回本人订单，越权拒绝 |
| `my_orders` | 我的订单列表 | read | 登录用户名下最近 10 单（倒序）；`status` 可选过滤（paid/shipped/delivered/completed/refunding/refunded/cancelled）；零业务参数即可用——身份即授权（`user_id` 经 InjectedState 注入，不由模型/用户提供）；只读连接（mode=ro），库缺失降级 `data_source_unavailable`（2026-09-19） |
| `shipment_query` | 物流查询 | read | 登录用户订单物流；`order_id` 可选（提供则精确查 + D4 属主校验，缺省优先返回在途订单物流，最多 5 条）；cancelled 单返回「已取消，无物流信息」；只读连接（mode=ro），库缺失降级 `data_source_unavailable`（2026-09-19） |
| `update_address` | 修改收货地址 | mutation | 幂等；schema 必填 `order_id` + `address`；内部实现校验本人订单后写 `customers.address`，`user_id` 经 InjectedState 注入；状态规则由 safety_check 裁决：`paid` 允许，`shipped` / `signed` / `completed` / `cancelled` / `refunding` / `refunded` 返回 `address_not_editable` |
| `update_phone` | 修改联系电话 | mutation | 幂等；内部实现写模拟电商库 `customers.phone`，`user_id` 经 InjectedState 注入 |
| `update_order_note` | 修改订单备注 | mutation | 幂等；内部实现写模拟电商库 `orders.note`；仅限本人订单（D4） |
| `create_invoice` | 开具发票 | mutation | 幂等；内部实现写模拟电商库 `invoices`；仅限本人订单；状态规则由 safety_check 裁决：`cancelled` / `refunding` / `refunded` 返回 `invoice_not_allowed`；金额规则由 safety_check 裁决（`amount + 已开票累计 <= orders.total_amount`），一单一开、允许部分开票 |
| `create_service_ticket` | 创建售后工单 | mutation | 幂等；内部实现写模拟电商库 `service_tickets`；仅限本人订单；工单-会话关联留待 handoff 集成阶段实现 |
| `cancel_order` | 取消订单 | high_risk | 幂等；需人工审批；通过 `OrderRepository` 执行；仅限本人 `paid` 订单；`paid -> cancelled`；必须显式携带 `order_id`，不自动选择候选；不触发退款、不回补库存；同一 `request_id` 仅在取消事务成功提交后生成幂等记录 |
| `refund_apply` | 退款申请 | high_risk | 需人工审批；通过 `OrderRepository` 执行；必须显式携带 `order_id`，不自动选择候选；状态规则由 safety_check 裁决：`paid` 或签收 7 天窗口内的 `signed` 允许，`signed` 超窗返回 `refund_window_expired`，`completed` 返回 `refund_not_allowed`，`refunding` / `refunded` 分别返回 `order_already_refunding` / `order_already_refunded` |

- **P3 实现（D5 定案）**：`account_update` / `order_cancel` / `refund_apply` 以 stub 落地（内存 fake store，不引入真实外部系统依赖），`idempotent=True` + 参数带 `request_id`（防重放，见 §2）；按 `min_risk_level` 档注册进 ToolRegistry，`for_risk_level` 分层注入不变。
- **P4 实现（D3/D4 定案）**：`orders.py` 升级为最小订单状态模型（`order_id -> {user_id, status, signed_at, cancelled_at}`，含确定性种子数据供离线 eval/测试）；`order_lookup` 承担读侧属主校验（D4）；订单状态事实由 `precheck_fetch` 节点提供给预检（D3，见 safety-spec.md §1.2）。
- **P6 实现（2026-09-19）**：5 个 mutation 工具从内存 `_WRITE_STORE` 替换为查询/写入模拟电商库（同 `kb_search`/`order_lookup` 数据源替换模式）。`idempotency` 表位于 `mock_ecommerce.db`，复合主键 `(tool_name, request_id)`，与业务写入同连接同事务；拒绝路径不消耗幂等键。只读工具仍使用 `mode=ro` 连接，mutation 工具使用读写连接。高风险 `refund_apply` 仍为 stub；`cancel_order` 的当前实现见 P7。
- **P7 实现（2026-09-20）**：`cancel_order` 从内存 stub 替换为模拟电商库事务写入，沿用 `_run_db_mutation` 模式。幂等语义是**同一业务取消请求只能成功执行一次**：同一业务取消请求由 requester `user_id`、`order_id` 和 `request_id` 共同匹配；成功提交后该 requester/order/request 组合返回 `replayed=true`。同一 `request_id` 但 `order_id` 不匹配时按数据边界拒绝（`data_boundary`），不暴露已存储结果；失败、拒绝、过期、越权和状态不允许都不写幂等表。订单取消成功后，更换新 `request_id` 的重复请求仍会被状态机拒绝为 `order_already_cancelled`。
- **P8 实现（2026-09-23 修正）**：`refund_apply` 从内存 stub 替换为模拟电商库事务写入，沿用 `_run_db_mutation` 模式。新增 `refunds` 表（`refund_id PK`，业务身份与幂等分离）。幂等语义同 `cancel_order`：requester `user_id` + `order_id` + `request_id` 共同匹配。预检状态机：`paid` 或签收 7 天窗口内的 `signed` -> `refunding`；`signed` 超窗、`completed`、`refunded`/`refunding` 不可再退。已占用退款额度 = `SUM(amount) FROM refunds WHERE order_id=? AND status IN ('refunding','refunded')`；`amount` 默认全额（`total_amount - occupied`），部分退款累计不可超额度。`refunding` 状态允许继续追加部分退款（累计额度内），`refunded` 阻止。预检层（safety-spec §1.2）校验状态机 + 金额超限；工具执行事务内二次校验。`find_own_refundable_orders` 为 select 层缺 order_id 澄清提供候选列表（同 cancel_order 模式）。
- **P5（B1）**：只读工具补 select 侧 schema（`kb_search`=`{text, request_id}`、
  `order_lookup`=`{order_id, request_id}`；`request_id` 由 select 注入，`user_id` 经 InjectedState 注入不入 schema），
  注册表元数据完备（预检用）。

- **工具集重构（2026-09-17）**：`account_update` 拆分为 5 个聚焦 mutation 工具（`update_address` / `update_phone` / `update_order_note` / `create_invoice` / `create_service_ticket`）；`order_cancel` 重命名为 `cancel_order` 并留在 high_risk 档。

- 转人工**不是工具**：由 `check_handoff` 确定性节点检测信号并经 `interrupt(handoff_request)` 抛出（见 `safety-spec.md` §2、`graph-spec.md` §6.5），不作为工具注册、不进入任何 agent 工具集；Handoff Record 创建与 `conversation_mode` 切换由外层（Conversation Runtime）完成，子图不直接调用任何 Handoff API。

## 5. 变更纪律

- 新增/修改工具：更新本文件 + `safety-spec.md`（若涉及预检规则）+ 工具对应测试。
