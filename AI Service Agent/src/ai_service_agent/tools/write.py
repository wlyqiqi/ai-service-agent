"""写工具 stub（D5）：account_update（mutation）/ order_cancel（high_risk）/ refund_apply（high_risk）。

内存 fake store，深度同 Read 工具 stub，不引入真实外部系统依赖；`idempotent=True` + 参数带
`request_id`（tool-spec §2，防重放）。schema 供预检白名单校验（safety-spec §1.2）。
"""

from uuid import uuid4

from langchain_core.tools import tool
from pydantic import BaseModel


class AccountUpdateArgs(BaseModel):
    phone: str | None = None
    email: str | None = None
    address: str | None = None
    request_id: str


class OrderCancelArgs(BaseModel):
    order_id: str
    reason: str | None = None
    request_id: str


class RefundApplyArgs(BaseModel):
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
def account_update(phone: str | None = None, email: str | None = None, address: str | None = None, request_id: str = "") -> dict:
    """账户变更（mutation stub，幂等）。"""
    def _produce() -> dict:
        return {"field": "account", "phone": phone, "email": email, "address": address}
    return _run_idempotent(request_id or str(uuid4()), _produce)


@tool
def order_cancel(order_id: str, reason: str | None = None, request_id: str = "") -> dict:
    """订单取消（high_risk stub，需人工审批）。"""
    def _produce() -> dict:
        return {"field": "order_cancel", "order_id": order_id, "reason": reason}
    return _run_idempotent(request_id or str(uuid4()), _produce)


@tool
def refund_apply(order_id: str, amount: float | None = None, reason: str | None = None, request_id: str = "") -> dict:
    """退款申请（high_risk stub，需人工审批）。"""
    def _produce() -> dict:
        return {"field": "refund_apply", "order_id": order_id, "amount": amount, "reason": reason}
    return _run_idempotent(request_id or str(uuid4()), _produce)
