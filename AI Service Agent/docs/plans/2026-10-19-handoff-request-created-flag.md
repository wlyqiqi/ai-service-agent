# Handoff Request 幂等来源收敛（修复 A）

- 日期：2026-10-19
- 状态：**已实现**（分支 `fix/handoff-request-created-flag`）
- 相关：`docs/specs/handoff-spec.md` §2 / §2.1、ADR-0003、架构评审 c2
- 范围：**只修行为漂移**，不抽 `decide_entry`（那是 B），不动 Takeover/Resolve 闭包（那是 C，等 c4）

---

## 1. 问题（已核实，非推测）

同一状态机（`转人工 → Resolve/Cancel → 再转人工`），两个 Transport 给出不同结果：

```
[AgentBot] 2nd tip: 已收到您的转人工申请，请稍候…   seat note calls: 0   awaiting: True
[/chat]    2nd tip: 好的，已收到您的转人工申请…     seat note calls: 1   awaiting: True
```

AgentBot 侧**不发 private note、不打 `handoff-request` label**——坐席在 Chatwoot 收件箱里看不到这次申请。
而 ADR-0001 下生产主入口是 Widget→AgentBot，**出错的正是线上主路径**。

### 1.1 根因

`/chat` 用**状态**判重复（`get_awaiting() or get_active()`），AgentBot 用**日志**判重复（扫 `messages_store` 里有没有历史 tip 文案）。
Resolve / Cancel 之后 `awaiting` 已清空（应视为**新申请**），但历史 tip 仍在 store 里 → AgentBot 永远判成「重复」。

违反 `handoff-spec.md §2`：「挂起期间**重复**转人工措辞：幂等，仅短 tip」——只在挂起期间幂等，不是「这条会话曾经申请过就永远幂等」。
违反 `§2.1`：申请必须 private note + label `handoff-request`。

### 1.2 现状：tip 决策有 5 处，判重来源有 2 套

| # | 位置 | 判重来源 | 现状 |
|---|---|---|---|
| 1 | `server.py:151-177` `_agent_bot_callbacks_factory.on_handoff_requested` | 扫 store | **错**（本次要修的主因） |
| 2 | `server.py:735-751` `/chat` 入口 | `awaiting/active` | 对 |
| 3 | `server.py:817-831` `/chat` 图内事件 | 扫 store | 侥幸对（与 #2 同源，但判据不同） |
| 4 | `server.py:991-1001` `_approval_resume` 图内事件 | 扫 store | **错**（同一 bug，经审批恢复路径） |
| 5 | `agent_bot.py:615-636` 入口 fallback（无 `callbacks_factory` 时） | 恒强确认 | 对但不可配置 |

5 处各自拼文案、各自决定发不发 private note → 判据一分叉，行为就分叉。

---

## 2. 目标 / 非目标

### 目标
- **唯一幂等来源**：`HandoffService.request_handoff` 自己回答「这次是新建还是复用」。
- **唯一 tip 策略**：`created` 一个布尔决定「强确认 + private note + label」还是「幂等短 tip」。
- 删掉 **4 处** store 扫描（#1、#3、#4 的历史扫描 + #2 的 `already` 计算）。
- 不改公开事件契约（`handoff_requested` 的 SSE 形状保持 `{type, data}`）。

### 非目标
- 不抽 `decide_entry(mode, awaiting, active, text)`，不动 `/chat` 与 AgentBot 的入口副本（B）。
- 不动 `_seat_takeover_for_conversation` / `_complete_handoff_resolve` / `_cancel_pending_form_interrupt`（C，等 c4 的 checkpoint bind）。
- 不修 `request_handoff` 的并发竞态（两线程同见 None 会双建记录）——pre-existing，本改动不加剧，单独立项。
- 不改任何图节点 / 边 / checkpointer。

---

## 3. 设计

### 3.1 接口变更

`harness/handoff.py`：

```python
def request_handoff(self, conversation_id, reason, summary) -> dict:
    """返回 {"type": "handoff_requested", "data": rec, "created": bool}。
    created=True  → 本次新建了挂起记录（应发强确认 + 坐席 private note）
    created=False → 复用已有 awaiting/active（应只发幂等短 tip）
    """
```

新增同文件的唯一策略函数：

```python
def handoff_request_tip(*, created: bool) -> str:
    return HANDOFF_REQUEST_CONFIRM if created else handoff_request_idempotent_tip_text()
```

### 3.2 `created` 为什么挂在事件顶层，而不是 `data` 里

- `make_interrupt_callback` 的返回值会被 `event_stream` 直接当作事件 yield（`streaming.py:118-121`），图内中断路径**只能拿到事件本身**，拿不到第二个返回值 → 标志必须随事件走。
- 放 `data` 会改 `event-spec.md §2` 定义的 `handoff_requested.data = {handoff_id?, reason?, summary?}`。
- 放顶层 + 在 SSE 边界剥掉：公开契约零变化，控制字段有明确归属。

**边界规则**：`created` 是 Runtime 内部控制字段，**不得**出现在任何对外 SSE 事件里。剥除点唯一：`_emit_handoff_requested`。

### 3.3 数据流

```
Transport 入口 ──request_handoff()──> {type, data, created}
                                          │
                    ┌─────────────────────┴─────────────────────┐
                    │ created=True                created=False │
                    ▼                                           ▼
       强确认 + private note + label                  幂等短 tip，无坐席提示
                    │                                           │
                    └────────► messages_store + Chatwoot ◄──────┘
                                          │
                            SSE: 剥掉 created 后 yield
```

---

## 4. 改动清单

### 4.1 `src/ai_service_agent/harness/handoff.py`

| 位置 | 改动 |
|---|---|
| `:199-209` `request_handoff` | 三条返回路径各带 `"created"`：复用 awaiting → `False`；已 active → `False`；新建 → `True` |
| `:11-15` 常量区 | 新增 `handoff_request_tip(*, created)`；保留 `HANDOFF_REQUEST_CONFIRM`（测试与兼容引用） |
| docstring | 「幂等复用 awaiting」补一句：`created` 是 tip 策略的唯一判据 |

### 4.2 `src/ai_service_agent/harness/server.py`

| 位置 | 改动 |
|---|---|
| `:367-383` `_emit_handoff_requested` | 去掉 `first_request` 参数；`first = bool(ev.get("created", True))`；tip 用 `handoff_request_tip(created=first)`；yield 前构造 `{"type": ev["type"], "data": ev["data"]}`（剥 `created`） |
| `:151-177` `on_handoff_requested` | 删掉 `rows` / `already_tipped` 扫描（152-163），改用 `event.get("created", True)` |
| `:735-751` `/chat` 入口 | 删 `already = ...`（738）与三元 tip（740-744）；`tip = handoff_request_tip(created=ev["created"])`；`_emit_handoff_requested(conv_id, ev)` |
| `:817-831` `/chat` 图内事件 | 删 `rows` / `already_tipped` 扫描；`_emit_handoff_requested(conv_id, ev)` |
| `:991-1001` `_approval_resume` | 同上，删扫描 |
| imports | `handoff_request_idempotent_tip_text` 若不再直用则移除；`HANDOFF_REQUEST_CONFIRM` 视 `/chat` 入口改造后是否仍需要 |

### 4.3 `src/ai_service_agent/harness/agent_bot.py`

| 位置 | 改动 |
|---|---|
| `:615-636` 入口 intent 分支 | fallback 分支（无 `on_handoff_requested`）的 tip 由恒强确认改为 `handoff_request_tip(created=ev["created"])`；`response` 同步 |
| `:602` 注释 | 「Seat-gated entry detectors」补一句判重来源：`request_handoff.created` |

### 4.4 不改

`graph/workflows/copy.py`（文案不变）、`form_gate.py`、`streaming.py`、所有图代码、DB schema。

---

## 5. 删除清单（本次的核心收益）

| 删除项 | 行数 |
|---|---|
| `server.py:152-163` store 扫描 | ~12 |
| `server.py:818-827` store 扫描 | ~10 |
| `server.py:992-998` store 扫描 | ~7 |
| `server.py:738` + `740-744` `already` 与三元 tip | ~6 |
| `_emit_handoff_requested` 的 `first_request` 形参与 4 处传参 | ~5 |
| **合计** | **≈40 行** |

删除后全仓「判断这次申请是不是重复」只剩一个表达式：`created`。

---

## 6. 测试计划

### 6.1 新增（4 条）

| 文件 | 用例 | 断言 |
|---|---|---|
| `tests/test_handoff.py` | `test_request_handoff_created_flag_transitions` | 首申 `created=True` → 挂起期重复 `False` → `resolve` 后再申 `True`；`cancel` 后再申 `True` |
| `tests/test_seat_gated_handoff.py` | `test_agent_bot_resolve_then_request_prompts_seat_again` | AgentBot（走 `_agent_bot_callbacks_factory` 生产接线）：resolve 后再申请 → store 末条 = 强确认；`prompt_handoff_request` 再 await 1 次 |
| 同上 | `test_agent_bot_cancel_then_request_prompts_seat_again` | 取消后再申请 → 同样强确认 + private note（当前最常见的真实路径） |
| 同上 | `test_handoff_requested_sse_has_no_control_fields` | `/chat` 的 `handoff_requested` 事件键集 == `{"type", "data"}`，`data` 键集 ⊆ `{handoff_id, reason, summary, status, conversation_id}` |

### 6.2 保留（必须继续绿）

- `test_re_request_while_awaiting_idempotent`（`test_seat_gated_handoff.py:145`）：挂起期间仍只发短 tip、只 1 条 awaiting、`prompt_handoff_request` 只 1 次。
- `test_handoff_entrance.py:95`：单次申请 store 为 `["我要转人工", HANDOFF_REQUEST_CONFIRM]`。
- `test_server.py:1124`、`test_e2e_human_lane.py:263`：单次申请文案不变。

### 6.3 断言口径

新增用例必须**同时**断言「文案」与「坐席信号」——本次 bug 的实质是后者缺失，只断言文案会漏掉。

---

## 7. 回归与验收

```bash
# 定向
.venv/Scripts/python.exe -m pytest tests/test_handoff.py tests/test_seat_gated_handoff.py \
  tests/test_handoff_entrance.py tests/test_server.py tests/test_agent_bot.py \
  tests/test_e2e_human_lane.py tests/test_handoffs_query.py -q

# 全量（evals 外）
.venv/Scripts/python.exe -m pytest tests --ignore=tests/evals -q
```

**DoD**
1. 全量 0 failed（基线 773 passed / 2 skipped）。
2. 探针脚本复跑：AgentBot 在「resolve→再申请」「cancel→再申请」两种场景下 `seat note calls == 1`、tip == 强确认。
3. `grep -rn "already_tipped" src/` 无结果。
4. `handoff_requested` 的 SSE 事件不含 `created`。

---

## 8. 风险与回滚

| 风险 | 评估 | 处置 |
|---|---|---|
| 图内中断路径 `created` 缺失 | 只有手工构造事件的老调用方会缺 | 默认 `True`（fail-open 到「发强提示」），偏坐席可见性 |
| private note 发送失败但 store 已记 tip | 今天同样存在（append 在 try 之前） | 本改动不改变该失败模式，不在范围内 |
| 并发双申请双建记录 | pre-existing 竞态 | 不在范围；`created` 如实反映各自结果 |
| 事件多出 `created` 泄漏到外部 | 已在 SSE 边界剥除，并用用例锁死 | 若漏，回滚单 commit 即可 |

回滚：单 commit revert，无 schema / 配置 / 数据迁移。

**改动规模预估**：4 文件，约 +60 / −45。

---

## 9. 与后续项的关系

- **B（`decide_entry` 纯函数）** 在本项之后做：A 只把「重复判定」收敛成一个标志，B 再把「mode → cancel → request」的顺序收敛成一个函数。顺序反了会先抽函数、再改判据，两次碰同一批行。
- **C（Takeover/Resolve 模块化）** 等 c4 的 checkpoint bind 落地，否则新模块会重新打开 `AsyncSqliteSaver`，只是把重复搬家。
- 本项完成后，c2 评审里「两 Transport 行为漂移」这一条可关闭；「入口副本」仍在，留给 B。

---

## 10. 实现记录

| 项 | 结果 |
|---|---|
| 改动文件 | `handoff.py` / `server.py` / `agent_bot.py` + 2 个测试文件（+206 / −65） |
| `created` 落地 | `request_handoff` 三条返回路径；默认值 `True`（fail-open 到「发强提示」） |
| 唯一 tip 策略 | `handoff.handoff_request_tip(*, created)`，4 个调用点共用 |
| 删除 | `already_tipped` 扫描 4 处 + `/chat` 入口 `already` 计算 + `first_request` 形参 |
| SSE 边界 | `_emit_handoff_requested` 只 yield `{type, data}` |
| 测试 | 新增 6 条（2 单元 + 4 集成，比计划多 2 条：`/chat` 同场景守卫、cancel 路径） |
| 全量 | 779 collected / **777 passed / 2 skipped / 0 failed**（`tests --ignore=tests/evals`） |
| 探针复跑 | AgentBot 在 resolve→再申请、cancel→再申请 两种场景 `seat note calls == 1`、tip == 强确认 |

**偏差**：无行为偏差。测试数量比计划多 2 条（`/chat` resolve→再申请守卫、AgentBot cancel 路径）。
