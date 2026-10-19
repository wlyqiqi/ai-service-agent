"""面向用户的确定性文案层（客服语气）：承接 -> 结果 -> 下一步引导。

所有状态类固定文案集中此处；节点只消费模板函数。SSE 事件结构零变更，
状态关键词（完成/取消/过期/拒绝）不可弱化；拒绝文案必须含原因与补救建议。
"""

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass


RUNTIME_ERROR_TEXT = (
    "抱歉，系统刚才遇到技术问题，暂时无法完成该操作。"
    "请稍后再试，或输入“转人工”联系人工客服。"
)


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


def operation_clarify_text() -> str:
    """Operation Clarify：tool 未定前用聊天问清操作类型（不用表单做操作类型菜单）。"""
    return (
        "好的，您是想修改订单相关信息。请问具体要办理哪一项呢？"
        "例如：收货地址、联系电话、订单备注、开发票，或申请售后工单。"
        "直接告诉我即可～"
    )


def clarify_fallback_text() -> str:
    return "抱歉，我没有完全理解您的需求。您可以换个说法，或者输入「转人工」由人工客服为您处理。"


def confirmation_summary(tool: str, args: dict) -> str:
    return f"您即将进行「{_label(tool)}」操作，参数：{args}，请确认是否继续。"


def approval_summary(
    tool: str, args: dict, facts: dict | None, user_id: str
) -> tuple[str | None, str | None]:
    """Build the approver-facing summary (approval-spec §3).

    Returns ``(summary, unavailable_reason)``. ``cancel_order`` without a matching
    ``paid`` snapshot yields a reason so the caller can end in ``safety_final``;
    ``refund_apply`` falls back to a generic summary instead (existing behaviour).
    """
    args = args or {}
    order_id = args.get("order_id", "")
    facts = facts if isinstance(facts, dict) else None
    facts_match = (
        facts is not None
        and facts.get("order_id") == order_id
        and facts.get("user_id") == user_id
    )

    if tool == "cancel_order":
        if not (facts_match and facts.get("status") == "paid"):
            return None, "cancel_order_facts_unavailable"
        return (
            f"取消订单 {facts['order_id']}｜"
            f"金额 {facts['total_amount']}｜"
            f"商品：{facts.get('product_summary', '商品信息不可用')}｜"
            f"申请人 {user_id}｜"
            f"状态 已付款"
        ), None

    if tool == "refund_apply" and facts_match and facts.get("status") in ("paid", "signed"):
        amount = args.get("amount")
        occupied = float(facts.get("occupied_refund_amount", 0.0))
        resolved_amount = (
            round(float(amount), 2)
            if amount is not None
            else round(float(facts["total_amount"]) - occupied, 2)
        )
        return (
            f"退款申请 {facts['order_id']}｜"
            f"退款金额 {resolved_amount}｜"
            f"订单总额 {facts['total_amount']}｜"
            f"已占用退款 {occupied}｜"
            f"商品：{facts.get('product_summary', '商品信息不可用')}｜"
            f"申请人 {user_id}｜"
            f"状态 {'已付款' if facts['status'] == 'paid' else '已签收'}"
        ), None

    return f"即将执行「{tool}」，参数：{args}", None


def completion_text(tool: str, args: dict | None = None) -> str:
    if tool == "update_address" and args:
        order_id = args.get("order_id")
        address = args.get("address")
        if order_id and address:
            return (
                f"订单 {order_id} 的收货地址已修改成功：{address}。"
                "后续包裹将按新地址配送。"
            )
    return _COMPLETION_TEXTS.get(tool, f"好的，您的{_label(tool)}已办理完成。")


def mutation_tool_failed_text(tool: str, error: str) -> str:
    operation_reference = hashlib.sha256(error.encode("utf-8")).hexdigest()[:8]
    return (
        f"很抱歉，您的{_label(tool)}操作未成功（处理编号：{operation_reference}）。"
        "请不要担心，您可以稍后重试，或输入「转人工」由人工客服为您跟进。"
    )


def operation_cancelled_text() -> str:
    return "好的，已按您的要求取消本次操作，订单信息未做任何变更。还有其他需要，随时告诉我。"


# update_address four-state copy (#71 open-form / empty-state; locked plan §2).
_UPDATE_ADDRESS_FORM_INTRO = (
    "好的。请填写下面的表单，提交后我们会按新地址处理（仅限已支付且未发货的订单）。"
)
_UPDATE_ADDRESS_FORM_TITLE = "修改收货地址"
_UPDATE_ADDRESS_FORM_SUBTITLE = "仅已支付且未发货订单可改"
_UPDATE_ADDRESS_FORM_FOOTNOTE = (
    "提交后会更新账户收货地址；改聊别的或离开表单，等于放弃本次修改。"
)
_UPDATE_ADDRESS_EMPTY_STATE = (
    "当前没有可改地址的订单（需要已支付且尚未发货）。你可以先查订单，或支付完成后再改。"
)
_UPDATE_ADDRESS_PLACEHOLDER = "省市区 + 街道门牌，例如：山东省青岛市..."
_UPDATE_ADDRESS_CURRENT_EMPTY = "暂无"


def update_address_form_intro_text() -> str:
    return _UPDATE_ADDRESS_FORM_INTRO


def update_address_form_title_text() -> str:
    return _UPDATE_ADDRESS_FORM_TITLE


def update_address_form_subtitle_text() -> str:
    return _UPDATE_ADDRESS_FORM_SUBTITLE


def update_address_form_footnote_text() -> str:
    return _UPDATE_ADDRESS_FORM_FOOTNOTE


def update_address_empty_state_text() -> str:
    return _UPDATE_ADDRESS_EMPTY_STATE


def update_address_new_address_placeholder() -> str:
    return _UPDATE_ADDRESS_PLACEHOLDER


def update_address_current_address_display(current: str | None) -> str:
    text = (current or "").strip()
    return text if text else _UPDATE_ADDRESS_CURRENT_EMPTY


# Shared write-success card (#73 address card, now all seven write tools).
# Widget often collapses a single ``\\n`` into one paragraph, so each fact is a
# ``• `` bullet with a blank line between bullets.
_SUCCESS_STATUS_LABEL = "状态"
_SUCCESS_STATUS_VALUE = "已完成"
_UPDATE_ADDRESS_SUCCESS_STATUS = _SUCCESS_STATUS_VALUE
_UPDATE_ADDRESS_SUCCESS_TITLE = "地址已修改成功"
_UPDATE_ADDRESS_SUCCESS_ACCOUNT_UPDATED = "已同步更新"
_UPDATE_ADDRESS_SUCCESS_FOLLOWUP = (
    "已帮您更新。发货前都会按新地址安排，有问题随时找我。"
)

_SUCCESS_CARD_TITLES = {
    "update_address": _UPDATE_ADDRESS_SUCCESS_TITLE,
    "update_phone": "手机号已修改成功",
    "update_order_note": "订单备注已更新",
    "create_invoice": "发票已开具成功",
    "create_service_ticket": "售后工单已创建",
    "cancel_order": "订单已取消成功",
    "refund_apply": "退款申请已提交",
}

_SUCCESS_CARD_BUTTON = "知道了"


def _format_card_value(value: object) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    if isinstance(value, int):
        return str(value)
    return str(value).strip()


def _inner_tool_result(result: dict | None) -> dict:
    if not isinstance(result, dict):
        return {}
    inner = result.get("result")
    if isinstance(inner, dict):
        return inner
    return result


def _success_facts(args: dict | None, result: dict | None) -> dict[str, object]:
    facts: dict[str, object] = {}
    for key, value in (args or {}).items():
        if key in ("request_id", "field", "ok", "replayed"):
            continue
        if _format_card_value(value):
            facts[key] = value
    for key, value in _inner_tool_result(result).items():
        if key in ("request_id", "field", "ok", "replayed"):
            continue
        if _format_card_value(value):
            facts[key] = value
    return facts


def format_success_card_description(lines: Sequence[tuple[str, str]]) -> str:
    """Bullet + blank line so a Widget that eats ``\\n`` still separates rows."""
    bullets: list[str] = []
    for label, value in lines:
        text = value.strip() if isinstance(value, str) else _format_card_value(value)
        if not text:
            continue
        bullets.append(f"• {label}：{text}")
    return "\n\n".join(bullets)


def mutation_success_followup_text(tool: str) -> str:
    """Short bubble under the card. Address keeps the #73 tweak; others reuse completion copy."""
    if tool == "update_address":
        return _UPDATE_ADDRESS_SUCCESS_FOLLOWUP
    return _COMPLETION_TEXTS.get(tool, f"好的，您的{_label(tool)}已办理完成。")


def _optional(facts: dict[str, object], key: str) -> str:
    return _format_card_value(facts.get(key))


def _success_detail_lines(tool: str, facts: dict[str, object]) -> list[tuple[str, str]]:
    if tool == "update_address":
        return [
            ("订单号", _optional(facts, "order_id")),
            ("新收货地址", _optional(facts, "address")),
            ("账户地址", _UPDATE_ADDRESS_SUCCESS_ACCOUNT_UPDATED),
        ]
    if tool == "update_phone":
        return [
            ("订单号", _optional(facts, "order_id")),
            ("新手机号", _optional(facts, "phone")),
            ("通知号码", "已切换"),
        ]
    if tool == "update_order_note":
        return [
            ("订单号", _optional(facts, "order_id")),
            ("备注内容", _optional(facts, "note")),
        ]
    if tool == "create_invoice":
        return [
            ("订单号", _optional(facts, "order_id")),
            ("发票号", _optional(facts, "invoice_id")),
            ("抬头", _optional(facts, "company")),
            ("税号", _optional(facts, "tax_no")),
            ("金额", _optional(facts, "amount")),
            ("下载提示", "可在订单详情中下载查看"),
        ]
    if tool == "create_service_ticket":
        return [
            ("工单号", _optional(facts, "ticket_id")),
            ("订单号", _optional(facts, "order_id")),
            ("问题描述", _optional(facts, "reason")),
            ("后续联系", "工程师会尽快与您联系，请保持电话畅通"),
        ]
    if tool == "cancel_order":
        return [
            ("订单号", _optional(facts, "order_id")),
            ("取消原因", _optional(facts, "reason")),
            ("退款路径提示", "退款将按原支付路径退回"),
        ]
    if tool == "refund_apply":
        return [
            ("订单号", _optional(facts, "order_id")),
            ("申请金额", _optional(facts, "amount")),
            ("原因", _optional(facts, "reason")),
            ("进度提示", "请留意到账通知"),
        ]
    return []


@dataclass(frozen=True)
class MutationSuccessCardSpec:
    """One Chatwoot success card: title, bullet description, 「知道了」, follow-up bubble."""

    tool: str
    title: str
    lines: tuple[tuple[str, str], ...]
    followup: str
    button_text: str = _SUCCESS_CARD_BUTTON

    @property
    def description(self) -> str:
        return format_success_card_description(self.lines)

    @property
    def ack_payload(self) -> str:
        return f"{self.tool}_success_ack"


def build_mutation_success_card(
    tool: str,
    args: dict | None = None,
    result: dict | None = None,
) -> MutationSuccessCardSpec | None:
    """Build the shared success card for a write tool. Unknown tools return None."""
    title = _SUCCESS_CARD_TITLES.get(tool)
    if title is None:
        return None
    facts = _success_facts(args, result)
    lines = [(_SUCCESS_STATUS_LABEL, _SUCCESS_STATUS_VALUE), *_success_detail_lines(tool, facts)]
    return MutationSuccessCardSpec(
        tool=tool,
        title=title,
        lines=tuple(lines),
        followup=mutation_success_followup_text(tool),
    )


def update_address_success_status_text() -> str:
    return _UPDATE_ADDRESS_SUCCESS_STATUS


def update_address_success_card_title_text() -> str:
    return _UPDATE_ADDRESS_SUCCESS_TITLE


def update_address_success_account_updated_text() -> str:
    return _UPDATE_ADDRESS_SUCCESS_ACCOUNT_UPDATED


def update_address_success_followup_text() -> str:
    return _UPDATE_ADDRESS_SUCCESS_FOLLOWUP


def update_address_success_card_description(order_id: str, address: str) -> str:
    """Chatwoot cards description: status first, then ``标签：值`` bullets."""
    spec = build_mutation_success_card(
        "update_address",
        {"order_id": order_id, "address": address},
        None,
    )
    assert spec is not None
    return spec.description


def human_service_header_text() -> str:
    """Widget 顶栏：仅 conversation_mode=human 时显示。"""
    return "人工服务中"


def handoff_request_confirm_text() -> str:
    """Handoff Request 挂起确认（mode 仍 ai；与 Joined Notice 区分）。"""
    return (
        "好的，已收到您的转人工申请。人工客服接入前我会继续为您服务；"
        "如需取消请回复「取消转人工」。"
    )


def handoff_joined_notice_text() -> str:
    """Seat Takeover 后 Handoff Joined Notice。"""
    return "人工客服已接入，接下来将由人工为您服务。"


def handoff_cancel_confirm_text() -> str:
    """客户取消挂起 Handoff Request 后的短 tip。"""
    return "好的，已取消转人工申请。有需要随时告诉我。"


def handoff_request_idempotent_tip_text() -> str:
    """挂起期间重复转人工：仅短 tip，不再发强提示。"""
    return "已收到您的转人工申请，请稍候，人工客服接入前我仍会继续为您服务。"


def handoff_request_private_note_text(summary: str = "") -> str:
    """Chatwoot 强提示 private note 正文。"""
    base = "【转人工申请】客户请求人工客服接入（Conversation Mode 仍为 AI，待 Take over）。"
    if summary:
        return f"{base}\n摘要：{summary}"
    return base


def handoff_form_cancelled_customer_text() -> str:
    """进入 human 时取消未提交 Mutation Form 的客户可见提示。"""
    return "人工客服已接入。尚未提交的修改已取消，不会写入订单。"


def handoff_form_cancelled_seat_note_text() -> str:
    """进入 human 时取消未提交表单的坐席 private note。"""
    return "【未提交表单已取消】客户有未完成的修改表单，接入人工时已取消，迟到的提交不会写入。"


def handoff_returned_to_bot_text() -> str:
    """交回 Bot（status pending / Resolve）时客户与坐席都可见的短提示。"""
    return "已交回AI客服助手"

def approval_approved_text(tool: str) -> str:
    return f"您的{_label(tool)}申请已通过审核并执行完成，相关结果可以在订单详情中查看。"


def approval_failed_text(tool: str, error: str) -> str:
    operation_reference = hashlib.sha256(error.encode("utf-8")).hexdigest()[:8]
    return (
        f"审核已通过，但执行时遇到问题（处理编号：{operation_reference}）。"
        "请不要担心，您可以稍后重试，或输入「转人工」由人工客服为您跟进。"
    )


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
        if status == "refunded":
            return ("抱歉，该订单已完成退款，无法修改收货地址。"
                    "如涉及收货或账户信息需要跟进，请输入「转人工」，人工客服会为您处理。")
        if status == "refunding":
            return "抱歉，该订单正在退款流程中，暂无法修改地址。退款完成后如仍有需要，欢迎再次咨询。"
        return "抱歉，当前订单状态暂不支持修改收货地址。您可以提供订单号，我帮您确认一下具体状态。"
    if reason == "invoice_not_allowed":
        if status == "cancelled":
            return "抱歉，已取消的订单无法开具发票。如您已支付但订单被取消，款项会按原路退回，请留意到账通知。"
        if status == "refunded":
            return "抱歉，已退款的订单无法开具发票。如需核对退款凭证或账务信息，请提供订单号，我帮您进一步确认。"
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
