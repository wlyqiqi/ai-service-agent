# Chatwoot Widget 商业级视觉（主题 + 表单 + 卡片 + pre-chat）

本文件是 **运维 runbook**。目标：Widget 内的表单、结果卡片、pre-chat 表单，
视觉与 `web/landing.html` 同一套语言——**暖橙（陶土橙 `#C0562B`）+ 米白纸感 + 46px 控件**。

- CSS 全文：`docs/ops/chatwoot/widget-commercial.css`（逐字锁定，改样式只改这份）
- 评审结论与实测数据：`docs/reports/landing-widget-form-review-2026-10-28.md`
- 与 `chatwoot-z2-form-echo-hide.md` 同机制、互不冲突（作用域不重叠）

## 1. 为什么必须落在 Chatwoot 侧

Widget 在**跨域 iframe** 内（`localhost:3000/widget`），`web/landing.html` 的样式穿不进去。
`landing.html` 里现有的 `.woot-widget-holder` 规则只能改**外层容器尺寸**，改不到 iframe 内部。

## 2. 落地方式（与 Z2 相同的 P2 变体）

| 项 | 值 |
|---|---|
| CSS 源文件 | `docs/ops/chatwoot/widget-commercial.css` |
| 容器内路径 | `/app/public/chengyuan/widget-commercial.css`（只读挂载） |
| layout 引用 | `/app/app/views/widgets/show.html.erb` 内加一行 `<link rel="stylesheet" href="/chengyuan/widget-commercial.css">` |
| compose 目录 | `D:/AI_Project/AI客服Demo/harness/chatwoot`（与 Z2 同一份 `docker-compose.yml`） |
| 生效 | `docker compose -p chatwoot -f docker-compose.yml up -d chatwoot` |
| 回滚 | 去掉挂载与 `<link>` 后 `up -d chatwoot` |

> layout 已是 Z2 的覆盖副本（`z2/widgets-show.html.erb`）。**追加 link 时改这份副本，不要从容器重新导出**
> ——否则会丢掉 Z2 那行。Chatwoot 升级后两份 link 都要重新补（见 Z2 runbook §4）。

### 2.1 本环境实际落地记录（2026-10-28）

| 项 | 值 |
|---|---|
| compose 备份 | `docker-compose.yml.bak-form` |
| CSS 源文件 | 同目录 `widget-commercial.css`（从 `docs/ops/chatwoot/widget-commercial.css` 复制） |
| 容器内路径 | `/app/public/chengyuan/widget-commercial.css:ro` |
| layout | `z2/widgets-show.html.erb` 追加 `<link>`（与 Z2 那行并列） |
| 生效命令 | `docker compose -p chatwoot -f docker-compose.yml up -d chatwoot`（Recreate，约 45s 就绪） |
| 回滚 | 恢复 `docker-compose.yml.bak-form`、删掉 layout 新增的 `<link>`，再 `up -d chatwoot` |

⚠️ **两份 CSS 要同步**：仓库里那份是源，`D:/AI_Project/AI客服Demo/harness/chatwoot/widget-commercial.css`
是挂载副本。改样式后必须 `cp` 过去，否则线上不生效。

> 历史：2026-10-28 上午第一版文件名是 `form-commercial.css`（绿色主色），同日改为
> `widget-commercial.css`（暖橙 + 卡片）。旧文件已删，`docker-compose.yml.bak-form` 是改名前的备份。

## 3. 验证

```bash
# 1) 静态文件可访问
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:3000/chengyuan/widget-commercial.css   # 200

# 2) widget HTML 带上了 link
curl -s "http://localhost:3000/widget?website_token=<token>" | grep widget-commercial              # 命中

# 3) iframe 内实测（Playwright，跨域 frame 直接 evaluate）
```

2026-10-28 实测（线上 link 生效后的计算值）：

```
form    : 535×266  radius 16px  border 1px #e6d8c6  bg #fffdf8  padding 20/20/18
label   : 12px/16px 600  #6f6357
select  : 46px  radius 10  border 1px  bg #f9f4ee  chevron 内联 SVG
button  : 46px  bg #c0562b  15px 600  radius 10
card    : 535×210  radius 16px  border 1px #e6d8c6
card p  : white-space pre-line  13px/19.5  #6f6357     ← 让 "\n\n" 真正换行
card btn: 44px  bg #c0562b  14px 600
userBubble: #c0562b
```

## 4. 这份 CSS 改了什么、没改什么

改：主题 token（暖橙）、卡片化（表单卡 + 结果卡）、标签层级、控件尺寸与边框、focus 环、
下拉 chevron、按钮规格、访客气泡、错误文案与边框、卡片描述换行、卡片宽度撑满、
重复署名去重、pre-chat 居中限宽。

**没改（做不到）**：

| 现象 | 原因 | 该改哪里 |
|---|---|---|
| 表单标题/副标题不显示 | Chatwoot form 消息只渲染 `items`，`content` 被丢弃 | `mutation_form_projection.send_mutation_form`：标题另发一条 bot 消息 |
| 订单下拉没有「请选择」 | Chatwoot select 无 placeholder；首项隐式选中 | `order_picker_options` 加 `value=""` 占位项 + `form_gate` 拒绝空值 |
| 错误文案无法逐字段差异化 | 组件只有一个 `span.error-message` | 字段差异继续走 label/placeholder（`_apply_errors_to_mutation_form_items`） |
| 卡片描述条目间距 | 投影用 `\n\n`，CSS 只能靠 `pre-line` 原样呈现 | 想更紧凑就改 `format_success_card_description` 用 `\n`（要同步测试断言） |
| 表单无「取消」按钮 | items 由投影决定 | 需要时在投影加 action select（改地址表单刻意不加，见 #71） |

## 5. Chatwoot 侧配置（CSS 之外的必做项）

CSS 只覆盖 iframe 内样式；下列属于 Inbox 设置，不做就仍有割裂：

1. **Widget color 改成 `#C0562B`**（Settings → Inboxes → 当前网站 Inbox → Configuration）。
   CSS 里 `--widget-color` 只是兜底，邮件/后台等非 CSS 面仍取设置值。
2. **Pre-chat 文案**：`Share your queries or comments here.` → 中文（界面 `zh_CN`，这句是英文）。
3. **Pre-chat 默认国家**：当前 `+886`（🇹🇼），东南亚站点应为 SG/MY/TH/ID/VN 或留空由访客选。
4. **Pre-chat 字段**：`Phone Number` 与 `消息` 混语言；必填项无标记。
5. **`由 Chatwoot 支持` 品牌位**：社区版要求保留。商业级白标需付费版或自建构建，不要用 CSS 隐藏。

## 6. 已知未覆盖

- 移动端 375px（Widget 满宽 `100vw`）未实测，`@media` 分支未加。
- 字段数 > 3 的表单（如开票：抬头/税号/邮箱）在 620px 高度内需滚动，未做密度适配。
- 深色模式：Widget 默认跟随系统，本文件只锁定浅色暖橙值，未做 `prefers-color-scheme` 分支。
- `web/landing.html` 仍是绿主色，与 widget 暖橙并存属跨面不一致；统一需改落地页 `--accent`，另一片改动。

---

## 7. 坐席端审批卡（`seat-approval.css` + `seat-approval.js`，2026-10-28）

客户 Widget 的样式管不到**坐席端 Dashboard**（`/app/accounts/3/...`，另一套 bundle、另一个 layout）。
审批卡是 Runtime 生成的 HTML（`harness/approval_card.py`，全部内联样式），**不依赖这两个文件也能看**；
它们只补 HTML 进不去的三件事：藏掉重复消息、藏掉「无响应」、让按钮在会话里直接完成决策。

| 项 | 值 |
|---|---|
| 文件 | `docs/ops/chatwoot/seat-approval.css` + `seat-approval.js` |
| 容器内路径 | `/app/public/chengyuan/seat-approval.{css,js}`（只读挂载） |
| layout 引用 | `/app/app/views/layouts/vueapp.html.erb`（覆盖副本 `z2/vueapp.html.erb`） |
| compose 目录 | `D:/AI_Project/AI客服Demo/harness/chatwoot` |
| 生效 | `docker compose -p chatwoot -f docker-compose.yml up -d chatwoot` |
| 回滚 | 去掉两行挂载 + 删 `<link>`/`<script>` 后 `up -d chatwoot`（卡片仍可读，按钮退回确认页） |

### 7.1 它解决什么

1. **「无响应」占位**：`components-next/message/bubbles/Form.vue` 在
   `isAWebWidgetInbox && !submittedValues.length` 时渲染 `CONVERSATION.NO_RESPONSE`。
   审批卡永远没有 `submittedValues`（决策走签名链接），这句话会被坐席误读成「客户没回复」。
   选择器 `div:has(> span > div[style*="width:424px"]) > div.my-2.font-medium` 只命中含卡片的气泡。
2. **私备注黄底**（`#FFE4B5`）与暖橙卡片撞色，同气泡内换成 `#fdfaf5`。
3. **重复的客户侧表单消息**（`seat-approval.js`）：客户在 Widget 里填表时，Chatwoot 会把同一条
   form 消息同步到坐席视图 ——「您即将进行「订单取消」操作，参数：{'order_id': ..., 'request_id': ...}，
   请确认是否继续。」审批卡已经把这笔单讲清楚，这条只剩噪音（还带内部 `request_id`）。
   JS 按 `.message-bubble-container` + 文本标记（「您即将进行」+「请确认是否继续」）整行隐藏。
4. **按钮就地决策**（`seat-approval.js`）：把卡片里的 `a[data-approval-action]` 接管成按钮 ——
   批准直接 `fetch POST /approvals/{id}/decide`，成功后就地把卡片换成结果态；拒绝就地展开理由输入。
   不跳页面、不刷会话。结果记在 `localStorage["cy-approval-state"]`，Vue 重渲染 / 刷新后自动重放。

### 7.2 为什么卡片用内联样式

坐席端 `FormBubble` 只渲染 `content`，且过 `v-dompurify-html`（默认白名单，`style` 属性保留、
`<style>` 与 `on*` 被剥）。实测 2026-10-28：卡片 424px 宽、`#FFFDF8` 底、12px 圆角、
1px `#E6D8C6` 边框在坐席端**全部生效**——所以样式必须内联，不要试图改成 class + 外链 CSS。

### 7.3 决策链接的运行时配置

| env | 作用 | 缺省 |
|---|---|---|
| `SEAT_APPROVAL_SECRET` | 座位口令；决策链接密钥由它派生 | 空 → 链接一律判无效（fail closed，卡片仍可读） |
| `APPROVAL_LINK_SECRET` | 显式覆盖派生密钥；换它即让所有旧链接失效 | 空 |
| `APPROVAL_LINK_BASE_URL` | 卡片按钮指向的 Runtime 对外地址 | 空 → 按 `AI_SERVICE_PORT` 推 `http://127.0.0.1:<port>` |
| `APPROVAL_LINK_CORS_ORIGINS` | 坐席端脚本跨源 fetch 的白名单（逗号分隔） | 空 → 用 `CHATWOOT_BASE_URL` + 回环变体 |

> 生产必须配 `APPROVAL_LINK_BASE_URL` 成坐席浏览器可达的地址，否则按钮指向 `127.0.0.1` 点不开。

### 7.4 注入脚本的行为边界

| 场景 | 行为 |
|---|---|
| 点「批准」 | 按钮置灰 → `POST /approvals/{id}/decide` → 卡片就地换成「已批准 · 时间」 |
| 点「拒绝」 | 就地展开理由输入（必填）→ 提交 → 卡片换成「已拒绝」 |
| 已被别人处理（409） | 显示「已处理」，不假装成功 |
| 网络 / 服务异常 | 卡片内红条提示，按钮恢复可点；链接仍指向确认页可兜底 |
| JS 没加载 | 卡片保持原样，按钮退回「打开确认页」（降级路径，功能不丢） |
| 页面刷新 / Vue 重渲染 | 从 `localStorage["cy-approval-state"]` 重放结果态 |

依赖 **Runtime 的 CORS**：脚本从 `:3000` fetch `:8766`。白名单由 `APPROVAL_LINK_CORS_ORIGINS`
控制（缺省用 `CHATWOOT_BASE_URL`，自动补 `localhost`/`127.0.0.1` 变体）；只放行 `POST`/`OPTIONS`。
没配白名单时脚本会拿到 CORS 错误，卡片停在原状（可点链接兜底）。

### 7.5 验证

```bash
# 卡片里的按钮能落到确认页（token 15 分钟有效、一次性）
curl -s -o /dev/null -w "%{http_code}\n" "http://127.0.0.1:8766/approvals/<id>/decide?t=<token>"

# 坐席端 layout 带上了 css + js
curl -s http://127.0.0.1:3000/app/accounts/3/inbox-view/conversation/861 | grep -o "seat-approval\.[a-z]*" | sort -u

# CORS preflight（坐席端 fetch 的前提）
curl -s -o /dev/null -w "%{http_code}\n" -X OPTIONS "http://127.0.0.1:8766/approvals/<id>/decide" \
  -H "Origin: http://127.0.0.1:3000" -H "Access-Control-Request-Method: POST"
```

坐席端实测（Playwright，会话 862）：点「批准并退款」→ `POST /decide 200` → 卡片就地变成
「已批准 / 今天 08:11 / 已执行，客户会立即收到结果通知。」，URL 未跳转。

截图：`docs/ops/chatwoot/approval-card-live.png`（坐席端卡片）、
`approval-decide-page.png` / `approval-decide-reject.png` / `approval-decide-expired.png`。
