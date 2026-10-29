"""T1/T2/T3：坐席审批卡的结构化事实、resume 双形状、存储迁移。

这些是「卡片能画出什么」的地基——卡片不自己猜数据，全部读 ``approval_facts``。
"""

import json
import sqlite3

import pytest

from ai_service_agent.domain.orders import SQLiteOrderRepository
from ai_service_agent.graph.approval import resolve_approval_outcome
from ai_service_agent.graph.workflows.copy import approval_facts
from ai_service_agent.harness.approvals import ApprovalService

PAID = {
    "order_id": "ORD-1",
    "user_id": "u1",
    "status": "paid",
    "total_amount": 240.31,
    "occupied_refund_amount": 0.0,
    "product_summary": "澄元智能破壁机 Pro Max 01 ×1",
}
PROFILE = {"user_id": "u1", "name": "张伟", "phone": "+65-81234567", "address": "addr"}


# --- T1 结构化事实 -----------------------------------------------------------


def test_refund_partial_amount_uses_requested_value():
    facts, reason = approval_facts(
        "refund_apply", {"order_id": "ORD-1", "amount": 100}, PAID, "u1", PROFILE
    )
    assert reason is None
    assert facts["amount"] == 100.0
    assert facts["order_total"] == 240.31
    assert facts["currency"] == "CNY"
    assert facts["action_label"] == "退款申请"


def test_refund_without_amount_falls_back_to_remaining():
    facts, _ = approval_facts("refund_apply", {"order_id": "ORD-1"}, PAID, "u1", PROFILE)
    assert facts["amount"] == 240.31


def test_refund_subtracts_occupied_refund():
    occupied = {**PAID, "occupied_refund_amount": 40.31}
    facts, _ = approval_facts("refund_apply", {"order_id": "ORD-1"}, occupied, "u1", PROFILE)
    assert facts["amount"] == 200.0
    assert facts["refunded"] == 40.31


def test_cancel_order_without_snapshot_keeps_reason():
    facts, reason = approval_facts("cancel_order", {"order_id": "ORD-1"}, None, "u1", PROFILE)
    assert facts is None
    assert reason == "cancel_order_facts_unavailable"


def test_cancel_order_requires_paid_status():
    shipped = {**PAID, "status": "shipped"}
    facts, reason = approval_facts("cancel_order", {"order_id": "ORD-1"}, shipped, "u1", PROFILE)
    assert facts is None
    assert reason == "cancel_order_facts_unavailable"


def test_cancel_order_amount_is_refundable_total():
    facts, _ = approval_facts("cancel_order", {"order_id": "ORD-1"}, PAID, "u1", PROFILE)
    assert facts["amount"] == 240.31
    assert facts["order_status_label"] == "已付款 · 未发货"


def test_generic_tool_keeps_order_id_without_amounts():
    facts, reason = approval_facts(
        "update_address", {"order_id": "ORD-1", "address": "新地址"}, PAID, "u1", PROFILE
    )
    assert reason is None
    assert facts["order_id"] == "ORD-1"
    assert facts["amount"] is None
    assert facts["customer_quote"] is None


def test_facts_never_leak_placeholder_or_none_text():
    """卡片不能出现「商品信息不可用」这类占位串，也不能是 Python 的 None。"""
    empty = {**PAID, "product_summary": "商品信息不可用"}
    facts, _ = approval_facts("refund_apply", {"order_id": "ORD-1"}, empty, "u1", None)
    assert facts["product_summary"] is None
    assert facts["customer"] is None
    dumped = json.dumps(facts, ensure_ascii=False)
    assert "不可用" not in dumped
    assert "None" not in dumped


def test_customer_quote_from_reason():
    facts, _ = approval_facts(
        "refund_apply", {"order_id": "ORD-1", "reason": "  不想要了  "}, PAID, "u1", PROFILE
    )
    assert facts["customer_quote"] == "不想要了"


def test_facts_mismatched_user_is_ignored():
    """快照属于别人时不能当成本单事实（越权展示）。"""
    facts, _ = approval_facts("refund_apply", {"order_id": "ORD-1"}, PAID, "u2", PROFILE)
    assert facts["order_total"] is None
    assert facts["amount"] is None


def test_facts_missing_order_id_does_not_match_none():
    facts, _ = approval_facts("refund_apply", {}, {**PAID, "order_id": None}, "u1", PROFILE)
    assert facts["amount"] is None


# --- 客户档案（卡片上的「张伟 · 尾号 4567」） --------------------------------


def test_profile_exposes_name_and_phone_tail(tmp_path):
    db = tmp_path / "c.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE customers (customer_id TEXT PRIMARY KEY, name TEXT, phone TEXT, address TEXT)"
    )
    # 真实数据带尾随空格（实测 '15198775666 '），尾号必须仍然正确
    conn.execute("INSERT INTO customers VALUES ('u0034', '张伟', '15198775666 ', 'addr')")
    conn.commit()
    conn.close()

    profile = SQLiteOrderRepository(str(db)).get_user_profile("u0034")
    assert profile["name"] == "张伟"
    facts, _ = approval_facts(
        "refund_apply", {"order_id": "ORD-1"}, PAID, "u0034", profile
    )
    assert facts["customer"] == {"name": "张伟", "phone_tail": "5666"}


# --- T2 resume 双形状 --------------------------------------------------------


@pytest.mark.parametrize(
    ("resume", "expected"),
    [
        ({"action": "approve"}, "approved"),
        ({"action": "reject", "note": "已发货"}, "rejected"),
        ({"decisions": [{"type": "approve"}]}, "approved"),
        ({"decisions": [{"type": "reject", "message": "已发货"}]}, "rejected"),
        ({"decisions": [{"type": "approve"}], "action": "reject"}, "approved"),
        ({"result": "expired", "decisions": [{"type": "approve"}]}, "expired"),
        ({}, "rejected"),
        (None, "rejected"),
        ("approve", "rejected"),
    ],
)
def test_resolve_approval_outcome_accepts_both_shapes(resume, expected):
    assert resolve_approval_outcome(resume) == expected


# --- T3 存储迁移 -------------------------------------------------------------


_LEGACY_SCHEMA = """
CREATE TABLE approval_record (
    approval_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'high_risk',
    summary TEXT NOT NULL,
    requester_user_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    decision TEXT,
    decided_by TEXT,
    created_at TEXT,
    expires_at TEXT NOT NULL,
    decided_at TEXT
);
"""


def _legacy_db(tmp_path):
    conn = sqlite3.connect(tmp_path / "approvals.db")
    conn.execute(_LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO approval_record (approval_id, conversation_id, thread_id, summary, expires_at)"
        " VALUES ('A-1', 'c1', 't1', '旧记录', '2099-01-01T00:00:00+00:00')"
    )
    conn.commit()
    return conn


def test_migration_adds_columns_to_existing_table(tmp_path):
    conn = _legacy_db(tmp_path)
    ApprovalService(conn)

    columns = {row[1] for row in conn.execute("PRAGMA table_info(approval_record)")}
    assert {"payload_json", "projected_at"} <= columns
    # 老记录照常可读，新列为 NULL
    record = ApprovalService(conn).get("A-1")
    assert record["summary"] == "旧记录"
    assert record["payload_json"] is None
    assert record["projected_at"] is None


def test_migration_is_idempotent(tmp_path):
    conn = _legacy_db(tmp_path)
    ApprovalService(conn)
    ApprovalService(conn)  # 二次启动不报错

    columns = [row[1] for row in conn.execute("PRAGMA table_info(approval_record)")]
    assert columns.count("payload_json") == 1
    assert columns.count("projected_at") == 1


def test_insert_persists_payload_and_marks_projected(tmp_path):
    conn = _legacy_db(tmp_path)
    store = ApprovalService(conn)
    payload = {"approval_id": "A-2", "summary": "退款", "x_approval": {"facts": {"amount": 1}}}

    store.insert_if_absent("A-2", "c1", "t1", payload)
    record = store.get("A-2")
    assert json.loads(record["payload_json"])["x_approval"]["facts"]["amount"] == 1
    assert record["projected_at"] is None

    store.mark_projected("A-2")
    first = store.get("A-2")["projected_at"]
    assert first is not None

    store.mark_projected("A-2")  # 重复投影不刷新时间
    assert store.get("A-2")["projected_at"] == first


# --- T8 客户侧文案 -----------------------------------------------------------


def test_fund_moves_state_arrival_window():
    """资金类结果必须交代到账时效，否则客户下一秒就来问钱什么时候到。"""
    from ai_service_agent.graph.workflows.copy import approval_approved_text

    assert "1–3 个工作日" in approval_approved_text("refund_apply")
    assert "1–3 个工作日" in approval_approved_text("cancel_order")
    assert "1–3 个工作日" not in approval_approved_text("update_address")


# --- T7 留痕文案 -------------------------------------------------------------


def test_failure_reference_is_extracted_from_customer_copy():
    from ai_service_agent.graph.workflows.copy import (
        approval_failed_text,
        approval_failure_reference,
    )

    text = approval_failed_text("refund_apply", "db is locked")
    reference = approval_failure_reference(text)
    assert reference and len(reference) == 8
    assert approval_failure_reference("一切正常") is None
    assert approval_failure_reference(None) is None


def test_seat_note_composition():
    from ai_service_agent.graph.workflows.copy import approval_seat_decision_note

    assert approval_seat_decision_note("approved", actor="王琦", at="17:52") == "已批准 · 王琦 · 17:52"
    assert "理由：已发货" in approval_seat_decision_note(
        "rejected", actor="王琦", at="17:52", reason="已发货"
    )
    assert "处理编号 8f3a1c2b" in approval_seat_decision_note(
        "approved", actor="王琦", at="17:52", reference="8f3a1c2b"
    )
