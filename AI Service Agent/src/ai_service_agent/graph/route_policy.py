from dataclasses import dataclass
from typing import Literal

from langchain_core.tools import BaseTool

from ai_service_agent.tools.registry import ToolRegistry


RouteIntent = Literal[
    "faq",
    "smalltalk",
    "order_query",
    "my_orders",
    "shipment_query",
    "account_profile_update",
    "order_mutation",
    "cancel_order_execute",
    "refund_apply_execute",
    "unsupported_action",
    "unknown",
]

TargetGraph = Literal[
    "read_graph",
    "mutation_graph",
    "high_risk_graph",
    "fallback",
]

ExecutionClass = Literal[
    "read_only",
    "standard_write",
    "high_risk",
]


@dataclass(frozen=True)
class RoutePolicy:
    target_graph: TargetGraph
    execution_class: ExecutionClass
    allowed_tools: tuple[str, ...] = ()
    requires_approval: bool = False


ROUTE_POLICY: dict[str, RoutePolicy] = {
    "faq": RoutePolicy(
        target_graph="read_graph",
        execution_class="read_only",
        allowed_tools=("kb_search",),
    ),
    "smalltalk": RoutePolicy(
        target_graph="read_graph",
        execution_class="read_only",
        allowed_tools=(),
    ),
    "order_query": RoutePolicy(
        target_graph="read_graph",
        execution_class="read_only",
        allowed_tools=("order_lookup",),
    ),
    "my_orders": RoutePolicy(
        target_graph="read_graph",
        execution_class="read_only",
        allowed_tools=("my_orders",),
    ),
    "shipment_query": RoutePolicy(
        target_graph="read_graph",
        execution_class="read_only",
        allowed_tools=("shipment_query",),
    ),
    "account_profile_update": RoutePolicy(
        target_graph="mutation_graph",
        execution_class="standard_write",
        allowed_tools=("update_address", "update_phone"),
    ),
    "order_mutation": RoutePolicy(
        target_graph="mutation_graph",
        execution_class="standard_write",
        allowed_tools=("update_order_note", "create_invoice", "create_service_ticket"),
    ),
    "cancel_order_execute": RoutePolicy(
        target_graph="high_risk_graph",
        execution_class="high_risk",
        allowed_tools=("cancel_order",),
        requires_approval=True,
    ),
    "refund_apply_execute": RoutePolicy(
        target_graph="high_risk_graph",
        execution_class="high_risk",
        allowed_tools=("refund_apply",),
        requires_approval=True,
    ),
    "unsupported_action": RoutePolicy(
        target_graph="fallback",
        execution_class="read_only",
        allowed_tools=(),
    ),
    "unknown": RoutePolicy(
        target_graph="fallback",
        execution_class="read_only",
        allowed_tools=(),
    ),
}


EXECUTION_CLASS_THRESHOLDS: dict[str, float] = {
    "read_only": 0.60,
    "standard_write": 0.70,
    "high_risk": 0.80,
}


def resolve_route_policy(route_intent: str) -> RoutePolicy:
    """Resolve unknown or invalid intents to the safe fallback policy."""
    return ROUTE_POLICY.get(route_intent, ROUTE_POLICY["unknown"])


def route_intent_is_confident(route_intent: str, confidence: float) -> bool:
    """Apply the deterministic execution-class threshold to a route candidate."""
    policy = resolve_route_policy(route_intent)
    return float(confidence) >= EXECUTION_CLASS_THRESHOLDS[policy.execution_class]


def tools_for_policy(registry: ToolRegistry, policy: RoutePolicy) -> list[BaseTool]:
    """Resolve the policy allowlist into concrete tools using registry order."""
    return registry.tools_for_names(policy.allowed_tools)
