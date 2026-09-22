"""模拟电商库（mock_ecommerce.db）离线测试：schema/FK 完整性、确定性、
order_lookup 数据源替换（夹具优先 → DB → not_found）、库缺失降级。"""

import importlib.util
import sqlite3
import tempfile
from pathlib import Path

import pytest

from ai_service_agent.tools import orders as orders_mod

ROOT = Path(__file__).resolve().parents[1]


def _load_seed_module():
    spec = importlib.util.spec_from_file_location(
        "seed_mock_db", ROOT / "scripts" / "seed_mock_db.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def seed():
    module = _load_seed_module()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        db_a = tmp / "a.db"
        db_b = tmp / "b.db"
        module.build_database(str(db_a))
        module.build_database(str(db_b))
        yield module, db_a, db_b


def _rows(db_path: Path) -> dict:
    conn = sqlite3.connect(db_path)
    try:
        tables = [
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        out = {}
        for t in sorted(tables):
            cols = [c[1] for c in conn.execute(f"PRAGMA table_info({t})")]
            rows = conn.execute(f"SELECT * FROM {t} ORDER BY {','.join(cols)}").fetchall()
            out[t] = rows
        return out
    finally:
        conn.close()


def test_schema_tables_and_fk_integrity(seed):
    _, db_a, _ = seed
    conn = sqlite3.connect(db_a)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {
            "customers", "products", "orders", "order_items",
            "shipments", "devices", "service_tickets", "conversations",
        } <= tables
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_deterministic_regeneration(seed):
    _, db_a, db_b = seed
    assert _rows(db_a) == _rows(db_b)


def test_canonical_fixture_orders_in_db(seed):
    _, db_a, _ = seed
    conn = sqlite3.connect(db_a)
    try:
        rows = {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT order_id, customer_id, status FROM orders"
            " WHERE order_id IN ('ORD-1','ORD-2','ORD-10086','ORD-CANCEL-1')"
        ).fetchall()}
    finally:
        conn.close()
    assert rows == {
        "ORD-1": ("u1", "delivered"),
        "ORD-2": ("u2", "delivered"),
        "ORD-10086": ("u1", "delivered"),
        "ORD-CANCEL-1": ("u1", "cancelled"),
    }


def _use_db(monkeypatch, db_path):
    class _S:
        mock_ecommerce_db_path = str(db_path)

    monkeypatch.setattr(orders_mod, "get_settings", lambda: _S())


def test_order_lookup_db_source_and_boundary(monkeypatch, seed):
    _, db_a, _ = seed
    _use_db(monkeypatch, db_a)
    conn = sqlite3.connect(db_a)
    try:
        db_order_id, owner = conn.execute(
            "SELECT order_id, customer_id FROM orders"
            " WHERE order_id LIKE 'ORD-2026-%' AND status='delivered' LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    result = orders_mod.order_lookup.invoke({"order_id": db_order_id, "user_id": owner})
    assert result["order_id"] == db_order_id
    assert result["status"] == "已送达"
    assert result["amount"] > 0
    assert result["user_id"] == owner

    other = orders_mod.order_lookup.invoke({"order_id": db_order_id, "user_id": "someone-else"})
    assert other == {"error": "data_boundary", "order_id": db_order_id}

    missing = orders_mod.order_lookup.invoke({"order_id": "ORD-NOPE", "user_id": owner})
    assert missing == {"error": "not_found", "order_id": "ORD-NOPE"}


def test_lookup_order_state_fixture_priority_and_db_mapping(monkeypatch, seed):
    _, db_a, _ = seed
    _use_db(monkeypatch, db_a)

    fixture = orders_mod.lookup_order_state("ORD-1")
    assert fixture["user_id"] == "u1"
    assert fixture["status"] == "signed"
    assert fixture["signed_at"] is not None

    conn = sqlite3.connect(db_a)
    try:
        db_order_id, owner = conn.execute(
            "SELECT order_id, customer_id FROM orders"
            " WHERE order_id LIKE 'ORD-2026-%' AND status='cancelled' LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    state = orders_mod.lookup_order_state(db_order_id)
    assert state["order_id"] == db_order_id
    assert state["user_id"] == owner
    assert state["status"] == "cancelled"
    assert state["cancelled_at"] is not None

    assert orders_mod.lookup_order_state("ORD-NOPE") is None


def test_db_missing_degrades(monkeypatch, seed):
    _use_db(monkeypatch, Path(tempfile.mkdtemp()) / "not-exist.db")

    fixture = orders_mod.order_lookup.invoke({"order_id": "ORD-1", "user_id": "u1"})
    assert fixture["order_id"] == "ORD-1"

    missing = orders_mod.order_lookup.invoke({"order_id": "ORD-NOPE", "user_id": "u1"})
    assert missing == {"error": "not_found", "order_id": "ORD-NOPE"}

    assert orders_mod.lookup_order_state("ORD-10086") is not None
    assert orders_mod.lookup_order_state("ORD-2026-10001") is None


def test_lookup_order_state_exposes_db_unavailability(monkeypatch):
    def broken_connect():
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(orders_mod, "_db_connect", broken_connect)

    assert orders_mod.lookup_order_state_with_availability("ORD-NOPE") == (None, False)
    assert orders_mod.lookup_order_state("ORD-NOPE") is None


def test_my_orders_lists_own_orders(monkeypatch, seed):
    """my_orders：零业务参数按登录态查询本人订单；状态过滤与无效值处理。"""
    _, db_a, _ = seed
    _use_db(monkeypatch, db_a)
    conn = sqlite3.connect(db_a)
    try:
        owner, n_orders = conn.execute(
            "SELECT customer_id, COUNT(*) FROM orders GROUP BY customer_id"
            " ORDER BY COUNT(*) DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    result = orders_mod.my_orders.invoke({"user_id": owner})
    assert result["count"] == min(n_orders, 10)
    assert all(o["order_id"] for o in result["orders"])
    ordered = [o["ordered_at"] for o in result["orders"]]
    assert ordered == sorted(ordered, reverse=True)

    filtered = orders_mod.my_orders.invoke({"status": "delivered", "user_id": owner})
    assert all(o["status"] == "已送达" for o in filtered["orders"])

    invalid = orders_mod.my_orders.invoke({"status": "hacked", "user_id": owner})
    assert invalid == {"error": "invalid_status", "status": "hacked"}

    empty = orders_mod.my_orders.invoke({"user_id": "u-nobody"})
    assert empty == {"orders": [], "count": 0}

    not_logged = orders_mod.my_orders.func(status=None, user_id=None)
    assert not_logged == {"error": "not_logged_in"}


def test_shipment_query_without_order_id(monkeypatch, seed):
    """shipment_query：缺省单号直接查本人物流，在途优先。"""
    _, db_a, _ = seed
    _use_db(monkeypatch, db_a)
    conn = sqlite3.connect(db_a)
    try:
        owner = conn.execute(
            "SELECT o.customer_id FROM orders o JOIN shipments s ON s.order_id = o.order_id"
            " WHERE o.status = 'shipped' LIMIT 1"
        ).fetchone()[0]
    finally:
        conn.close()

    result = orders_mod.shipment_query.invoke({"user_id": owner})
    assert result["count"] >= 1
    first = result["shipments"][0]
    assert first["carrier"] and first["tracking_no"]
    assert first["status"] in {"待发货", "运输中", "已签收"}
    assert all(s["order_id"] for s in result["shipments"])


def test_shipment_query_with_order_id_boundaries(monkeypatch, seed):
    """shipment_query：指定单号时属主校验 / not_found / 取消单语义。"""
    _, db_a, _ = seed
    _use_db(monkeypatch, db_a)
    conn = sqlite3.connect(db_a)
    try:
        owned = conn.execute(
            "SELECT o.order_id, o.customer_id FROM orders o"
            " JOIN shipments s ON s.order_id = o.order_id"
            " WHERE o.status = 'delivered' LIMIT 1"
        ).fetchone()
        other_owner = conn.execute(
            "SELECT customer_id FROM customers WHERE customer_id != ?", (owned[1],)
        ).fetchone()[0]
        cancelled = conn.execute(
            "SELECT order_id, customer_id FROM orders WHERE status = 'cancelled' LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    own = orders_mod.shipment_query.invoke({"order_id": owned[0], "user_id": owned[1]})
    assert own["count"] == 1
    assert own["shipments"][0]["status"] == "已签收"

    cross = orders_mod.shipment_query.invoke({"order_id": owned[0], "user_id": other_owner})
    assert cross == {"error": "data_boundary", "order_id": owned[0]}

    gone = orders_mod.shipment_query.invoke({"order_id": "ORD-NOPE", "user_id": owned[1]})
    assert gone == {"error": "not_found", "order_id": "ORD-NOPE"}

    cancelled_result = orders_mod.shipment_query.invoke(
        {"order_id": cancelled[0], "user_id": cancelled[1]}
    )
    assert cancelled_result["status"] == "已取消"
    assert "无物流" in cancelled_result["message"]


def test_new_read_tools_degrade_when_db_missing(monkeypatch, seed):
    """库缺失：my_orders / shipment_query 确定性降级，不抛异常。"""
    import tempfile

    _use_db(monkeypatch, Path(tempfile.mkdtemp()) / "not-exist.db")
    assert orders_mod.my_orders.func(status=None, user_id="u1") == {"error": "data_source_unavailable"}
    assert orders_mod.shipment_query.func(order_id=None, user_id="u1") == {"error": "data_source_unavailable"}


def test_find_own_cancellable_paid_orders_returns_owned_paid_orders(monkeypatch, seed):
    _, db_a, _ = seed

    class _Settings:
        mock_ecommerce_db_path = str(db_a)

    monkeypatch.setattr(orders_mod, "get_settings", lambda: _Settings())
    conn = sqlite3.connect(db_a)
    try:
        owner = conn.execute(
            "SELECT customer_id FROM orders WHERE status='paid' LIMIT 1"
        ).fetchone()[0]
    finally:
        conn.close()

    result = orders_mod.find_own_cancellable_paid_orders(owner)
    assert isinstance(result, list)
    assert result
    assert all(
        {"order_id", "total_amount", "ordered_at", "product_summary"} <= item.keys()
        for item in result
    )


def test_find_own_cancellable_paid_orders_rejects_missing_login(monkeypatch, seed):
    _, db_a, _ = seed

    class _Settings:
        mock_ecommerce_db_path = str(db_a)

    monkeypatch.setattr(orders_mod, "get_settings", lambda: _Settings())
    assert orders_mod.find_own_cancellable_paid_orders("") == {
        "error": "not_logged_in"
    }


def test_find_own_refundable_orders_returns_paid_and_completed(monkeypatch, seed):
    """Refund candidates include only the user's paid/completed orders."""
    _, db_a, _ = seed

    class _Settings:
        mock_ecommerce_db_path = str(db_a)

    monkeypatch.setattr(orders_mod, "get_settings", lambda: _Settings())
    conn = sqlite3.connect(db_a)
    try:
        owner = conn.execute(
            "SELECT customer_id FROM orders WHERE status IN ('paid','completed') LIMIT 1"
        ).fetchone()[0]
        expected_order_ids = {
            row[0]
            for row in conn.execute(
                "SELECT order_id FROM orders WHERE customer_id=? AND status IN ('paid','completed')",
                (owner,),
            )
        }
    finally:
        conn.close()

    result = orders_mod.find_own_refundable_orders(owner)
    assert isinstance(result, list)
    assert {item["order_id"] for item in result} == expected_order_ids
    assert all(
        {"order_id", "total_amount", "ordered_at", "product_summary"} <= item.keys()
        for item in result
    )
    assert all(isinstance(item["total_amount"], float) for item in result)


def test_find_own_refundable_orders_rejects_missing_login(monkeypatch, seed):
    _, db_a, _ = seed

    class _Settings:
        mock_ecommerce_db_path = str(db_a)

    monkeypatch.setattr(orders_mod, "get_settings", lambda: _Settings())
    assert orders_mod.find_own_refundable_orders("") == {"error": "not_logged_in"}


def test_order_items_summary_handles_owned_and_missing_orders(monkeypatch, seed):
    _, db_a, _ = seed

    class _Settings:
        mock_ecommerce_db_path = str(db_a)

    monkeypatch.setattr(orders_mod, "get_settings", lambda: _Settings())
    conn = sqlite3.connect(db_a)
    try:
        order_id = conn.execute(
            "SELECT o.order_id FROM orders o JOIN order_items oi ON oi.order_id=o.order_id LIMIT 1"
        ).fetchone()[0]
    finally:
        conn.close()

    assert orders_mod.order_items_summary(order_id)
    assert orders_mod.order_items_summary("ORD-NOPE") == "商品信息不可用"
