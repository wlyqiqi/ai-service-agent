from ai_service_agent.tools.kb import kb_search
from ai_service_agent.tools.orders import order_lookup
from ai_service_agent.tools.registry import ToolMeta, ToolRegistry
from ai_service_agent.tools.write import (
    AccountUpdateArgs,
    OrderCancelArgs,
    RefundApplyArgs,
    account_update,
    order_cancel,
    refund_apply,
)

# 默认注册表（tool-spec.md §4 v1 清单 + D5 写工具 stub）：
# read 档：kb_search / order_lookup；mutation 档：+ account_update；high_risk 档：+ order_cancel / refund_apply。
DEFAULT_TOOL_REGISTRY = ToolRegistry()
DEFAULT_TOOL_REGISTRY.register(ToolMeta("kb_search", "知识库检索", "read"), kb_search)
DEFAULT_TOOL_REGISTRY.register(ToolMeta("order_lookup", "订单查询", "read"), order_lookup)
DEFAULT_TOOL_REGISTRY.register(
    ToolMeta("account_update", "账户变更", "mutation", schema=AccountUpdateArgs, idempotent=True),
    account_update,
)
DEFAULT_TOOL_REGISTRY.register(
    ToolMeta("order_cancel", "订单取消", "high_risk", schema=OrderCancelArgs, idempotent=True),
    order_cancel,
)
DEFAULT_TOOL_REGISTRY.register(
    ToolMeta("refund_apply", "退款申请", "high_risk", schema=RefundApplyArgs, idempotent=True),
    refund_apply,
)
