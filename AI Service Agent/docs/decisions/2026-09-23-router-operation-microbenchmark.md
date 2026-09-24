# Router Operation Microbenchmark（2026-09-23）

## 目的

验证在 Jev Router 中加入小规模 `operation` 分类是否能提升高风险路由能力。本实验只做 Router 分类，不接入业务 `router.py`，不修改 Policy Gate。

## 数据集

- 文件：`tests/evals/router_benchmark/high_risk_micro.jsonl`
- 数量：50
- 分布：
  - `high_risk`：40
  - `mutation`：6
  - `read`：4
- operation 分布：
  - `cancel_order`：10
  - `refund_apply`：10
  - `account_security_action`：8
  - `data_admin_action`：6
  - `order_transfer_or_reship`：6
  - `ordinary_action`：10

## 实验设计

同一批样本分别测试两个 Jev 请求：

1. baseline：`Choice(capability)` + `Choice(risk_level)`
2. operation：`Choice(capability)` + `Choice(operation)` + `Choice(risk_level)`

operation 方案的最终风险由代码合并：

```text
cancel_order / refund_apply / account_security_action /
data_admin_action / order_transfer_or_reship
    -> high_risk

ordinary_action
    -> 使用 risk_level 结果
```

## 第一次运行：2.5s timeout

第一次使用当前默认 `JEV_TIMEOUT_SECONDS=2.5`。

结果不可靠：

```text
baseline  有效响应 35，超时/错误 15
operation 有效响应 28，超时/错误 22
```

其中 `account_security_action` 的 8 条 operation 请求全部没有有效响应。

结论：2.5 秒阈值会大量截断 operation 请求，无法评估 operation 拆分本身的效果。因此使用 5 秒 timeout 重跑。

## 第二次运行：5s timeout

| 指标 | baseline | operation |
|---|---:|---:|
| accepted rate | 66% | **78%** |
| joint accuracy | 54% | **72%** |
| policy target accuracy | 54% | **74%** |
| high_risk recall | 50% | **75%** |
| mutation recall | 50% | 50% |
| read recall | 100% | 100% |
| accepted joint accuracy | 81.82% | **92.31%** |
| accepted high_risk recall | 83.33% | **100%** |
| P50 | 1573 ms | 1632 ms |
| P95 | 6510 ms | 6643 ms |

在 44 条有效 operation 响应中：

```text
operation raw accuracy = 97.73%
```

唯一一次 operation 错误是：

```text
ordinary_action -> refund_apply
```

## 结论

1. `operation` 拆分有效。
2. 在被接受的请求上，operation 方案的 joint accuracy 从 `81.82%` 提升到 `92.31%`。
3. 高风险召回显著提升：`50% -> 75%`；被接受请求中的高风险召回为 `100%`。
4. P50 延迟基本不变，P95 有轻微波动。
5. 主要收益来自 operation 到 high_risk 的确定性合并，而不是 `risk_level` 本身变准。
6. `account_security_action` 仍偏弱：2.5s 下全部超时；5s 下也有较多被 Jev 判为 `unknown`。

## 下一步

1. 将小规模 operation 集成到业务 Router，但保留 feature flag。
2. 为 operation 路径单独设置超时，建议默认 `5s`，不要直接复用当前 `2.5s`。
3. 继续优化 `account_security_action` 的 criteria 和负例。
4. 在 master Router 子集上重跑 baseline vs operation，确认结果不是 micro benchmark 过拟合。
5. 上述结果稳定后，再进入 threshold sweep 和 Noul 保险层。
