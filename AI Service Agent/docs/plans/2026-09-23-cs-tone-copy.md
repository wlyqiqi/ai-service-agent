# Customer-Service Tone Copy Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 将 Mutation/HighRisk/Clarify/预检拒绝/Guard/Handoff 的用户面文案收敛为统一文案层，改为客服语气；同时新增地址修改与开票的状态业务规则，并修正退款规则与 tool-spec 的不一致。

**Architecture:** 新建 `graph/workflows/copy.py` 作为唯一文案出口（模板函数按工具/reason/状态出句）；`safety.py` 新增两条确定性业务规则并修正 `signed` 退款窗口语义；mutation / high_risk / clarify / guard / safety 节点改为消费 copy 层。SSE 事件类型与结构零变更，只改 `AIMessage` 文本与 summary 措辞。

**Tech Stack:** Python 3.14 / LangGraph 1.2.10 / pytest。零新增依赖。

**Spec:** `docs/specs/safety-spec.md` §1.2（业务规则）、`docs/specs/tool-spec.md`（工具状态机）、`docs/specs/graph-spec.md`（workflow 输出）、`docs/specs/event-spec.md`（事件结构不变）。

## Global Constraints

- 挂在循环上：文案层是纯函数模块，节点消费，不新增 hook / 循环侵入。
- 确定性不变：所有状态类输出仍为确定性模板，不引入 LLM 生成回复。
- SSE 事件协议不变：`confirmation_required` / `approval_required` / `safety_rejected` / `clarification_required` 的事件 `type` 与结构字段完全不变，只改文本内容。
- 状态语义词不可弱化：完成 / 取消 / 过期 / 拒绝等关键词在文案中必须保留。
- 拒绝文案必须包含「原因 + 补救建议」。
- Git 显式 `git add -- <files>`，禁止 `git add .`。
- 每任务定向测试；全量回归 + `langgraph validate` 仅最后执行。

## 已验证的代码事实（2026-09-23）

- 预检事实层状态归一化（`orders.py` `_DB_MACHINE_STATUS`）：DB 原生 `delivered` → 归一化 `signed`；`completed` / `shipped` / `paid` / `cancelled` / `refunding` / `refunded` 原样。文案函数的 `status` 入参即此归一化值。
- `safety.py:62` 现规则 `if status not in ("paid", "completed"): return "refund_not_allowed"`——导致 `signed`（签收 7 天内）被拒，与 tool-spec「paid/delivered 可退」矛盾（本计划修正）。
- `safety.py` 无 `update_address` / `create_invoice` 状态规则（本计划新增）。
- 文案精确断言分布：`test_clarify.py:67-68,234-235`（clarify_text 精确相等）、`test_mutation_graph.py:198,213`、`test_server.py:268,274,402`、`test_streaming.py:130,184`。`test_high_risk_graph.py:1045,1053` 断言 `"操作已完成" not in …`（保留有效）。
- `GUARD_REJECT_TEXT` 被 `builder.py` import（`_safety_final` 使用）；改为 `guard.py` 从 copy 层 import 并 re-export，builder 不动。
- `HANDOFF_NOTICE` 在 `harness/handoff.py`，仅文本内容可改。

---

### Task 1: copy.py 文案层（TDD）

**Files:**
- Create: `src/ai_service_agent/graph/workflows/copy.py`
- Test: `tests/test_copy.py`

**Interfaces:**
- Produces（后续任务消费的精确签名）:
  - `FIELD_LABELS: dict[str, str]`
  - `clarify_missing_text(missing: list[str]) -> str`
  - `clarify_candidates_text(candidates: list[dict]) -> str`
  - `clarify_no_candidates_text() -> str`
  - `clarify_tier_text() -> str`
  - `clarify_fallback_text() -> str`
  - `confirmation_summary(tool: str, args: dict) -> str`
  - `completion_text(tool: str) -> str`
  - `operation_cancelled_text() -> str`
  - `approval_approved_text(tool: str) -> str`
  - `approval_failed_text(tool: str, error: str) -> str`
  - `approval_rejected_text(tool: str) -> str`
  - `approval_expired_text(tool: str) -> str`
  - `recheck_failed_text() -> str`
  - `precheck_reject_text(reason: str, status: str | None = None) -> str`
  - `guard_reject_text() -> str`

- [x] **Step 1: 写失败测试**（创建 `tests/test_copy.py`）

```python
from ai_service_agent.graph.workflows.copy import (
    approval_approved_text,
    approval_expired_text,
    approval_failed_text,
    approval_rejected_text,
    clarify_candidates_text,
    clarify_fallback_text,
    clarify_missing_text,
    clarify_no_candidates_text,
    completion_text,
    confirmation_summary,
    guard_reject_text,
    operation_cancelled_text,
    precheck_reject_text,
    recheck_failed_text,
)


def test_clarify_missing_text_uses_labels():
    text = clarify_missing_text(["order_id", "amount"])
    assert "订单号" in text and "金额" in text
    assert "请" in text


def test_clarify_missing_text_unknown_field_falls_back():
    assert "自定义字段" in clarify_missing_text(["自定义字段"])


def test_clarify_candidates_text_lists_orders():
    candidates = [
        {"order_id": "ORD-1", "product_summary": "空气炸锅"},
        {"order_id": "ORD-2", "product_summary": "电饭煲"},
    ]
    text = clarify_candidates_text(candidates)
    assert "ORD-1" in text and "ORD-2" in text and "空气炸锅" in text
    assert "取消" in text or "哪一笔" in text


def test_clarify_fixed_texts_have_cs_tone():
    assert "转人工" in clarify_fallback_text()
    assert "暂不支持" in clarify_no_candidates_text() or "没有" in clarify_no_candidates_text()
    assert "退款" in clarify_tier_text() or "取消" in clarify_tier_text()


def test_confirmation_summary_keeps_facts():
    summary = confirmation_summary("update_address", {"order_id": "ORD-1", "address": "北京"})
    assert "update_address" in summary or "收货地址" in summary
    assert "ORD-1" in summary and "北京" in summary


def test_completion_text_per_tool():
    assert "收货地址" in completion_text("update_address")
    assert "发票" in completion_text("create_invoice")
    assert "工单" in completion_text("create_service_ticket")
    assert "已办理完成" in completion_text("unknown_tool")


def test_operation_cancelled_text():
    text = operation_cancelled_text()
    assert "取消" in text and "未" in text


def test_approval_texts_keep_status_keywords():
    assert "通过审核" in approval_approved_text("refund_apply")
    assert "未通过" in approval_rejected_text("refund_apply")
    assert "过期" in approval_expired_text("refund_apply") and "取消" in approval_expired_text("refund_apply")
    failed = approval_failed_text("refund_apply", "db_error")
    assert "遇到问题" in failed and "db_error" in failed and "人工" in failed


def test_recheck_failed_text():
    assert "中止" in recheck_failed_text() and "人工" in recheck_failed_text()


def test_guard_reject_text():
    assert "人工" in guard_reject_text()


def test_precheck_reject_address_by_status():
    shipped = precheck_reject_text("address_not_editable", "shipped")
    assert "已发货" in shipped and ("改派" in shipped or "退换" in shipped)
    signed = precheck_reject_text("address_not_editable", "signed")
    assert "已签收" in signed and "售后" in signed
    cancelled = precheck_reject_text("address_not_editable", "cancelled")
    assert "已取消" in cancelled


def test_precheck_reject_invoice_by_status():
    assert "无法开具发票" in precheck_reject_text("invoice_not_allowed", "cancelled")
    assert "无法开具发票" in precheck_reject_text("invoice_not_allowed", "refunded")
    assert "退款" in precheck_reject_text("invoice_not_allowed", "refunding")


def test_precheck_reject_refund_scenarios():
    assert "7 天" in precheck_reject_text("refund_window_expired")
    assert "质量" in precheck_reject_text("refund_window_expired")
    assert "已完成退款" in precheck_reject_text("order_already_refunded")
    assert "正在处理" in precheck_reject_text("order_already_refunding")
    assert "额度" in precheck_reject_text("refund_amount_exceeds")
    assert "暂不支持" in precheck_reject_text("refund_not_allowed")


def test_precheck_reject_generic_scenarios():
    assert "权限" in precheck_reject_text("data_boundary")
    assert "订单号" in precheck_reject_text("order_not_found")
    assert "转人工" in precheck_reject_text("data_source_unavailable")
    assert "转人工" in precheck_reject_text("unknown_reason")
```

- [x] **Step 2: 运行测试确认失败**

Run: `uv run --no-sync pytest tests/test_copy.py -q`
Expected: FAIL（`ModuleNotFoundError: ... copy`）

- [x] **Step 3: 实现 `copy.py`**

```python
"""面向用户的确定性文案层（客服语气）：承接 -> 结果 -> 下一步引导。

所有状态类固定文案集中此处；节点只消费模板函数。SSE 事件结构零变更，
状态关键词（完成/取消/过期/拒绝）不可弱化；拒绝文案必须含原因与补救建议。
"""


FIELD_LABELS = {
    "order_id": "订单号", "amount": "金额", "reason": "原因",
    "phone": "手机号", "email": "邮箱", "address": "收货地址",
    "text": "检索内容", "company": "公司名", "tax_no": "税号", "note": "订单备注",
}

_TOOL_ACTION_LABELS = {
    "update_address": "收货地址修改", "update_phone": "联系电话修改",
    "update_order_note": "订单备注修改", "create_invoice": "发票开具",
    "create_service_ticket": "售后工单创建", "cancel_order": "订单取消",
    "refund_apply": "退款申请",
}

_COMPLETION_TEXTS = {
    "update_address": "好的，您的收货地址已更新完成，后续包裹将按新地址配送。",
    "update_phone": "好的，您的联系电话已更新，后续通知将发送到新号码。",
    "update_order_note": "好的，订单备注已更新。",
    "create_invoice": "好的，发票已为您开具，您可以在订单详情中下载查看。",
    "create_service_ticket": "好的，售后工单已创建，工程师会尽快与您联系，请保持电话畅通。",
    "cancel_order": "好的，订单已成功取消，退款将按原支付路径退回，请留意到账通知。",
    "refund_apply": "好的，退款申请已提交成功，我们会尽快为您处理，请留意到账通知。",
}


def _label(tool: str) -> str:
    return _TOOL_ACTION_LABELS.get(tool, tool)


def clarify_missing_text(missing: list[str]) -> str:
    labels = [FIELD_LABELS.get(f, f) for f in missing]
    return "好的，为了尽快为您办理，请补充：" + "、".join(labels) + "，直接回复我就可以～"


def clarify_candidates_text(candidates: list[dict]) -> str:
    options = "、".join(
        f"{item['order_id']}（{item.get('product_summary', '商品信息不可用')}）"
        for item in candidates
    )
    return f"查到您名下有多笔符合条件的订单，请问要取消哪一笔呢？{options}"


def clarify_no_candidates_text() -> str:
    return "当前没有查询到可取消的已付款订单。您可以确认一下订单状态，或告诉我订单号，我再帮您查一次。"


def clarify_tier_text() -> str:
    return "您的请求涉及较高风险操作，但我无法确定具体要执行哪一项（如：申请退款、取消订单），麻烦您明确说明一下。"


def clarify_fallback_text() -> str:
    return "抱歉，我没有完全理解您的需求。您可以换个说法，或者输入「转人工」由人工客服为您处理。"


def confirmation_summary(tool: str, args: dict) -> str:
    return f"您即将进行「{_label(tool)}」操作，参数：{args}，请确认是否继续。"


def completion_text(tool: str) -> str:
    return _COMPLETION_TEXTS.get(tool, f"好的，您的{_label(tool)}已办理完成。")


def operation_cancelled_text() -> str:
    return "好的，已按您的要求取消本次操作，订单信息未做任何变更。还有其他需要，随时告诉我。"


def approval_approved_text(tool: str) -> str:
    return f"您的{_label(tool)}申请已通过审核并执行完成，相关结果可以在订单详情中查看。"


def approval_failed_text(tool: str, error: str) -> str:
    return f"审核已通过，但执行时遇到问题（{error}）。请不要担心，您可以稍后重试，或输入「转人工」由人工客服为您跟进。"


def approval_rejected_text(tool: str) -> str:
    return f"您的{_label(tool)}申请本次未通过审核，订单保持不变。如需调整后重新申请，随时告诉我。"


def approval_expired_text(tool: str) -> str:
    return f"由于超时未收到确认，您的{_label(tool)}申请已过期取消，订单保持不变。如仍有需要，重新发起即可。"


def recheck_failed_text() -> str:
    return "抱歉，操作复核未通过，本次操作已中止。请重新发起，或输入「转人工」由人工客服为您核实。"


def guard_reject_text() -> str:
    return "抱歉，该请求可能存在安全风险，我无法继续处理。如有疑问，请输入「转人工」联系人工客服为您核实。"


def precheck_reject_text(reason: str, status: str | None = None) -> str:
    """按拒绝原因 + 订单状态出句：原因 + 补救建议。fallback 必须保留人工出口。"""
    if reason == "address_not_editable":
        if status == "shipped":
            return ("非常抱歉，您的订单已发货，物流信息已锁定，无法修改收货地址。"
                    "您可以尝试联系配送员改派，或包裹签收后联系我为您办理退换。")
        if status == "signed":
            return ("非常抱歉，您的订单已签收，无法直接修改收货地址。"
                    "如地址有误需要退换，我可以帮您发起售后流程。")
        if status == "cancelled":
            return "抱歉，该订单已取消，无需修改地址。如需重新购买，我可以帮您确认商品信息。"
        if status in ("refunding", "refunded"):
            return "抱歉，该订单正在退款流程中，暂无法修改地址。退款完成后如仍有需要，欢迎再次咨询。"
        return "抱歉，当前订单状态暂不支持修改收货地址。您可以提供订单号，我帮您确认一下具体状态。"
    if reason == "invoice_not_allowed":
        if status == "cancelled":
            return "抱歉，已取消的订单无法开具发票。如您已支付但订单被取消，款项会按原路退回，请留意到账通知。"
        if status == "refunded":
            return "抱歉，已退款的订单无法开具发票。"
        if status == "refunding":
            return "抱歉，该订单正在退款处理中，需暂停开票；退款完成后如您仍需要，可以重新申请。"
        return "抱歉，当前订单状态暂不支持开具发票。您可以提供订单号，我帮您确认一下。"
    if reason == "refund_window_expired":
        return "抱歉，您的订单已超过签收后 7 天无理由退款期。如商品存在质量问题，请描述具体情况，我帮您申请售后。"
    if reason == "order_already_refunded":
        return "您的订单已完成退款，无需重复申请。如未收到退款，请告诉我，我帮您查询。"
    if reason == "order_already_refunding":
        return "您的订单已有一笔退款正在处理中，无需重复申请，结果出来后会第一时间通知您。"
    if reason == "refund_amount_exceeds":
        return "抱歉，退款金额超出了该订单可退额度。需要的话我可以帮您核对剩余可退金额。"
    if reason == "refund_not_allowed":
        return "抱歉，当前订单状态暂不支持申请退款。您可以提供订单号，我帮您确认一下具体状态。"
    if reason == "cancel_not_allowed":
        return "抱歉，当前订单状态暂不支持取消。您可以提供订单号，我帮您确认一下。"
    if reason == "order_already_cancelled":
        return "您的订单已取消，无需重复操作。如需重新下单，我可以帮您确认商品信息。"
    if reason == "order_not_found":
        return "没有查询到该订单，麻烦您核对一下订单号是否正确，我再帮您查一次。"
    if reason == "data_source_unavailable":
        return "抱歉，订单系统暂时不可用，请稍后再试，或输入「转人工」由人工客服为您处理。"
    if reason == "data_boundary":
        return "抱歉，您没有权限操作该订单。如该订单确属您的账号，请确认登录账号是否正确，或联系人工客服核实。"
    return "抱歉，该操作未能通过安全校验。如有疑问，请输入「转人工」联系人工客服为您核实。"
```

- [x] **Step 4: 运行测试确认通过**

Run: `uv run --no-sync pytest tests/test_copy.py -q`
Expected: 14 passed

- [x] **Step 5: Commit**

```bash
git add -- src/ai_service_agent/graph/workflows/copy.py tests/test_copy.py
git commit -m "feat: add customer-service tone copy layer"
```

### Task 2: 业务规则新增与退款规则修正（TDD）

**Files:**
- Modify: `src/ai_service_agent/graph/workflows/safety.py`（`_apply_business_rules`）
- Modify: `docs/specs/safety-spec.md` §1.2 规则表、`docs/specs/tool-spec.md` 状态机行
- Test: `tests/test_mutation_graph.py`、`tests/test_high_risk_graph.py`

**Interfaces:**
- Consumes: 无新依赖（规则层只返回 reason 码；文案消费在 Task 3）。
- Produces: 新 reason 码 `address_not_editable`、`invoice_not_allowed`；`refund_apply` 对 `signed`（窗口内）放行、对 `completed` 拒绝（`refund_not_allowed`）。

- [x] **Step 1: 写失败测试**

`tests/test_mutation_graph.py` 追加（沿用文件内既有 seed/monkeypatch 夹具）：

```python
def test_update_address_rejected_after_shipped(seed, monkeypatch):
    """已发货订单不可修改地址（业务规则 address_not_editable）。"""
    from ai_service_agent.graph.workflows.safety import _apply_business_rules

    pending = {"tool": "update_address", "args": {"order_id": "ORD-2026-10031"}}
    facts = {"order_id": "ORD-2026-10031", "user_id": "u0026", "status": "shipped"}
    reason = _apply_business_rules(pending, facts, "u0026")
    assert reason == "address_not_editable"


def test_update_address_allowed_when_paid(seed, monkeypatch):
    from ai_service_agent.graph.workflows.safety import _apply_business_rules

    pending = {"tool": "update_address", "args": {"order_id": "ORD-2026-10031"}}
    facts = {"order_id": "ORD-2026-10031", "user_id": "u0026", "status": "paid"}
    assert _apply_business_rules(pending, facts, "u0026") is None


def test_create_invoice_rejected_when_cancelled(seed, monkeypatch):
    from ai_service_agent.graph.workflows.safety import _apply_business_rules

    pending = {"tool": "create_invoice", "args": {"order_id": "ORD-X", "amount": 10.0}}
    facts = {"order_id": "ORD-X", "user_id": "u1", "status": "cancelled",
             "total_amount": 100.0, "invoiced_amount": 0.0}
    assert _apply_business_rules(pending, facts, "u1") == "invoice_not_allowed"
```

`tests/test_high_risk_graph.py` 追加（refund 规则修正用例）：

```python
def test_refund_rejected_when_completed(monkeypatch, tmp_path):
    from ai_service_agent.graph.workflows.safety import _apply_business_rules

    pending = {"tool": "refund_apply", "args": {"order_id": "ORD-X", "amount": 10.0}}
    facts = {"order_id": "ORD-X", "user_id": "u1", "status": "completed",
             "total_amount": 100.0, "occupied_refund_amount": 0.0}
    assert _apply_business_rules(pending, facts, "u1") == "refund_not_allowed"


def test_refund_allowed_when_signed_within_window():
    from datetime import datetime, timedelta, timezone
    from ai_service_agent.graph.workflows.safety import _apply_business_rules

    pending = {"tool": "refund_apply", "args": {"order_id": "ORD-X", "amount": 10.0}}
    facts = {"order_id": "ORD-X", "user_id": "u1", "status": "signed",
             "signed_at": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
             "total_amount": 100.0, "occupied_refund_amount": 0.0}
    assert _apply_business_rules(pending, facts, "u1") is None
```

- [x] **Step 2: 运行测试确认失败**

Run: `uv run --no-sync pytest tests/test_mutation_graph.py tests/test_high_risk_graph.py -q`
Expected: 新增用例 FAIL（address/invoice 无规则返回 None；completed 被放行返回 None；signed 窗口内被拒）

- [x] **Step 3: 实现**

`safety.py` `_apply_business_rules` 修改（三处）：

```python
    if tool_name == "create_invoice":
        status = precheck_data.get("status")
        if status in ("cancelled", "refunded", "refunding"):
            return "invoice_not_allowed"
        # 原 invoice 金额校验保持不变
    if tool_name == "update_address":
        status = precheck_data.get("status")
        if status in ("shipped", "signed", "completed", "cancelled", "refunding", "refunded"):
            return "address_not_editable"
```

并将退款规则 `if status not in ("paid", "completed"):` 改为 `if status not in ("paid", "signed"):`

同步更新 `docs/specs/safety-spec.md` §1.2 规则表（新增两行 + 修正 refund 行为 paid / signed-窗口内）与 `docs/specs/tool-spec.md` 相应状态机行。

- [x] **Step 4: 运行测试确认通过**

Run: `uv run --no-sync pytest tests/test_mutation_graph.py tests/test_high_risk_graph.py -q`
Expected: 全部 passed（若既有用例依赖 completed 可退款，按新语义修正该用例）

- [x] **Step 5: Commit**

```bash
git add -- src/ai_service_agent/graph/workflows/safety.py docs/specs/safety-spec.md docs/specs/tool-spec.md tests/test_mutation_graph.py tests/test_high_risk_graph.py
git commit -m "feat: add address/invoice status rules and fix refund window semantics"
```

### Task 3: 接线文案层 + 更新既有断言

**Files:**
- Modify: `src/ai_service_agent/graph/workflows/mutation_agent.py`（finalize_ok / finalize_rejected / finalize_aborted / prepare_confirm summary）
- Modify: `src/ai_service_agent/graph/workflows/high_risk_agent.py`（finalize_ok 含 tool_error 分支 / rejected / expired / aborted）
- Modify: `src/ai_service_agent/graph/workflows/clarify.py`（四分支改调 copy 层；`FIELD_LABELS` / `clarify_text` / 两个常量移除）
- Modify: `src/ai_service_agent/graph/workflows/safety.py`（`PRECHECK_REJECT_TEXT` 移除；`safety_final` 改调 `precheck_reject_text(reason, facts.status)`）
- Modify: `src/ai_service_agent/graph/workflows/guard.py`（`GUARD_REJECT_TEXT` 从 copy 层 import 并 re-export）
- Modify: `src/ai_service_agent/harness/handoff.py`（`HANDOFF_NOTICE` 文本软化，符号名不变）
- Modify: `tests/test_clarify.py`、`tests/test_mutation_graph.py`、`tests/test_server.py`、`tests/test_streaming.py`

**Interfaces:**
- Consumes: Task 1 全部模板函数；Task 2 的 reason 码。
- Produces: 用户面文本全部客服语气；`clarify_text` / `PRECHECK_REJECT_TEXT` / `CLARIFY_FALLBACK_TEXT` / `TIER_CLARIFY_TEXT` 公共符号移除（grep 确认仅 clarify.py / safety.py / 测试引用）。

- [x] **Step 1: 先更新测试断言到新文案语义**

```python
# tests/test_clarify.py:67-68,234-235 改为语义断言（clarify_text 改为 clarify_missing_text）
    assert "订单号" in clarify_missing_text(["order_id"])
    assert "订单号" in clarify_missing_text(["order_id", "amount"]) and "金额" in clarify_missing_text(["order_id", "amount"])
# :234-235 同理改子串断言

# tests/test_mutation_graph.py:198
    assert "收货地址" in t1["messages"][-1].content
# :213
    assert "收货地址已更新" in t3["messages"][-1].content

# tests/test_server.py:268,274（该流程工具为 update_address）
    assert "收货地址已更新" in body["final"]
    assert "收货地址已更新" in rows[-1]["content"]
# :402
    assert any(row["role"] == "assistant" and "通过审核" in row["content"] for row in rows)

# tests/test_streaming.py:130（该流程工具为 update_address）
    assert "收货地址已更新" in done["data"]["final"]
# :184
    assert "收货地址" in ev1[-1]["data"]["final"]
```

- [x] **Step 2: 运行相关测试确认失败**

Run: `uv run --no-sync pytest tests/test_clarify.py tests/test_mutation_graph.py tests/test_server.py tests/test_streaming.py -q`
Expected: 上述断言 FAIL（文案仍是旧的）

- [x] **Step 3: 接线（各文件只改文本来源，逻辑不动）**

`mutation_agent.py`：

```python
from ai_service_agent.graph.workflows.copy import (
    completion_text, confirmation_summary, operation_cancelled_text, recheck_failed_text,
)

# prepare_confirm: summary = confirmation_summary(pending['tool'], pending['args'])
# finalize_ok: content=completion_text(pending['tool'])
# finalize_rejected: content=operation_cancelled_text()
# finalize_aborted: content=recheck_failed_text()
```

`high_risk_agent.py`：

```python
from ai_service_agent.graph.workflows.copy import (
    approval_approved_text, approval_failed_text, approval_expired_text,
    approval_rejected_text, recheck_failed_text,
)

# finalize_ok 无错分支: content=approval_approved_text(pending['tool'])
# finalize_ok 错误分支: content=approval_failed_text(pending['tool'], tool_error)
# finalize_rejected: approval_rejected_text(pending['tool'])
# finalize_expired: approval_expired_text(pending['tool'])
# finalize_aborted: recheck_failed_text()
```

（`finalize_ok` 中 `pending` 已从 state 取得，保持原取值方式。）

`clarify.py`：

```python
from ai_service_agent.graph.workflows.copy import (
    clarify_candidates_text, clarify_fallback_text, clarify_missing_text,
    clarify_no_candidates_text, clarify_tier_text,
)

# tier 分支: text = clarify_tier_text()
# candidates 分支: text = clarify_candidates_text(candidates)
# 无候选: text = clarify_no_candidates_text()
# 缺字段: text = clarify_missing_text(missing)
# 超轮次: content = clarify_fallback_text()
# 删除本文件 FIELD_LABELS / clarify_text / CLARIFY_FALLBACK_TEXT / TIER_CLARIFY_TEXT
```

（`test_clarify.py` 直接 import `clarify_text` 的用例改 import `clarify_missing_text`。）

`safety.py`：

```python
from ai_service_agent.graph.workflows.copy import precheck_reject_text

def safety_final(state: dict) -> dict:
    ctx = state.get("context", {})
    reason = ctx.get("precheck_reason", "precheck")
    facts = ctx.get("precheck_data") or {}
    status = facts.get("status") if isinstance(facts, dict) else None
    return {"messages": [AIMessage(content=precheck_reject_text(reason, status))], "execution_status": "completed"}
```

（删除 `PRECHECK_REJECT_TEXT` 常量；`mutation_agent.py` / `high_risk_agent.py` 对它的 import 移除——改用 `recheck_failed_text()`。）

`guard.py`：

```python
from ai_service_agent.graph.workflows.copy import guard_reject_text
GUARD_REJECT_TEXT = guard_reject_text()
```

（builder.py 对 `GUARD_REJECT_TEXT` 的 import 不变。）

`harness/handoff.py`：`HANDOFF_NOTICE` 文本改为客服语气（如 "好的，已为您转接人工客服，请稍候。人工客服会继续在这个对话里为您服务。"），符号名不变。

- [x] **Step 4: 运行定向测试确认通过**

Run: `uv run --no-sync pytest tests/test_clarify.py tests/test_mutation_graph.py tests/test_high_risk_graph.py tests/test_server.py tests/test_streaming.py tests/test_guard.py -q`
Expected: 全部 passed

- [x] **Step 5: grep 残留检查**

Run: `rg -n "操作已完成。|已取消该操作。|该操作未能通过安全校验|审批未通过，操作已取消|审批已过期，操作已取消|请提供：" src tests`
Expected: 无残留（测试新断言除外）

- [x] **Step 6: Commit**

```bash
git add -- src/ai_service_agent/graph/workflows/mutation_agent.py src/ai_service_agent/graph/workflows/high_risk_agent.py src/ai_service_agent/graph/workflows/clarify.py src/ai_service_agent/graph/workflows/safety.py src/ai_service_agent/graph/workflows/guard.py src/ai_service_agent/harness/handoff.py tests/test_clarify.py tests/test_mutation_graph.py tests/test_server.py tests/test_streaming.py
git commit -m "feat: wire customer-service tone copy across workflows"
```

### Task 4: 全量回归与最终验证（无新提交）

- [x] **Step 1: 全量测试**

Run: `$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest -q --basetemp="D:\AI_Project\AI Service Agent\.pytest-tmp"`
Expected: 全部 passed（375 基线 + 新增）

- [x] **Step 2: langgraph 校验**

Run: `langgraph validate`
Expected: 通过

## 验收清单

- [x] 所有用户面固定文案集中在 `copy.py`
- [x] 语气准则落地（承接/结果/下一步；拒绝含原因+补救）
- [x] `update_address` / `create_invoice` 状态规则生效且有测试
- [x] 退款 `signed` 窗口语义修正，spec 同步
- [x] SSE 事件类型/结构零变更
- [x] 全量回归 + `langgraph validate` 通过

