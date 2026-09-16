# P5 Select 工具风险对齐实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复真实模型在写图内选择只读工具导致的 D1 澄清盲区与"对只读操作发起确认/审批"漂移：B1 给只读工具补 select 侧 schema；B2b 写图 select 档内严格、低档工具选中走 tier_mismatch 澄清；D3 离线回归 + ARK 真机探针复验。Guard / Approval 迁移 / Memory 仅登记观察（D4）。

**Architecture:** 写图保持"单工具 select + 显式 Workflow"（无 ReAct 循环）。B1 在 ToolRegistry 元数据层为 `kb_search` / `order_lookup` 补 select 侧 pydantic schema（`request_id` 由 select 注入、`user_id` 是 `InjectedState` 不入 schema），ReadGraph（create_agent 消费函数签名）零影响。B2b 在 `make_select_node` 内把允许集与系统提示过滤为 `min_risk_level == tier` 的写工具：选中已注册低档工具 → 写 `context["tier_clarify"]` → 条件边路由 clarify 节点（普通轮非 interrupt，发 `clarification_required` + `reason:"tier_mismatch"`，`execution_status=clarifying`，`clarify_count` 计轮、>N=2 fallback）；未注册 / 高档工具保持 `unknown_tool` 拒绝。ToolNode 分层注入不变，select 是写图唯一工具选择闸。

**Tech Stack:** Python 3.14；LangGraph 1.2.x / langchain-core（现有 pinned）；pydantic v2（现有）；`ToolCallingFakeModel` + `router_json_message` / `select_json_message`（json_mode 契约）；pytest + MemorySaver；ARK 真机冒烟脚本（`scripts/real_model_http_smoke.py` 既有）；无新依赖。

**Spec:**
- `docs/decisions/2026-09-16-p5-select-tier-alignment.md`（本计划 Task 3 新建，D1–D4 登记）
- `docs/decisions/2026-09-16-ctrl-0076-clarify-verification.md`（0076 收尾 + B→P5 OQ）
- `docs/real-model-smoke.md`（§7 漂移证据）
- `docs/specs/tool-spec.md`（§1/§4）
- `docs/specs/graph-spec.md`（§6.2/§6.3/§9）
- `docs/specs/state-spec.md`（§1）
- `docs/specs/event-spec.md`（§2）

## Global Constraints

- **决策依据**：P5 Design Audit D1–D4（2026-09-16 用户批准）：D1=B1 只读工具补 schema；D2=B2b 写图 select 档内严格；D3=离线回归 + ARK 探针；D4=Guard LLM / Approval 迁移 / Memory 仅观察。
- **写图保持显式 Workflow**：不引入 ReAct / agent 循环；不实现"先查后写"多步组合；子图之间不内部转交（AGENTS.md §8）。
- **B2b 只改 select 允许集/提示与 tier_mismatch 路由**：`for_risk_level` 分层语义与 ToolNode 注入不变；ReadGraph / harness / guard / safety 预检规则零改动（safety `_check` 对只读工具新增的 schema 校验成为纵深防御，行为不激活）。
- **json_mode 契约**：select 输入必须与真实模型 `response_format={"type":"json_object"}` 输出一致（fake 用 `select_json_message`）；禁止解析 ToolStrategy 描述性文本。
- **决策路由一律条件边**：节点返回普通 dict + 条件边（LangGraph 1.2.11 实测 `Command(goto)` 会并发双写）。
- **事件协议唯一权威 `event-spec.md`**：`clarification_required` 新增 `reason?` 字段必须同步更新该文件（Task 3）。
- **离线确定性（AGENTS.md §16）**：eval `master.jsonl` 不改（CTRL-0076 `expected_policy=clarify` 保持）；runner scripted select 均写工具，预期四维基线不变（policy 76 / guard 100 / routing 91 / handoff 91）。
- **Guard v1 不动**；D4 观察项不实现、不建计划任务。
- **TDD / commit**：每任务测试全绿后由用户手动 `git commit`（git 根 `D:\AI_Project`，在 `AI Service Agent` 目录内执行，commit 自动带 `AI Service Agent/` 前缀）；`langgraph validate` 保持通过。
- **运行前缀**：`$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy";`，一律 `uv run --no-sync ...`。
- **不新增依赖**。

---

### Task 0: 计划文件落库

- [ ] **Step 1: Commit 本计划文件**

```bash
git add docs/plans/2026-09-16-p5-select-tier-alignment.md
git commit -m "docs: P5 Select 工具风险对齐实现计划（B1/B2b/D3）"
```

### Task 1: B1 只读工具 select 侧 schema（D1）

**Files:**
- Modify: `src/ai_service_agent/tools/kb.py`（新增 `KbSearchArgs`）
- Modify: `src/ai_service_agent/tools/orders.py`（新增 `OrderLookupArgs`）
- Modify: `src/ai_service_agent/tools/__init__.py`（注册 schema）
- Test: Modify `tests/test_tools.py`

**Interfaces:**
- Consumes: `ToolMeta.schema`（现有字段，P2 OQ-1 默认 None）
- Produces:
  - `KbSearchArgs(BaseModel)`：字段 `text: str`、`request_id: str`，`extra="forbid"`
  - `OrderLookupArgs(BaseModel)`：字段 `order_id: str`、`request_id: str`，`extra="forbid"`
  - `DEFAULT_TOOL_REGISTRY.meta_for("kb_search").schema is not None`、`meta_for("order_lookup").schema is not None`

- [ ] **Step 1: 写失败测试**（`tests/test_tools.py` 追加）

```python
def test_read_tool_schemas_registered():
    """B1（P5 D1）：只读工具注册 select 侧 schema，注册表元数据完备（tool-spec §1 预检用）。"""
    assert DEFAULT_TOOL_REGISTRY.meta_for("order_lookup").schema is not None
    assert DEFAULT_TOOL_REGISTRY.meta_for("kb_search").schema is not None


def test_read_tool_schema_contracts():
    """B1：select 侧契约——业务字段 + request_id（select 注入）；user_id 为 InjectedState 注入、不入 schema。"""
    from ai_service_agent.tools.orders import OrderLookupArgs
    from ai_service_agent.tools.kb import KbSearchArgs

    assert "order_id" in OrderLookupArgs.model_fields
    assert "request_id" in OrderLookupArgs.model_fields
    assert "user_id" not in OrderLookupArgs.model_fields
    assert OrderLookupArgs.model_validate({"order_id": "ORD-1", "request_id": "r1"}).order_id == "ORD-1"

    assert "text" in KbSearchArgs.model_fields
    assert "request_id" in KbSearchArgs.model_fields
    assert KbSearchArgs.model_validate({"text": "退款", "request_id": "r1"}).text == "退款"
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_tools.py -q`
Expected: FAIL（`schema is None` / `ImportError: cannot import name 'OrderLookupArgs'`）

- [ ] **Step 3: 最小实现**

`src/ai_service_agent/tools/orders.py` 追加：

```python
from pydantic import BaseModel, ConfigDict


class OrderLookupArgs(BaseModel):
    """order_lookup 的 select 侧契约（P5 B1）：仅业务字段 + request_id（select 注入）；
    user_id 由 langgraph.prebuilt.InjectedState 注入，不入 schema。"""
    model_config = ConfigDict(extra="forbid")
    order_id: str
    request_id: str
```

`src/ai_service_agent/tools/kb.py` 追加：

```python
from pydantic import BaseModel, ConfigDict


class KbSearchArgs(BaseModel):
    """kb_search 的 select 侧契约（P5 B1）。"""
    model_config = ConfigDict(extra="forbid")
    text: str
    request_id: str
```

`src/ai_service_agent/tools/__init__.py` 顶部 import 与注册改为：

```python
from ai_service_agent.tools.kb import KbSearchArgs, kb_search
from ai_service_agent.tools.orders import OrderLookupArgs, lookup_order_state, order_lookup

DEFAULT_TOOL_REGISTRY.register(
    ToolMeta("kb_search", "知识库检索", "read", schema=KbSearchArgs), kb_search,
)
DEFAULT_TOOL_REGISTRY.register(
    ToolMeta("order_lookup", "订单查询", "read", schema=OrderLookupArgs), order_lookup,
)
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_tools.py -q`
Expected: PASS（含既有分层允许集 / 属主边界断言不变）

- [ ] **Step 5: Commit**

```bash
git add src/ai_service_agent/tools/kb.py src/ai_service_agent/tools/orders.py src/ai_service_agent/tools/__init__.py tests/test_tools.py
git commit -m "feat: P5 D1 只读工具补 select 侧 schema（B1）——order_lookup/kb_search 注册表元数据完备"
```

### Task 2: B2b 写图 select 档内严格 + tier_mismatch 澄清（D2）

**Files:**
- Modify: `src/ai_service_agent/graph/workflows/select.py`
- Modify: `src/ai_service_agent/graph/workflows/clarify.py`
- Modify: `src/ai_service_agent/graph/workflows/mutation_agent.py`、`src/ai_service_agent/graph/workflows/high_risk_agent.py`（`_select_target`）
- Test: Modify `tests/test_select.py`、`tests/test_clarify.py`

**Interfaces:**
- Consumes: `make_select_node(model, tier, tools, registry)`（现有签名）、`RISK_ORDER`（`safety.py` 模块级，复用避免重复定义）
- Produces:
  - select 节点：写图允许集 = `min_risk_level == tier` 的写工具；选中已注册低档工具 → `context["tier_clarify"]=<tool_name>`（不落 `pending_tool`）；完整 select 时清 `tier_clarify` 与 `clarify_missing`
  - `clarify.make_clarify_node()`：读 `tier_clarify` → 发 `clarification_required`（`{"missing": [], "reason": "tier_mismatch"}`）+ `TIER_CLARIFY_TEXT`；`execution_status=clarifying`、`clarify_count` 自增、>N fallback
  - mutation/high_risk `_select_target`：`tier_clarify` 存在 → `"clarify"`

- [ ] **Step 1: 写失败测试**

`tests/test_select.py` 追加：

```python
def test_select_system_template_tier_strict_no_read_tools():
    """B2b：写图 select 系统提示只列该档写工具，只读工具不进入允许集。"""
    model, node = _select_node()
    node({"messages": [HumanMessage(content="改一下收货地址")], "context": {}})
    assert "account_update" in model.seen_system
    assert "kb_search" not in model.seen_system
    assert "order_lookup" not in model.seen_system
```

`tests/test_clarify.py` 追加：

```python
def _tier_model(tool_name, risk):
    return ToolCallingFakeModel(responses=[
        router_json_message("order", risk, 0.9),
        select_json_message(tool_name, {}),
    ])


def test_highrisk_read_tool_selected_tier_clarifies():
    """B2b：high_risk 图内 fake select 只读工具 order_lookup -> tier_mismatch 澄清（普通轮），
    不进审批闸、无 interrupt。镜像真机冒烟 §7 Run 2 的漂移路径。"""
    out = _invoke(_graph(_tier_model("order_lookup", "high_risk")), "退了吧", "b2-h1")
    assert out["execution_status"] == "clarifying"
    assert not out.get("__interrupt__")
    assert out["context"].get("tier_clarify") == "order_lookup"
    assert "pending_tool" not in out["context"]
    assert "请明确" in out["messages"][-1].content


def test_mutation_read_tool_selected_tier_clarifies():
    """B2b：mutation 图内 fake select 只读工具 kb_search -> tier_mismatch 澄清。"""
    out = _invoke(_graph(_tier_model("kb_search", "mutation")), "先查一下再改", "b2-m1")
    assert out["execution_status"] == "clarifying"
    assert out["context"].get("tier_clarify") == "kb_search"
    assert not out.get("__interrupt__")


def test_tier_clarify_streams_event_with_reason():
    """B2b：事件契约——clarification_required 带 reason=tier_mismatch，末事件 done。"""
    events = asyncio.run(_acollect(event_stream(
        _graph(_tier_model("order_lookup", "high_risk")),
        {"configurable": {"thread_id": "b2-s1"}},
        [HumanMessage(content="退了吧")],
    )))
    types = [e["type"] for e in events]
    assert "clarification_required" in types
    clar = next(e for e in events if e["type"] == "clarification_required")
    assert clar["data"].get("reason") == "tier_mismatch"
    assert clar["data"]["missing"] == []
    assert types[-1] == "done"
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_select.py tests/test_clarify.py -q`
Expected: FAIL（`kb_search` 仍在提示中 / 无 `tier_clarify`、直接进确认/审批闸）

- [ ] **Step 3: 最小实现**

`src/ai_service_agent/graph/workflows/select.py`：顶部 import 增加

```python
from ai_service_agent.graph.workflows.safety import RISK_ORDER
```

`make_select_node` 开头改为（允许集/提示只含本档工具）：

```python
def make_select_node(model, tier: str, tools: list, registry: ToolRegistry) -> Callable[[dict], dict]:
    # B2b（P5 D2）：写图 select 档内严格——允许集与系统提示仅含 min_risk_level == tier 的工具；
    # 只读工具不进入写图 select（ToolNode 分层注入不变，select 是写图唯一工具选择闸）。
    tier_tools = [
        t for t in tools
        if (meta := registry.meta_for(t.name)) is not None and meta.min_risk_level == tier
    ]
    allowed = {t.name for t in tier_tools}
    lines = []
    for t in tier_tools:
        line = f"- {t.name}: {t.description}"
        contract = _describe_args_schema(registry.meta_for(t.name))
        if contract:
            line += f"\n  参数：{contract}"
        lines.append(line)
    tool_list = "\n".join(lines)
    bound = _bind_json(model)
```

在 `_clarify` 之后新增：

```python
    def _tier_clarify(state: dict, tool_name: str) -> dict:
        # B2b：已注册低档工具在写图内被选中 = 意图与风险档不匹配 -> 澄清（普通轮）。
        # 不 reject（工具真实存在）、不进闸门、不执行。
        return {"context": {**state.get("context", {}), "tier_clarify": tool_name}}
```

`select()` 内工具档校验段改为：

```python
        meta = registry.meta_for(tool_name) if isinstance(tool_name, str) else None
        if meta is None:
            return _reject(state, "unknown_tool")
        if tool_name not in allowed:
            if RISK_ORDER[meta.min_risk_level] < RISK_ORDER[tier]:
                return _tier_clarify(state, tool_name)
            return _reject(state, "unknown_tool")
```

完整 select 的清理段（`ctx.pop("clarify_missing", None)` 之后）增加一行：

```python
        ctx.pop("tier_clarify", None)
```

`src/ai_service_agent/graph/workflows/clarify.py`：

```python
TIER_CLARIFY_TEXT = "您的请求涉及较高风险操作，但我无法确定具体要执行哪一项业务（如：申请退款、取消订单），请明确说明。"
```

`FIELD_LABELS` 增加 `"text": "检索内容"`。`make_clarify_node` 的 `clarify` 主体改为：

```python
    def clarify(state: dict) -> dict:
        ctx = state.get("context", {})
        count = int(ctx.get("clarify_count", 0))
        max_turns = get_settings().clarify_max_turns
        if count >= max_turns:
            return {
                "messages": [AIMessage(content=CLARIFY_FALLBACK_TEXT)],
                "context": {**ctx, "clarify_count": count + 1},
                "execution_status": "completed",
            }
        if ctx.get("tier_clarify"):
            data = {"missing": [], "reason": "tier_mismatch"}
            text = TIER_CLARIFY_TEXT
        else:
            missing = ctx.get("clarify_missing") or []
            data = {"missing": missing}
            text = clarify_text(missing)
        get_stream_writer()({"type": "clarification_required", "data": data})
        return {
            "messages": [AIMessage(content=text)],
            "context": {**ctx, "clarify_count": count + 1},
            "execution_status": "clarifying",
        }
```

`mutation_agent.py` 与 `high_risk_agent.py` 的 `_select_target` 首行判断改为：

```python
        if "clarify_missing" in ctx or ctx.get("tier_clarify"):
            return "clarify"
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_select.py tests/test_clarify.py tests/test_mutation_graph.py tests/test_high_risk_graph.py -q`
Expected: PASS（含既有"缺必填→澄清"、unknown_tool 拒绝、确认/审批闸用例全部保持）

- [ ] **Step 5: Commit**

```bash
git add src/ai_service_agent/graph/workflows/select.py src/ai_service_agent/graph/workflows/clarify.py src/ai_service_agent/graph/workflows/mutation_agent.py src/ai_service_agent/graph/workflows/high_risk_agent.py tests/test_select.py tests/test_clarify.py
git commit -m "feat: P5 D2 写图 select 档内严格（B2b）——低档工具选中走 tier_mismatch 澄清"
```

### Task 3: Spec / 决策文档同步（D1/D2 契约登记）

**Files:**
- Create: `docs/decisions/2026-09-16-p5-select-tier-alignment.md`
- Modify: `docs/specs/tool-spec.md`、`docs/specs/graph-spec.md`、`docs/specs/state-spec.md`、`docs/specs/event-spec.md`

**Interfaces:** 无代码接口；把 B1/B2b 契约（schema、`tier_clarify`、`reason` 字段）登记到唯一权威文档。

- [ ] **Step 1: 创建决策记录**

写入 `docs/decisions/2026-09-16-p5-select-tier-alignment.md`：

```markdown
# P5 Select 工具风险对齐决策（2026-09-16）

- 状态：已审核通过（D1–D3 APPROVED，D4 OBSERVE ONLY）
- 关联：`docs/real-model-smoke.md` §7（真机漂移证据）· `docs/specs/tool-spec.md` §1/§4 ·
  `docs/specs/graph-spec.md` §6.2/§6.3/§9 · `docs/specs/state-spec.md` §1 · `docs/specs/event-spec.md` §2 ·
  `docs/decisions/2026-09-16-ctrl-0076-clarify-verification.md`

## 背景
- 真机冒烟（§7）两次运行：真实 ARK 对"退了吧"稳定选择只读工具 order_lookup；无 ToolMeta.schema →
  D1 参数完备性不触发 → Run 1 mutation 确认闸、Run 2 high_risk 对只读操作发起审批（不该发生的审批流）。
- 根因：只读工具无 schema + `for_risk_level` 分层把只读工具放进写图 select 允许集；select 的
  schema 校验与 D1 澄清整体包在 `if meta.schema is not None` 内。
- 写图为单工具显式 Workflow：select 锁定唯一工具 → 预检 → interrupt 确认/审批 → 执行；无"先查后写"
  多步循环，只读工具在写图 select 内无正当用例（查订单事实由确定性 precheck_fetch 节点提供）。

## 决策
- **D1（B1，采纳）**：只读工具补 select 侧 schema——`order_lookup`=`{order_id, request_id}`、
  `kb_search`=`{text, request_id}`（`request_id` 由 select 注入、非模型输出；`user_id` 为 InjectedState、
  不入 schema）。作用：注册表元数据完备（tool-spec §1 预检用）+ safety 预检对只读工具获得 args 白名单
  （纵深防御）。ReadGraph 消费工具函数签名，不读 ToolMeta.schema，零影响。
- **D2（B2b，采纳）**：写图 select 档内严格——允许集与系统提示仅含 `min_risk_level == tier` 的写工具；
  选中已注册低档工具 → `context["tier_clarify"]` → clarify 节点（普通轮，`clarification_required` +
  `reason:"tier_mismatch"`，`execution_status=clarifying`，计 `clarify_count`、>N=2 fallback）；
  未注册 / 高档工具仍 `unknown_tool` 拒绝。ToolNode 工具注入保持分层（tool-spec §1），select 是写图唯一
  工具选择闸。同步修订 tool-spec / graph-spec / state-spec / event-spec。
- **D3（采纳）**：离线回归（新增只读工具选中的 tier 澄清断言，fake 对齐真实 json_mode 路径）+ 真机 ARK
  探针复验"退了吧"（`clarification_required` 出现、`approval_required` 不出现），更新 `docs/real-model-smoke.md` §7。
- **D4（OBSERVE ONLY）**：Guard LLM 评估 / Approval Service SQLite→Postgres 迁移预研 / Memory 子计划，
  维持登记排队，不进入本计划实现。

## 影响评估
- 现有测试：写图 fake 均选写工具（refund_apply / order_cancel / account_update），档内严格不影响；
  eval master.jsonl 零改动（CTRL-0076 `expected_policy=clarify` 不变，runner scripted select 均写工具）；
  预期 138 + 6 新增用例全绿。
- 残余风险（D3 关联，维持登记）：写工具必填参数来源校验仍未实现（模型可能编造 order_id 导致 select 放行），
  不属于本计划范围。
```

- [ ] **Step 2: 修订四个 spec**

1. `docs/specs/tool-spec.md` §1：分层注入三条之后补一句；§4 表格下方补 B1 注记。

```markdown
- 写图 select 节点允许集为**档内严格**（P5 B2b）：仅 `min_risk_level == tier` 的写工具可被选中；
  只读工具不进入写图 select 允许集与系统提示，若模型仍选中已注册低档工具 → `tier_clarify` 澄清
  （普通轮，见 graph-spec.md §6.2/§6.3），未知工具仍拒绝。ToolNode 工具注入保持分层。

- **P5（B1）**：只读工具补 select 侧 schema（`kb_search`=`{text, request_id}`、
  `order_lookup`=`{order_id, request_id}`；`request_id` 由 select 注入，`user_id` 经 InjectedState 注入不入 schema），
  注册表元数据完备（预检用）。
```

2. `docs/specs/graph-spec.md`：§6.2（mutation）与 §6.3（high_risk）的 select 描述处各补一句：

```markdown
写图 select 允许集为档内严格（P5 B2b）：只读工具不可选；选中已注册低档工具 → `tier_clarify` →
clarify 节点（普通轮，`clarification_required` + `reason:"tier_mismatch"`，`execution_status=clarifying`）。
```

§9 状态表两行改为：

```markdown
| select（mutation/high_risk） | `messages`、`context`（`clarify_count`） | `context`（`pending_tool` 或 `clarify_missing` / `tier_clarify`；完整 select 时清零 `clarify_count`） |
| clarify | `context`（`clarify_missing` / `tier_clarify`、`clarify_count`） | `messages`（确定性模板文案）、`context`（`clarify_count` 自增）、`execution_status=clarifying`，并发出 `clarification_required` |
```

3. `docs/specs/state-spec.md` §1：`context` 业务键列表补一项。

```markdown
- `tier_clarify`（P5 B2b 登记）：写图 select 选中已注册低档工具时的意图-档不匹配标记，值为工具名；
  由 clarify 节点消费，完整 select 时清除。
```

4. `docs/specs/event-spec.md` §2：`clarification_required` 数据形状改为。

```markdown
| `clarification_required` | `{missing: [...], reason?: "tier_mismatch"}` | select 参数不完整 / 低档工具档不匹配（tier_mismatch）→ clarify 节点（普通轮、非 interrupt，D1/B2b，见 graph-spec.md §6.2/§6.3） |
```

- [ ] **Step 3: 一致性核验**

Run: `rg -n "tier_clarify|tier_mismatch" docs/specs docs/decisions`
Expected: 四处 spec + 决策记录均有登记；无代码测试。

- [ ] **Step 4: Commit**

```bash
git add docs/decisions/2026-09-16-p5-select-tier-alignment.md docs/specs/tool-spec.md docs/specs/graph-spec.md docs/specs/state-spec.md docs/specs/event-spec.md
git commit -m "docs: P5 D1/D2 spec 同步——tool-spec/graph-spec/state-spec/event-spec + 决策记录"
```

### Task 4: Regression（全量 + eval + validate）

**Files:** 无新增；验证门禁。

- [ ] **Step 1: 全量测试**

Run: `pytest -q`
Expected: PASS（138 + Task 1 的 2 + Task 2 的 4 = 144 全绿）

- [ ] **Step 2: 离线 eval**

Run: `python -m ai_service_agent.evals`
Expected: policy 76 / guard 100 / routing 91 / handoff 91（基线不变，`handoff_rule_pending` 跳过 9 条）。
若 routing 变化，先定位是否为 scripted select 受影响，报告现象不自行扩范围。

- [ ] **Step 3: langgraph validate**

Run: `langgraph validate`（仓库根目录，全局 CLI 0.4.31；venv 内无 CLI）
Expected: valid

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "test: P5 regression——全量 pytest + eval + langgraph validate"
```

### Task 5: ARK 真机探针复验（D3）

**Files:**
- Modify: `docs/real-model-smoke.md`（§7 结果更新）
- Modify（如需）: `scripts/real_model_http_smoke.py`（探针断言若需强化）

- [ ] **Step 1: 前置确认**

离线全量全绿（Task 4）；`.env` 已配置 `ARK_MODEL` / `ARK_BASE_URL` / `ARK_API_KEY`。

- [ ] **Step 2: 运行冒烟**

Run: `python scripts/real_model_http_smoke.py`
（脚本每次运行使用全新临时 SQLite DB，可重复执行；关注 Clarify0076 行，其余四场景作为回归对照）

- [ ] **Step 3: 判据（D3）**

Clarify0076 行 PASS = SSE 含 `clarification_required`、不含 `approval_required`、`thread_id` 一致。
真实模型若选 `order_lookup`（→ tier_mismatch 澄清）或 `refund_apply` 空参（→ 字段澄清）均视为通过；
若 FAIL，记录事件时间线与根因层（Transport / Runtime / Graph / Checkpoint），不扩范围，报告后处理。

- [ ] **Step 4: 更新冒烟文档**

`docs/real-model-smoke.md` §7 重写为复验结果：新事件时间线、结论、四场景回归对照。

- [ ] **Step 5: Commit**

```bash
git add docs/real-model-smoke.md scripts/real_model_http_smoke.py
git commit -m "docs+test: P5 D3 真机探针复验——Clarify0076 结果更新"
```
