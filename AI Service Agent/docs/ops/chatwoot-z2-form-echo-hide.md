# Chatwoot Z2：隐藏已提交 Form 的橙色回显

本文件是 **运维 runbook**，不是 Runtime 代码。目标：访客提交 Mutation Form 后，Widget 里
**0 条**橙色 `submitted_values` 回显气泡。

- 拍板：Scheme A（原生 `cards` 成功/失败卡）+ **Z2 CSS 零回显**；否决自定义成功 iframe。
- CSS 全文：`docs/ops/chatwoot/form-echo-hide.css`（与本文件同目录，逐字锁定）。
- **禁止**把这段 CSS 写进 `web/landing.html`：Widget 在跨域 iframe 内，外层样式穿不进去。

## 1. 根因（DOM 依据）

`app/javascript/widget/components/AgentMessage.vue`：

- 表单**未提交**：`v-if="!isASubmittedForm"` 为真 → 渲染 `.agent-message`（含 form 控件）。
- 表单**已提交**：`.agent-message` 不渲染，只剩 `.agent-message-wrap.has-response` 下的多个
  `UserMessage`（即橙色回显）。官方 API 没有关回显的开关。

所以选择器锁在「wrap 里有 response、但没有 agent-message」这一形态：

```css
.agent-message-wrap.has-response:not(:has(.agent-message)) {
  display: none !important;
}
```

## 2. 落地方式（按可行性自上而下选一种，PR/变更单里写明实际采用）

| 优先级 | 做法 | 操作 |
|---|---|---|
| **P1** | 进 Chatwoot 源码的 Widget 样式 | ① 新增 `app/javascript/widget/assets/scss/form-echo-hide.scss`，写入 §1 的 CSS 全文<br>② 在 Widget 样式 barrel（常见 `app/javascript/widget/assets/scss/woot.scss` 或当前版本等价 Vite entry）`@import` 该文件；**或**在 Widget layout（`app/views/widgets/show.html.erb` / 当前版本等价视图）加 `<link rel="stylesheet">`<br>③ 重建 widget 产物（`pnpm`/`vite build`）→ 重建或重启 Chatwoot 容器，确认 `/vite/assets`（旧版 `/packs`）里带上新 CSS |
| **P2** | 运维层挂载静态 CSS + layout 一行 link | ① 把本仓 `docs/ops/chatwoot/form-echo-hide.css` 挂进容器 `public/chengyuan/form-echo-hide.css`<br>② layout 引用 `/chengyuan/form-echo-hide.css`<br>③ **源文件必须持久化**（挂载或镜像内），不要只改带 hash 的 bundle |
| 禁止 | 只改 `ai-service-agent/web/landing.html` | 跨域，无效 |

> 本仓不含 Chatwoot 源码。若按 P1 做，改动落在 Chatwoot 仓/运维补丁里，与本仓 PR
> 互相引用；**同一验收切片两边都要绿**才可合并。

### 2.1 本环境实际落地记录（2026-10-27，P2 变体）

Chatwoot 为预构建镜像 `chatwoot/chatwoot:latest`（无源码），故走 **P2**，用 compose 只读挂载持久化：

| 项 | 值 |
|---|---|
| compose 目录 | `D:/AI_Project/AI客服Demo/harness/chatwoot`（备份：`docker-compose.yml.bak-z2`） |
| CSS 源文件 | 同目录 `z2/form-echo-hide.css` → 容器 `/app/public/chengyuan/form-echo-hide.css:ro` |
| layout 覆盖 | 同目录 `z2/widgets-show.html.erb`（原 layout 副本 + 一行 `<link>`）→ 容器 `/app/app/views/widgets/show.html.erb:ro` |
| 生效命令 | `docker compose -p chatwoot -f docker-compose.yml up -d chatwoot` |
| 回滚 | 恢复 `docker-compose.yml.bak-z2` 后 `up -d chatwoot`（两个挂载消失即回到原状） |

⚠️ layout 覆盖属于「拷贝原文件 + 加一行」，**Chatwoot 升级后该副本会落后于新版模板**。升级时必须重新导出容器内 `show.html.erb`、重新加 link、再覆盖（见 §4 第 3 条）。

验证命令与结果：

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:3000/chengyuan/form-echo-hide.css   # 200
curl -s "http://127.0.0.1:3000/widget?website_token=<token>" | grep chengyuan/form-echo-hide   # 命中
```

Widget 内实测（Playwright，注入两条合成节点验选择器）：

| 探针 | 期望 | 实测 |
|---|---|---|
| `.agent-message-wrap.has-response`（无 `.agent-message`） | 隐藏 | `display: none` ✅ |
| `.agent-message-wrap.has-response` + `.agent-message` | 可见 | `display: block` ✅ |
| `document.styleSheets` 是否含本 CSS | 是 | `http://localhost:3000/chengyuan/form-echo-hide.css` ✅ |

截图：`docs/ops/chatwoot/z2-verify.png`（左下探针：ECHO 不可见、FORM 可见）。

## 3. 验收（改完立刻验）

1. 访客 Widget：改地址 → 提交 → **0 条橙色气泡**，成功卡仍在。
   > 注意区分：**右对齐**的橙气泡是访客自己发的消息（正常）；Z2 要藏的是**左对齐、挂在 AI 消息下方**的 `submitted_values` 回显。
2. **未提交**的 form 仍可正常填写、可提交（不要连表单一起藏掉）。
3. `input_select` 等**仍渲染 `.agent-message`** 的消息不受影响。
4. 失败路径（`form_rejected`）重推的 form 可继续填写；失败卡可见。
5. 坐席 Dashboard 仍能看到 form 的 `submitted_values`（CSS 只作用于访客 Widget，这是预期）。

## 4. 升级 checklist（每次升级 / 重建 Chatwoot 镜像后逐条过）

1. [ ] 产物里仍有本 CSS：搜注释 `chengyuan: hide Chatwoot form submitted_values` 或选择器。
2. [ ] 真机：改地址提交 → 访客侧 **0** 橙气泡；成功卡仍在。
3. [ ] 若 Chatwoot 改了 `AgentMessage.vue` 的类名（没有 `.agent-message-wrap` / `.has-response` / `.agent-message`）
       → **阻塞升级**：改选 Z3（patch 模板）或更新选择器后再升。
4. [ ] **P2 落地必查**：`z2/widgets-show.html.erb` 是旧版 layout 的拷贝，升级后可能与新版模板不一致
       → 重新 `docker exec <容器> cat /app/app/views/widgets/show.html.erb` 导出、只加那一行 `<link>`、再覆盖。
5. [ ] Dashboard 坐席侧仍可见 form 提交值（预期行为，不要「修」）。
6. [ ] 记一条变更单：Chatwoot 版本号 + CSS 落地方式（P1/P2）+ 验证人。

## 5. 相关

| 文件 | 用途 |
|---|---|
| `docs/ops/chatwoot/form-echo-hide.css` | CSS 全文（唯一副本，勿另存变体） |
| `docs/ops/chatwoot/z2-verify.png` | 选择器实测截图（合成探针：ECHO 隐藏 / FORM 可见） |
| `src/ai_service_agent/harness/mutation_form_projection.py` | Form items 减噪（展示项移出 items） |
| `src/ai_service_agent/harness/mutation_projection.py` | 成功卡 / 失败卡投影 |
| `docs/ops/chatwoot-widget-agentbot-setup.md` | Widget × AgentBot 接线主 runbook |
