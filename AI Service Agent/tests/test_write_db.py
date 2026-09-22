"""Mutation 工具落模拟电商库测试：真实写入、复合幂等、数据边界与开票金额规则。"""

import importlib.util
import sqlite3
import tempfile
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langchain_core.messages import HumanMessage

from ai_service_agent.graph.builder import build_graph
from ai_service_agent.testing import ToolCallingFakeModel, router_json_message, select_json_message
from ai_service_agent.tools import orders as orders_mod
from ai_service_agent.tools import write as write_mod


ROOT = Path(__file__).resolve().parents[1]


def _load_seed_module():
    spec = importlib.util.spec_from_file_location(
        "seed_mock_db_write", ROOT / "scripts" / "seed_mock_db.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def seed():
    module = _load_seed_module()
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "write.db"
        module.build_database(str(db_path))
        yield module, db_path


def _use_db(monkeypatch, db_path: Path):
    class _Settings:
        mock_ecommerce_db_path = str(db_path)

    monkeypatch.setattr(write_mod, "get_settings", lambda: _Settings())
    monkeypatch.setattr(orders_mod, "get_settings", lambda: _Settings())


def _snapshot(db_path: Path):
    conn = sqlite3.connect(db_path)
    try:
        return {
            "customers": conn.execute("SELECT * FROM customers ORDER BY customer_id").fetchall(),
            "orders": conn.execute("SELECT * FROM orders ORDER BY order_id").fetchall(),
            "refunds": conn.execute("SELECT * FROM refunds ORDER BY refund_id").fetchall(),
            "invoices": conn.execute("SELECT * FROM invoices ORDER BY invoice_id").fetchall(),
            "service_tickets": conn.execute(
                "SELECT * FROM service_tickets ORDER BY ticket_id"
            ).fetchall(),
            "idempotency": conn.execute(
                "SELECT * FROM idempotency ORDER BY tool_name, request_id"
            ).fetchall(),
        }
    finally:
        conn.close()


def test_seed_schema_supports_mutations(seed):
    _, db_path = seed
    conn = sqlite3.connect(db_path)
    try:
        customers = {row[1] for row in conn.execute("PRAGMA table_info(customers)")}
        orders = {row[1] for row in conn.execute("PRAGMA table_info(orders)")}
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        conn.close()
    assert "address" in customers
    assert "note" in orders
    assert {"invoices", "idempotency"} <= tables


def test_account_updates_write_db_and_replay(monkeypatch, seed):
    _, db_path = seed
    _use_db(monkeypatch, db_path)
    first = write_mod.update_address.invoke(
        {"address": "北京市朝阳区测试路 1 号", "request_id": "addr-1", "user_id": "u1"}
    )
    replay = write_mod.update_address.invoke(
        {"address": "故意不同的地址", "request_id": "addr-1", "user_id": "u1"}
    )
    assert first["replayed"] is False and replay["replayed"] is True
    assert first["result"] == replay["result"]

    conn = sqlite3.connect(db_path)
    try:
        address = conn.execute("SELECT address FROM customers WHERE customer_id='u1'").fetchone()[0]
        count = conn.execute(
            "SELECT COUNT(*) FROM idempotency WHERE tool_name='update_address' AND request_id='addr-1'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert address == "北京市朝阳区测试路 1 号"
    assert count == 1

    phone = write_mod.update_phone.invoke(
        {"phone": "+65-80000000", "request_id": "phone-1", "user_id": "u1"}
    )
    assert phone["replayed"] is False
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT phone FROM customers WHERE customer_id='u1'").fetchone()[0] == "+65-80000000"
    finally:
        conn.close()


def test_composite_idempotency_prevents_cross_tool_replay(monkeypatch, seed):
    _, db_path = seed
    _use_db(monkeypatch, db_path)
    a = write_mod.update_address.invoke({"address": "复合键地址", "request_id": "same-1", "user_id": "u1"})
    b = write_mod.update_phone.invoke({"phone": "+65-81111111", "request_id": "same-1", "user_id": "u1"})
    assert a["replayed"] is False and b["replayed"] is False


def test_order_note_and_ticket_respect_boundary(monkeypatch, seed):
    _, db_path = seed
    _use_db(monkeypatch, db_path)
    before = _snapshot(db_path)
    conn = sqlite3.connect(db_path)
    try:
        before_b_order = conn.execute("SELECT * FROM orders WHERE order_id='ORD-2'").fetchone()
    finally:
        conn.close()

    denied = write_mod.update_order_note.invoke(
        {"order_id": "ORD-2", "note": "A 试图改 B", "request_id": "denied-1", "user_id": "u1"}
    )
    assert denied == {"error": "data_boundary", "order_id": "ORD-2"}
    not_found = write_mod.update_order_note.invoke(
        {"order_id": "ORD-NOPE", "note": "n", "request_id": "nf-1", "user_id": "u1"}
    )
    assert not_found == {"error": "not_found", "order_id": "ORD-NOPE"}

    allowed = write_mod.update_order_note.invoke(
        {"order_id": "ORD-1", "note": "放驿站即可", "request_id": "note-1", "user_id": "u1"}
    )
    assert allowed["replayed"] is False
    ticket = write_mod.create_service_ticket.invoke(
        {"order_id": "ORD-1", "reason": "屏幕有裂痕", "request_id": "ticket-1", "user_id": "u1"}
    )
    assert ticket["replayed"] is False

    conn = sqlite3.connect(db_path)
    try:
        note = conn.execute("SELECT note FROM orders WHERE order_id='ORD-1'").fetchone()[0]
        ticket_row = conn.execute(
            "SELECT customer_id, order_id, issue_type, status FROM service_tickets WHERE ticket_id=?",
            (ticket["result"]["ticket_id"],),
        ).fetchone()
        denied_order = conn.execute("SELECT * FROM orders WHERE order_id='ORD-2'").fetchone()
    finally:
        conn.close()
    assert note == "放驿站即可"
    assert ticket_row == ("u1", "ORD-1", "屏幕有裂痕", "open")
    assert denied_order in before["orders"]
    after_denied = _snapshot(db_path)
    assert after_denied["customers"] == before["customers"]
    conn = sqlite3.connect(db_path)
    try:
        after_b_order = conn.execute("SELECT * FROM orders WHERE order_id='ORD-2'").fetchone()
    finally:
        conn.close()
    assert after_b_order == before_b_order


def test_create_invoice_writes_and_replays(monkeypatch, seed):
    _, db_path = seed
    _use_db(monkeypatch, db_path)
    first = write_mod.create_invoice.invoke(
        {
            "order_id": "ORD-10086", "amount": 99.0, "company": "星辰科技",
            "tax_no": "91110108MA01AB12CD", "request_id": "invoice-1", "user_id": "u1",
        }
    )
    replay = write_mod.create_invoice.invoke(
        {
            "order_id": "ORD-10086", "amount": 999.0, "company": "不同参数",
            "tax_no": "different", "request_id": "invoice-1", "user_id": "u1",
        }
    )
    assert first["replayed"] is False and replay["replayed"] is True
    assert first["result"] == replay["result"]
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT customer_id, order_id, amount, company, tax_no FROM invoices WHERE order_id='ORD-10086'"
        ).fetchall()
    finally:
        conn.close()
    assert rows == [("u1", "ORD-10086", 99.0, "星辰科技", "91110108MA01AB12CD")]


def test_mutation_graph_rejects_cross_user_without_pollution(monkeypatch, seed):
    _, db_path = seed
    _use_db(monkeypatch, db_path)
    before = _snapshot(db_path)
    model = ToolCallingFakeModel(responses=[
        router_json_message("order", "mutation", 0.9),
        select_json_message("update_order_note", {"order_id": "ORD-2", "note": "越权修改"}),
    ])
    graph = build_graph(model, tools=[], checkpointer=MemorySaver())
    out = graph.invoke(
        {"messages": [HumanMessage(content="帮我改订单备注")], "user_id": "u1", "session_id": "boundary", "context": {}},
        {"configurable": {"thread_id": "boundary"}},
    )
    assert not out.get("__interrupt__")
    assert out["context"]["precheck_reason"] == "data_boundary"
    assert [m for m in out["messages"] if getattr(m, "type", "") == "tool"] == []
    after = _snapshot(db_path)
    assert after["orders"] == before["orders"]
    assert after["idempotency"] == before["idempotency"]


def test_mutation_graph_rejects_invoice_over_remaining(monkeypatch, seed):
    _, db_path = seed
    _use_db(monkeypatch, db_path)
    model = ToolCallingFakeModel(responses=[
        router_json_message("order", "mutation", 0.9),
        select_json_message(
            "create_invoice",
            {"order_id": "ORD-10086", "amount": 9999.0, "company": "A", "tax_no": "T"},
        ),
    ])
    graph = build_graph(model, tools=[], checkpointer=MemorySaver())
    out = graph.invoke(
        {"messages": [HumanMessage(content="帮我开发票")], "user_id": "u1", "session_id": "invoice", "context": {}},
        {"configurable": {"thread_id": "invoice"}},
    )
    assert not out.get("__interrupt__")
    assert out["context"]["precheck_reason"] == "invoice_amount_exceeds"
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 1
    finally:
        conn.close()


def test_cancel_order_success_replay_and_business_rejection(monkeypatch, seed):
    _, db_path = seed
    _use_db(monkeypatch, db_path)
    conn = sqlite3.connect(db_path)
    try:
        owner, paid_order = conn.execute(
            "SELECT customer_id, order_id FROM orders WHERE status='paid' LIMIT 1"
        ).fetchone()
        cancelled_owner, cancelled_order = conn.execute(
            "SELECT customer_id, order_id FROM orders WHERE status='cancelled' LIMIT 1"
        ).fetchone()
        shipped_owner, shipped_order = conn.execute(
            "SELECT customer_id, order_id FROM orders WHERE status NOT IN ('paid','cancelled') LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    before = _snapshot(db_path)
    first = write_mod.cancel_order.invoke(
        {
            "order_id": paid_order,
            "reason": "不想要了",
            "request_id": "cancel-1",
            "user_id": owner,
        }
    )
    assert first["replayed"] is False
    assert first["result"]["status"] == "cancelled"
    assert first["result"]["cancelled_at"]

    replay = write_mod.cancel_order.invoke(
        {
            "order_id": paid_order,
            "reason": "换一个理由",
            "request_id": "cancel-1",
            "user_id": owner,
        }
    )
    assert replay["replayed"] is True
    assert replay["result"] == first["result"]

    after_cancel = _snapshot(db_path)
    assert after_cancel["orders"] != before["orders"]
    assert after_cancel["idempotency"] != before["idempotency"]

    already = write_mod.cancel_order.invoke(
        {
            "order_id": cancelled_order,
            "request_id": "cancel-already",
            "user_id": cancelled_owner,
        }
    )
    assert already == {"error": "order_already_cancelled", "order_id": cancelled_order}

    not_allowed = write_mod.cancel_order.invoke(
        {
            "order_id": shipped_order,
            "request_id": "cancel-not-allowed",
            "user_id": shipped_owner,
        }
    )
    assert not_allowed["error"] == "cancel_not_allowed"
    assert not_allowed["order_id"] == shipped_order

    after_rejections = _snapshot(db_path)
    assert after_rejections["orders"] == after_cancel["orders"]
    assert after_rejections["idempotency"] == after_cancel["idempotency"]


def test_cancel_order_replay_is_bound_to_requester(monkeypatch, seed):
    _, db_path = seed
    _use_db(monkeypatch, db_path)
    conn = sqlite3.connect(db_path)
    try:
        owner, order_id = conn.execute(
            "SELECT customer_id, order_id FROM orders WHERE status='paid' LIMIT 1"
        ).fetchone()
        attacker = "u1" if owner != "u1" else "u2"
    finally:
        conn.close()

    first = write_mod.cancel_order.invoke(
        {
            "order_id": order_id,
            "reason": "owner cancel",
            "request_id": "cancel-replay-boundary",
            "user_id": owner,
        }
    )
    assert first["replayed"] is False
    assert first["result"]["status"] == "cancelled"

    conn = sqlite3.connect(db_path)
    try:
        key_count = conn.execute(
            "SELECT COUNT(*) FROM idempotency WHERE tool_name='cancel_order' AND request_id='cancel-replay-boundary'"
        ).fetchone()[0]
    finally:
        conn.close()

    replay = write_mod.cancel_order.invoke(
        {
            "order_id": order_id,
            "reason": "attacker replay",
            "request_id": "cancel-replay-boundary",
            "user_id": attacker,
        }
    )
    assert replay == {"error": "data_boundary", "order_id": order_id}

    conn = sqlite3.connect(db_path)
    try:
        after_key_count = conn.execute(
            "SELECT COUNT(*) FROM idempotency WHERE tool_name='cancel_order' AND request_id='cancel-replay-boundary'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert after_key_count == key_count == 1


def test_cancel_order_blocks_cross_user_without_pollution(monkeypatch, seed):
    _, db_path = seed
    _use_db(monkeypatch, db_path)
    conn = sqlite3.connect(db_path)
    try:
        victim_owner, order_id = conn.execute(
            "SELECT customer_id, order_id FROM orders WHERE status='paid' LIMIT 1"
        ).fetchone()
        attacker = "u1" if victim_owner != "u1" else "u2"
        before = conn.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
    finally:
        conn.close()

    denied = write_mod.cancel_order.invoke(
        {
            "order_id": order_id,
            "reason": "越权取消",
            "request_id": "cancel-cross-user",
            "user_id": attacker,
        }
    )
    assert denied == {"error": "data_boundary", "order_id": order_id}

    conn = sqlite3.connect(db_path)
    try:
        after = conn.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
        key_count = conn.execute(
            "SELECT COUNT(*) FROM idempotency WHERE tool_name='cancel_order' AND request_id='cancel-cross-user'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert after == before
    assert key_count == 0


def test_refund_apply_full_amount_writes_and_replays(monkeypatch, seed):
    """Full refund: amount=None defaults to total_amount; order -> refunding; idempotency row."""
    _, db_path = seed
    _use_db(monkeypatch, db_path)
    conn = sqlite3.connect(db_path)
    try:
        owner, order_id, total = conn.execute(
            "SELECT customer_id, order_id, total_amount FROM orders WHERE status='paid' LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    first = write_mod.refund_apply.invoke(
        {"order_id": order_id, "request_id": "ref-1", "user_id": owner}
    )
    assert first["replayed"] is False
    assert first["result"]["status"] == "refunding"
    assert first["result"]["amount"] == total
    assert first["result"]["refund_id"]
    conn = sqlite3.connect(db_path)
    try:
        status = conn.execute("SELECT status FROM orders WHERE order_id=?", (order_id,)).fetchone()[0]
        refund = conn.execute("SELECT amount, status FROM refunds WHERE order_id=?", (order_id,)).fetchone()
        idem = conn.execute("SELECT tool_name, request_id FROM idempotency WHERE tool_name='refund_apply'").fetchall()
    finally:
        conn.close()
    assert status == "refunding"
    assert refund[0] == total
    assert refund[1] == "refunding"
    assert len(idem) == 1
    replay = write_mod.refund_apply.invoke(
        {"order_id": order_id, "request_id": "ref-1", "user_id": owner}
    )
    assert replay["replayed"] is True


def test_refund_apply_partial_amount_tracks_cumulative(monkeypatch, seed):
    """Partial refund: amount=50; second partial allowed if cumulative <= total."""
    _, db_path = seed
    _use_db(monkeypatch, db_path)
    conn = sqlite3.connect(db_path)
    try:
        owner, order_id, total = conn.execute(
            "SELECT customer_id, order_id, total_amount FROM orders WHERE status='paid' AND total_amount > 100 LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    half = round(total / 2, 2)
    first = write_mod.refund_apply.invoke(
        {"order_id": order_id, "amount": half, "request_id": "ref-p1", "user_id": owner}
    )
    assert first["replayed"] is False
    second = write_mod.refund_apply.invoke(
        {"order_id": order_id, "amount": half, "request_id": "ref-p2", "user_id": owner}
    )
    assert second["replayed"] is False
    third = write_mod.refund_apply.invoke(
        {"order_id": order_id, "amount": 0.01, "request_id": "ref-p3", "user_id": owner}
    )
    assert third == {"error": "refund_amount_exceeds", "order_id": order_id}


def test_refund_apply_blocks_wrong_status_and_cross_user(monkeypatch, seed):
    """Non-refundable status or cross-user -> error, no DB pollution."""
    _, db_path = seed
    _use_db(monkeypatch, db_path)
    conn = sqlite3.connect(db_path)
    try:
        shipped_owner, shipped_order = conn.execute(
            "SELECT customer_id, order_id FROM orders WHERE status='shipped' LIMIT 1"
        ).fetchone()
        _, other_order = conn.execute(
            "SELECT customer_id, order_id FROM orders WHERE status='paid' LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    result = write_mod.refund_apply.invoke(
        {"order_id": shipped_order, "request_id": "ref-s1", "user_id": shipped_owner}
    )
    assert result == {"error": "refund_not_allowed", "order_id": shipped_order, "status": "shipped"}
    cross = write_mod.refund_apply.invoke(
        {"order_id": other_order, "request_id": "ref-x1", "user_id": "nobody"}
    )
    assert cross == {"error": "data_boundary", "order_id": other_order}


def test_refund_cross_user_blocked_without_pollution(monkeypatch, seed):
    _, db_path = seed
    _use_db(monkeypatch, db_path)
    conn = sqlite3.connect(db_path)
    try:
        victim_owner, order_id = conn.execute(
            "SELECT customer_id, order_id FROM orders WHERE status='paid' LIMIT 1"
        ).fetchone()
        attacker = "u1" if victim_owner != "u1" else "u2"
        before_order = conn.execute(
            "SELECT * FROM orders WHERE order_id=?", (order_id,)
        ).fetchone()
        before_refunds = conn.execute("SELECT * FROM refunds ORDER BY refund_id").fetchall()
        before_idem = conn.execute("SELECT * FROM idempotency ORDER BY tool_name, request_id").fetchall()
    finally:
        conn.close()

    denied = write_mod.refund_apply.invoke(
        {
            "order_id": order_id,
            "amount": 1.0,
            "request_id": "refund-cross-user",
            "user_id": attacker,
        }
    )
    assert denied == {"error": "data_boundary", "order_id": order_id}

    conn = sqlite3.connect(db_path)
    try:
        after_order = conn.execute(
            "SELECT * FROM orders WHERE order_id=?", (order_id,)
        ).fetchone()
        after_refunds = conn.execute("SELECT * FROM refunds ORDER BY refund_id").fetchall()
        after_idem = conn.execute(
            "SELECT * FROM idempotency ORDER BY tool_name, request_id"
        ).fetchall()
    finally:
        conn.close()
    assert after_order == before_order
    assert after_refunds == before_refunds
    assert after_idem == before_idem
