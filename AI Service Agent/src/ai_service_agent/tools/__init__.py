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
    create_invoice,
    create_service_ticket,
    make_cancel_order_tool,
    make_refund_apply_tool,
    update_address,
    update_order_note,
    update_phone,
)

# 默认注册表（tool-spec.md §4 v1 清单 + P5 工具集定案）：
# read 档：kb_search / order_lookup / my_orders / shipment_query；mutation 档：+ update_address / update_phone / update_order_note /
# create_invoice / create_service_ticket。
# high_risk（cancel_order / refund_apply）不在此注册——必须经 make_tool_registry(order_repository)
# 走 OrderRepository（ADR 2026-10-10）。
DEFAULT_TOOL_REGISTRY = ToolRegistry()
DEFAULT_TOOL_REGISTRY.register(
    ToolMeta(
        "kb_search",
        "检索澄元智能家电企业知识库（产品参数、使用方法、售后与政策等）。",
        "read",
        schema=KbSearchArgs,
    ),
    kb_search,
)
DEFAULT_TOOL_REGISTRY.register(
    ToolMeta("order_lookup", "查询指定订单号的订单详情。", "read", schema=OrderLookupArgs),
    order_lookup,
)
DEFAULT_TOOL_REGISTRY.register(
    ToolMeta("my_orders", "罗列当前登录用户名下的订单。", "read", schema=MyOrdersArgs),
    my_orders,
)
DEFAULT_TOOL_REGISTRY.register(
    ToolMeta("shipment_query", "查询当前登录用户订单的物流信息。", "read", schema=ShipmentQueryArgs),
    shipment_query,
)
# mutation 档：5 个写工具
DEFAULT_TOOL_REGISTRY.register(
    ToolMeta(
        "update_address",
        "修改本人订单关联的收货地址。",
        "mutation",
        schema=UpdateAddressArgs,
        idempotent=True,
    ),
    update_address,
)
DEFAULT_TOOL_REGISTRY.register(
    ToolMeta("update_phone", "修改登录用户的联系电话。", "mutation", schema=UpdatePhoneArgs, idempotent=True),
    update_phone,
)
DEFAULT_TOOL_REGISTRY.register(
    ToolMeta(
        "update_order_note",
        "修改本人订单备注。",
        "mutation",
        schema=UpdateOrderNoteArgs,
        idempotent=True,
    ),
    update_order_note,
)
DEFAULT_TOOL_REGISTRY.register(
    ToolMeta("create_invoice", "为本人订单开具发票。", "mutation", schema=CreateInvoiceArgs, idempotent=True),
    create_invoice,
)
DEFAULT_TOOL_REGISTRY.register(
    ToolMeta(
        "create_service_ticket",
        "为本人订单创建售后工单。",
        "mutation",
        schema=CreateServiceTicketArgs,
        idempotent=True,
    ),
    create_service_ticket,
)

_CANCEL_META = ToolMeta(
    "cancel_order", "取消本人订单；需人工审批。", "high_risk", schema=CancelOrderArgs, idempotent=True,
)
_REFUND_META = ToolMeta(
    "refund_apply", "申请订单退款；需人工审批。", "high_risk", schema=RefundApplyArgs, idempotent=True,
)


def make_tool_registry(order_repository=None) -> ToolRegistry:
    """Create a registry; high-risk commands require an OrderRepository."""
    registry = ToolRegistry()
    for meta, tool_fn in DEFAULT_TOOL_REGISTRY._tools.values():
        registry.register(meta, tool_fn)
    if order_repository is not None:
        registry.register(_CANCEL_META, make_cancel_order_tool(order_repository))
        registry.register(_REFUND_META, make_refund_apply_tool(order_repository))
    return registry
