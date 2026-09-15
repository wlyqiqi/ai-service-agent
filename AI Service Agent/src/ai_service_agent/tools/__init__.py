from ai_service_agent.tools.kb import kb_search
from ai_service_agent.tools.orders import order_lookup
from ai_service_agent.tools.registry import ToolMeta, ToolRegistry

# 默认注册表：MVP/P2 只注册 read 档只读工具（tool-spec.md §4 v1 清单）；
# mutation / high_risk 工具在 P3/P4 子图落地时按档注册。
DEFAULT_TOOL_REGISTRY = ToolRegistry()
DEFAULT_TOOL_REGISTRY.register(ToolMeta("kb_search", "知识库检索", "read"), kb_search)
DEFAULT_TOOL_REGISTRY.register(ToolMeta("order_lookup", "订单查询", "read"), order_lookup)
