import langgraph.types as _lg_types
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from ai_service_agent.config import get_settings
from ai_service_agent.graph.state import CustomerServiceState


def _last_user_content(state: dict) -> str:
    """转人工信号只来自用户输入（safety-spec.md §2），不扫描 AI 自己的回复。"""
    for m in reversed(state.get("messages", [])):
        if isinstance(m, HumanMessage):
            return m.content if isinstance(m.content, str) else str(m.content)
    return ""


def check_handoff(state: dict) -> dict | Command:
    """确定性规则：显式转人工关键词命中才置位（safety-spec.md §2）；LLM 无权临时决定。

    命中时先置位再中断：Command(update) 保证 execution_status=waiting_handoff
    在中断时刻生效（state-spec.md §1），随后路由到 pause 节点抛 interrupt。
    """
    last_content = _last_user_content(state)
    if any(kw in last_content for kw in get_settings().handoff_keywords):
        return Command(
            update={"handoff": True, "execution_status": "waiting_handoff"},
            goto="pause",
        )
    return {"handoff": False, "execution_status": "completed"}


def pause(state: dict) -> dict:
    """统一抛 interrupt(handoff_request)，由外层接管（graph-spec.md §6.5）。"""
    last_content = _last_user_content(state)
    _lg_types.interrupt(
        {
            "type": "handoff_request",
            "reason": "用户显式请求转人工",
            "summary": str(last_content)[:80],
        }
    )
    return {}


def make_fallback_graph(model):
    """Fallback / Concierge：check_handoff 前置（graph-spec.md §6.4 P1 定案）。

    命中 handoff -> pause（interrupt，图内不生成文案）；未命中 -> explain 兜底回复。
    """
    def explain(state: dict) -> dict:
        return {"messages": [AIMessage(content="我暂时无法处理，请换个说法或联系人工客服。")]}

    builder = StateGraph(CustomerServiceState)
    builder.add_node("check_handoff", check_handoff)
    builder.add_node("explain", explain)
    builder.add_node("pause", pause)
    builder.add_edge(START, "check_handoff")
    builder.add_conditional_edges(
        "check_handoff",
        lambda s: "pause" if s.get("handoff") else "explain",
        {"pause": "pause", "explain": "explain"},
    )
    builder.add_edge("explain", END)
    builder.add_edge("pause", END)
    return builder.compile()
