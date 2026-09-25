"""面向用户的确定性文案层（客服语气）：承接 -> 结果 -> 下一步引导。

所有状态类固定文案集中此处；节点只消费模板函数。SSE 事件结构零变更，
状态关键词（完成/取消/过期/拒绝）不可弱化；拒绝文案必须含原因与补救建议。
"""

import hashlib


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
