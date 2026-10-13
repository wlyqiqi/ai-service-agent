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
    order_id: str
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
    order_id: str,
    address: str,
    request_id: str = "",
    user_id: Annotated[str, InjectedState("user_id")] = "",
) -> dict:
    """修改本人订单关联收货地址（mutation，幂等；订单状态由安全预检裁决）。"""
    def _produce(conn: sqlite3.Connection) -> dict:
        boundary = _own_order(conn, order_id, user_id)
        if boundary is not None:
            return boundary
        exists = conn.execute(
            "SELECT 1 FROM customers WHERE customer_id=?", (user_id,)
        ).fetchone()
        if exists is None:
            return {"error": "not_found", "user_id": user_id}
        conn.execute("UPDATE customers SET address=? WHERE customer_id=?", (address, user_id))
        return {"field": "update_address", "order_id": order_id, "address": address}
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


def make_cancel_order_tool(order_repository):
    """Build the high-risk tool against an injected order command gateway."""
    @tool
    def cancel_order(
        order_id: str,
        reason: str | None = None,
        request_id: str = "",
        user_id: Annotated[str, InjectedState("user_id")] = "",
    ) -> dict:
        """取消本人订单（high_risk，需人工审批；仅 paid -> cancelled）。"""
        return order_repository.cancel_order(order_id, user_id, reason, request_id)

    return cancel_order


def make_refund_apply_tool(order_repository):
    """Build the high-risk tool against an injected order command gateway."""
    @tool
    def refund_apply(
        order_id: str,
        amount: float | None = None,
        reason: str | None = None,
        request_id: str = "",
        user_id: Annotated[str, InjectedState("user_id")] = "",
    ) -> dict:
        """退款申请（high_risk；需人工审批；paid/7 日内 signed -> refunding）。"""
        return order_repository.refund_apply(order_id, user_id, amount, reason, request_id)

    return refund_apply
