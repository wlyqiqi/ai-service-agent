"""Order domain boundary used by HighRisk workflows and write tools.

Graph nodes must depend on the repository interface, never on SQLite, the
mock database path, or the table schema.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import json
import sqlite3
from uuid import uuid4


class OrderRepositoryUnavailable(RuntimeError):
    """Raised when the configured order source cannot answer a query."""


@dataclass(frozen=True)
class OrderSnapshot:
    order_id: str
    user_id: str
    status: str
    total_amount: float
    invoiced_amount: float = 0.0
    occupied_refund_amount: float = 0.0
    signed_at: datetime | None = None
    cancelled_at: datetime | None = None
    product_summary: str = "商品信息不可用"
    version: str | None = None

    def to_precheck_data(self) -> dict:
        return {
            "order_id": self.order_id,
            "user_id": self.user_id,
            "status": self.status,
            "total_amount": self.total_amount,
            "invoiced_amount": self.invoiced_amount,
            "occupied_refund_amount": self.occupied_refund_amount,
            "signed_at": self.signed_at,
            "cancelled_at": self.cancelled_at,
            "product_summary": self.product_summary,
            "version": self.version,
        }


class OrderRepository(ABC):
    """Order facts and commands needed by the customer-service graph."""

    @abstractmethod
    def get_order_snapshot(self, order_id: str, user_id: str) -> OrderSnapshot | None:
        raise NotImplementedError

    @abstractmethod
    def list_actionable_orders(self, user_id: str, action: str) -> list[dict]:
        raise NotImplementedError

    @abstractmethod
    def list_order_picker_candidates(self, user_id: str, tool: str) -> list[dict]:
        """Order Picker rows for a known mutation tool. Empty when the tool has no list."""
        raise NotImplementedError

    @abstractmethod
    def get_user_profile(self, user_id: str) -> dict | None:
        """Customer profile for Transport presentation. None when the user is unknown."""
        raise NotImplementedError

    @abstractmethod
    def list_user_orders(self, user_id: str, *, address_editable: bool = False) -> list[dict] | None:
        """Orders for Transport forms. None when the user is unknown; empty list when none match."""
        raise NotImplementedError

    @abstractmethod
    def cancel_order(
        self,
        order_id: str,
        user_id: str,
        reason: str | None,
        request_id: str,
    ) -> dict:
        raise NotImplementedError

    @abstractmethod
    def refund_apply(
        self,
        order_id: str,
        user_id: str,
        amount: float | None,
        reason: str | None,
        request_id: str,
    ) -> dict:
        raise NotImplementedError


_STATUS_MAP = {
    "paid": "paid",
    "shipped": "shipped",
    "delivered": "signed",
    "completed": "completed",
    "refunding": "refunding",
    "refunded": "refunded",
    "cancelled": "cancelled",
}
ORDER_DISPLAY_STATUS = {
    "paid": "已付款",
    "shipped": "已发货",
    "signed": "已发货",
    "delivered": "已送达",
    "completed": "已完成",
    "refunding": "退款中",
    "refunded": "已退款",
    "cancelled": "已取消",
}
# Raw DB statuses eligible for address edits (pre-fulfillment).
ADDRESS_EDITABLE_STATUSES = frozenset({"paid"})
# Shared with safety precheck (single source — avoid A/B/safety drift).
REFUND_WINDOW_DAYS = 7
_CUMULATIVE_REFUND_STATUSES = ("paid", "signed", "refunding")
# Raw orders.status values. Address edits stay pre-fulfillment; other mutation
# tools list orders that are not cancelled or in a refund terminal state.
_ORDER_PICKER_STATUSES = {
    "update_address": ("paid",),
    "update_phone": ("paid", "shipped", "delivered", "completed"),
    "update_order_note": ("paid", "shipped", "delivered", "completed"),
    "create_invoice": ("paid", "shipped", "delivered", "completed"),
    "create_service_ticket": ("paid", "shipped", "delivered", "completed"),
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class SQLiteOrderRepository(OrderRepository):
    """SQLite-backed adapter; the only place that knows the order schema."""

    def __init__(self, db_path: str):
        self.db_path = db_path

    def _connect(self, *, write: bool = False) -> sqlite3.Connection:
        path = Path(self.db_path).resolve()
        if write:
            conn = sqlite3.connect(path, timeout=2)
            conn.execute("PRAGMA foreign_keys = ON")
            return conn
        return sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=2)

    def get_order_snapshot(self, order_id: str, user_id: str) -> OrderSnapshot | None:
        del user_id  # Ownership is checked by the caller; snapshot is a raw fact.
        try:
            conn = self._connect()
        except (sqlite3.Error, OSError) as exc:
            raise OrderRepositoryUnavailable(str(exc)) from exc
        try:
            row = conn.execute(
                """
                SELECT order_id, customer_id, status, signed_at, cancelled_at,
                       total_amount, rowid
                  FROM orders WHERE order_id = ?
                """,
                (order_id,),
            ).fetchone()
            if row is None:
                return None
            invoiced = float(
                conn.execute(
                    "SELECT COALESCE(SUM(amount), 0) FROM invoices WHERE order_id = ?",
                    (order_id,),
                ).fetchone()[0]
            )
            occupied = float(
                conn.execute(
                    "SELECT COALESCE(SUM(amount), 0) FROM refunds"
                    " WHERE order_id = ? AND status IN ('refunding', 'refunded')",
                    (order_id,),
                ).fetchone()[0]
            )
            items = conn.execute(
                """
                SELECT p.product_name, oi.quantity
                  FROM order_items oi
                  JOIN products p ON p.product_id = oi.product_id
                 WHERE oi.order_id = ?
                 ORDER BY oi.id
                """,
                (order_id,),
            ).fetchall()
        except sqlite3.Error as exc:
            raise OrderRepositoryUnavailable(str(exc)) from exc
        finally:
            conn.close()
        return OrderSnapshot(
            order_id=row[0],
            user_id=row[1],
            status=_STATUS_MAP.get(row[2], row[2]),
            total_amount=float(row[5]),
            invoiced_amount=invoiced,
            occupied_refund_amount=occupied,
            signed_at=datetime.fromisoformat(row[3]) if row[3] else None,
            cancelled_at=datetime.fromisoformat(row[4]) if row[4] else None,
            product_summary=(
                "、".join(f"{name} ×{quantity}" for name, quantity in items)
                or "商品信息不可用"
            ),
            version=f"{row[6]}:{row[2]}:{row[3] or ''}:{row[4] or ''}",
        )

    def list_actionable_orders(self, user_id: str, action: str) -> list[dict]:
        if action == "cancel_order":
            statuses = ("paid",)
        elif action == "refund_apply":
            statuses = ("paid", "completed")
        else:
            raise ValueError(f"unsupported actionable order action: {action}")
        return self._list_orders_for_customer(user_id, statuses)

    def list_order_picker_candidates(self, user_id: str, tool: str) -> list[dict]:
        statuses = _ORDER_PICKER_STATUSES.get(tool)
        if not user_id or not statuses:
            return []
        return self._list_orders_for_customer(user_id, statuses)

    def get_user_profile(self, user_id: str) -> dict | None:
        if not user_id:
            return None
        try:
            conn = self._connect()
        except (sqlite3.Error, OSError) as exc:
            raise OrderRepositoryUnavailable(str(exc)) from exc
        try:
            row = conn.execute(
                "SELECT phone, address FROM customers WHERE customer_id = ?",
                (user_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise OrderRepositoryUnavailable(str(exc)) from exc
        finally:
            conn.close()
        if row is None:
            return None
        return {"user_id": user_id, "phone": row[0], "address": row[1]}

    def list_user_orders(self, user_id: str, *, address_editable: bool = False) -> list[dict] | None:
        if not user_id:
            return None
        try:
            conn = self._connect()
        except (sqlite3.Error, OSError) as exc:
            raise OrderRepositoryUnavailable(str(exc)) from exc
        try:
            exists = conn.execute(
                "SELECT 1 FROM customers WHERE customer_id = ?",
                (user_id,),
            ).fetchone()
            if exists is None:
                return None
            sql = (
                "SELECT order_id, status, ordered_at, total_amount, currency "
                "FROM orders WHERE customer_id = ?"
            )
            params: list = [user_id]
            if address_editable:
                editable = tuple(sorted(ADDRESS_EDITABLE_STATUSES))
                placeholders = ",".join("?" for _ in editable)
                sql += f" AND status IN ({placeholders})"
                params.extend(editable)
            sql += " ORDER BY ordered_at DESC LIMIT 20"
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            raise OrderRepositoryUnavailable(str(exc)) from exc
        finally:
            conn.close()
        return [
            {
                "order_id": row[0],
                "status_display": ORDER_DISPLAY_STATUS.get(row[1], row[1]),
                "ordered_at": row[2],
                "amount": row[3],
                "currency": row[4],
            }
            for row in rows
        ]

    def _list_orders_for_customer(self, user_id: str, statuses: tuple[str, ...]) -> list[dict]:
        if not user_id:
            raise OrderRepositoryUnavailable("not_logged_in")
        placeholders = ",".join("?" for _ in statuses)
        try:
            conn = self._connect()
        except (sqlite3.Error, OSError) as exc:
            raise OrderRepositoryUnavailable(str(exc)) from exc
        try:
            rows = conn.execute(
                f"""
                SELECT o.order_id, o.total_amount, o.ordered_at,
                       COALESCE(GROUP_CONCAT(p.product_name || ' x' || oi.quantity), '')
                  FROM orders o
                  LEFT JOIN order_items oi ON oi.order_id = o.order_id
                  LEFT JOIN products p ON p.product_id = oi.product_id
                 WHERE o.customer_id = ? AND o.status IN ({placeholders})
                 GROUP BY o.order_id, o.total_amount, o.ordered_at
                 ORDER BY o.ordered_at DESC
                 LIMIT 10
                """,
                (user_id, *statuses),
            ).fetchall()
        except sqlite3.Error as exc:
            raise OrderRepositoryUnavailable(str(exc)) from exc
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

    def _replay_result(
        self,
        tool_name: str,
        request_id: str,
        user_id: str,
        order_id: str,
    ) -> dict | None:
        conn = self._connect(write=True)
        try:
            row = conn.execute(
                "SELECT result_json FROM idempotency WHERE tool_name = ? AND request_id = ?",
                (tool_name, request_id),
            ).fetchone()
            if row is None:
                return None
            result = json.loads(row[0])
            owner = conn.execute(
                "SELECT customer_id FROM orders WHERE order_id = ?", (order_id,)
            ).fetchone()
            if result.get("order_id") != order_id or owner is None or owner[0] != user_id:
                return {"error": "data_boundary", "order_id": order_id}
            return {"ok": True, "replayed": True, "result": result}
        except sqlite3.Error as exc:
            raise OrderRepositoryUnavailable(str(exc)) from exc
        finally:
            conn.close()

    def _execute_command(self, tool_name: str, request_id: str, produce) -> dict:
        conn = self._connect(write=True)
        try:
            result = produce(conn)
            if "error" in result:
                conn.rollback()
                return result
            conn.execute(
                "INSERT INTO idempotency(tool_name, request_id, result_json, created_at)"
                " VALUES (?,?,?,?)",
                (tool_name, request_id, json.dumps(result, ensure_ascii=False), _now()),
            )
            conn.commit()
            return {"ok": True, "replayed": False, "result": result}
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            if tool_name == "create_invoice" and "invoices.order_id" in str(exc):
                return {"error": "invoice_already_exists"}
            return {"error": "write_conflict"}
        except sqlite3.Error as exc:
            conn.rollback()
            raise OrderRepositoryUnavailable(str(exc)) from exc
        finally:
            conn.close()

    def cancel_order(
        self,
        order_id: str,
        user_id: str,
        reason: str | None,
        request_id: str,
    ) -> dict:
        if not user_id:
            return {"error": "not_logged_in"}
        request_id = request_id or str(uuid4())
        replay = self._replay_result("cancel_order", request_id, user_id, order_id)
        if replay is not None:
            return replay

        def produce(conn: sqlite3.Connection) -> dict:
            owner = conn.execute(
                "SELECT customer_id FROM orders WHERE order_id = ?", (order_id,)
            ).fetchone()
            if owner is None:
                return {"error": "not_found", "order_id": order_id}
            if owner[0] != user_id:
                return {"error": "data_boundary", "order_id": order_id}
            cancelled_at = _now()
            cursor = conn.execute(
                "UPDATE orders SET status='cancelled', cancelled_at=?"
                " WHERE order_id=? AND status='paid'",
                (cancelled_at, order_id),
            )
            if cursor.rowcount != 1:
                status = conn.execute(
                    "SELECT status FROM orders WHERE order_id=?", (order_id,)
                ).fetchone()
                if status is None:
                    return {"error": "not_found", "order_id": order_id}
                if status[0] == "cancelled":
                    return {"error": "order_already_cancelled", "order_id": order_id}
                return {"error": "cancel_not_allowed", "order_id": order_id, "status": status[0]}
            return {
                "field": "cancel_order",
                "order_id": order_id,
                "status": "cancelled",
                "cancelled_at": cancelled_at,
                "reason": reason,
            }

        return self._execute_command("cancel_order", request_id, produce)

    def refund_apply(
        self,
        order_id: str,
        user_id: str,
        amount: float | None,
        reason: str | None,
        request_id: str,
    ) -> dict:
        if not user_id:
            return {"error": "not_logged_in"}
        request_id = request_id or str(uuid4())
        replay = self._replay_result("refund_apply", request_id, user_id, order_id)
        if replay is not None:
            return replay

        def produce(conn: sqlite3.Connection) -> dict:
            owner = conn.execute(
                "SELECT customer_id FROM orders WHERE order_id = ?", (order_id,)
            ).fetchone()
            if owner is None:
                return {"error": "not_found", "order_id": order_id}
            if owner[0] != user_id:
                return {"error": "data_boundary", "order_id": order_id}
            row = conn.execute(
                "SELECT total_amount, status, signed_at FROM orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
            if row is None:
                return {"error": "not_found", "order_id": order_id}
            total, status, signed_at = row
            if status == "refunded":
                return {"error": "order_already_refunded", "order_id": order_id}
            if status not in _CUMULATIVE_REFUND_STATUSES:
                return {"error": "refund_not_allowed", "order_id": order_id, "status": status}
            if status == "signed":
                signed_time = datetime.fromisoformat(signed_at)
                if signed_time.tzinfo is None:
                    signed_time = signed_time.replace(tzinfo=timezone.utc)
                age = (datetime.now(timezone.utc) - signed_time).total_seconds()
                if age > REFUND_WINDOW_DAYS * 86400:
                    return {"error": "refund_window_expired", "order_id": order_id}
            occupied = float(conn.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM refunds"
                " WHERE order_id=? AND status IN ('refunding','refunded')",
                (order_id,),
            ).fetchone()[0])
            resolved_amount = (
                round(float(amount), 2)
                if amount is not None
                else round(float(total) - occupied, 2)
            )
            if resolved_amount <= 0 or resolved_amount > float(total) - occupied:
                return {"error": "refund_amount_exceeds", "order_id": order_id}
            refund_id = f"RFD-{uuid4().hex[:12]}"
            conn.execute(
                "INSERT INTO refunds(refund_id, order_id, customer_id, amount, reason, status, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (refund_id, order_id, user_id, resolved_amount, reason, "refunding", _now()),
            )
            conn.execute(
                "UPDATE orders SET status='refunding'"
                " WHERE order_id=? AND status IN ('paid','signed')",
                (order_id,),
            )
            if conn.execute(
                "SELECT status FROM orders WHERE order_id=?", (order_id,)
            ).fetchone()[0] != "refunding":
                return {"error": "write_conflict", "order_id": order_id}
            return {
                "field": "refund_apply",
                "order_id": order_id,
                "refund_id": refund_id,
                "amount": resolved_amount,
                "status": "refunding",
                "reason": reason,
            }

        return self._execute_command("refund_apply", request_id, produce)
