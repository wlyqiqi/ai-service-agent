# 模拟电商库数据字典（mock_ecommerce.db）

- 生成：`uv run python scripts/seed_mock_db.py`（默认输出 `data/mock_ecommerce.db`，
  可用 `--out` 指定路径；运行时路径配置 `Settings.mock_ecommerce_db_path`）
- 确定性：固定种子 `20260915` + 固定时间锚点 `2026-09-15 10:00 (+08:00)`，
  同一命令产物逐行一致（Eval 规则：离线、可重复）
- 定位：`order_lookup`（及未来保修查询等 read 工具）的真实数据源替代品，
  替换工具内部 stub 实现，`ToolMeta.schema` 与对外接口不变（见 `tool-spec.md` §4）
- 品牌：澄元智能家电（Chengyuan Smart Home）；姓名为程序生成的中文虚构姓名，
  邮箱/电话/单号均为 `example.com` 域或明显假号段，不含真实个人信息

## 表结构

| 表 | 行数 | 说明 |
|---|---|---|
| `customers` | 122 | u1/u2 为规范化夹具属主（张伟/李娜，SG/MY），其余 `u0001..u0120`；市场 MY/SG/TH/ID/VN，语言偏好、会员等级（普通/高级/VIP） |
| `products` | 40 | 两大品类（厨房电器/清洁电器）6 系列：CY-AF 空气炸锅 / CY-RC 电饭煲 / CY-BL 破壁机 / CY-CM 咖啡机 / CY-VS 吸尘器 / CY-RV 扫地机器人；含 `latest_firmware` 与保修期（12/24 月） |
| `orders` | 400 | 状态分布贴近真实：completed 55% / delivered 15% / shipped 12% / paid 8% / refunding 4% / refunded 3% / cancelled 3%；渠道 官网/App/Shopee/Lazada/TikTok Shop；币种按市场（MYR/SGD/THB/IDR/VND） |
| `order_items` | ~630 | 每单 1–3 件；单价 = CNY 基准价 × 市场汇率 |
| `shipments` | ~390 | SEA 真实物流商：J&T Express / Ninja Van / Flash Express / JNE / GHN / GHTK；paid 单为 pending，cancelled 单无运单 |
| `devices` | 150 | Chengyuan Home App 配对设备；序列号 `CYSN*`；约 1/4 留旧固件（固件更新场景） |
| `service_tickets` | 30 | 维修申请/退换货/配件补发/退款咨询；状态 open/processing/resolved/closed |
| `conversations` | 40 | 会话 id `conv-*`；约 1/4 关联工单（handoff 演示） |

## 规范化夹具订单（同时存在于 ORDER_STORE 与 DB）

运行时解析顺序 = `ORDER_STORE` → 模拟电商库 → `not_found`（夹具永远优先，
因其 `signed_at` 相对 now 生成，保证离线 eval 窗口语义恒成立）：

| 订单号 | 属主 | 状态 | 评测用途 |
|---|---|---|---|
| `ORD-1` | u1 | delivered | 本人订单正常查询 |
| `ORD-2` | u2 | delivered | 跨用户边界（D4 data_boundary） |
| `ORD-10086` | u1 | delivered | 签收超 7 天窗口（CTRL-0086 退款时限） |
| `ORD-CANCEL-1` | u1 | cancelled | 已取消订单（CTRL-0090 取消状态机） |

## 预设客服测试场景

- **配送中**：`orders.status='shipped'` + `shipments.status='in_transit'`
- **退款窗口边界**：`delivered` 且 `signed_at` 距锚点 1–6 天（窗口内）/ 7 天以上（超窗）
- **固件更新**：`devices.firmware` < `products.latest_firmware`（约 1/4 设备）
- **保修边界**：`devices.warranty_until` 早于/晚于锚点（过期/有效）
- **人工会话**：`conversations.mode='human'` + 关联工单
