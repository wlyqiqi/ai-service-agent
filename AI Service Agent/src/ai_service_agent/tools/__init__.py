from ai_service_agent.tools.kb import KbSearchArgs, kb_search
from ai_service_agent.tools.orders import (
    MyOrdersArgs,
    OrderLookupArgs,
    ShipmentQueryArgs,
    my_orders,
    order_lookup,
    shipment_query,
)
from ai_service_agent.tools.registry import ToolMeta, ToolRegistry
from ai_service_agent.tools.write import (
    CancelOrderArgs,
    CreateInvoiceArgs,
    CreateServiceTicketArgs,
    RefundApplyArgs,
    UpdateAddressArgs,
    UpdateOrderNoteArgs,
    UpdatePhoneArgs,
    cancel_order,
    create_invoice,
    create_service_ticket,
    make_cancel_order_tool,
    make_refund_apply_tool,
    refund_apply,
    update_address,
    update_order_note,
    update_phone,
)

# 默认注册表（tool-spec.md §4 v1 清单 + P5 工具集定案）：
# read 档：kb_search / order_lookup / my_orders / shipment_query；mutation 档：+ update_address / update_phone / update_order_note /
# create_invoice / create_service_ticket；high_risk 档：+ cancel_order（原 order_cancel）/ refund_apply。
DEFAULT_TOOL_REGISTRY = ToolRegistry()
DEFAULT_TOOL_REGISTRY.register(
    ToolMeta("kb_search", "知识库检索", "read", schema=KbSearchArgs), kb_search,
)
DEFAULT_TOOL_REGISTRY.register(
    ToolMeta("order_lookup", "订单查询", "read", schema=OrderLookupArgs), order_lookup,
)
DEFAULT_TOOL_REGISTRY.register(
    ToolMeta("my_orders", "我的订单列表", "read", schema=MyOrdersArgs), my_orders,
)
DEFAULT_TOOL_REGISTRY.register(
    ToolMeta("shipment_query", "物流查询", "read", schema=ShipmentQueryArgs), shipment_query,
)
# mutation 档：5 个写工具
DEFAULT_TOOL_REGISTRY.register(
    ToolMeta("update_address", "修改收货地址", "mutation", schema=UpdateAddressArgs, idempotent=True), update_address,
)
DEFAULT_TOOL_REGISTRY.register(
    ToolMeta("update_phone", "修改联系电话", "mutation", schema=UpdatePhoneArgs, idempotent=True), update_phone,
)
DEFAULT_TOOL_REGISTRY.register(
    ToolMeta("update_order_note", "修改订单备注", "mutation", schema=UpdateOrderNoteArgs, idempotent=True), update_order_note,
)
DEFAULT_TOOL_REGISTRY.register(
    ToolMeta("create_invoice", "开具发票", "mutation", schema=CreateInvoiceArgs, idempotent=True), create_invoice,
)
DEFAULT_TOOL_REGISTRY.register(
    ToolMeta("create_service_ticket", "创建售后工单", "mutation", schema=CreateServiceTicketArgs, idempotent=True), create_service_ticket,
)
# high_risk 档：cancel_order（原 order_cancel 重命名）+ refund_apply
DEFAULT_TOOL_REGISTRY.register(
    ToolMeta("cancel_order", "取消订单", "high_risk", schema=CancelOrderArgs, idempotent=True), cancel_order,
)
DEFAULT_TOOL_REGISTRY.register(
    ToolMeta("refund_apply", "退款申请", "high_risk", schema=RefundApplyArgs, idempotent=True), refund_apply,
)


def make_tool_registry(order_repository=None) -> ToolRegistry:
    """Create a registry; high-risk commands use the supplied repository when present."""
    registry = ToolRegistry()
    for meta, tool_fn in DEFAULT_TOOL_REGISTRY._tools.values():
        if meta.name == "cancel_order" and order_repository is not None:
            tool_fn = make_cancel_order_tool(order_repository)
        elif meta.name == "refund_apply" and order_repository is not None:
            tool_fn = make_refund_apply_tool(order_repository)
        registry.register(meta, tool_fn)
    return registry
