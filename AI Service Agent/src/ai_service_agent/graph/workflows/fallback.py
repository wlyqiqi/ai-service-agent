import langgraph.types as _lg_types
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from ai_service_agent.config import get_settings
from ai_service_agent.graph.context import ContextBudgetMiddleware, make_fallback_policy
from ai_service_agent.graph.fault_tolerance import (
    RETRY_LLM,
    make_subgraph_error_handler,
    make_timeout_policy,
)
from ai_service_agent.graph.workflows.copy import RUNTIME_ERROR_TEXT
from ai_service_agent.graph.complaint_rules import emotion_auto_handoff_allowed
from ai_service_agent.graph.handoff_rules import detect_handoff_intent
from ai_service_agent.graph.state import CustomerServiceState
from ai_service_agent.prompts import FALLBACK_SYSTEM_PROMPT


def _last_user_content(state: dict) -> str:
    """转人工信号只来自用户输入（safety-spec.md §2），不扫描 AI 自己的回复。"""
    for m in reversed(state.get("messages", [])):
        if isinstance(m, HumanMessage):
            return m.content if isinstance(m.content, str) else str(m.content)
    return ""


def _emotion_signal(text: str) -> bool:
    """情绪自动转人工检测器。PR-A 不启用；测试可替换以锁待答禁令。"""
    del text
    return False


def emotion_handoff_reason(state: dict) -> str | None:
    """情绪规则出口。``complaint_offer_pending`` 期间必须返回 None。"""
    if not emotion_auto_handoff_allowed(state.get("context")):
        return None
    if _emotion_signal(_last_user_content(state)):
        return "情绪信号"
    return None


def check_handoff(state: dict) -> dict | Command:
    """确定性规则：显式转人工 / 连续 fallback 超限才置位（safety-spec.md §2）；LLM 无权临时决定。

    命中时先置位再中断：Command(update) 保证 execution_status=waiting_handoff
    在中断时刻生效（state-spec.md §1），随后路由到 pause 节点抛 interrupt。
    """
    last_content = _last_user_content(state)
    fallback_count = int(state.get("context", {}).get("fallback_count", 0))
    if fallback_count >= get_settings().fallback_max_turns:
        return Command(
            update={
                "handoff": True,
                "handoff_reason": "fallback limit exceeded",
                "execution_status": "waiting_handoff",
                "context": {**state.get("context", {}), "fallback_count": 0},
            },
            goto="pause",
        )
    if detect_handoff_intent(last_content):
        return Command(
            update={
                "handoff": True,
                "handoff_reason": "用户显式请求转人工",
                "execution_status": "waiting_handoff",
            },
            goto="pause",
        )
    emotion_reason = emotion_handoff_reason(state)
    if emotion_reason:
        return Command(
            update={
                "handoff": True,
                "handoff_reason": emotion_reason,
                "execution_status": "waiting_handoff",
            },
            goto="pause",
        )
    return {
        "handoff": False,
        "execution_status": "completed",
        "context": {**state.get("context", {}), "fallback_count": fallback_count + 1},
    }


def pause(state: dict) -> dict:
    """统一抛 interrupt(handoff_request)，由外层接管（graph-spec.md §6.5）。"""
    last_content = _last_user_content(state)
    _lg_types.interrupt(
        {
            "type": "handoff_request",
            "reason": state.get("handoff_reason", "用户显式请求转人工"),
            "summary": str(last_content)[:80],
        }
    )
    return {}


def fallback_failed(state: dict) -> dict:
    """Fallback 本图错误收尾：不依赖 root 默认 final 节点。"""
    return {"messages": [AIMessage(content=RUNTIME_ERROR_TEXT)], "execution_status": "failed"}


def make_fallback_graph(model):
    """Fallback / Concierge：check_handoff 前置（graph-spec.md §6.4 P1 定案）。

    命中 handoff -> pause（interrupt，图内不生成文案）；未命中 -> explain_llm
    （LLM 生成友好兜底回复，FALLBACK_SYSTEM_PROMPT，人工客服口吻）。
    """
    # LLM 节点（2026-09-18）：无工具 ReAct，仅生成友好兜底文案；token 流经 harness messages 模式。
    agent = create_agent(
        model,
        tools=[],
        system_prompt=FALLBACK_SYSTEM_PROMPT,
        middleware=[ContextBudgetMiddleware(make_fallback_policy(get_settings()))],
    )

    async def explain_llm(state: dict, config: RunnableConfig) -> dict:
        return await agent.ainvoke(state, config=config)

    builder = StateGraph(CustomerServiceState).set_node_defaults(
        error_handler=make_subgraph_error_handler("fallback_failed"),
    )
    builder.add_node("check_handoff", check_handoff)
    builder.add_node("fallback_failed", fallback_failed)
    builder.add_node("pause", pause)
    builder.add_node(
        "explain_llm",
        explain_llm,
        retry_policy=RETRY_LLM,
        timeout=make_timeout_policy(get_settings().llm_node_timeout_seconds),
    )
    builder.add_edge(START, "check_handoff")
    builder.add_conditional_edges(
        "check_handoff",
        lambda s: "pause" if s.get("handoff") else "explain_llm",
        {"pause": "pause", "explain_llm": "explain_llm"},
    )
    builder.add_edge("explain_llm", END)
    builder.add_edge("fallback_failed", END)
    builder.add_edge("pause", END)
    return builder.compile()
