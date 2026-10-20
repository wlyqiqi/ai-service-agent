from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from ai_service_agent.tools import DEFAULT_TOOL_REGISTRY, make_tool_registry
from ai_service_agent.tools.kb import kb_search
from ai_service_agent.tools.orders import lookup_order_state, order_lookup
from ai_service_agent.tools.registry import ToolMeta, ToolRegistry
from ai_service_agent.tools.write import (
    CancelOrderArgs,
    CreateInvoiceArgs,
    CreateServiceTicketArgs,
    RefundApplyArgs,
    UpdateAddressArgs,
    UpdateOrderNoteArgs,
    UpdatePhoneArgs,
    create_invoice,
    create_service_ticket,
    make_cancel_order_tool,
    make_refund_apply_tool,
    update_address,
    update_order_note,
    update_phone,
)


def test_registry_injects_by_risk_level():
    reg = ToolRegistry()
    repo = MagicMock()
    reg.register(ToolMeta("kb_search", "检索", "read"), kb_search)
    reg.register(ToolMeta("order_lookup", "查订单", "read"), order_lookup)
    reg.register(ToolMeta("update_address", "改地址", "mutation", schema=None, idempotent=True), update_address)
    reg.register(ToolMeta("update_phone", "改电话", "mutation", schema=None, idempotent=True), update_phone)
    reg.register(ToolMeta("update_order_note", "改备注", "mutation", schema=None, idempotent=True), update_order_note)
    reg.register(ToolMeta("create_invoice", "开发票", "mutation", schema=None, idempotent=True), create_invoice)
    reg.register(ToolMeta("create_service_ticket", "售后工单", "mutation", schema=None, idempotent=True), create_service_ticket)
    reg.register(
        ToolMeta("cancel_order", "取消订单", "high_risk", schema=None, idempotent=True),
        make_cancel_order_tool(repo),
    )
    reg.register(
        ToolMeta("refund_apply", "退款", "high_risk", schema=None, idempotent=True),
        make_refund_apply_tool(repo),
    )
    assert {t.name for t in reg.for_risk_level("read")} == {"kb_search", "order_lookup"}
    assert {t.name for t in reg.for_risk_level("mutation")} == {
        "kb_search", "order_lookup", "update_address", "update_phone", "update_order_note",
        "create_invoice", "create_service_ticket",
    }
    assert {t.name for t in reg.for_risk_level("high_risk")} == {
        "kb_search", "order_lookup", "update_address", "update_phone", "update_order_note",
        "create_invoice", "create_service_ticket", "cancel_order", "refund_apply",
    }


def test_stub_tools_return_values():
    # kb_search 真实检索断言已移至 tests/test_kb_search.py（Task 2）
    assert order_lookup.invoke({"order_id": "ORD-1", "user_id": "u1"})["order_id"] == "ORD-1"


def test_default_registry_excludes_high_risk_tools():
    """DEFAULT has read+mutation only; high-risk requires make_tool_registry(repo)."""
    read_tools = {t.name for t in DEFAULT_TOOL_REGISTRY.for_risk_level("read")}
    mutation_tools = {t.name for t in DEFAULT_TOOL_REGISTRY.for_risk_level("mutation")}
    high_risk_tools = {t.name for t in DEFAULT_TOOL_REGISTRY.for_risk_level("high_risk")}
    assert read_tools == {"kb_search", "order_lookup", "my_orders", "shipment_query"}
    assert mutation_tools == read_tools | {
        "update_address", "update_phone", "update_order_note",
        "create_invoice", "create_service_ticket",
    }
    assert high_risk_tools == mutation_tools
    assert DEFAULT_TOOL_REGISTRY.meta_for("cancel_order") is None
    assert DEFAULT_TOOL_REGISTRY.meta_for("refund_apply") is None
    assert "account_update" not in mutation_tools
    assert "order_cancel" not in high_risk_tools


def test_make_tool_registry_adds_high_risk_via_repository():
    repo = MagicMock()
    reg = make_tool_registry(repo)
    assert reg.meta_for("cancel_order") is not None
    assert reg.meta_for("cancel_order").min_risk_level == "high_risk"
    assert reg.meta_for("refund_apply") is not None
    high = {t.name for t in reg.for_risk_level("high_risk")}
    assert {"cancel_order", "refund_apply"} <= high
    cancel = next(t for t in reg.for_risk_level("high_risk") if t.name == "cancel_order")
    cancel.invoke({"order_id": "ORD-1", "request_id": "r1", "user_id": "u1"})
    repo.cancel_order.assert_called_once_with("ORD-1", "u1", None, "r1")


def test_tool_meta_carries_schema_field():
    meta = ToolMeta("kb_search", "检索", "read")
    assert meta.schema is None
    meta2 = ToolMeta("cancel_order", "取消", "high_risk", schema=None, idempotent=True)
    assert meta2.idempotent is True


def _docstring_first_sentence(text: str) -> str:
    head, sep, _rest = text.strip().partition("。")
    assert sep == "。"
    return head + sep


def test_registry_descriptions_match_docstring_first_sentence():
    """11 个工具的 ToolMeta.description 等于 @tool docstring 第一句。"""
    reg = make_tool_registry(MagicMock())
    tools = reg.for_risk_level("high_risk")
    assert {t.name for t in tools} == {
        "kb_search", "order_lookup", "my_orders", "shipment_query",
        "update_address", "update_phone", "update_order_note",
        "create_invoice", "create_service_ticket",
        "cancel_order", "refund_apply",
    }
    for tool in tools:
        meta = reg.meta_for(tool.name)
        assert meta is not None
        assert meta.description == _docstring_first_sentence(tool.description)


def test_write_tools_not_logged_in():
    result = update_address.invoke({"order_id": "ORD-REFUND-1", "address": "北京市朝阳区", "request_id": "rq-1"})
    assert result == {"error": "not_logged_in"}


def test_write_tool_schemas():
    assert UpdateAddressArgs(order_id="ORD-1", address="a", request_id="rq").address == "a"
    assert UpdatePhoneArgs(phone="13800000000", request_id="rq").phone == "13800000000"
    assert UpdateOrderNoteArgs(order_id="ORD-1", note="n", request_id="rq").note == "n"
    inv = CreateInvoiceArgs(order_id="10086", amount=99.0, company="c", tax_no="t", request_id="rq")
    assert inv.amount == 99.0 and inv.company == "c" and inv.tax_no == "t"
    assert CreateServiceTicketArgs(order_id="10086", reason="r", request_id="rq").reason == "r"
    assert CancelOrderArgs(order_id="10086", request_id="rq").order_id == "10086"
    assert RefundApplyArgs(order_id="10086", amount=99.0, request_id="rq").amount == 99.0


def test_registry_meta_for_high_risk_via_factory():
    reg = make_tool_registry(MagicMock())
    meta = reg.meta_for("cancel_order")
    assert meta is not None and meta.min_risk_level == "high_risk" and meta.idempotent is True
    assert reg.meta_for("nope") is None


def test_write_schemas_forbid_unknown_fields():
    """select 契约漂移（2026-09-15 真机冒烟）：未知字段必须显式 invalid_args，绝不静默丢参。"""
    with pytest.raises(ValidationError):
        UpdateAddressArgs.model_validate({"action": "update_shipping_address", "request_id": "rq"})
    with pytest.raises(ValidationError):
        UpdateAddressArgs.model_validate({"field": "address", "request_id": "rq"})
    with pytest.raises(ValidationError):
        CancelOrderArgs.model_validate({"order_id": "10086", "action": "x", "request_id": "rq"})
    with pytest.raises(ValidationError):
        RefundApplyArgs.model_validate({"order_id": "10086", "extra": 1, "request_id": "rq"})


def test_registry_risk_sets_v2():
    """新工具集定案：mutation 5 工具 + high_risk 2 工具（经 make_tool_registry）。"""
    reg = make_tool_registry(MagicMock())
    read = {t.name for t in reg.for_risk_level("read")}
    mutation = {t.name for t in reg.for_risk_level("mutation")}
    high = {t.name for t in reg.for_risk_level("high_risk")}
    assert read == {"kb_search", "order_lookup", "my_orders", "shipment_query"}
    assert mutation == read | {
        "update_address", "update_phone", "update_order_note",
        "create_invoice", "create_service_ticket",
    }
    assert high == mutation | {"cancel_order", "refund_apply"}


def test_cancel_order_meta_is_high_risk():
    meta = make_tool_registry(MagicMock()).meta_for("cancel_order")
    assert meta is not None and meta.min_risk_level == "high_risk" and meta.idempotent


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
    assert own["order_id"] == "ORD-1" and "已送达" in str(own)
    other = order_lookup.invoke({"order_id": "ORD-1", "user_id": "other"})
    assert other.get("error") == "data_boundary"
    cross = order_lookup.invoke({"order_id": "ORD-2", "user_id": "u1"})  # ORD-2 属主 u2（同事订单）
    assert cross.get("error") == "data_boundary"
    assert lookup_order_state("ORD-1") and lookup_order_state("ORD-1")["user_id"] == "u1"


def test_read_tool_schemas_registered():
    """B1（P5 D1）：只读工具注册 select 侧 schema，注册表元数据完备（tool-spec §1 预检用）。"""
    assert DEFAULT_TOOL_REGISTRY.meta_for("order_lookup").schema is not None
    assert DEFAULT_TOOL_REGISTRY.meta_for("kb_search").schema is not None


def test_read_tool_schema_contracts():
    """B1：select 侧契约——业务字段 + request_id（select 注入）；user_id 为 InjectedState 注入、不入 schema。"""
    from ai_service_agent.tools.orders import OrderLookupArgs
    from ai_service_agent.tools.kb import KbSearchArgs

    assert "order_id" in OrderLookupArgs.model_fields
    assert "request_id" in OrderLookupArgs.model_fields
    assert "user_id" not in OrderLookupArgs.model_fields
    assert OrderLookupArgs.model_validate({"order_id": "ORD-1", "request_id": "r1"}).order_id == "ORD-1"

    assert "text" in KbSearchArgs.model_fields
    assert "request_id" in KbSearchArgs.model_fields
    assert KbSearchArgs.model_validate({"text": "退货", "request_id": "r1"}).text == "退货"
