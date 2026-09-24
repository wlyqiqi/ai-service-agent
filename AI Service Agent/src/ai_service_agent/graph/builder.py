import re

from langchain_core.messages import AIMessage
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from ai_service_agent.config import get_settings, make_model
from ai_service_agent.graph.agents.read_agent import make_read_graph
from ai_service_agent.graph.route_policy import resolve_route_policy
from ai_service_agent.graph.fault_tolerance import RETRY_DEEPSEEK_ROUTER
from ai_service_agent.graph.router import make_deepseek_router, make_jev_router
from ai_service_agent.graph.state import CustomerServiceState
from ai_service_agent.graph.workflows.fallback import make_fallback_graph
from ai_service_agent.graph.workflows.guard import GUARD_REJECT_TEXT, make_guard_node
from ai_service_agent.graph.workflows.high_risk_agent import make_high_risk_graph
from ai_service_agent.graph.workflows.mutation_agent import make_mutation_graph
from ai_service_agent.tools import DEFAULT_TOOL_REGISTRY


# Policy 纯确定性映射（graph-spec.md §5）：read 组合走 ReadGraph；mutation / high_risk 在
# P3 Task 4/5 陆续纳入；未映射组合由默认值兜底到 Fallback。
POLICY = {
    ("faq", "read"): "read_graph",
    ("order", "read"): "read_graph",
    ("account", "read"): "read_graph",
    ("smalltalk", "read"): "read_graph",
    ("unknown", "read"): "fallback",
    ("order", "mutation"): "mutation_graph",
    ("account", "mutation"): "mutation_graph",
    ("faq", "mutation"): "mutation_graph",     # CTRL-0094（deny，需进 MutationGraph 由预检拦截）
    ("order", "high_risk"): "high_risk_graph",
    ("account", "high_risk"): "high_risk_graph",
}


_PROFILE_UPDATE_PATTERN = re.compile(
    r"(收货地址|联系电话|手机号)[^。\n]{0,24}(修改|改成|改为|换成|换绑|更新)"
    r"|(修改|改成|改为|换成|换绑|更新)[^。\n]{0,24}(收货地址|联系电话|手机号)"
)


def _latest_user_text(state: dict) -> str:
    messages = state.get("messages") or []
    for message in reversed(messages):
        if isinstance(message, dict):
            role = message.get("type", message.get("role", ""))
            content = message.get("content", "")
        else:
            role = getattr(message, "type", "")
            content = getattr(message, "content", "")
        if role == "human":
            return content if isinstance(content, str) else str(content)
    return ""


def _route_target(state: dict) -> str:
    if get_settings().route_intent_enabled:
        return state.get("target_graph", "fallback")
    return POLICY.get((state.get("capability", "unknown"), state.get("risk_level", "read")), "fallback")


def _guard_aware_target(state: dict) -> str:
    """Guard fail -> 安全收尾（Router 并行结果不消费）；否则 Policy 映射（graph-spec §2/§5）。"""
    if state.get("guard_status") == "fail":
        return "safety_final"
    return _route_target(state)


def _policy_gate(state: dict) -> dict:
    """Router/Guard join. v2 deterministically derives the route policy; v1 is a no-op join."""
    if not get_settings().route_intent_enabled:
        return {}

    route_intent = state.get("route_intent", "unknown")
    if (
        route_intent == "order_mutation"
        and _PROFILE_UPDATE_PATTERN.search(_latest_user_text(state))
    ):
        route_intent = "account_profile_update"

    policy = resolve_route_policy(route_intent)
    return {
        "route_intent": route_intent,
        "target_graph": policy.target_graph,
        "execution_class": policy.execution_class,
        "allowed_tools": list(policy.allowed_tools),
        "requires_approval": policy.requires_approval,
    }


def _safety_final(state: dict) -> dict:
    get_stream_writer()({"type": "safety_rejected", "data": {"rule": "guard", "reason": "guard_keyword_hit"}})
    return {"messages": [AIMessage(content=GUARD_REJECT_TEXT)], "execution_status": "completed"}


def _router_join(state: dict) -> dict:
    return {}


def build_graph(model, tools: list, checkpointer=None, registry=None, tool_hooks=None):
    """总图：Guard ∥ Router -> policy_gate -> ReadGraph / Fallback / safety_final（统一出口）。

    registry 参数：按档注入 mutation / high_risk 子图工具（tool-spec §1 分层注入，Task 4/5 消费）。
    """
    reg = registry if registry is not None else DEFAULT_TOOL_REGISTRY
    guard = make_guard_node()
    read_graph = make_read_graph(model, tools, tool_hooks=tool_hooks)
    fallback = make_fallback_graph(model)
    mutation_graph = make_mutation_graph(model, reg.for_risk_level("mutation"), reg, tool_hooks=tool_hooks)
    high_risk_graph = make_high_risk_graph(model, reg.for_risk_level("high_risk"), reg, tool_hooks=tool_hooks)

    def finalize(state: dict) -> dict:
        # 保留子图产出的终态 clarifying（P4 D1 普通轮澄清）；failed 防覆盖。
        # 其余（含 waiting_handoff resume 后）由 final 置 completed（graph-spec §3 / state-spec §1：
        # 人工 Resolve 主动 resume 完成后最终置 completed）。
        if state.get("execution_status") in ("clarifying", "failed"):
            return {}
        return {"execution_status": "completed"}

    builder = StateGraph(CustomerServiceState)
    settings = get_settings()
    use_jev = (
        settings.route_intent_enabled
        and settings.jev_enabled
        and bool(settings.jev_api_key)
        and not settings.fake_model
    )
    if use_jev:
        builder.add_node("router_jev", make_jev_router())
        builder.add_node(
            "router_deepseek",
            make_deepseek_router(model, route_intent_enabled=True),
            retry_policy=RETRY_DEEPSEEK_ROUTER,
        )
    else:
        builder.add_node(
            "router_deepseek",
            make_deepseek_router(model, route_intent_enabled=settings.route_intent_enabled),
            retry_policy=RETRY_DEEPSEEK_ROUTER,
        )
    builder.add_node("guard", guard)
    builder.add_node("policy_gate", _policy_gate)
    builder.add_node("safety_final", _safety_final)
    builder.add_node("router_join", _router_join)
    builder.add_node("read_graph", read_graph)
    builder.add_node("mutation_graph", mutation_graph)
    builder.add_node("high_risk_graph", high_risk_graph)
    builder.add_node("fallback", fallback)
    builder.add_node("final", finalize)
    builder.add_edge(START, "guard")
    if use_jev:
        builder.add_edge(START, "router_jev")
        builder.add_edge(["guard", "router_jev"], "router_join")
    else:
        builder.add_edge(START, "router_deepseek")
        builder.add_edge(["guard", "router_deepseek"], "router_join")
    builder.add_conditional_edges(
        "router_join",
        lambda state: "router_deepseek" if state.get("routing_fallback") else "policy_gate",
        {"router_deepseek": "router_deepseek", "policy_gate": "policy_gate"},
    )
    builder.add_edge("router_deepseek", "policy_gate")
    builder.add_conditional_edges(
        "policy_gate", _guard_aware_target,
        {"safety_final": "safety_final", "read_graph": "read_graph", "fallback": "fallback", "mutation_graph": "mutation_graph", "high_risk_graph": "high_risk_graph"},
    )
    builder.add_edge("read_graph", "final")
    builder.add_edge("mutation_graph", "final")
    builder.add_edge("high_risk_graph", "final")
    builder.add_edge("fallback", "final")
    builder.add_edge("safety_final", "final")
    builder.add_edge("final", END)
    return builder.compile(checkpointer=checkpointer)


# langgraph platform / CLI 入口（langgraph.json -> builder.py:app）
app = build_graph(make_model(), tools=[])
