import langgraph.types as _lg_types
from langchain_core.messages import AIMessage
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from ai_service_agent.config import get_settings
from ai_service_agent.graph.state import CustomerServiceState


def check_handoff(state: dict) -> dict | Command:
    """确定性规则：显式转人工关键词命中才置位（safety-spec.md §2）；LLM 无权临时决定。

    命中时先置位再中断：Command(update) 保证 execution_status=waiting_handoff
    在中断时刻生效（state-spec.md §1），随后路由到 pause 节点抛 interrupt。
    """
    last_content = state["messages"][-1].content if state.get("messages") else ""
    if any(kw in last_content for kw in get_settings().handoff_keywords):
        return Command(
            update={"handoff": True, "execution_status": "waiting_handoff"},
            goto="pause",
        )
    return {"handoff": False, "execution_status": "completed"}


def pause(state: dict) -> dict:
    """统一抛 interrupt(handoff_request)，由外层接管（graph-spec.md §6.5）。"""
    last_content = state["messages"][-1].content if state.get("messages") else ""
    _lg_types.interrupt(
        {
            "type": "handoff_request",
            "reason": "用户显式请求转人工",
            "summary": str(last_content)[:80],
        }
    )
    return {}


def make_fallback_graph(model):
    """Fallback / Concierge：兜底解释 + check_handoff（graph-spec.md §6.4）。"""
    def explain(state: dict) -> dict:
        return {"messages": [AIMessage(content="我暂时无法处理，已为你转接人工客服。")]}

    builder = StateGraph(CustomerServiceState)
    builder.add_node("fallback_explain", explain)
    builder.add_node("check_handoff", check_handoff)
    builder.add_node("pause", pause)
    builder.add_edge(START, "fallback_explain")
    builder.add_edge("fallback_explain", "check_handoff")
    builder.add_conditional_edges(
        "check_handoff",
        lambda s: "pause" if s.get("handoff") else "__end__",
        {"pause": "pause", "__end__": END},
    )
    builder.add_edge("pause", END)
    return builder.compile()
