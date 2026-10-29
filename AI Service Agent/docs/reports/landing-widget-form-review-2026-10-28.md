# Landing Widget 表单与卡片评审（2026-10-28）

评审对象：`web/landing.html` 内嵌 Chatwoot Widget 里的三类界面——
① 业务表单（mutation form：改地址 / 开票 / 售后等）② 结果卡片（成功卡 / 失败卡）③ 访客首屏 pre-chat 表单。
标准：内容排版达到商业级；UI 与顶级商业公司（同品类：Apple / Dyson / 小米商城客服）同档。

**主色基调：暖橙（陶土橙 `#C0562B`）**，体现家居产品的温暖感。绿色只作落地页的辅助色，
不作为 widget 主色。

- 真实环境实测：Runtime `127.0.0.1:8766` + Chatwoot `localhost:3000`，Chromium 1440×900（DPR 2），
  Widget iframe 720×620，账号 `+65-89136375`（3 笔已支付订单）。
- 截图：`docs/ops/chatwoot/form-{before,after,error}.png`、`card-{before,after}.png`、`prechat-{before,after}.png`
- 样式实现：`docs/ops/chatwoot/widget-commercial.css`；落地步骤见 `docs/ops/chatwoot-widget-form.md`

## 1. 结论

| 维度 | 评审前 | 实施后 | 说明 |
|---|---|---|---|
| 表单 内容排版 | 4 / 10 | 8 / 10 | 标签/控件/按钮三级层级建立；表单标题缺失仍需改代码 |
| 表单 UI 精细度 | 4 / 10 | 8 / 10 | 边框、聚焦环、错误态补齐 |
| 卡片 内容排版 | 2 / 10 | 8 / 10 | **描述被折叠成一行**是主因，已用 `white-space: pre-line` 修好 |
| 卡片 UI | 3 / 10 | 8 / 10 | 卡片化（边框/圆角/留白/阴影）、动作按钮升级 |
| 品牌一致性 | 2 / 10 | 9 / 10 | 橙 `#E07E00` → 陶土橙 `#C0562B`；卡片/表单/气泡同一套 |
| 写操作防误 | 3 / 10 | 4 / 10 | 订单下拉仍无占位、首项隐式选中，**必须改代码** |

一句话：**样式层面已经到商业级；剩下的都是「信息完整性」和「防误选」，属于投影层（Python）与 Inbox 配置，CSS 无解。**

## 2. 主色：为什么是 `#C0562B` 而不是默认橙

| 方案 | 问题 |
|---|---|
| Chatwoot 默认 `#E07E00` | 偏黄、饱和度高，在白底/米白底上刺眼；按钮大面积实底时像"促销横幅" |
| 品牌绿 `#3F7359` | 与"家居温暖"的定位相反，冷、偏科技感；与米白纸感不搭 |
| **陶土橙 `#C0562B`** | 同色相深一档：暖、稳、有材质感；白字对比度 4.9:1（AA）；与 `#F9F4EE` 米白同属暖色系 |

配套 token（都在 CSS 顶部，一处可换）：

```
--cy-accent        #c0562b   主色（按钮、聚焦环、气泡、chevron）
--cy-accent-strong #a2461f   hover
--cy-tint          #fbf0e6   浅暖底（可用作强调区）
--cy-surface       #fffdf8   卡片/表单底
--cy-paper         #f9f4ee   输入框底、页面米白
--cy-line          #e6d8c6   边框
--cy-text          #2a211b   暖黑
--cy-muted         #6f6357   次要文字
```

## 3. 表单（mutation form）

### 现状实测（before）

| 元素 | 尺寸 | 关键样式 | 问题 |
|---|---|---|---|
| 卡片 | 535×226，padding 12/16 | `border:0`、`#fdfdfd`、`radius 4px 20px 20px 4px` | 借了聊天气泡尾巴的圆角，表单不是气泡 |
| 标签 | 14px/500 `#1c2024` | 与正文同号同色 | 无层级 |
| 下拉/输入 | 503×44 | `border:0`、`#fdfdfd`、`radius 8` | 浅底上不可辨，像只读文本 |
| 提交 | 503×40 | 14px/400、橙底、`radius 8` | 与落地页按钮语言不一致 |
| 错误 | — | `display:none`、文案「无效字段」 | 开发语言 |
| 标题 | — | **不渲染** | `titleRendered:false` |

### 已修（after）

| 项 | before | after |
|---|---|---|
| 卡片 | 535×226，`border:0`，`radius 4/20` | 535×266，`1px #e6d8c6`，`#fffdf8`，`radius 16`，轻阴影 |
| 标签 | 14px/500 `#1c2024` | 12px/600 `#6f6357` + 8px 间距 |
| 控件 | 44px，无边框 | 46px，`1px #e6d8c6`，`#f9f4ee` 底，`radius 10`，focus 3px 暖橙环 |
| 下拉 | 原生箭头 | 主色 chevron（内联 SVG），`padding-right:40px` |
| 提交 | 40px / 14/400 | 46px / 15/600 / `#c0562b`，hover `#a2461f` |
| 错误 | 「无效字段」14px | 「请检查这一项」12px + 字段边框与标签转红 |

## 4. 卡片（成功 / 失败卡）

### 现状实测（before）

```
div.card-message.chat-bubble           535×142  padding 12/16  radius 4px 20px 20px 4px  bg #fdfdfd  无边框
  > img[src=""]                        空 <img>，无 media_url 时仍渲染，占一条空白
  > div.card-body
      > h4                             16px/24 500  #1c2024  margin 4px 0
      > p                              14px/21 400  #60646c  white-space: normal
      > button.action-button           503×40  14px/14 500  橙字 + #fdfdfd 底 + 1px 橙边  radius 8
```

**核心问题（"内容挤在一起"的根因）**：
投影层 `format_success_card_description` 用 `"• 状态：已完成\n\n• 订单号：…"`（空行分隔，
为了兼容"吃掉 `\n` 的 widget"），但 Chatwoot 卡片正文是普通 `<p>`，`white-space: normal`
把 `\n\n` 全部折叠成空格——实测 `innerText` 为
`"• 状态：已完成 • 订单号：ORD-2026-10138 • 新地址：… • 账户地址：已同步更新"`，
四行信息挤成一段。

其余：卡片没有卡片感（透明底、无边框、无分层），标题/正文/按钮直接铺在气泡上；
动作按钮 40px 描边比正文还弱；卡片宽度被 `w-fit` 容器收缩到 370px，右侧留 165px 空白；
连续消息每条重复「澄元AI助手」。

### 已修（after）

| 项 | before | after |
|---|---|---|
| 卡片容器 | 535×142，无边框，`radius 4/20` | 535×210，`1px #e6d8c6`，`#fffdf8`，`radius 16`，轻阴影 |
| 宽度 | 370（w-fit 收缩） | 535（撑满会话列） |
| 空 img | 占位空白 | `display:none` |
| 标题 | 16px/24 500 | 15px/21.75 650，暖黑 `#2a211b`，下间距 10px |
| 描述 | 折叠成一行 | `white-space: pre-line` → 一条一行；13px/19.5 `#6f6357` |
| 按钮 | 40px 描边橙字 | 44px 实底 `#c0562b` 白字 600，`radius 10`，hover 加深 |
| 重复署名 | 每条都有 | 连续消息只留第一条 |
| 卡片间距 | 紧贴 | 8px |

关键 CSS（一行解决"挤在一起"）：

```css
div.card-message .card-body p { white-space: pre-line !important; }
```

## 5. Pre-chat 表单

| 项 | before | after |
|---|---|---|
| 按钮 | 橙 `#E07E00` 圆角 8 | `#c0562b` 46px `radius 10` |
| 输入框 | 无边框 | `1px #e6d8c6` + `#f9f4ee` 底 + 46px |
| 布局 | 顶对齐、全宽 500px，下方 200px 空白 | 居中、限宽 460px |
| placeholder | 裸字段名 `phoneNumber` | 隐藏（CSS 改不了文本，只能不显示） |

**仍是配置问题（CSS 改不了）**：引导语 `Share your queries or comments here.` 是英文、
标签 `Phone Number` 与 `消息` 混语言、默认区号 `+886 🇹🇼`（东南亚站点）、必填无标记。
见 runbook §5。

## 6. CSS 做不到的三件事（必须改代码/配置）

1. **表单标题不渲染**（P0）：Chatwoot 的 form 消息只渲染 `items`，`content` 被丢弃 →
   `mutation_form_title_from_form_data` 拼的副标题/表单级错误在会话视图 100% 不可见。
   修法：`mutation_form_projection.send_mutation_form` 在发 form 前把 title 另发一条 bot 消息。
2. **订单下拉无占位、首项隐式选中**（P0）：`order_picker_options` 只给 `label=value=订单号`，
   Chatwoot select 没有 placeholder 能力。改地址是写操作，不点下拉直接提交就会改错订单。
   修法：options 首位插 `{"label":"请选择订单","value":""}` + `form_gate` 拒绝空值。
3. **卡片描述的分组语义**（P1）：现在靠 `pre-line` 让 `\n\n` 生效，条目间是"空一行"。
   若想更紧凑，可把 `format_success_card_description` 的 `\n\n` 改成 `\n`（需同步测试断言）。

## 7. 验收

- 视觉：`card-after.png` / `form-after.png` 与 landing 同屏无违和（米白底、暖橙主色、46px 控件）。
- 交互：聚焦暖橙环；错误态中文文案 + 红框；按钮 hover/active 有反馈。
- 回归：Z2 零回显（`.agent-message-wrap.has-response:not(:has(.agent-message))`）与新样式无冲突
  ——作用域不重叠（Z2 在 message wrap，本文件在 `div.form` / `div.card-message` 内部）。
- 未覆盖：移动端 375px 未实测；字段数 >3 的表单需滚动。
- 遗留：`web/landing.html` 仍是绿主色，与 widget 暖橙并存属于跨面不一致——若要统一，
  改 `landing.html :root` 的 `--accent`（+ 4 处写死的 `rgba(63,115,89,…)`）即可，属另一片改动。
