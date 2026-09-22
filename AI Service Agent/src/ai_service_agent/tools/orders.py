import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState
from pydantic import BaseModel, ConfigDict

from ai_service_agent.config import get_settings


class OrderLookupArgs(BaseModel):
    """order_lookup 的 select 侧契约（P5 B1）：仅业务字段 + request_id（select 注入）；
    user_id 由 langgraph.prebuilt.InjectedState 注入，不入 schema。"""
    model_config = ConfigDict(extra="forbid")
    order_id: str
    request_id: str


class MyOrdersArgs(BaseModel):
    """my_orders 的 select 侧契约：status 可选过滤；user_id 由 InjectedState 注入。"""
    model_config = ConfigDict(extra="forbid")
    status: str | None = None
    request_id: str


class ShipmentQueryArgs(BaseModel):
    """shipment_query 的 select 侧契约：order_id 可选（缺省查本人名下物流）；
    user_id 由 InjectedState 注入，永不由模型/用户提供。"""
    model_config = ConfigDict(extra="forbid")
    order_id: str | None = None
    request_id: str


def _days_ago(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


# 确定性种子订单状态（D3，P4）：供离线 eval 与测试；signed_at 相对 now 生成，
# 保证「签收超 7 天窗口」（CTRL-0086）与「已取消」（CTRL-0090）恒成立。
ORDER_STORE: dict[str, dict] = {
    "ORD-1": {"order_id": "ORD-1", "user_id": "u1", "status": "signed", "signed_at": _days_ago(2), "amount": 99.0},
    "ORD-2": {"order_id": "ORD-2", "user_id": "u2", "status": "signed", "signed_at": _days_ago(2), "amount": 99.0},  # 同事订单（跨用户边界，D4）
    "ORD-10086": {"order_id": "ORD-10086", "user_id": "u1", "status": "signed", "signed_at": _days_ago(10), "amount": 99.0},
    "ORD-CANCEL-1": {
        "order_id": "ORD-CANCEL-1",
        "user_id": "u1",
        "status": "cancelled",
        "signed_at": _days_ago(20),
        "cancelled_at": _days_ago(3),
        "amount": 99.0,
    },
}

# 模拟电商库（data/mock_ecommerce.db，scripts/seed_mock_db.py 生成）：order_lookup
# 真实数据源替代品。解析顺序 = ORDER_STORE（评测夹具，相对时间语义）→ DB → not_found，
# 与 kb_search stub→真实检索同一模式；工具 ToolMeta.schema 与对外接口不变。
_DB_MACHINE_STATUS = {
    "paid": "paid",
    "shipped": "shipped",
    "delivered": "signed",
    "completed": "completed",
    "refunding": "refunding",
    "refunded": "refunded",
    "cancelled": "cancelled",
}
_DISPLAY_STATUS = {
    "paid": "已付款", "shipped": "已发货", "signed": "已发货",
    "delivered": "已送达", "completed": "已完成",
    "refunding": "退款中", "refunded": "已退款", "cancelled": "已取消",
}
_SHIPMENT_DISPLAY = {
    "pending": "待发货",
    "in_transit": "运输中",
    "delivered": "已签收",
}
_ORDER_STATUS_FILTERS = set(_DISPLAY_STATUS) - {"signed"}


def _db_connect() -> sqlite3.Connection:
    """模拟电商库只读连接（mode=ro URI）：工具层物理不可写。"""
    path = get_settings().mock_ecommerce_db_path
    return sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True, timeout=2)


def _db_order_record_with_availability(order_id: str) -> tuple[dict | None, bool]:
    """只读查询模拟电商库；库缺失 / 损坏 -> None（确定性降级到夹具层 / not_found）。"""
    try:
        conn = _db_connect()
    except (sqlite3.Error, OSError):
        return None, False
    try:
        row = conn.execute(
            "SELECT order_id, customer_id, status, signed_at, cancelled_at, total_amount"
            " FROM orders WHERE order_id = ?",
            (order_id,),
        ).fetchone()
    except sqlite3.Error:
        return None, False
    finally:
        conn.close()
    if row is None:
        return None, True
    return {
        "order_id": row[0],
        "user_id": row[1],
        "status": row[2],
        "signed_at": datetime.fromisoformat(row[3]) if row[3] else None,
        "cancelled_at": datetime.fromisoformat(row[4]) if row[4] else None,
        "amount": row[5],
    }, True


def _db_order_record(order_id: str) -> dict | None:
    """兼容查询：只返回事实；数据源可用性由 lookup_order_state_with_availability 暴露。"""
    return _db_order_record_with_availability(order_id)[0]


def _db_invoiced_total(order_id: str) -> float:
    """订单已开票累计（只读）；库缺失/损坏按 0 处理，业务裁决由预检层负责。"""
    try:
        conn = _db_connect()
    except (sqlite3.Error, OSError):
        return 0.0
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM invoices WHERE order_id=?", (order_id,)
        ).fetchone()
    except sqlite3.Error:
        return 0.0
    finally:
        conn.close()
    return float(row[0]) if row else 0.0


def _db_occupied_refund_total(order_id: str) -> float:
    """已占用退款额度（refunding + refunded）；库缺失/损坏按 0 处理。"""
    try:
        conn = _db_connect()
    except (sqlite3.Error, OSError):
        return 0.0
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM refunds"
            " WHERE order_id=? AND status IN ('refunding','refunded')",
            (order_id,),
        ).fetchone()
    except sqlite3.Error:
        return 0.0
    finally:
        conn.close()
    return float(row[0]) if row else 0.0


def find_own_cancellable_paid_orders(user_id: str) -> list[dict] | dict:
    """查询本人可取消的已付款订单；只读，不做裁决，最多返回最近 10 笔。"""
    if not user_id:
        return {"error": "not_logged_in"}
    try:
        conn = _db_connect()
    except (sqlite3.Error, OSError):
        return {"error": "data_source_unavailable"}
    try:
        rows = conn.execute(
            """
            SELECT o.order_id, o.total_amount, o.ordered_at,
                   COALESCE(GROUP_CONCAT(p.product_name || ' x' || oi.quantity), '')
            FROM orders o
            LEFT JOIN order_items oi ON oi.order_id = o.order_id
            LEFT JOIN products p ON p.product_id = oi.product_id
            WHERE o.customer_id = ? AND o.status = 'paid'
            GROUP BY o.order_id, o.total_amount, o.ordered_at
            ORDER BY o.ordered_at DESC
            LIMIT 10
            """,
            (user_id,),
        ).fetchall()
    except sqlite3.Error:
        return {"error": "data_source_unavailable"}
    finally:
        conn.close()
    return [
        {
            "order_id": row[0],
            "total_amount": float(row[1]),
            "ordered_at": row[2],
            "product_summary": row[3] or "商品信息不可用",
        }
        for row in rows
    ]


def find_own_refundable_orders(user_id: str) -> list[dict] | dict:
    """查询本人可退款的 paid/completed 订单；只读，最多返回最近 10 笔。"""
    if not user_id:
        return {"error": "not_logged_in"}
    try:
        conn = _db_connect()
    except (sqlite3.Error, OSError):
        return {"error": "data_source_unavailable"}
    try:
        rows = conn.execute(
            """
            SELECT o.order_id, o.total_amount, o.ordered_at,
                   COALESCE(GROUP_CONCAT(p.product_name || ' x' || oi.quantity), '')
            FROM orders o
            LEFT JOIN order_items oi ON oi.order_id = o.order_id
            LEFT JOIN products p ON p.product_id = oi.product_id
            WHERE o.customer_id = ? AND o.status IN ('paid', 'completed')
            GROUP BY o.order_id, o.total_amount, o.ordered_at
            ORDER BY o.ordered_at DESC
            LIMIT 10
            """,
            (user_id,),
        ).fetchall()
    except sqlite3.Error:
        return {"error": "data_source_unavailable"}
    finally:
        conn.close()
    return [
        {
            "order_id": row[0],
            "total_amount": float(row[1]),
            "ordered_at": row[2],
            "product_summary": row[3] or "商品信息不可用",
        }
        for row in rows
    ]


def order_items_summary(order_id: str) -> str:
    """审批卡片用商品摘要；库缺失或订单不存在时确定性降级。"""
    try:
        conn = _db_connect()
    except (sqlite3.Error, OSError):
        return "商品信息不可用"
    try:
        rows = conn.execute(
            """
            SELECT p.product_name, oi.quantity
            FROM order_items oi
            JOIN products p ON p.product_id = oi.product_id
            WHERE oi.order_id = ?
            ORDER BY oi.id
            """,
            (order_id,),
        ).fetchall()
    except sqlite3.Error:
        return "商品信息不可用"
    finally:
        conn.close()
    if not rows:
        return "商品信息不可用"
    return "、".join(f"{name} ×{quantity}" for name, quantity in rows)


def lookup_order_state_with_availability(order_id: str) -> tuple[dict | None, bool]:
    """订单状态事实查询（D3，P4）：夹具优先 -> 模拟电商库 -> None；仅返回事实，不做授权/业务裁决。"""
    fixture = ORDER_STORE.get(order_id)
    if fixture is not None:
        db_record, _ = _db_order_record_with_availability(order_id)
        if db_record is not None:
            return {
                **fixture,
                "total_amount": db_record["amount"],
                "invoiced_amount": _db_invoiced_total(order_id),
                "occupied_refund_amount": _db_occupied_refund_total(order_id),
            }, True
        return {
            **fixture,
            "total_amount": fixture["amount"],
            "invoiced_amount": 0.0,
            "occupied_refund_amount": 0.0,
        }, True
    record, db_available = _db_order_record_with_availability(order_id)
    if not db_available:
        return None, False
    if record is None:
        return None, True
    return {
        "order_id": record["order_id"],
        "user_id": record["user_id"],
        "status": _DB_MACHINE_STATUS.get(record["status"], record["status"]),
        "signed_at": record["signed_at"],
        "cancelled_at": record["cancelled_at"],
        "total_amount": record["amount"],
        "invoiced_amount": _db_invoiced_total(order_id),
        "occupied_refund_amount": _db_occupied_refund_total(order_id),
    }, True


def lookup_order_state(order_id: str) -> dict | None:
    """订单状态事实查询；数据源不可用与不存在同样返回 None 以保持既有公共行为。"""
    return lookup_order_state_with_availability(order_id)[0]


@tool
def order_lookup(order_id: str, user_id: Annotated[str, InjectedState("user_id")]) -> dict:
    """订单查询（D4 属主校验）：仅返回本人订单；越权返回 data_boundary（safety-spec §1.4，
    数据边界由工具自身保证，ReadGraph 不增加 precheck）。数据源 = 模拟电商库（夹具优先）。"""
    rec = ORDER_STORE.get(order_id) or _db_order_record(order_id)
    if rec is None:
        return {"error": "not_found", "order_id": order_id}
    if rec["user_id"] != user_id:
        return {"error": "data_boundary", "order_id": order_id}
    display = _DISPLAY_STATUS.get(rec["status"], rec["status"])
    return {"order_id": order_id, "status": display, "amount": rec["amount"], "user_id": user_id}


@tool
def my_orders(
    status: Annotated[str | None, "可选状态过滤，缺省查全部"] = None,
    *,
    user_id: Annotated[str, InjectedState("user_id")],
) -> dict:
    """我的订单列表（read，只读）：查询当前登录用户名下最近 10 笔订单，无需提供订单号。

    当用户询问「我的订单」「我买过什么」「退款单进度」等需要罗列本人订单时调用。
    status 可选：paid/shipped/delivered/completed/refunding/refunded/cancelled。
    只返回登录用户本人的订单；数据源不可用返回 data_source_unavailable。
    """
    if not user_id:
        return {"error": "not_logged_in"}
    if status is not None and status not in _ORDER_STATUS_FILTERS:
        return {"error": "invalid_status", "status": status}
    try:
        conn = _db_connect()
    except (sqlite3.Error, OSError):
        return {"error": "data_source_unavailable"}
    try:
        sql = (
            "SELECT order_id, status, ordered_at, total_amount, currency FROM orders"
            " WHERE customer_id = ?"
        )
        params: list = [user_id]
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY ordered_at DESC LIMIT 10"
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.Error:
        return {"error": "data_source_unavailable"}
    finally:
        conn.close()
    return {
        "orders": [
            {
                "order_id": r[0],
                "status": _DISPLAY_STATUS.get(r[1], r[1]),
                "ordered_at": r[2],
                "amount": r[3],
                "currency": r[4],
            }
            for r in rows
        ],
        "count": len(rows),
    }


@tool
def shipment_query(
    order_id: Annotated[str | None, "可选订单号；缺省时查询本人名下订单物流"] = None,
    *,
    user_id: Annotated[str, InjectedState("user_id")],
) -> dict:
    """物流查询（read，只读）：查询当前登录用户订单的物流信息，无需提供账号或订单号。

    当用户询问「我的快递到哪了」「什么时候送到」等物流问题时调用。
    order_id 可选：提供则精确查询该单物流（仅限本人订单）；缺省则优先返回在途订单物流（最多 5 条）。
    越权/不存在的订单号返回 not_found 或 data_boundary；数据源不可用返回 data_source_unavailable。
    """
    if not user_id:
        return {"error": "not_logged_in"}
    try:
        conn = _db_connect()
    except (sqlite3.Error, OSError):
        return {"error": "data_source_unavailable"}
    try:
        if order_id is not None:
            order = conn.execute(
                "SELECT customer_id, status FROM orders WHERE order_id = ?", (order_id,)
            ).fetchone()
            if order is None:
                return {"error": "not_found", "order_id": order_id}
            if order[0] != user_id:
                return {"error": "data_boundary", "order_id": order_id}
            if order[1] == "cancelled":
                return {"order_id": order_id, "status": "已取消", "message": "订单已取消，无物流信息"}
            rows = conn.execute(
                "SELECT o.order_id, o.status, s.carrier, s.tracking_no, s.status, s.shipped_at, s.eta"
                " FROM orders o JOIN shipments s ON s.order_id = o.order_id"
                " WHERE o.order_id = ? AND o.customer_id = ?",
                (order_id, user_id),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT o.order_id, o.status, s.carrier, s.tracking_no, s.status, s.shipped_at, s.eta"
                " FROM orders o JOIN shipments s ON s.order_id = o.order_id"
                " WHERE o.customer_id = ?"
                " ORDER BY CASE o.status WHEN 'shipped' THEN 0 WHEN 'paid' THEN 1 ELSE 2 END,"
                " o.ordered_at DESC LIMIT 5",
                (user_id,),
            ).fetchall()
    except sqlite3.Error:
        return {"error": "data_source_unavailable"}
    finally:
        conn.close()
    return {
        "shipments": [
            {
                "order_id": r[0],
                "order_status": _DISPLAY_STATUS.get(r[1], r[1]),
                "carrier": r[2],
                "tracking_no": r[3],
                "status": _SHIPMENT_DISPLAY.get(r[4], r[4]),
                "shipped_at": r[5],
                "eta": r[6],
            }
            for r in rows
        ],
        "count": len(rows),
    }
