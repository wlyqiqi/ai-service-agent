import re

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langgraph.config import get_stream_writer
from langgraph.errors import NodeError, NodeTimeoutError
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from ai_service_agent.config import get_settings, make_router_fallback_model, make_router_model
from ai_service_agent.domain.orders import OrderRepository, SQLiteOrderRepository
from ai_service_agent.graph.agents.read_agent import make_read_graph
from ai_service_agent.graph.messages import message_text
from ai_service_agent.graph.route_policy import resolve_route_policy
from ai_service_agent.graph.fault_tolerance import (
    RETRY_DEEPSEEK_ROUTER,
    make_timeout_policy,
    root_error_handler,
)
from ai_service_agent.graph.router import (
    jev_fallback_result,
    make_deepseek_router,
    make_jev_router,
)
from ai_service_agent.graph.runtime_context import GraphContext
from ai_service_agent.graph.state import CustomerServiceState
from ai_service_agent.graph.workflows.fallback import make_fallback_graph
from ai_service_agent.graph.workflows.guard import GUARD_REJECT_TEXT, make_guard_node
from ai_service_agent.graph.workflows.high_risk_agent import make_high_risk_graph
from ai_service_agent.graph.workflows.mutation_agent import make_mutation_graph
from ai_service_agent.graph.workflows.operation_clarify import (
    make_hybrid_tool_resolve_node,
    make_operation_clarify_node,
)
from ai_service_agent.tools import make_tool_registry


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
            return message_text(message)
    return ""


def _route_target(state: dict) -> str:
    return state.get("target_graph", "fallback")


def _guard_aware_target(state: dict) -> str:
    """Guard fail -> 安全收尾；mutation 且 tool 未定 -> Operation Clarify（进子图前）。"""
    if state.get("guard_status") == "fail":
        return "safety_final"
    if (
        state.get("target_graph") == "mutation_graph"
        and state.get("context", {}).get("operation_clarify")
    ):
        return "operation_clarify"
    return _route_target(state)


def _policy_gate(state: dict) -> dict:
    """Router/Guard join. Deterministically derives route policy from route_intent."""
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
    reason = state.get("guard_reason") or "guard_rule_hit"
    get_stream_writer()({"type": "safety_rejected", "data": {"rule": "guard", "reason": reason}})
    return {"messages": [AIMessage(content=GUARD_REJECT_TEXT)], "execution_status": "completed"}


def _router_join(state: dict) -> dict:
    return {}


def _jev_timeout_error_handler(state: dict, error: NodeError) -> Command:
    if isinstance(error.error, NodeTimeoutError):
        return Command(update=jev_fallback_result(), goto="router_deepseek")
    return root_error_handler(state, error)


def build_graph(
    model,
    tools: list,
    checkpointer=None,
    registry=None,
    tool_hooks=None,
    order_repository: OrderRepository | None = None,
    router_model=None,
    router_fallback_model=None,
    select_model=None,
    fallback_model=None,
):
    """总图：Guard ∥ Router -> policy_gate -> ReadGraph / Fallback / safety_final（统一出口）。

    registry 参数：按档注入 mutation / high_risk 子图工具（tool-spec §1 分层注入，Task 4/5 消费）。
    model：ReadGraph 模型（向后兼容测试单一 Fake）。
    router_model / router_fallback_model / select_model / fallback_model：缺省回落为 model
    （Fake 单测零改动；router_fallback_model 缺省为 None，不启用二段 Router 调用）。
    """
    repository = order_repository or SQLiteOrderRepository(get_settings().mock_ecommerce_db_path)
    reg = registry if registry is not None else make_tool_registry(repository)
    select = select_model if select_model is not None else model
    fallback = fallback_model if fallback_model is not None else model
    guard = make_guard_node()
    read_graph = make_read_graph(model, tools, tool_hooks=tool_hooks)
    fallback_graph = make_fallback_graph(fallback)
    mutation_graph = make_mutation_graph(
        select,
        reg.for_risk_level("mutation"),
        reg,
        tool_hooks=tool_hooks,
        order_repository=repository,
    )
    high_risk_graph = make_high_risk_graph(
        select,
        reg.for_risk_level("high_risk"),
        reg,
        tool_hooks=tool_hooks,
        order_repository=repository,
    )

    def finalize(state: dict) -> dict:
        # 保留子图产出的终态 clarifying（P4 D1 普通轮澄清）；failed 防覆盖。
        # 其余（含 waiting_handoff resume 后）由 final 置 completed（graph-spec §3 / state-spec §1：
        # 人工 Resolve 主动 resume 完成后最终置 completed）。
        if state.get("execution_status") in ("clarifying", "failed"):
            return {}
        return {"execution_status": "completed"}

    builder = StateGraph(CustomerServiceState, context_schema=GraphContext).set_node_defaults(error_handler=root_error_handler)
    settings = get_settings()
    use_jev = (
        settings.jev_enabled
        and bool(settings.jev_api_key)
        and not settings.fake_model
    )
    if router_model is None:
        if (
            settings.router_model_name
            and not settings.fake_model
            and not isinstance(model, FakeMessagesListChatModel)
        ):
            router_model = make_router_model()
        else:
            router_model = model
    if (
        router_fallback_model is None
        and not settings.fake_model
        and not isinstance(model, FakeMessagesListChatModel)
        and (settings.router_fallback_model_name or "").strip()
    ):
        router_fallback_model = make_router_fallback_model()
    if use_jev:
        builder.add_node(
            "router_jev",
            make_jev_router(),
            error_handler=_jev_timeout_error_handler,
            timeout=make_timeout_policy(settings.jev_node_timeout_seconds),
        )
    builder.add_node(
        "router_deepseek",
        make_deepseek_router(router_model, fallback_model=router_fallback_model),
        error_handler=root_error_handler,
        retry_policy=RETRY_DEEPSEEK_ROUTER,
        timeout=make_timeout_policy(settings.router_node_timeout_seconds),
    )
    builder.add_node("guard", guard)
    builder.add_node("policy_gate", _policy_gate)
    builder.add_node("hybrid_tool_resolve", make_hybrid_tool_resolve_node())
    builder.add_node("operation_clarify", make_operation_clarify_node())
    builder.add_node("safety_final", _safety_final)
    builder.add_node("router_join", _router_join)
    builder.add_node("read_graph", read_graph)
    builder.add_node("mutation_graph", mutation_graph)
    builder.add_node("high_risk_graph", high_risk_graph)
    builder.add_node("fallback", fallback_graph)
    builder.add_node("final", finalize)
    builder.add_edge(START, "guard")
    if use_jev:
        builder.add_edge(START, "router_jev")
        builder.add_edge(["guard", "router_jev"], "router_join")
        builder.add_edge("router_deepseek", "policy_gate")
    else:
        builder.add_edge(START, "router_deepseek")
        builder.add_edge(["guard", "router_deepseek"], "router_join")
    builder.add_conditional_edges(
        "router_join",
        lambda state: "router_deepseek" if state.get("route_control") == "deepseek_fallback" else "policy_gate",
        {
            "router_deepseek": "router_deepseek",
            "policy_gate": "policy_gate",
        },
    )
    builder.add_edge("policy_gate", "hybrid_tool_resolve")
    builder.add_conditional_edges(
        "hybrid_tool_resolve",
        _guard_aware_target,
        {
            "safety_final": "safety_final",
            "operation_clarify": "operation_clarify",
            "read_graph": "read_graph",
            "fallback": "fallback",
            "mutation_graph": "mutation_graph",
            "high_risk_graph": "high_risk_graph",
        },
    )
    builder.add_edge("read_graph", "final")
    builder.add_edge("mutation_graph", "final")
    builder.add_edge("high_risk_graph", "final")
    builder.add_edge("fallback", "final")
    builder.add_edge("operation_clarify", "final")
    builder.add_edge("safety_final", "final")
    builder.add_edge("final", END)
    return builder.compile(checkpointer=checkpointer)
