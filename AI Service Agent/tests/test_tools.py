from datetime import datetime, timezone

from ai_service_agent.tools import DEFAULT_TOOL_REGISTRY
from ai_service_agent.tools.kb import kb_search
from ai_service_agent.tools.orders import lookup_order_state, order_lookup
from ai_service_agent.tools.registry import ToolMeta, ToolRegistry
from ai_service_agent.tools.write import (
    AccountUpdateArgs,
    OrderCancelArgs,
    RefundApplyArgs,
    account_update,
    order_cancel,
    refund_apply,
)


def test_registry_injects_by_risk_level():
    reg = ToolRegistry()
    reg.register(ToolMeta("kb_search", "检索", "read"), kb_search)
    reg.register(ToolMeta("order_lookup", "查订单", "read"), order_lookup)
    reg.register(ToolMeta("account_update", "改账户", "mutation", schema=None, idempotent=True), account_update)
    reg.register(ToolMeta("order_cancel", "取消", "high_risk", schema=None, idempotent=True), order_cancel)
    reg.register(ToolMeta("refund_apply", "退款", "high_risk", schema=None, idempotent=True), refund_apply)
    assert {t.name for t in reg.for_risk_level("read")} == {"kb_search", "order_lookup"}
    assert {t.name for t in reg.for_risk_level("mutation")} == {"kb_search", "order_lookup", "account_update"}
    assert {t.name for t in reg.for_risk_level("high_risk")} == {
        "kb_search", "order_lookup", "account_update", "order_cancel", "refund_apply",
    }


def test_stub_tools_return_values():
    assert "结果" in kb_search.invoke("退款")
    assert order_lookup.invoke({"order_id": "ORD-1", "user_id": "u1"})["order_id"] == "ORD-1"


def test_default_registry_tiered():
    read_tools = {t.name for t in DEFAULT_TOOL_REGISTRY.for_risk_level("read")}
    mutation_tools = {t.name for t in DEFAULT_TOOL_REGISTRY.for_risk_level("mutation")}
    high_risk_tools = {t.name for t in DEFAULT_TOOL_REGISTRY.for_risk_level("high_risk")}
    assert read_tools == {"kb_search", "order_lookup"}
    assert mutation_tools == read_tools | {"account_update"}
    assert high_risk_tools == read_tools | {"account_update", "order_cancel", "refund_apply"}


def test_tool_meta_carries_schema_field():  # OQ-1
    meta = ToolMeta("kb_search", "检索", "read")
    assert meta.schema is None
    meta2 = ToolMeta("order_cancel", "取消", "high_risk", schema=None, idempotent=True)
    assert meta2.idempotent is True


def test_write_tools_stub_idempotent_by_request_id():
    r1 = account_update.invoke({"phone": "13800000000", "request_id": "rq-1"})
    r2 = account_update.invoke({"phone": "13800000000", "request_id": "rq-1"})
    assert r1["replayed"] is False and r2["replayed"] is True
    assert r1["result"] == r2["result"]
    r3 = account_update.invoke({"phone": "13900000000", "request_id": "rq-2"})
    assert r3["replayed"] is False


def test_write_tool_schemas():
    assert AccountUpdateArgs(request_id="rq").request_id == "rq"
    assert OrderCancelArgs(order_id="10086", request_id="rq").order_id == "10086"
    assert RefundApplyArgs(order_id="10086", amount=99.0, request_id="rq").amount == 99.0


def test_registry_meta_for():
    meta = DEFAULT_TOOL_REGISTRY.meta_for("order_cancel")
    assert meta is not None and meta.min_risk_level == "high_risk" and meta.idempotent is True
    assert DEFAULT_TOOL_REGISTRY.meta_for("nope") is None


import pytest
from pydantic import ValidationError


def test_write_schemas_forbid_unknown_fields():
    """select 契约漂移（2026-09-15 真机冒烟）：未知字段必须显式 invalid_args，绝不静默丢参。"""
    with pytest.raises(ValidationError):
        AccountUpdateArgs.model_validate({"action": "update_shipping_address", "request_id": "rq"})
    with pytest.raises(ValidationError):
        AccountUpdateArgs.model_validate({"field": "phone", "request_id": "rq"})
    with pytest.raises(ValidationError):
        OrderCancelArgs.model_validate({"order_id": "10086", "action": "x", "request_id": "rq"})
    with pytest.raises(ValidationError):
        RefundApplyArgs.model_validate({"order_id": "10086", "extra": 1, "request_id": "rq"})


def test_order_seed_lookup_order_state():
    """D3（P4）：确定性种子订单状态事实；signed_at 相对 now 生成恒超 7 天窗口。"""
    signed = lookup_order_state("ORD-10086")
    assert signed and signed["user_id"] == "u1" and signed["status"] == "signed"
    assert (datetime.now(timezone.utc) - signed["signed_at"]).days >= 7
    cancelled = lookup_order_state("ORD-CANCEL-1")
    assert cancelled and cancelled["status"] == "cancelled" and cancelled["cancelled_at"]
    assert lookup_order_state("NOPE-1") is None


def test_order_lookup_owner_boundary():
    """D4（P4）：属主校验在工具层（ReadGraph 无 precheck，safety-spec §1.4）——
    本人可查；他人 / 跨用户订单返回 data_boundary。"""
    own = order_lookup.invoke({"order_id": "ORD-1", "user_id": "u1"})
    assert own["order_id"] == "ORD-1" and "已发货" in str(own)
    other = order_lookup.invoke({"order_id": "ORD-1", "user_id": "other"})
    assert other.get("error") == "data_boundary"
    cross = order_lookup.invoke({"order_id": "ORD-2", "user_id": "u1"})  # ORD-2 属主 u2（同事订单）
    assert cross.get("error") == "data_boundary"
    assert lookup_order_state("ORD-1") and lookup_order_state("ORD-1")["user_id"] == "u1"
