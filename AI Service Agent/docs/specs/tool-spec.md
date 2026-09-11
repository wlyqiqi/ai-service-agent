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

## 2. 工具实现约定

- 工具函数签名：参数带类型注解与描述；通过 `InjectedState` / `InjectedStore` / `ToolRuntime` 访问会话/记忆/运行时。
- 返回值必须是 JSON 可序列化结构；工具内部异常由 `handle_tool_errors` 归一化，不得裸抛到图外。
- 写工具必须支持幂等键（`idempotent=True` + 参数带 `request_id`），防重放。

## 3. 与安全预检的衔接

- 写/高险工具执行前必须经过 `safety-spec.md` 的预检节点（参数白名单、权限、业务规则、数据边界）。
- 工具自身不得绕过预检（预检是图内强制节点，非工具内自检）。

## 4. v1 工具清单

| 工具 | 类型 | min_risk_level | 说明 |
|---|---|---|---|
| `kb_search` | 知识库检索 | read | sqlite-vec / 关键词兜底 |
| `order_lookup` | 订单查询 | read | stub 客户端 |
| `account_update` | 账户变更 | mutation | 幂等 |
| `order_cancel` | 订单取消 | high_risk | 需人工审批 |
| `refund_apply` | 退款申请 | high_risk | 需人工审批 |

- 转人工**不是工具**：由 `check_handoff` 确定性节点直接执行（见 `safety-spec.md` §2），不作为工具注册、不进入任何 agent 工具集。

## 5. 变更纪律

- 新增/修改工具：更新本文件 + `safety-spec.md`（若涉及预检规则）+ 工具对应测试。
