# Form Gate 深模块：统一「挂起中的 Form Interrupt + 入站」决策

- 日期：2026-10-19
- 状态：待评审
- 相关：ADR-0001、`CONTEXT.md`（Implicit Cancel / Form-Only Write）、#71、#72

---

## 1. 问题（已核实）

| # | 事实 | 证据 |
|---|---|---|
| 1 | Form Resume / Implicit Cancel / primary_submit 全在 AgentBot 闭包里 | `harness/agent_bot.py:669-718`（`_handle_locked_agent_bot_message` 内层）；两阶段隐式取消在 `run_graph()` `:768-786` |
| 2 | `form_resume.py` 只是浅解析 | 仅 `form_resume_model` / `resume_action` / `resume_field_values` / `schema_field_args`，零决策 |
| 3 | `/chat` 仍 tip + 重推 | `harness/server.py:783-793`（`form_plain_text_refuse_text()` + `project_form_interrupt`） |
| 4 | 同一状态两套策略 | AgentBot：隐式取消 + 处理新消息；`/chat`：拒答 + 吞掉新消息 |
| 5 | 与术语约定冲突 | `CONTEXT.md:47-49` 明确 Implicit Cancel 为规范，且 `_Avoid_: tip+重推旧表当主路径` |
| 6 | 字段过滤集 3 份副本 | `form_resume.py:46`、`agent_bot.py:88`、`agent_bot.py:694`（后者多 `confirmation`） |
| 7 | 决策点实为 3 个 | AgentBot 闭包 / `/chat` / `/actions/confirm`（`server.py:840`，自拼 `{"action": normalized}`，过滤集第 4 份） |

**严重度**：非线上可见 bug。`web/landing-auth-widget.js` 只调 `/me/conversation_mode`、`/config`、`/me/chatwoot_identity`，**不调 `/chat`、不调 `/actions/confirm`**；生产主入口是 Widget→AgentBot。属**维护陷阱 + 公开端点契约不一致**。

**附带**：ADR-0001 仍把「纯文本拒答并重推表单」列为 v1 范围，与 #72 及 `CONTEXT.md` 已不符。

---

## 2. 目标 / 非目标

### 目标
- 一个 deep module 独占「挂起中的 Form Interrupt + 入站 → 决策」，产出四类结果：**confirm / cancel / implicit_cancel / refuse**。
- 三个入口（AgentBot、`/chat`、`/actions/confirm`）消费同一 interface。
- 字段过滤集收敛为**唯一真相源**。
- 决策逻辑可用**纯函数**单测，不再只能整链路验证。

### 非目标
- **不**改任何图节点/边；`MutationGraph` 只保留消费侧（`resume_action` / `resume_field_values` / `schema_field_args`）。
- **不**把图调用、事件投影、Chatwoot 出站搬进模块（见 §3 分层）。
- **不**把 SSE `/chat` 抬成写权威（ADR-0001）。
- 不动 handoff / approval 的 resume 调度。

---

## 3. 目标架构

```
harness/form_gate.py          ← 新增：唯一决策点（纯函数）
  decide_form_inbound(interrupt_value, inbound, *, pending_policy) -> FormDecision

graph/workflows/form_resume.py ← 保持：消费侧助手（schema / action / 字段过滤）
                                新增导出 FORM_RESUME_SKIP_FIELDS（唯一真相源）

调用方（只做执行与投影，不做决策）
  ├─ harness/agent_bot.py       : FORM_SUBMIT / TEXT
  ├─ harness/server.py /chat    : TEXT
  └─ harness/server.py /actions/confirm : EXPLICIT
```

**分层红线**：`harness/` 可以 import `graph/`，反向不允许。故 `FORM_RESUME_SKIP_FIELDS` 定义在 `form_resume.py`（graph 层），由 `form_gate.py` 导入。

**为什么模块只返回决策、不执行**：Implicit Cancel 是**两阶段**（先静默 `Command(resume={"action":"cancel"})` 排空，再同 thread 起新 turn）。这是图调用编排，属调用方职责。若模块吸收执行，会同时拥有决策+执行+投影，违反 `AGENTS.md §4`。

### 接口草案

```python
class FormInboundKind(str, Enum):
    FORM_SUBMIT = "form_submit"   # Chatwoot message_updated + submitted_values
    TEXT = "text"                 # 普通文本
    EXPLICIT = "explicit"         # HTTP 按钮（/actions/confirm）

class FormDecisionKind(str, Enum):
    CONFIRM = "confirm"
    CANCEL = "cancel"
    IMPLICIT_CANCEL = "implicit_cancel"
    REFUSE = "refuse"             # 保持挂起 + 由调用方重推表单

class PendingFormPolicy(str, Enum):
    IMPLICIT_CANCEL = "implicit_cancel"   # 默认（主入口 / CONTEXT.md 规范）
    REFUSE = "refuse"                     # 严格 Form-Only Write（仅显式声明）

@dataclass(frozen=True)
class FormDecision:
    kind: FormDecisionKind
    resume_payload: dict | None = None    # CONFIRM / CANCEL 可直接喂 Command(resume=...)

def normalize_submitted_values(value) -> dict | None: ...   # 从 agent_bot 迁入
def decide_form_inbound(
    interrupt_value: dict,
    *,
    kind: FormInboundKind,
    submitted_values: dict | None = None,
    fields: dict | None = None,
    pending_policy: PendingFormPolicy = PendingFormPolicy.IMPLICIT_CANCEL,
) -> FormDecision: ...
```

> 实现备注：签名比初稿更小——去掉了未使用的 `text`。Form-Only Write 要求纯文本**永不**被解析成业务字段，决策只看 `kind` + `pending_policy`，因此文本内容不参与判定。

### 决策表（Phase 1 严格保持现状）

| 入站 kind | 条件 | 决策 | resume_payload |
|---|---|---|---|
| FORM_SUBMIT | `action ∈ {confirm, confirmed}` | CONFIRM | `{action:"confirm", **过滤后字段}` |
| FORM_SUBMIT | `action ∈ {cancel, canceled}` | CANCEL | `{action:"cancel"}` |
| FORM_SUBMIT | 无 action ∧ `tool=="update_address"` ∧ 有业务字段 | CONFIRM（primary submit） | `{action:"confirm", **过滤后字段}` |
| FORM_SUBMIT | 无 action ∧ 其他（畸形提交） | 按 `pending_policy`（现状=IMPLICIT_CANCEL） | `{action:"cancel"}` 或 None |
| TEXT | 任意 | 按 `pending_policy`（默认 IMPLICIT_CANCEL） | `{action:"cancel"}` 或 None |
| EXPLICIT | confirm / cancel | CONFIRM / CANCEL | `{action, **过滤后字段}` |

**过滤集（唯一）**：`{action, confirmation, request_id, current_address, abandon_footnote}`。

---

## 4. 分阶段执行（每个 Phase 一个独立 PR）

### Phase 0 — 决策确认（需你拍板，见 §7）
- D1：`/chat` 是否切到 Implicit Cancel。
- D2：`/actions/confirm` 保留与否。
- D3：`pending_policy` 默认值。

### Phase 1 — 纯抽取，**零行为变更**（PR 1，可独立合并）

改动：
1. 新增 `src/ai_service_agent/harness/form_gate.py`（§3 接口）。
2. `graph/workflows/form_resume.py`：导出 `FORM_RESUME_SKIP_FIELDS`，`resume_field_values` 改用它。
3. `harness/agent_bot.py`：
   - 删除内联 `_resume_skip`、`primary_submit_confirm`、`_normalize_submitted_values`；
   - 改为 `decision = decide_form_inbound(...)`，按 `decision.kind` 分支；
   - `run_graph()` 的两阶段执行**保持原样**，只把 `implicit_cancel` 的来源换成 `decision.kind is IMPLICIT_CANCEL`。
4. 新增 `tests/test_form_gate.py`：表驱动，覆盖决策表 6 行 + 字段过滤 + list/object 两种提交形状。

验证：
```bash
pytest tests/test_form_gate.py tests/test_agent_bot_implicit_cancel.py \
       tests/test_agent_bot_address_form_open.py tests/test_agent_bot.py -q
```
→ 全绿；且 `agent_bot.py` 的 diff 不含任何文案/行为变更。

### Phase 2 — 对齐三个入口（PR 2）

按 Phase 0 决策：
- `/chat`（`server.py:783`）改为 `decide_form_inbound(kind=TEXT)`。
  - 若 D1=A：删掉 tip+重推分支，改走隐式取消两阶段；新增 `/chat` 隐式取消测试。
  - 若 D1=B：保留 REFUSE，但**显式**传 `pending_policy=REFUSE` 并加注释指向 ADR，把隐式漂移变成显式契约。
- `/actions/confirm`（`server.py:871`）改为 `decide_form_inbound(kind=EXPLICIT, fields=body.fields)`，删除本地过滤循环。
- 补 `tests/test_server.py` 的 `/chat` form-pending 用例。

### Phase 3 — 文档与去重（PR 3）

- 更新 `docs/adr/0001-widget-agentbot-form-contract.md`：删除/限定「纯文本拒答并重推表单」。
- `CONTEXT.md` 的 Implicit Cancel 条目补一句「所有 Transport 一致，无 per-entry 例外」。
- 删除 `agent_bot.py:88 _FORM_SCHEMA_SKIP_FIELDS` 重复定义。
- 若 Phase 1 未合并 `confirmation` 到统一过滤集，此处收口。

---

## 5. 测试矩阵

| 层 | 文件 | 覆盖 |
|---|---|---|
| 纯函数（新） | `tests/test_form_gate.py` | 6 行决策表、字段过滤、畸形提交、list/object 形状、policy 切换 |
| AgentBot | `test_agent_bot_implicit_cancel.py` | 4 个隐式取消场景（FAQ / 新意图 / 陈旧提交 / form_rejected 不触发） |
| AgentBot | `test_agent_bot_address_form_open.py` | primary submit、current_address 忽略、form_rejected 重推 |
| AgentBot | `test_agent_bot.py` | 表单投影、成功卡片、审批旁路 |
| SSE | `test_server.py` / `test_form_interrupt_api.py` | `/chat` 挂起行为（Phase 2 新增） |
| E2E | `test_e2e_real.py` | `/actions/confirm` 端到端 |
| 回归 | 全量 `pytest tests --ignore=tests/evals` | 无新增失败 |

---

## 6. 验收标准

1. Phase 1 后：`agent_bot.py` 行为零变化（现有 4 + 9 个相关测试全绿）。
2. Phase 2 后：三个入口对同一 `(interrupt, 入站)` 产出**同一决策**，有测试断言三者一致。
3. 字段过滤集全仓只剩 **1 处**定义（`grep -rn "abandon_footnote" src` 仅 `form_resume.py` + 表单项构造处）。
4. 全量 `pytest tests --ignore=tests/evals`：0 failures / 0 errors（对比基线不新增）。
5. 图节点/边零改动：`git diff main -- src/ai_service_agent/graph/builder.py graph/workflows/*agent.py` 为空。

---

## 7. 需要你拍板

| ID | 问题 | 选项 | 建议 |
|---|---|---|---|
| D1 | `/chat` 遇到「form 挂起 + 纯文本」怎么办 | A. 切 Implicit Cancel；B. 保留 REFUSE 但显式化 | **A** — `CONTEXT.md` 已规定；web 不调 `/chat`；唯一对外变化是 legacy 端点 |
| D2 | `/actions/confirm` 是否保留 | A. 保留并接模块；B. 标记 deprecated | **A** — 有 E2E 覆盖，先收口不删 |
| D3 | 默认 `pending_policy` | A. IMPLICIT_CANCEL；B. REFUSE | **A** — 与主入口一致 |

---

## 8. 风险与回滚

| 风险 | 影响 | 缓解 |
|---|---|---|
| `/chat` 契约变化 | 外部 SSE 客户端行为改变 | Phase 2 独立 PR；必要时保留 `pending_policy` 开关；PR 描述标注 |
| 隐式取消两阶段被误搬进模块 | 分层破坏、难测 | §3 红线：模块只返回决策 |
| 畸形提交被误判 | 误取消表单 | Phase 1 严格保持现状（IMPLICIT_CANCEL）；Phase 2 再评估是否改 REFUSE |
| `confirmation` 纳入统一过滤集 | 若某工具 schema 有同名字段会被丢 | 已由 `schema_field_args` 二次过滤，行为中性；加单测锁定 |

**回滚**：三个 Phase 各自独立 commit，Phase 1 零行为变更可单独回滚；Phase 2 回滚即恢复 `/chat` 旧分支。

---

## 9. 执行顺序

```
Phase 0（决策）
   └─ Phase 1（纯抽取）── 可独立合并
         └─ Phase 2（对齐入口）
               └─ Phase 3（文档/去重）
```

Phase 1 与 Phase 2 之间建议留一次全量回归，避免把「抽取错误」与「策略变更」混在同一次失败里定位。
