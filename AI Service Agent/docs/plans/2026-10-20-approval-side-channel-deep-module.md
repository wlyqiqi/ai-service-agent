# Approval Side-Channel 深模块：把 ADR-0002 清单收进一个接缝

- 日期：2026-10-20
- 状态：待评审（未改代码）
- 相关：ADR-0002、`approval-spec.md` §3.2.1 / §3.2.2 / §5、`CONTEXT.md`（Seat Approver / Approval SoD / Approval Side-Channel）
- 同型先例：`docs/plans/2026-10-19-form-gate-deep-module.md`（form_gate 已收敛，approval 是剩余 outlier）

---

## 1. 问题（已核实）

| # | 事实 | 证据 |
|---|---|---|
| 1 | 坐席表单**投影**住在客户侧适配器里 | `harness/agent_bot.py:328-397`（`approval_form_title_from_payload` / `approval_form_items_from_payload` / `parse_approval_form_submission` / `send_approval_form`） |
| 2 | `server.py` 反向 import `agent_bot` 取用坐席侧投影 | `harness/server.py:37-43` |
| 3 | **鉴权 / `decided_by` / resume / 出站门控**全在 `create_app` 闭包 | `server.py:306`（owner）、`:310`（工牌）、`:347`（只读）、`:940`（`_approval_resume`）、`:998`（`_decide_approval_as_seat`） |
| 4 | 存储在浅层 `approvals.py` | 170 行纯 CRUD + `make_interrupt_callback`，零决策（**这是 spec 要求的**，见 §3.1） |
| 5 | 出站 mode 门控 **3 份副本** | `server.py:824-834`（`/chat`）、`server.py:982-995`（`_approval_resume`）、`agent_bot.py:787-801` |
| 6 | 门控分歧已可观测 | human 期间批准落库长文案「通过审核」（`tests/test_approval_resume_gates.py:95`）；ai 期间落库短 followup「退款申请已提交」（`:150`）——同一审批，Message Store 内容按 mode 不同 |
| 7 | resume 后 follow-on `approval_required` **不投影** | `_approval_resume` 事件循环只处理 `done` / `handoff_requested`（`server.py:964-978`），与 `approval-spec §3.2.2`「不得静默」不符；测试只覆盖 follow-on handoff（`:158`） |
| 8 | 决策面不可单测 | 5 个关键函数都是 `create_app` 内嵌；现有 27 个审批用例（4 文件）**全部**经 `TestClient` 走 HTTP |

**严重度**：非线上可见 bug（human 期间批准两个用例通过）。属**维护陷阱 + spec 契约缺口**：改一次门控要同步 3 处，改一次 authz 要读 1300 行 server。

**根因不是「approvals.py 太浅」**——`approval-spec §3.1` 明确它是「轻量基础设施状态存储，不承担任何业务决策」。真正的缺口是：**侧信道的策略（谁批 / 批完做什么 / 怎么回话）没有归属模块**，被摊在适配器与 Transport 闭包里。

---

## 2. 目标 / 非目标

### 目标

- 一个 deep module 独占 Approval Side-Channel 的**执行链**：投影 → 解析 → SoD/工牌 → `mark_decided` → resume → Message Store / 按 mode 门控发送。
- 三个入口（Chatwoot webhook、`/approvals/*` HTTP、`/chat` 与 AgentBot 的 `approval_required` 投影）消费同一 interface。
- 出站门控与「落库哪条文案」收敛为**唯一纯函数**，消除 §1.6 的 mode 分歧。
- follow-on `approval_required` 与 follow-on `handoff_request` 同级投影，不再静默。
- 决策/门控可用**纯单测**覆盖，不再只能起 FastAPI。

### 非目标

- **不**把业务决策搬进模块：批准后执行什么 / 拒绝怎么收尾仍在 `HighRiskGraph`（`approval-spec §3.1`、`AGENTS.md §6`）。
- **不**把 `approvals.py` 做深：它保持浅存储；`approval-spec §3.1` 的定位不变。
- **不**改 resume schema（`graph-spec.md §3` 唯一 schema）。
- **不**动图节点 / 边；不动 handoff 路径（#95 刚收敛 `decide_entry`，不混改）。
- **不**引入坐席↔客户绑定表（ADR-0002 残余风险保留）。
- **不**新增依赖。

---

## 3. 目标架构

```
harness/approval_side_channel.py     ← 新增：侧信道执行链唯一归属
  ├─ 纯函数层（可直接单测）
  │    decide_delivery(mode, success_spec, final) -> DeliveryDecision
  │    parse_seat_submission(values)  -> SeatDecision | None      # 从 agent_bot 迁入
  │    approval_form_items / title     -> Chatwoot form payload    # 从 agent_bot 迁入
  └─ 编排层（注入 runner，不自己建图）
       ApprovalSideChannel.project(conv_id, interrupt_value)
       ApprovalSideChannel.decide(approval_id, action, *, actor, note)
       ApprovalSideChannel.decide_from_seat_form(payload) -> bool
       ApprovalSideChannel.interrupt_callback(conv_id, thread_id)

harness/approvals.py                 ← 不变：纯 CRUD 存储（spec §3.1）
harness/agent_bot.py                 ← 只保留一个守卫调用 + `approval_required` 转发
harness/server.py                    ← 端点变 3 行：工牌 → decide；webhook → decide_from_seat_form
```

**分层红线**

1. 模块在 `harness/`，可 import `harness/*` 与 `graph/*`；`graph/` 永不 import 它。
2. 模块**不**构造图：注入 `ResumeRunner`，把「建图 + `AsyncSqliteSaver` + `event_stream`」留在 Transport 侧（`AGENTS.md §4`）。模块拥有的是**顺序与门控**（precheck → mark → resume → persist → gate → send），这正是 ADR-0002 清单的 leverage。
3. `approvals.py` 与模块是**存储 / 策略**分离，不是包含关系；模块单向依赖 store。

### 接口草案

```python
# harness/approval_side_channel.py

ResumeRunner = Callable[..., AsyncIterator[dict]]
#   async def runner(thread_id, *, resume_payload, on_interrupt, user_id) -> AsyncIterator[event]

@dataclass(frozen=True)
class SeatActor:
    actor_id: str            # Chatwoot sender.id
    @property
    def decided_by(self) -> str:   # 唯一命名空间（ADR-0002）
        return f"seat:{self.actor_id}"

class SeatAction(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"

@dataclass(frozen=True)
class DeliveryDecision:
    persist: str | None      # 落 Message Store 的 assistant 文案（None = 不落）
    outbound: bool           # 是否发 Chatwoot
    notice: str | None       # "ModeChanged" 等对外提示码（None = 静默丢弃）

def decide_delivery(*, mode: str, success_spec, final: str | None) -> DeliveryDecision:
    """唯一门控：Message Store 与出站文案**同源**，不再按 mode 分叉文案。"""

def parse_seat_submission(values) -> SeatDecision | None: ...
def approval_form_title(payload: dict) -> str: ...
def approval_form_items(payload: dict) -> list[dict]: ...

class ApprovalSideChannel:
    def __init__(self, *, store, status, messages, chatwoot, runner,
                 seat_secret: str | None, simulate_approval: bool = False): ...

    async def project(self, conversation_id: str, interrupt_value: dict) -> None: ...
    async def decide(self, approval_id: str, action: SeatAction, *,
                     actor: SeatActor, note: str | None = None) -> dict: ...
    async def decide_from_seat_form(self, payload: dict,
                                    *, conversation_for) -> bool: ...
    def interrupt_callback(self, conversation_id: str, thread_id: str): ...
```

**`decide` 内部（唯一实现，两个入口共用）**

```
1. store.get(approval_id) → 404 / thread_id / conv_id
2. thread lock
3. store.precheck(approval_id)          # 锁内权威预检：并发双 approve 只过 1 个
4. store.mark_decided(decided, actor.decided_by)   # 图执行前（T6 裁定）
5. runner(thread_id, resume_payload, on_interrupt=combined_cb, user_id)
      ├─ 收集 done / write_success
      ├─ approval_required → self.project(...)          # 修 §1.7
      └─ handoff_requested → 交给注入的 handoff 回调
6. decide_delivery(mode, success_spec, final)
7. persist → messages.append（始终）
8. outbound 且 chatwoot → 卡片或气泡
```

**为什么投影要迁出 `agent_bot.py`**：`send_approval_form` 是**坐席侧**产物，却住在客户侧适配器里，导致 `server → agent_bot` 的反向依赖（§1.2）。迁出后 `agent_bot` 只剩一句守卫：

```python
if submitted_values is not None and parse_seat_submission(submitted_values) is not None:
    return None   # 客户 Widget 提交审批卡永不成为坐席决策（ADR-0001）
```

### 决策表（门控，取代 3 份副本）

| mode | 有 success_spec | `final` | persist | outbound | notice |
|---|---|---|---|---|---|
| `ai` | 是 | — | 短 followup | ✅ 卡片 + 气泡 | — |
| `ai` | 否 | 非空 | `final` | ✅ 气泡 | — |
| `human` | 是 | — | **短 followup**（与 ai 同源，修 §1.6） | ❌ | — |
| `human` | 否 | 非空 | `final` | ❌ | — |
| 任意 | 否 | 空 | — | ❌ | — |

`/chat` 额外需要「被切走后回 `ModeChanged` 错误事件」：`decide_delivery` 返回 `outbound=False` 时由 `/chat` 自行决定是否发 notice——**notice 属于 SSE 契约，不进模块**。

---

## 4. 分阶段执行（每 Phase 一个 PR）

### Phase 0 — 决策确认（见 §7）

### Phase 1 — 纯搬移，零行为变更

1. 新增 `harness/approval_side_channel.py`，先只放**投影 + 解析**（从 `agent_bot.py:328-397` 原样搬，不改逻辑）。
2. `agent_bot.py` 改 import；保留 `parse_seat_submission` 守卫一行。
3. `server.py:37-43` 改从新模块 import。
4. 新增 `tests/test_approval_side_channel.py`：投影形状 + 解析（object / `[{name,value}]` 两种形状）+ 非审批提交返回 None。

验证：`pytest tests/test_chatwoot_approval_form.py tests/test_agent_bot.py -q` 全绿，`git diff` 无文案变化。

### Phase 2 — 门控单点（唯一行为变更）

1. 新增 `decide_delivery` 纯函数 + `tests/test_approval_delivery.py`（表驱动，5 行决策表）。
2. 替换三处：`server.py:824-834`、`server.py:982-995`、`agent_bot.py:787-801`。
3. 若 D1=A：`tests/test_approval_resume_gates.py:95` 的断言从「通过审核」改为「退款申请已提交」，并加一条「human / ai 落库文案一致」的断言。

### Phase 3 — 搬 authz + decide

1. 把 `_authenticate_seat_badge` / `_ensure_approval_owner` / `_approval_read_access` / `_approval_resume` / `_decide_approval_as_seat` / `_seat_decide_from_chatwoot` 搬入模块。
2. `server.py` 注入 `runner`（约 8 行闭包），端点变：

```python
@app.post("/approvals/{approval_id}/approve")
async def approve_approval(approval_id: str, request: Request):
    actor = seat_channel.authenticate_badge(request.headers)   # 403 fail-closed
    return await seat_channel.decide(approval_id, SeatAction.APPROVE, actor=actor)
```

3. `interrupt_callback` 从 `approvals.py` 迁入模块（落库 + simulate 属于侧信道，不属于存储）；`approvals.py` 退回纯 CRUD。
4. 纯单测：工牌 fail-closed（无 secret / 错口令 / 缺 actor）、幂等双 approve、`decided_by=seat:{id}`、human 期间 resume 但不发、ai 期间卡片 + 气泡。

### Phase 4 — 补齐 follow-on 投影

`decide` 事件循环加 `approval_required → self.project(...)`；新增单测（fake runner 先 yield done 再 yield approval_required，断言 `send_interactive_message` 被调用）。

### Phase 5 — 文档同步

- `approval-spec §3.2.2` 补「实现指针：`harness/approval_side_channel.py`」。
- ADR-0002 的 Implementation checklist 逐条标注落地位置（4 项 → 同一文件）。
- `CONTEXT.md` 的 Approval Side-Channel 条目补「执行链唯一归属」。

---

## 5. 测试矩阵

| 层 | 文件 | 覆盖 |
|---|---|---|
| 纯函数（新） | `tests/test_approval_delivery.py` | 5 行门控表、human/ai 文案同源、空 final |
| 纯单测（新） | `tests/test_approval_side_channel.py` | 投影形状、解析两形状、工牌 3 种 fail-closed、`seat:` 命名空间、幂等双批、follow-on 投影 |
| 集成（保留） | `tests/test_approval_resume_gates.py` | 4 个 TestClient 用例（Phase 2 改断言） |
| 集成（保留） | `tests/test_chatwoot_approval_form.py` | webhook 验签 → 私有表单 → 决策 8 例 |
| 集成（保留） | `tests/test_approvals.py` / `test_approval_request.py` | 存储语义 15 例 |
| 回归 | 全量 `pytest tests --ignore=tests/evals` | 不新增失败 |

---

## 6. 验收标准

1. `grep -rn "_approval_resume\|_decide_approval_as_seat\|_authenticate_seat_badge" src/` → 仅 `approval_side_channel.py`。
2. `grep -rn "get_mode(.*) == \"ai\"" src/ai_service_agent/harness/` → 仅 `decide_delivery` 的调用点（`/chat`、`agent_bot`、模块内）。
3. `server.py` 审批相关代码 < 60 行（当前约 250 行）。
4. 同一审批在 `ai` / `human` 下 Message Store 落**同一文案**（新断言）。
5. follow-on `approval_required` 有坐席表单投影（新断言）。
6. `approvals.py` 无新增方法，仍无决策（`approval-spec §3.1` 不变）。
7. 图零改动：`git diff main -- src/ai_service_agent/graph/` 为空。
8. 全量 `pytest tests --ignore=tests/evals`：0 failures（对比基线不新增）。

---

## 7. 已拍板（2026-10-20）

| ID | 问题 | 决策 | 影响 |
|---|---|---|---|
| D1 | human 期间批准的 Message Store 文案 | **A：与 ai 对齐（短 followup）** | T3 改 `test_approval_resume_gates.py:95` 断言 + 新增同源断言 |
| D2 | follow-on `approval_required` 是否推新坐席表单 | **A：投影** | T5 在 `decide` 事件循环内接 `project()` |
| D3 | `interrupt_callback` 归属 | **A：迁入侧信道模块** | T4 从 `approvals.py` 移出，`approvals.py` 退回纯 CRUD |
| D4 | 是否独立 `outbound_gate.py` | **A：不独立** | 门控只在模块内，两处调用 |


---

## 8. 风险与回滚

| 风险 | 影响 | 缓解 |
|---|---|---|
| Phase 3 搬移误改 `create_app` 捕获的闭包变量（`db_path` / `model` / `order_repository` / `status`） | 审批 resume 打到错 DB | 注入 runner 时**不**复制这些变量进模块；模块只拿 `store/status/messages/chatwoot/runner` |
| `runner` 注入把建图参数复制成第二份 | 与 `/chat` 漂移（`make_ai_graph` 注释已警告） | runner 闭包内**必须**调用同一个 `make_ai_graph`；禁止模块内 import builder |
| Phase 2 改文案断言 | 现有测试红 | 断言与实现同 PR 改；PR 描述标注「唯一行为变更」 |
| `agent_bot` 守卫失效（投影迁走后漏判） | 客户 Widget 提交变坐席决策（ADR-0001 违规） | Phase 1 保留守卫 + 现有 `test_chatwoot_approval_form.py` 的 Widget 旁路用例 |
| 范围膨胀到 handoff | 与 #95 冲突 | §2 非目标显式排除；handoff 若同型另开计划 |

**回滚**：Phase 1/2/3 各自独立 commit。Phase 1 零行为变更可单独回滚；Phase 2 回滚恢复三处副本；Phase 3 回滚恢复 `create_app` 闭包实现（模块可留着不调用）。

---

## 9. 执行顺序

```
Phase 0（决策 D1-D4）
   └─ Phase 1（纯搬移投影）── 可独立合并
         └─ Phase 2（门控单点）── 唯一行为变更
               └─ Phase 3（搬 authz + decide）
                     └─ Phase 4（follow-on 投影）
                           └─ Phase 5（文档同步）
```

Phase 1 与 Phase 2 之间留一次全量回归：把「搬移错误」与「门控语义变更」分开定位。

---

## 10. 执行状态

- 2026-10-20：问题已核实；计划待评审，**未改代码**。
- 2026-10-20：D1-D4 全部按建议拍板（见 §7）。
- 2026-10-20：**PR1 已实现**（分支 `refactor/approval-side-channel`，commit `3e7213fc`）：
  - 新增 `harness/approval_side_channel.py`（投影 + 解析）与 `harness/mutation_projection.py`（`WriteSuccessCapture` + 成功卡片）；
  - `agent_bot.py` 净减 ~170 行，只保留 Widget 提交守卫；
  - 附带修复：审批决定项 `options` 由 `dict()` 浅拷贝改为每次新建（原实现跨调用共享列表）；
  - 新增 `tests/test_approval_side_channel.py`（10 例纯单测）；
  - 全量 `pytest tests --ignore=tests/evals` = **808 passed / 2 skipped**。
  - 待做：PR2（T2+T3，唯一行为变更）。

---

## 11. 任务级执行计划

> **PR 划分：4 个**（见下表）。T 是 commit 粒度，不单独开 PR；T6 是每个 PR 的准入门槛，不是 PR。
> 分支统一 `refactor/approval-side-channel`，每个 PR 合入前跑 `pytest tests --ignore=tests/evals`。

| PR | 标题 | 含 T | 行为变更 | 规模（估） | 为什么这样切 |
|---|---|---|---|---|---|
| **PR1** | `refactor(harness): 投影归位` | T0 + T1 | 零 | 新增 ~150 / 搬移 ~100 | 纯搬移，可单独回滚；先解循环 import |
| **PR2** | `fix(harness): 出站门控单点` | T2 + T3 | **唯一变更** | ~200（含测试） | 行为变更必须独占 PR，否则回归失败无法归因 |
| **PR3** | `refactor(harness): 审批执行链归位` | T4（含测试） | 等价重构 | 搬移 ~250 / 新增 ~250 | 大搬移，diff 以移动为主，便于 review |
| **PR4** | `fix(harness): follow-on 审批投影` + 文档 | T5 + T7 | 新增能力 | ~100 | 补 spec 缺口，与重构分开 |

**为什么不拆成 8 个**：T2 单独合并会留下「写了纯函数没人调用」的死代码；T5 单独成一个 PR 只改 15 行，评审成本高于收益；T7 文档与 T5 同源，分开会有一段文档与代码不符的窗口。

**为什么 PR3/PR4 不合并**：PR3 是纯搬移（可 `git diff --color-moved` 验证），PR4 改行为（新增投影）。合并后一旦 follow-on 测试红，无法判断是搬移错还是新逻辑错。

### T0 — 抽 `harness/mutation_projection.py`（前置，零行为变更）


**为什么先做**：`approval_side_channel` 需要 `WriteSuccessCapture` / `send_mutation_success_card`，而它们现在住在 `agent_bot.py`。若模块 import `agent_bot`，而 `agent_bot` 又要 import 模块（守卫），即成环。

**改动**
- 新增 `src/ai_service_agent/harness/mutation_projection.py`，迁入 `agent_bot.py:51-77`（`WriteSuccessCapture`）、`:816-869`（`mutation_success_card_items` / `update_address_success_card_items` / `send_mutation_success_card`）。
- `agent_bot.py`、`server.py:37-43` 改 import 源。
- 不新增测试；现有用例即回归。

**验收**
```bash
grep -rn "WriteSuccessCapture\|send_mutation_success_card" src/ai_service_agent/harness/agent_bot.py   # 仅 import + 调用
pytest tests/test_agent_bot.py tests/test_approval_resume_gates.py -q
```

### T1 — 建模块，搬投影（零行为变更）

**改动**
- 新增 `src/ai_service_agent/harness/approval_side_channel.py`：`SeatActor` / `SeatAction` / `parse_seat_submission` / `approval_form_title` / `approval_form_items` / `send_approval_form` / `project()`（先只包 `send_approval_form`）。
- `agent_bot.py` 删除 `:328-397`，保留守卫：
  `if submitted_values is not None and parse_seat_submission(submitted_values) is not None: return None`
- `server.py:170`、`:821` 改调模块函数。
- 新增 `tests/test_approval_side_channel.py`（本任务只覆盖投影形状 + 解析两形状 + 非审批提交 → None）。

**验收**：`pytest tests/test_chatwoot_approval_form.py tests/test_agent_bot.py tests/test_approval_side_channel.py -q` 全绿；`grep -rn "def send_approval_form" src/` 仅 1 处。

> 实现备注（2026-10-20）：T1 采用**纯搬移 + 沿用旧名**（`approval_form_title_from_payload` / `parse_approval_form_submission`），使 diff 可用 `--color-moved` 验证；`SeatActor` / `SeatAction` 与短名（`parse_seat_submission`）随 T4 引入。

### T2 — `decide_delivery` 纯函数 + 表驱动单测

**改动**
- `approval_side_channel.py` 加 `DeliveryDecision` + `decide_delivery(mode, success_spec, final)`（§3 决策表）。
- 新增 `tests/test_approval_delivery.py`：5 行表 + 「human / ai persist 同源」断言。

**验收**：`pytest tests/test_approval_delivery.py -q`；此时**尚未**接调用方，生产行为不变。

### T3 — 三处门控收敛（**唯一行为变更**）

**改动**
- `server.py:824-834`（`/chat`）：保留 `ModeChanged` 错误事件（SSE 契约），其余走 `decide_delivery`。
- `server.py:982-995`（`_approval_resume`）：删 `if/elif` 文案分叉。
- `agent_bot.py:787-801`：同。
- `tests/test_approval_resume_gates.py:95`：断言由「通过审核」改为「退款申请已提交」，并新增 human / ai 落库文案一致断言。

**验收**
```bash
grep -rn 'get_mode(.*) == "ai"' src/ai_service_agent/harness/    # 仅 decide_delivery 调用点
pytest tests/test_approval_resume_gates.py tests/test_agent_bot.py -q
```

### T4 — 搬 authz + `decide` + `decide_from_seat_form` + `interrupt_callback`

**改动**
- 迁入：`_authenticate_seat_badge` / `_require_seat_actor` 的头部解析 / `_ensure_approval_owner` / `_approval_read_access` / `_approval_resume` / `_decide_approval_as_seat` / `_seat_decide_from_chatwoot`。
- `approvals.py`：`make_interrupt_callback` 移入模块（D3=A），文件回到纯 CRUD。
- `server.py`：新增 `_resume_runner` 闭包（`AsyncSqliteSaver` + `make_ai_graph` + `event_stream`，约 10 行），构造 `ApprovalSideChannel(...)`；端点变 3 行。
- 补 `tests/test_approval_side_channel.py`：工牌 fail-closed 3 例、`decided_by=seat:{id}`、并发双 approve 只 1 次 resume（fake runner 计数）。

**验收**
```bash
grep -rn "_approval_resume\|_decide_approval_as_seat\|_authenticate_seat_badge" src/   # 仅 approval_side_channel.py
pytest tests/test_approvals.py tests/test_chatwoot_approval_form.py \
       tests/test_approval_resume_gates.py tests/test_seat_gated_handoff.py -q
```

### T5 — follow-on `approval_required` 投影（D2=A）

**改动**：`decide` 事件循环加 `approval_required → self.project(conv_id, ev["data"])`；`handoff_requested` 继续走注入回调。
**测试**：fake runner 依次 yield `done` + `approval_required`，断言 `send_interactive_message` 被调用且 `content_type=="form"`、`private=True`。

### T6 — 全量回归 + main 冒烟

```bash
pytest tests --ignore=tests/evals
# 核对方式：--collect-only 数量 == 进度点 + skip；grep '^FAILED' 为 0
```
合并后 `main` 起 runtime，冒烟：`/chat` 触发审批 → Chatwoot 私有表单 → 提交 → 卡片；human 期间再批一次，确认无出站、Message Store 有短文案。

### T7 — 文档同步

- `approval-spec §3.2.2`：补实现指针。
- ADR-0002 Implementation checklist：4 项逐条标注落点（同一文件 + 行号）。
- `CONTEXT.md` Approval Side-Channel 条目：补「执行链唯一归属」。
- 本文件 §10 状态改「已实现」+ 实现记录。

### 依赖关系

```
T0 ──▶ T1 ──▶ T2 ──▶ T3 ──▶ T4 ──▶ T5 ──▶ T7
（PR1 ──────────┘  （PR2 ──────┘  （PR3 ──┘  （PR4
```

T2 不依赖 T3，但 T3 依赖 T2；T0 不阻塞 T1 的投影部分，但 T4 前必须完成（否则成环）。T6 在每个 PR 都跑。
