from ai_service_agent.tools.kb import kb_search
from ai_service_agent.tools.orders import order_lookup
from ai_service_agent.tools.registry import ToolMeta, ToolRegistry


def test_registry_injects_by_risk_level():
    reg = ToolRegistry()
    reg.register(ToolMeta("kb_search", "检索", "read"), kb_search)
    reg.register(ToolMeta("order_lookup", "查订单", "read"), order_lookup)
    assert {t.name for t in reg.for_risk_level("read")} == {"kb_search", "order_lookup"}
    # MVP 只有 read 工具；后续写工具在 higher risk 才注入
    assert {t.name for t in reg.for_risk_level("high_risk")} == {"kb_search", "order_lookup"}


def test_stub_tools_return_values():
    assert "结果" in kb_search.invoke("退款")
    assert order_lookup.invoke("ORD-1")["order_id"] == "ORD-1"

from ai_service_agent.tools import DEFAULT_TOOL_REGISTRY


def test_default_registry_read_only():
    read_tools = {t.name for t in DEFAULT_TOOL_REGISTRY.for_risk_level("read")}
    assert read_tools == {"kb_search", "order_lookup"}
    # P2 不注册 mutation / high_risk 工具：仅 read 档（tool-spec.md §4 v1 清单）
    assert {t.name for t in DEFAULT_TOOL_REGISTRY.for_risk_level("mutation")} == read_tools
    assert {t.name for t in DEFAULT_TOOL_REGISTRY.for_risk_level("high_risk")} == read_tools


def test_tool_meta_carries_schema_field():  # OQ-1
    meta = ToolMeta("kb_search", "检索", "read")
    assert meta.schema is None
    meta2 = ToolMeta("order_cancel", "取消", "high_risk", schema=None, idempotent=True)
    assert meta2.idempotent is True
