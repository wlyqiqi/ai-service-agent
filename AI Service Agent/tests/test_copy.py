from ai_service_agent.graph.workflows.copy import (
    approval_approved_text,
    approval_expired_text,
    approval_failed_text,
    approval_rejected_text,
    approval_summary,
    clarify_candidates_text,
    clarify_fallback_text,
    clarify_missing_text,
    clarify_no_candidates_text,
    clarify_tier_text,
    handoff_joined_notice_text,
    handoff_returned_to_bot_text,
    human_service_header_text,
    operation_clarify_text,
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
    op = operation_clarify_text()
    assert "地址" in op and "发票" in op
    assert "表单" not in op


def test_operation_clarify_text_only_lists_allowlisted_capabilities():
    """兜底追问必须跟着当前白名单，不能提系统此刻不会做的事。"""
    profile = operation_clarify_text(["update_address", "update_phone"])
    assert "改收货地址" in profile and "改联系电话" in profile
    assert "开发票" not in profile
    assert "售后" not in profile
    assert "备注" not in profile

    single = operation_clarify_text(["update_phone"])
    assert "改联系电话" in single
    assert "改收货地址" not in single

    # 未知工具名不参与列举，避免把内部 id 漏给用户
    unknown = operation_clarify_text(["not_a_tool"])
    assert "not_a_tool" not in unknown


def test_operation_clarify_text_without_tools_keeps_full_list():
    """无白名单信息时退回完整列举（兼容旧调用）。"""
    op = operation_clarify_text()
    assert "收货地址" in op and "开发票" in op and "售后" in op


def test_confirmation_summary_keeps_facts():
    summary = confirmation_summary("update_address", {"order_id": "ORD-1", "address": "北京"})
    assert "update_address" in summary or "收货地址" in summary
    assert "ORD-1" in summary and "北京" in summary


def test_completion_text_per_tool():
    assert "收货地址" in completion_text("update_address")
    assert "发票" in completion_text("create_invoice")
    assert "工单" in completion_text("create_service_ticket")
    assert "已办理完成" in completion_text("unknown_tool")


def test_completion_text_address_shows_order_and_new_address():
    text = completion_text(
        "update_address",
        {"order_id": "ORD-1", "address": "北京市朝阳区测试路 1 号"},
    )
    assert "订单 ORD-1" in text
    assert "北京市朝阳区测试路 1 号" in text
    assert "已修改成功" in text


def test_handoff_system_notice_copy():
    assert handoff_joined_notice_text() == "人工客服已接入，接下来将由人工为您服务。"
    assert handoff_returned_to_bot_text() == "已交回AI客服助手"
    assert human_service_header_text() == "人工服务中"


def test_operation_cancelled_text():
    text = operation_cancelled_text()
    assert "取消" in text and "未" in text


def test_approval_texts_keep_status_keywords():
    assert "通过审核" in approval_approved_text("refund_apply")
    assert "未通过" in approval_rejected_text("refund_apply")
    assert "过期" in approval_expired_text("refund_apply") and "取消" in approval_expired_text("refund_apply")
    failed = approval_failed_text("refund_apply", "db_error")
    assert "遇到问题" in failed and "人工" in failed
    assert "db_error" not in failed
    assert approval_failed_text("refund_apply", "db_error") == failed
    assert approval_failed_text("refund_apply", "connection_timeout") != failed


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
    refunded = precheck_reject_text("address_not_editable", "refunded")
    assert "完成退款" in refunded and "无法修改收货地址" in refunded
    assert "人工" in refunded


def test_precheck_reject_invoice_by_status():
    assert "无法开具发票" in precheck_reject_text("invoice_not_allowed", "cancelled")
    refunded = precheck_reject_text("invoice_not_allowed", "refunded")
    assert "无法开具发票" in refunded
    assert "请提供订单号" in refunded or "联系人工客服" in refunded
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


def _paid_facts(**overrides):
    facts = {
        "order_id": "10086",
        "user_id": "u1",
        "status": "paid",
        "total_amount": 599.0,
        "occupied_refund_amount": 99.0,
        "product_summary": "空气炸锅",
    }
    facts.update(overrides)
    return facts


def test_approval_summary_cancel_order_uses_snapshot():
    summary, reason = approval_summary("cancel_order", {"order_id": "10086"}, _paid_facts(), "u1")
    assert reason is None
    assert "取消订单 10086" in summary
    assert "599.0" in summary and "空气炸锅" in summary and "u1" in summary


def test_approval_summary_cancel_order_without_paid_snapshot_returns_reason():
    summary, reason = approval_summary(
        "cancel_order", {"order_id": "10086"}, _paid_facts(status="shipped"), "u1"
    )
    assert summary is None
    assert reason == "cancel_order_facts_unavailable"


def test_approval_summary_rejects_foreign_snapshot():
    summary, reason = approval_summary("cancel_order", {"order_id": "10086"}, _paid_facts(), "u2")
    assert summary is None and reason == "cancel_order_facts_unavailable"


def test_approval_summary_refund_without_amount_subtracts_occupied():
    summary, reason = approval_summary("refund_apply", {"order_id": "10086"}, _paid_facts(), "u1")
    assert reason is None
    assert "退款金额 500.0" in summary
    assert "已占用退款 99.0" in summary


def test_approval_summary_refund_with_explicit_amount_is_rounded():
    summary, _ = approval_summary(
        "refund_apply", {"order_id": "10086", "amount": 120.456}, _paid_facts(), "u1"
    )
    assert "退款金额 120.46" in summary


def test_approval_summary_refund_falls_back_when_snapshot_missing():
    summary, reason = approval_summary("refund_apply", {"order_id": "10086"}, None, "u1")
    assert reason is None
    assert summary.startswith("即将执行「refund_apply」")


def test_approval_summary_generic_tool_uses_args():
    summary, reason = approval_summary("cancel_order_v2", {"order_id": "1"}, None, "u1")
    assert reason is None
    assert "cancel_order_v2" in summary and "10086" not in summary

