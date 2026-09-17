# Mutation 工具集重构实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** MutationGraph 写工具集从单一 `account_update` 重构为 5 个聚焦工具（`update_address` / `update_phone` / `update_order_note` / `create_invoice` / `create_service_ticket`）；`order_cancel` 重命名为 `cancel_order` 并留在 high_risk 档（与 `refund_apply` 并列）；同步 select 提示词（动态示例 + 禁止编造）、预检规则、测试/eval/冒烟与文档。

**Architecture:** 工具定义集中在 `tools/write.py`（内存 fake store + `request_id` 幂等），经 `tools/__init__.py` 按 `min_risk_level` 注册进 `ToolRegistry`；mutation/high_risk 子图 select 节点档内严格注入允许集（P5 B2b），提示词模板 `SELECT_SYSTEM_TEMPLATE` 增加 `{examples}` 占位符，由 `make_select_node` 按档位动态生成该档工具的真实示例（含缺参示例）。

**Tech Stack:** Python 3.14 · LangGraph 1.2.10 · langchain-core 1.5.3 · pydantic v2 · pytest

**Spec:** `docs/specs/tool-spec.md`（§2 幂等、§4 工具清单）、`docs/specs/graph-spec.md`（§6.2/§6.3 select 与澄清）、`docs/specs/safety-spec.md`（预检）

## Global Constraints

- LangGraph 1.2.10；禁止 `langgraph.prebuilt.create_react_agent`（统一 `langchain.agents.create_agent`）。
- 写工具必须 `idempotent=True` + 参数带 `request_id`（tool-spec §2，防重放）。
- 工具 schema `model_config = ConfigDict(extra="forbid")`（safety-spec §1.2 白名单）。
- 写图 select 允许集档内严格（P5 B2b）：仅 `min_risk_level == tier` 写工具进 select 允许集与系统提示；只读工具不进入写图 select。
- 参数必须来自用户原话；提示词明确"缺失省略、严禁编造"（缓解 D3，不构成安全控制）。
- 测试执行前缀：`$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest ...`
- TDD：每任务先写失败测试（红）→ 最小实现（绿）→ commit。沙箱拦截 git 写操作，commit 命令由用户手动执行（每任务末尾给出）。
- json_mode 契约不变（Router/Select 仍 `bind(response_format={"type":"json_object"})`；fake model 输出结构一致）。
- 不改动未请求模块（Router 提示词、Guard、ReadGraph 不动）。

---

## 新工具集定案（已由用户确认 Q1-Q3）

**mutation 档（5 工具）**
- `update_address(address: str)` — 修改收货地址
- `update_phone(phone: str)` — 修改联系电话
- `update_order_note(note: str)` — 修改订单备注
- `create_invoice(order_id: str, amount: float, company: str, tax_no: str)` — 开具发票（金额/公司名/税号/订单号全必填）
- `create_service_ticket(order_id: str, reason: str)` — 创建售后工单

**high_risk 档（2 工具）**
- `cancel_order(order_id: str, reason: str | None = None)` — 原 `order_cancel` 重命名，仅 high_risk 档
- `refund_apply(order_id: str, amount: float | None = None, reason: str | None = None)` — 不变

所有 schema 带 `request_id`（select 注入幂等键）。

---

### Task 1: 写工具定义与注册重构

**Files:**
- Modify: `src/ai_service_agent/tools/write.py`（重写工具集）
- Modify: `src/ai_service_agent/tools/__init__.py`（注册）
- Test: `tests/test_tools.py`

**Interfaces:**
- Consumes: 现有 `ToolRegistry` / `ToolMeta`（`tools/registry.py` 不变）。
- Produces: 工具函数（签名见"新工具集定案"）+ 各 Args schema（pydantic，`extra="forbid"`，含 `request_id`）；`DEFAULT_TOOL_REGISTRY.for_risk_level("mutation")` 返回 5 个新写工具 + read 档工具，`for_risk_level("high_risk")` 返回 `cancel_order`/`refund_apply` + 低档工具。

- [ ] **Step 1: 写失败测试**（更新 `tests/test_tools.py`）

```python
def test_registry_risk_sets_v2():
    reg = DEFAULT_TOOL_REGISTRY
    read = {t.name for t in reg.for_risk_level("read")}
    mutation = {t.name for t in reg.for_risk_level("mutation")}
    high = {t.name for t in reg.for_risk_level("high_risk")}
    assert read == {"kb_search", "order_lookup"}
    assert mutation == read | {"update_address", "update_phone", "update_order_note",
                               "create_invoice", "create_service_ticket"}
    assert high == mutation | {"cancel_order", "refund_apply"}
    assert "account_update" not in mutation
    assert "order_cancel" not in high


def test_new_tools_idempotent_by_request_id():
    r1 = update_address.invoke({"address": "北京市朝阳区", "request_id": "rq-a"})
    r2 = update_address.invoke({"address": "北京市朝阳区", "request_id": "rq-a"})
    r3 = update_address.invoke({"address": "上海市浦东", "request_id": "rq-b"})
    assert r1["ok"] and not r1["replayed"]
    assert r2["replayed"] and r2["result"] == r1["result"]
    assert not r3["replayed"] and r3["result"]["address"] == "上海市浦东"


def test_cancel_order_meta_is_high_risk():
    meta = DEFAULT_TOOL_REGISTRY.meta_for("cancel_order")
    assert meta is not None and meta.min_risk_level == "high_risk" and meta.idempotent
```

> 现有用例同步适配（同一 Step 1 内完成）：`import account_update, order_cancel` 改为新工具 import；自建 `reg.register` 的 `account_update`→`update_address`、`order_cancel`→`cancel_order`；集合断言 mutation = read | {update_address, update_phone, update_order_note, create_invoice, create_service_ticket}、high_risk = mutation | {cancel_order, refund_apply}；幂等测试 `account_update.invoke`→`update_address.invoke`；`meta_for("order_cancel")`→`meta_for("cancel_order")`。

- [ ] **Step 2: 运行确认红**

Run: `pytest tests/test_tools.py -q`
Expected: FAIL（`account_update` 仍在、新工具未注册、`ImportError: cannot import name 'update_address'`）。

- [ ] **Step 3: 实现**——重写 `src/ai_service_agent/tools/write.py`

```python
"""写工具 stub：mutation 档（update_address/update_phone/update_order_note/create_invoice/create_service_ticket）
+ high_risk 档（cancel_order/refund_apply）。内存 fake store，幂等（request_id）。"""

from uuid import uuid4

from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict


class UpdateAddressArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    address: str
    request_id: str


class UpdatePhoneArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phone: str
    request_id: str


class UpdateOrderNoteArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str
    request_id: str


class CreateInvoiceArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: str
    amount: float
    company: str
    tax_no: str
    request_id: str


class CreateServiceTicketArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: str
    reason: str
    request_id: str


class CancelOrderArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: str
    reason: str | None = None
    request_id: str


class RefundApplyArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: str
    amount: float | None = None
    reason: str | None = None
    request_id: str


_WRITE_STORE: dict[str, dict] = {}


def _run_idempotent(key: str, produce) -> dict:
    """幂等键（tool-spec §2）：同 request_id 返回既有结果，防重放。"""
    if key in _WRITE_STORE:
        return {"ok": True, "replayed": True, "result": _WRITE_STORE[key]}
    result = produce()
    _WRITE_STORE[key] = result
    return {"ok": True, "replayed": False, "result": result}


@tool
def update_address(address: str, request_id: str = "") -> dict:
    """修改收货地址（mutation stub，幂等）。"""
    return _run_idempotent(request_id or str(uuid4()), lambda: {"field": "update_address", "address": address})


@tool
def update_phone(phone: str, request_id: str = "") -> dict:
    """修改联系电话（mutation stub，幂等）。"""
    return _run_idempotent(request_id or str(uuid4()), lambda: {"field": "update_phone", "phone": phone})


@tool
def update_order_note(note: str, request_id: str = "") -> dict:
    """修改订单备注（mutation stub，幂等）。"""
    return _run_idempotent(request_id or str(uuid4()), lambda: {"field": "update_order_note", "note": note})


@tool
def create_invoice(order_id: str, amount: float, company: str, tax_no: str, request_id: str = "") -> dict:
    """开具发票（mutation stub，幂等）。"""
    def _produce() -> dict:
        return {"field": "create_invoice", "order_id": order_id, "amount": amount, "company": company, "tax_no": tax_no}
    return _run_idempotent(request_id or str(uuid4()), _produce)


@tool
def create_service_ticket(order_id: str, reason: str, request_id: str = "") -> dict:
    """创建售后工单（mutation stub，幂等）。"""
    def _produce() -> dict:
        return {"field": "create_service_ticket", "order_id": order_id, "reason": reason}
    return _run_idempotent(request_id or str(uuid4()), _produce)


@tool
def cancel_order(order_id: str, reason: str | None = None, request_id: str = "") -> dict:
    """取消订单（high_risk stub，需人工审批，幂等）。"""
    def _produce() -> dict:
        return {"field": "cancel_order", "order_id": order_id, "reason": reason}
    return _run_idempotent(request_id or str(uuid4()), _produce)


@tool
def refund_apply(order_id: str, amount: float | None = None, reason: str | None = None, request_id: str = "") -> dict:
    """退款申请（high_risk stub，需人工审批，幂等）。"""
    def _produce() -> dict:
        return {"field": "refund_apply", "order_id": order_id, "amount": amount, "reason": reason}
    return _run_idempotent(request_id or str(uuid4()), _produce)
```

- [ ] **Step 4: 注册**——重写 `src/ai_service_agent/tools/__init__.py` 的 import 与注册段

```python
from ai_service_agent.tools.kb import KbSearchArgs, kb_search
from ai_service_agent.tools.orders import OrderLookupArgs, order_lookup
from ai_service_agent.tools.registry import ToolMeta, ToolRegistry
from ai_service_agent.tools.write import (
    CancelOrderArgs,
    CreateInvoiceArgs,
    CreateServiceTicketArgs,
    RefundApplyArgs,
    UpdateAddressArgs,
    UpdateOrderNoteArgs,
    UpdatePhoneArgs,
    cancel_order,
    create_invoice,
    create_service_ticket,
    refund_apply,
    update_address,
    update_order_note,
    update_phone,
)

DEFAULT_TOOL_REGISTRY = ToolRegistry()
DEFAULT_TOOL_REGISTRY.register(
    ToolMeta("kb_search", "知识库检索", "read", schema=KbSearchArgs), kb_search,
)
DEFAULT_TOOL_REGISTRY.register(
    ToolMeta("order_lookup", "订单查询", "read", schema=OrderLookupArgs), order_lookup,
)
# mutation 档：5 个写工具
DEFAULT_TOOL_REGISTRY.register(
    ToolMeta("update_address", "修改收货地址", "mutation", schema=UpdateAddressArgs, idempotent=True), update_address,
)
DEFAULT_TOOL_REGISTRY.register(
    ToolMeta("update_phone", "修改联系电话", "mutation", schema=UpdatePhoneArgs, idempotent=True), update_phone,
)
DEFAULT_TOOL_REGISTRY.register(
    ToolMeta("update_order_note", "修改订单备注", "mutation", schema=UpdateOrderNoteArgs, idempotent=True), update_order_note,
)
DEFAULT_TOOL_REGISTRY.register(
    ToolMeta("create_invoice", "开具发票", "mutation", schema=CreateInvoiceArgs, idempotent=True), create_invoice,
)
DEFAULT_TOOL_REGISTRY.register(
    ToolMeta("create_service_ticket", "创建售后工单", "mutation", schema=CreateServiceTicketArgs, idempotent=True), create_service_ticket,
)
# high_risk 档：cancel_order（原 order_cancel 重命名）+ refund_apply
DEFAULT_TOOL_REGISTRY.register(
    ToolMeta("cancel_order", "取消订单", "high_risk", schema=CancelOrderArgs, idempotent=True), cancel_order,
)
DEFAULT_TOOL_REGISTRY.register(
    ToolMeta("refund_apply", "退款申请", "high_risk", schema=RefundApplyArgs, idempotent=True), refund_apply,
)
```

- [ ] **Step 5: 运行确认绿**

Run: `pytest tests/test_tools.py -q`
Expected: PASS。

- [ ] **Step 6: Commit**

```powershell
cd "D:\AI_Project\AI Service Agent"
git add src/ai_service_agent/tools/write.py src/ai_service_agent/tools/__init__.py tests/test_tools.py
git commit -m "feat: 写工具集重构——mutation 档 5 工具 + cancel_order 重命名留 high_risk（幂等/extra=forbid）"
```

---

### Task 2: Clarify 字段标签扩展

**Files:**
- Modify: `src/ai_service_agent/graph/workflows/clarify.py`（FIELD_LABELS）
- Test: `tests/test_clarify.py`（新增两条断言）

**Interfaces:**
- Consumes: Task 1 注册的新工具 schema。
- Produces: `FIELD_LABELS` 覆盖 `company`/`tax_no`/`note`，使缺参澄清文案可读（"请提供：订单号、金额、公司名、税号。"）。

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_clarify.py`）

```python
def test_clarify_labels_invoice_fields():
    from ai_service_agent.graph.workflows.clarify import clarify_text
    assert clarify_text(["order_id", "amount", "company", "tax_no"]) == "请提供：订单号、金额、公司名、税号。"
    assert clarify_text(["note"]) == "请提供：订单备注。"
```

- [ ] **Step 2: 运行确认红**

Run: `pytest tests/test_clarify.py::test_clarify_labels_invoice_fields -q`
Expected: FAIL（`company`/`tax_no`/`note` 无映射，回落原字段名）。

- [ ] **Step 3: 实现**——`clarify.py` 的 `FIELD_LABELS` 增加 3 项

```python
FIELD_LABELS = {
    "order_id": "订单号",
    "amount": "金额",
    "reason": "原因",
    "phone": "手机号",
    "email": "邮箱",
    "address": "收货地址",
    "text": "检索内容",
    "company": "公司名",
    "tax_no": "税号",
    "note": "订单备注",
}
```

- [ ] **Step 4: 运行确认绿**

Run: `pytest tests/test_clarify.py -q`
Expected: PASS。

- [ ] **Step 5: Commit**

```powershell
git add src/ai_service_agent/graph/workflows/clarify.py tests/test_clarify.py
git commit -m "feat: clarify 字段标签扩展（company/tax_no/note）支持新工具缺参文案"
```

---

### Task 3: Select 提示词模板（动态示例 + 禁止编造）

**Files:**
- Modify: `src/ai_service_agent/prompts.py`（`SELECT_SYSTEM_TEMPLATE`）
- Modify: `src/ai_service_agent/graph/workflows/select.py`（`_build_examples` + format 调用）
- Test: `tests/test_select.py`

**Interfaces:**
- Consumes: Task 1 的工具注册；`_describe_args_schema(meta)`（现有函数，不变）。
- Produces: `SELECT_SYSTEM_TEMPLATE` 支持 `{tool_list}` 与 `{examples}` 两个占位符；`make_select_node` 按 `tier` 生成该档工具示例（每工具一个真实示例 + 一个缺参示例）。

- [ ] **Step 1: 写失败测试**（更新 `tests/test_select.py`）

```python
class RecordingSelectModel(ToolCallingFakeModel):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        object.__setattr__(self, "seen_system", next(
            (m.content for m in messages if getattr(m, "type", "") == "system"), ""
        ))
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def _select_node():
    model = RecordingSelectModel(responses=[
        select_json_message("update_address", {"address": "朝阳区"}),
    ])
    node = make_select_node(
        model, "mutation", DEFAULT_TOOL_REGISTRY.for_risk_level("mutation"), DEFAULT_TOOL_REGISTRY
    )
    return model, node


def test_select_system_template_carries_new_tools_and_schema():
    model, node = _select_node()
    node({"messages": [HumanMessage(content="改一下收货地址")], "context": {}})
    assert "update_address" in model.seen_system
    assert "create_invoice" in model.seen_system
    assert "address" in model.seen_system
    assert "company" in model.seen_system
    assert "account_update" not in model.seen_system


def test_select_system_template_forbids_extra_fields_and_fabrication():
    model, node = _select_node()
    node({"messages": [HumanMessage(content="改一下收货地址")], "context": {}})
    assert "不要输出 schema 之外的字段" in model.seen_system
    assert "严禁编造" in model.seen_system


def test_select_system_template_examples_match_tier():
    model, node = _select_node()
    node({"messages": [HumanMessage(content="改一下收货地址")], "context": {}})
    assert '"tool": "update_address"' in model.seen_system
    assert '"tool": "refund_apply"' not in model.seen_system   # 跨档示例不注入
    assert "参数不完整" in model.seen_system                     # 缺参示例
```

> 现有 4 个 select 测试同步适配：`test_select_system_template_carries_schema_field_contract` 断言 `address`/`phone` 保留（update_address/update_phone 参数仍在提示词）；`test_select_system_template_has_example` 的 `assert "account_update" in model.seen_system`→`"update_address"`；`test_select_system_template_tier_strict_no_read_tools` 的 `"account_update"`→`"update_address"`；`test_select_system_template_forbids_extra_fields` 不变。

- [ ] **Step 2: 运行确认红**

Run: `pytest tests/test_select.py -q`
Expected: FAIL（模板无"严禁编造"、无动态示例；`account_update` 仍在）。

- [ ] **Step 3: 实现模板**——`prompts.py` 的 `SELECT_SYSTEM_TEMPLATE` 替换为

```python
SELECT_SYSTEM_TEMPLATE = """你是客服操作选择器。根据用户意图，从允许的工具中选择一个并给出符合其 schema 的参数。
只输出 JSON，形状为 {{"tool": "<工具名>", "args": {{...}}}}，不要输出其它内容（禁止 Markdown 代码块）。

允许的工具：
{tool_list}

规则：
- args 只能包含该工具 schema 列出的字段名（输出未知字段会被拒绝）。
- 参数必须来自用户原话；用户没有提供的参数直接省略或留空，严禁编造（不得把订单号写成 "unknown" 或虚构数字）。

示例：
{examples}
"""
```

- [ ] **Step 4: 实现动态示例**——`select.py` 增加 `_build_examples` 并在 `select()` 中传参

```python
def _build_examples(tier_tools: list, registry: ToolRegistry) -> str:
    """按档位生成 select 示例：每工具一个真实示例（必填字段占位）+ 一个缺参示例。"""
    parts = []
    for t in tier_tools:
        meta = registry.meta_for(t.name)
        if meta is None or meta.schema is None:
            continue
        required = [
            n for n in meta.schema.model_fields
            if n != "request_id" and meta.schema.model_fields[n].is_required()
        ]
        if not required:
            continue
        args = {n: f"{n}示例" for n in required}
        parts.append(
            f"用户：{t.description}，请执行\n"
            f"输出：{{\"tool\": \"{t.name}\", \"args\": {json.dumps(args, ensure_ascii=False)}}}"
        )
    if tier_tools:
        first = tier_tools[0]
        first_meta = registry.meta_for(first.name)
        parts.append(
            f"用户：{first_meta.description}，但参数不完整\n"
            f"输出：{{\"tool\": \"{first.name}\", \"args\": {{}}}}"
        )
    return "\n\n".join(parts)
```

`select()` 内调用处改为：

```python
resp = bound.invoke([
    SystemMessage(content=SELECT_SYSTEM_TEMPLATE.format(
        tool_list=tool_list,
        examples=_build_examples(tier_tools, registry),
    )),
    HumanMessage(content=last),
])
```

- [ ] **Step 5: 运行确认绿**

Run: `pytest tests/test_select.py -q`
Expected: PASS。

- [ ] **Step 6: Commit**

```powershell
git add src/ai_service_agent/prompts.py src/ai_service_agent/graph/workflows/select.py tests/test_select.py
git commit -m "feat: select 提示词模板——{examples} 按档动态注入 + 禁止编造硬约束"
```

---

### Task 4: Mutation 图与流式测试适配

**Files:**
- Modify: `tests/test_mutation_graph.py`（`account_update` → `update_address`，跨轮 clarify 断言）
- Modify: `tests/test_streaming.py`（回归测试适配新工具）

**Interfaces:**
- Consumes: Task 1 工具注册 + Task 3 提示词。
- Produces: 无新接口；图行为不变（缺参→澄清、补参→确认、confirm→执行一次）。

- [ ] **Step 1: 写失败测试**——更新 `tests/test_mutation_graph.py` 全部 `select_json_message("account_update", ...)` 为 `update_address`，`tool_msgs` 名称断言同步；跨轮测试中 turn1 空参 → `missing=["address"]`（不再 3 字段），断言 `"请提供：收货地址。"`。更新 `tests/test_streaming.py` 的 `test_stream_done_final_null_on_interrupt_turn`：

```python
def test_stream_done_final_null_on_interrupt_turn():
    from langgraph.checkpoint.memory import MemorySaver
    from ai_service_agent.testing import router_json_message, select_json_message

    model = ToolCallingFakeModel(responses=[
        router_json_message("account", "mutation", 0.9),
        select_json_message("update_address", {}),
        router_json_message("account", "mutation", 0.9),
        select_json_message("update_address", {"address": "北京市朝阳区"}),
    ])
    graph = build_graph(model, tools=[kb_search], checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "interrupt-null"}}
    extra = {"user_id": "u1", "session_id": "s1", "context": {}}

    async def on_interrupt(value):
        if isinstance(value, dict) and value.get("type") == "confirmation_required":
            return {"type": "confirmation_required", "data": value}
        return None

    ev1 = _collect(event_stream(graph, config, [HumanMessage(content="我要改个地址")], extra_state=extra))
    assert ev1[-1]["data"]["final"] == "请提供：收货地址。"

    ev2 = _collect(event_stream(
        graph, config, [HumanMessage(content="新地址是北京市朝阳区")],
        extra_state=extra, on_interrupt=on_interrupt,
    ))
    types2 = [e["type"] for e in ev2]
    assert "confirmation_required" in types2
    assert ev2[-1]["data"]["final"] is None
    assert ev2[-1]["data"]["message_id"] is None
```

- [ ] **Step 2: 运行确认红**

Run: `pytest tests/test_mutation_graph.py tests/test_streaming.py -q`
Expected: FAIL（工具名/summary/文案不匹配）。

- [ ] **Step 3: 适配实现**——仅改测试断言（工具名 `account_update`→`update_address`；`summary` 断言、`"请提供"` 文案断言同步）。若某断言依赖 3 字段 missing，改为 `["address"]`。无 src 改动（图逻辑不变）。

- [ ] **Step 4: 运行确认绿**

Run: `pytest tests/test_mutation_graph.py tests/test_streaming.py -q`
Expected: PASS。

- [ ] **Step 5: Commit**

```powershell
git add tests/test_mutation_graph.py tests/test_streaming.py
git commit -m "test: mutation 图与流式测试适配新工具集（update_address，missing 单字段）"
```

---

### Task 5: HighRisk 预检与测试适配

**Files:**
- Modify: `src/ai_service_agent/graph/workflows/safety.py:41`（`order_cancel` → `cancel_order`）
- Modify: `tests/test_high_risk_graph.py`（CTRL-0090 工具名）
- Test: `tests/test_high_risk_graph.py`

**Interfaces:**
- Consumes: Task 1 注册的 `cancel_order`。
- Produces: 预检规则 `order_already_cancelled` 对 `cancel_order` 生效。

- [ ] **Step 1: 写失败测试**——`tests/test_high_risk_graph.py` 中 `order_cancel` 相关用例改为 `cancel_order`；保留 `refund_apply` 用例不变；新增预检断言

```python
def test_precheck_cancel_order_already_cancelled():
    from ai_service_agent.graph.workflows.safety import safety_check
    reason = safety_check("cancel_order", {"status": "cancelled"}, {"user_id": "u1"})
    assert reason == "order_already_cancelled"
```

- [ ] **Step 2: 运行确认红**

Run: `pytest tests/test_high_risk_graph.py -q`
Expected: FAIL（`order_cancel` 不再注册；`safety_check("cancel_order", ...)` 返回 None 而非 `order_already_cancelled`）。

- [ ] **Step 3: 实现**——`safety.py` 内工具名判定改为

```python
if tool_name == "cancel_order" and precheck_data.get("status") == "cancelled":
    return "order_already_cancelled"
```

- [ ] **Step 4: 运行确认绿**

Run: `pytest tests/test_high_risk_graph.py -q`
Expected: PASS。

- [ ] **Step 5: Commit**

```powershell
git add src/ai_service_agent/graph/workflows/safety.py tests/test_high_risk_graph.py
git commit -m "fix: 预检规则同步 cancel_order 重命名（order_already_cancelled）"
```

---

### Task 6: Eval Runner 适配

**Files:**
- Modify: `src/ai_service_agent/evals/runner.py`（`DENY_TOOL_SCRIPT`、fake select 输出、risk 判定）
- Test: `tests/test_evals.py`

**Interfaces:**
- Consumes: Task 1 工具注册。
- Produces: eval 维度计数不变（76 policy / 91 routing），scripted 工具名对齐新工具集。

- [ ] **Step 1: 写失败测试**——先改 `runner.py` 依赖断言前确认：更新 `tests/test_evals.py` 若引用工具名（如无则跳过），跑全量 eval 验证

Run: `pytest tests/test_evals.py -q`
Expected: 改 runner 前 FAIL（`unknown_tool` 或 deny 断言因工具名不匹配）。

- [ ] **Step 2: 适配 runner**——`src/ai_service_agent/evals/runner.py`

```python
DENY_TOOL_SCRIPT = {
    "CTRL-0086": {"tool": "refund_apply", "args": {"order_id": "ORD-10086"}},
    "CTRL-0089": {"tool": "delete_order_history", "args": {"order_id": "ORD-10086"}},
    "CTRL-0090": {"tool": "cancel_order", "args": {"order_id": "ORD-CANCEL-1"}},
    "CTRL-0088": {"tool": "update_phone", "args": {"phone": "13800000000"}},
    "CTRL-0092": {"tool": "order_set_status", "args": {"order_id": "10086", "status": "shipped"}},
    "CTRL-0093": {"tool": "delete_repair_records", "args": {}},
    "CTRL-0094": {"tool": "internal_update_inventory", "args": {}},
    "CTRL-0095": {"tool": "update_address", "args": {"address": "新地址"}},
}
```

mutation fake 输出与 risk 判定行：

```python
select_json_message("update_phone", {"phone": "13800000000"})   # 原 account_update
risk = "high_risk" if script["tool"] in ("refund_apply", "cancel_order") else "mutation"
```

- [ ] **Step 3: 运行确认绿**

Run: `pytest tests/test_evals.py -q`
Expected: PASS（`test_policy_dimension_by_expected_policy` 等全部通过）。

- [ ] **Step 4: Commit**

```powershell
git add src/ai_service_agent/evals/runner.py tests/test_evals.py
git commit -m "fix: eval runner scripted 工具对齐新工具集（cancel_order/update_phone/update_address）"
```

---

### Task 7: 真机冒烟脚本适配

**Files:**
- Modify: `scripts/real_model_http_smoke.py`

**Interfaces:**
- Consumes: Task 1 工具注册。
- Produces: 冒烟场景覆盖新工具（`update_address`）；离线 `SMOKE_MODEL=fake` 可跑通。

- [ ] **Step 1: 写失败测试**——更新脚本三处：

```python
# SmokeFakeModel select 分支（约 line 50）
payload = '{"tool": "update_address", "args": {"address": "北京市朝阳区"}}'

# scenario_mutation：count_tool_events(ev2, "update_address")；tool_msgs name == "update_address"
# scenario_mutation_clarify_multi_turn：turn1 select_json_message -> update_address；断言 final == "请提供：收货地址。"
```

- [ ] **Step 2: 离线冒烟验证**

Run:
```powershell
$env:SMOKE_MODEL="fake"; $env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync python scripts/real_model_http_smoke.py
```
Expected: 全部 PASS（fake 路径）。

- [ ] **Step 3: Commit**

```powershell
git add scripts/real_model_http_smoke.py
git commit -m "test: 冒烟脚本适配新 mutation 工具（update_address）"
```

---

### Task 8: 全量回归 + validate + 文档

**Files:**
- Modify: `docs/specs/tool-spec.md`（§4 工具清单表）
- 回归：全量 pytest + `langgraph validate`

- [ ] **Step 1: 更新 `docs/specs/tool-spec.md` §4 表格**

```markdown
| `kb_search` | 知识库检索 | read | sqlite-vec / 关键词兜底 |
| `order_lookup` | 订单查询 | read | stub 客户端；**P4（D4）属主校验**：`user_id` 经 `langgraph.prebuilt.InjectedState` 注入，仅返回本人订单，越权拒绝 |
| `update_address` | 修改收货地址 | mutation | 幂等 |
| `update_phone` | 修改联系电话 | mutation | 幂等 |
| `update_order_note` | 修改订单备注 | mutation | 幂等 |
| `create_invoice` | 开具发票 | mutation | 幂等；order_id/amount/company/tax_no 必填 |
| `create_service_ticket` | 创建售后工单 | mutation | 幂等 |
| `cancel_order` | 取消订单 | high_risk | 需人工审批（原 `order_cancel` 重命名） |
| `refund_apply` | 退款申请 | high_risk | 需人工审批 |
```

- [ ] **Step 2: 全量回归**

Run: `$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest -q`
Expected: 全部 PASS（原 146 + 新增用例）。

- [ ] **Step 3: langgraph validate**

Run: `$env:PYTHONPATH="D:\AI_Project\AI Service Agent\src"; langgraph validate`
Expected: Configuration file valid（1 graph found）。

- [ ] **Step 4: Commit**

```powershell
git add docs/specs/tool-spec.md
git commit -m "docs: tool-spec §4 工具清单更新（mutation 5 工具 + cancel_order）"
```

---

## Self-Review 记录

- Spec 覆盖：tool-spec §2 幂等（Task 1）· §4 清单（Task 8）· B2b 档内严格（Task 3）· 预检衔接（Task 5）· 变更纪律（Task 8 文档）。✅
- 无占位符：所有步骤含实际代码/命令。✅
- 类型一致：`update_address(address: str, request_id: str = "") -> dict` 等签名在 Task 1 定义、Task 3/7 消费一致；`cancel_order` 在 Task 1/5/6/8 命名一致。✅
- 已知边界：`order_cancel` 的旧预检 reason（`order_already_cancelled`）随重命名同步；`refund_apply` 用例不动。eval master.jsonl 无工具名（已确认），无需改数据。
