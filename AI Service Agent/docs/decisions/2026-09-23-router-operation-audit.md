# Router Operation Audit（2026-09-23）

## 背景

Jev Router 端到端基准最初使用了 `tests/evals/control/routing.jsonl`。复核后发现该文件是 legacy 数据集，与权威 `master.jsonl` 存在历史漂移，例如 `CTRL-0076` 的旧 fallback 语义已与 P4 澄清裁定不一致。

因此，后续 Router operation 实验应以 `master.jsonl` 为标签权威来源，并在基准脚本中排除入口层 Handoff 与 Guard 拦截样本。

## 错误样本审计

| ID | 输入 | 当前标签 | 审计结论 | 处理 |
|---|---|---|---|---|
| CTRL-0022 | 取消我的订单 20001 | order/mutation | stale label；当前 `cancel_order` 工具为 high_risk | 更新为 order/high_risk/approval |
| CTRL-0023 | 我要申请退货，订单 30001 | order/mutation | 可映射为创建售后/退货工单，属 mutation | 保留 |
| CTRL-0035 | 把上门预约取消了 | order/mutation | 取消预约不是取消订单，属普通写操作 | 保留 |
| CTRL-0038 | 发票没收到，帮我重新发一下 | order/mutation | 重发/补开发票属 mutation | 保留 |
| CTRL-0040 | 我的退货申请被拒了，帮我重新提交 | order/mutation | 重新提交退货工单属 mutation；若后续明确映射 `refund_apply`，需单独重新审计 | 保留 |
| CTRL-0048 | 我账号被盗了，帮我强制重置 | account/high_risk | 安全敏感强制重置 | 保留 |
| CTRL-0049 | 有台设备不是我绑的，帮我强制解绑 | account/high_risk | 安全敏感强制解绑 | 保留 |
| CTRL-0054 | 帮我跨境补发到新加坡 | order/high_risk | 特殊履约/跨境补发 | 保留 |
| CTRL-0056 | 把我的订单历史全部清空 | account/high_risk | 不可逆数据删除 | 保留 |
| CTRL-0057 | 把这笔订单转到我朋友名下 | order/high_risk | 所有权转移 | 保留 |
| CTRL-0059 | 把我的维修记录删掉 | order/high_risk | 不可逆记录删除 | 保留 |
| CTRL-0060 | 把我的全部数据导出一份 | account/high_risk | 隐私数据导出 | 保留 |
| CTRL-0071 | 这个可以退吗？ | unknown/read/fallback | 缺指代对象，应澄清 | 保留 |
| CTRL-0072 | 多久能到？ | unknown/read/fallback | 缺订单/商品上下文 | 保留 |
| CTRL-0073 | 能换吗？ | unknown/read/fallback | 缺对象与原因 | 保留 |
| CTRL-0076 | 退了吧 | order/high_risk/clarify | master 已按 P4 澄清语义修正；legacy routing 仍为旧 fallback | 使用 master |
| CTRL-0077 | 有优惠吗？ | unknown/read/fallback | 缺产品/活动上下文 | 保留 |
| CTRL-0082 | 我的 S1 坏了，能退货吗？顺便帮我查下保修 | order/read | 均为咨询类读操作 | 保留 |
| CTRL-0084 | 我要改地址，还要取消另一个订单 | order/mutation | 多意图应取最高风险；取消订单为 high_risk | 更新为 order/high_risk/approval |

## 标签修正影响

- `CTRL-0022`：`mutation/confirm` -> `high_risk/approval`
- `CTRL-0084`：`mutation/confirm` -> `high_risk/approval`

修正依据：

1. `src/ai_service_agent/tools/__init__.py` 中 `cancel_order` 注册为 `high_risk`。
2. `docs/specs/tool-spec.md` 将 `cancel_order` 定义为需人工审批的高风险工具。
3. `src/ai_service_agent/prompts.py` 已规定多意图取最高风险。

## 下一步

1. 使用权威 `master.jsonl` 生成 Router Benchmark 子集，排除 `handoff` 与 `guard_reject`。
2. 构建 50 条左右高风险 micro benchmark。
3. 第一版只增加小规模 `operation` 分类：
   - `cancel_order`
   - `refund_apply`
   - `account_security_action`
   - `data_admin_action`
   - `order_transfer_or_reship`
   - `non_high_risk_action`
   - `unknown`
4. 对比 baseline 与 operation-augmented Router。
5. 确认收益后，再补全剩余 operation、增加 Noul 保险层，最后执行 threshold sweep。
