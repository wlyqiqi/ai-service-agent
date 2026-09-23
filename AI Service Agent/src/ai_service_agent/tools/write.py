"""写工具：mutation 档落模拟电商库；幂等凭证与业务写入同事务。"""

import json
import sqlite3
from uuid import uuid4
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from langgraph.prebuilt import InjectedState
from langchain_core.tools import tool
from pydantic import Field
from pydantic import BaseModel, ConfigDict

from ai_service_agent.config import get_settings


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
    order_id: str
    note: str
    request_id: str


class CreateInvoiceArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: str
    amount: Annotated[float, Field(gt=0)]
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
_REFUNDABLE_STATUSES = ("paid", "signed")
_CUMULATIVE_REFUND_STATUSES = _REFUNDABLE_STATUSES + ("refunding",)
_OCCUPIED_REFUND_STATUSES = ("refunding", "refunded")
_REFUND_WINDOW_DAYS = 7


def _write_connect() -> sqlite3.Connection:
    path = get_settings().mock_ecommerce_db_path
    conn = sqlite3.connect(Path(path).resolve(), timeout=2)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _run_db_mutation(tool_name: str, request_id: str, user_id: str, produce, validate_replay=None) -> dict:
    """复合幂等键 = (tool_name, request_id)，与业务写入同连接同事务提交。"""
    if not user_id:
        return {"error": "not_logged_in"}
    try:
        conn = _write_connect()
    except (sqlite3.Error, OSError):
        return {"error": "data_source_unavailable"}
    try:
        row = conn.execute(
            "SELECT result_json FROM idempotency WHERE tool_name=? AND request_id=?",
            (tool_name, request_id),
        ).fetchone()
        if row is not None:
            result = json.loads(row[0])
            replay_error = validate_replay(conn, result) if validate_replay else None
            if replay_error is not None:
                return replay_error
            return {"ok": True, "replayed": True, "result": result}
        result = produce(conn)
        if "error" in result:
            conn.rollback()
            return result
        conn.execute(
            "INSERT INTO idempotency(tool_name, request_id, result_json, created_at) VALUES (?,?,?,?)",
            (tool_name, request_id, json.dumps(result, ensure_ascii=False), _now()),
        )
        conn.commit()
        return {"ok": True, "replayed": False, "result": result}
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        if tool_name == "create_invoice" and "invoices.order_id" in str(exc):
            return {"error": "invoice_already_exists"}
        return {"error": "write_conflict"}
    except sqlite3.Error:
        conn.rollback()
        return {"error": "data_source_unavailable"}
    finally:
        conn.close()


def _own_order(conn: sqlite3.Connection, order_id: str, user_id: str) -> dict | None:
    row = conn.execute(
        "SELECT customer_id FROM orders WHERE order_id=?", (order_id,)
    ).fetchone()
    if row is None:
        return {"error": "not_found", "order_id": order_id}
    if row[0] != user_id:
        return {"error": "data_boundary", "order_id": order_id}
    return None


def _run_idempotent(key: str, produce) -> dict:
    """幂等键（tool-spec §2）：同 request_id 返回既有结果，防重放。"""
    if key in _WRITE_STORE:
        return {"ok": True, "replayed": True, "result": _WRITE_STORE[key]}
    result = produce()
    _WRITE_STORE[key] = result
    return {"ok": True, "replayed": False, "result": result}


@tool
def update_address(
    address: str,
    request_id: str = "",
    user_id: Annotated[str, InjectedState("user_id")] = "",
) -> dict:
    """修改登录用户收货地址（mutation，幂等）。"""
    def _produce(conn: sqlite3.Connection) -> dict:
        exists = conn.execute(
            "SELECT 1 FROM customers WHERE customer_id=?", (user_id,)
        ).fetchone()
        if exists is None:
            return {"error": "not_found", "user_id": user_id}
        conn.execute("UPDATE customers SET address=? WHERE customer_id=?", (address, user_id))
        return {"field": "update_address", "address": address}
    return _run_db_mutation("update_address", request_id or str(uuid4()), user_id, _produce)


@tool
def update_phone(
    phone: str,
    request_id: str = "",
    user_id: Annotated[str, InjectedState("user_id")] = "",
) -> dict:
    """修改登录用户联系电话（mutation，幂等）。"""
    def _produce(conn: sqlite3.Connection) -> dict:
        exists = conn.execute(
            "SELECT 1 FROM customers WHERE customer_id=?", (user_id,)
        ).fetchone()
        if exists is None:
            return {"error": "not_found", "user_id": user_id}
        conn.execute("UPDATE customers SET phone=? WHERE customer_id=?", (phone, user_id))
        return {"field": "update_phone", "phone": phone}
    return _run_db_mutation("update_phone", request_id or str(uuid4()), user_id, _produce)


@tool
def update_order_note(
    order_id: str,
    note: str,
    request_id: str = "",
    user_id: Annotated[str, InjectedState("user_id")] = "",
) -> dict:
    """修改本人订单备注（mutation，幂等）。"""
    def _produce(conn: sqlite3.Connection) -> dict:
        boundary = _own_order(conn, order_id, user_id)
        if boundary is not None:
            return boundary
        conn.execute("UPDATE orders SET note=? WHERE order_id=?", (note, order_id))
        return {"field": "update_order_note", "order_id": order_id, "note": note}
    return _run_db_mutation("update_order_note", request_id or str(uuid4()), user_id, _produce)


@tool
def create_invoice(
    order_id: str,
    amount: float,
    company: str,
    tax_no: str,
    request_id: str = "",
    user_id: Annotated[str, InjectedState("user_id")] = "",
) -> dict:
    """为本人订单开具发票（mutation，幂等；金额上限由安全预检裁决）。"""
    def _produce() -> dict:
        return {"field": "create_invoice", "order_id": order_id, "amount": amount, "company": company, "tax_no": tax_no}
    def _produce_db(conn: sqlite3.Connection) -> dict:
        boundary = _own_order(conn, order_id, user_id)
        if boundary is not None:
            return boundary
        invoice_id = f"INV-{uuid4().hex[:12].upper()}"
        conn.execute(
            "INSERT INTO invoices(invoice_id, customer_id, order_id, amount, company, tax_no, status, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (invoice_id, user_id, order_id, amount, company, tax_no, "issued", _now()),
        )
        return {"field": "create_invoice", "invoice_id": invoice_id, "order_id": order_id, "amount": amount}
    return _run_db_mutation("create_invoice", request_id or str(uuid4()), user_id, _produce_db)


@tool
def create_service_ticket(
    order_id: str,
    reason: str,
    request_id: str = "",
    user_id: Annotated[str, InjectedState("user_id")] = "",
) -> dict:
    """为本人订单创建售后工单（mutation，幂等；会话关联留待 handoff 集成）。"""
    def _produce(conn: sqlite3.Connection) -> dict:
        boundary = _own_order(conn, order_id, user_id)
        if boundary is not None:
            return boundary
        ticket_id = f"TKT-{uuid4().hex[:12].upper()}"
        conn.execute(
            "INSERT INTO service_tickets(ticket_id, customer_id, order_id, issue_type, status, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (ticket_id, user_id, order_id, reason, "open", _now()),
        )
        return {"field": "create_service_ticket", "ticket_id": ticket_id, "order_id": order_id, "reason": reason}
    return _run_db_mutation("create_service_ticket", request_id or str(uuid4()), user_id, _produce)


@tool
def cancel_order(
    order_id: str,
    reason: str | None = None,
    request_id: str = "",
    user_id: Annotated[str, InjectedState("user_id")] = "",
) -> dict:
    """取消本人订单（high_risk，需人工审批；仅 paid -> cancelled）。"""
    def _produce(conn: sqlite3.Connection) -> dict:
        boundary = _own_order(conn, order_id, user_id)
        if boundary is not None:
            return boundary
        cancelled_at = _now()
        cursor = conn.execute(
            "UPDATE orders SET status='cancelled', cancelled_at=? WHERE order_id=? AND status='paid'",
            (cancelled_at, order_id),
        )
        if cursor.rowcount != 1:
            row = conn.execute(
                "SELECT status FROM orders WHERE order_id=?", (order_id,)
            ).fetchone()
            if row is None:
                return {"error": "not_found", "order_id": order_id}
            status = row[0]
            if status == "cancelled":
                return {"error": "order_already_cancelled", "order_id": order_id}
            return {"error": "cancel_not_allowed", "order_id": order_id, "status": status}
        return {
            "field": "cancel_order",
            "order_id": order_id,
            "status": "cancelled",
            "cancelled_at": cancelled_at,
            "reason": reason,
        }

    def _validate_replay(conn: sqlite3.Connection, result: dict) -> dict | None:
        if result.get("order_id") != order_id:
            return {"error": "data_boundary", "order_id": order_id}
        return _own_order(conn, order_id, user_id)

    return _run_db_mutation(
        "cancel_order", request_id or str(uuid4()), user_id, _produce, _validate_replay
    )


@tool
def refund_apply(
    order_id: str,
    amount: float | None = None,
    reason: str | None = None,
    request_id: str = "",
    user_id: Annotated[str, InjectedState("user_id")] = "",
) -> dict:
    """退款申请（high_risk；需人工审批；paid/7 日内 signed -> refunding；金额 ≤ 订单总额 - 已累计退款）。"""
    def _produce(conn: sqlite3.Connection) -> dict:
        boundary = _own_order(conn, order_id, user_id)
        if boundary is not None:
            return boundary
        row = conn.execute(
            "SELECT total_amount, status, signed_at FROM orders WHERE order_id=?", (order_id,)
        ).fetchone()
        if row is None:
            return {"error": "not_found", "order_id": order_id}
        total, status, signed_at = row
        if status == "refunded":
            return {"error": "order_already_refunded", "order_id": order_id}
        if status not in _CUMULATIVE_REFUND_STATUSES:
            return {"error": "refund_not_allowed", "order_id": order_id, "status": status}
        if status == "signed":
            try:
                signed_time = datetime.fromisoformat(signed_at)
                if signed_time.tzinfo is None:
                    signed_time = signed_time.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                return {"error": "refund_not_allowed", "order_id": order_id, "status": status}
            age_seconds = (
                datetime.now(timezone.utc) - signed_time
            ).total_seconds()
            if age_seconds > _REFUND_WINDOW_DAYS * 86400:
                return {"error": "refund_window_expired", "order_id": order_id}
        occupied = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM refunds"
            " WHERE order_id=? AND status IN ('refunding','refunded')",
            (order_id,),
        ).fetchone()[0]
        resolved_amount = (
            round(float(amount), 2)
            if amount is not None
            else round(float(total) - float(occupied), 2)
        )
        if resolved_amount <= 0 or resolved_amount > float(total) - float(occupied):
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
        if conn.execute("SELECT status FROM orders WHERE order_id=?", (order_id,)).fetchone()[0] != "refunding":
            return {"error": "write_conflict", "order_id": order_id}
        return {
            "field": "refund_apply",
            "order_id": order_id,
            "refund_id": refund_id,
            "amount": resolved_amount,
            "status": "refunding",
            "reason": reason,
        }

    def _validate_replay(conn: sqlite3.Connection, result: dict) -> dict | None:
        if result.get("order_id") != order_id:
            return {"error": "data_boundary", "order_id": order_id}
        return _own_order(conn, order_id, user_id)

    return _run_db_mutation(
        "refund_apply", request_id or str(uuid4()), user_id, _produce, _validate_replay
    )
