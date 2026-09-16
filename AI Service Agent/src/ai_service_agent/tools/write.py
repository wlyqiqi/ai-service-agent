"""写工具 stub：mutation 档（update_address/update_phone/update_order_note/create_invoice/create_service_ticket）
+ high_risk 档（cancel_order/refund_apply）。内存 fake store，幂等（request_id）。"""

from uuid import uuid4

from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict


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
    note: str
    request_id: str


class CreateInvoiceArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: str
    amount: float
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


def _run_idempotent(key: str, produce) -> dict:
    """幂等键（tool-spec §2）：同 request_id 返回既有结果，防重放。"""
    if key in _WRITE_STORE:
        return {"ok": True, "replayed": True, "result": _WRITE_STORE[key]}
    result = produce()
    _WRITE_STORE[key] = result
    return {"ok": True, "replayed": False, "result": result}


@tool
def update_address(address: str, request_id: str = "") -> dict:
    """修改收货地址（mutation stub，幂等）。"""
    return _run_idempotent(request_id or str(uuid4()), lambda: {"field": "update_address", "address": address})


@tool
def update_phone(phone: str, request_id: str = "") -> dict:
    """修改联系电话（mutation stub，幂等）。"""
    return _run_idempotent(request_id or str(uuid4()), lambda: {"field": "update_phone", "phone": phone})


@tool
def update_order_note(note: str, request_id: str = "") -> dict:
    """修改订单备注（mutation stub，幂等）。"""
    return _run_idempotent(request_id or str(uuid4()), lambda: {"field": "update_order_note", "note": note})


@tool
def create_invoice(order_id: str, amount: float, company: str, tax_no: str, request_id: str = "") -> dict:
    """开具发票（mutation stub，幂等）。"""
    def _produce() -> dict:
        return {"field": "create_invoice", "order_id": order_id, "amount": amount, "company": company, "tax_no": tax_no}
    return _run_idempotent(request_id or str(uuid4()), _produce)


@tool
def create_service_ticket(order_id: str, reason: str, request_id: str = "") -> dict:
    """创建售后工单（mutation stub，幂等）。"""
    def _produce() -> dict:
        return {"field": "create_service_ticket", "order_id": order_id, "reason": reason}
    return _run_idempotent(request_id or str(uuid4()), _produce)


@tool
def cancel_order(order_id: str, reason: str | None = None, request_id: str = "") -> dict:
    """取消订单（high_risk stub，需人工审批，幂等）。"""
    def _produce() -> dict:
        return {"field": "cancel_order", "order_id": order_id, "reason": reason}
    return _run_idempotent(request_id or str(uuid4()), _produce)


@tool
def refund_apply(order_id: str, amount: float | None = None, reason: str | None = None, request_id: str = "") -> dict:
    """退款申请（high_risk stub，需人工审批，幂等）。"""
    def _produce() -> dict:
        return {"field": "refund_apply", "order_id": order_id, "amount": amount, "reason": reason}
    return _run_idempotent(request_id or str(uuid4()), _produce)
