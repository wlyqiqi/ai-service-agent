"""薄投诉面：安抚 + 询问转人工。本轮不 interrupt、不建 awaiting_seat。

明确同意不在这里另起一套 interrupt。节点只落下 ``complaint_handoff_accept``，
由入口层复用既有 ``request_handoff``（``detect_handoff_intent`` 未命中、询问句
仍进了主图时的兜底）。裸「要」只再确认一次问法，pending 保持。
"""

from langchain_core.messages import AIMessage

from ai_service_agent.graph.messages import message_text
from ai_service_agent.graph.workflows.copy import complaint_empathy_text, complaint_reconfirm_text


def _latest_user_text(state: dict) -> str:
    messages = state.get("messages") or []
    for message in reversed(messages):
        if isinstance(message, dict):
            role = message.get("type", message.get("role", ""))
        else:
            role = getattr(message, "type", "")
        if role == "human":
            return message_text(message)
    return ""


def complaint_respond(state: dict) -> dict:
    """Policy 已把本轮送到投诉面。按 ``complaint_offer_decision`` 分三支。"""
    ctx = dict(state.get("context") or {})
    decision = ctx.pop("complaint_offer_decision", None)
    if decision == "accept":
        ctx["complaint_offer_pending"] = False
        ctx["complaint_handoff_accept"] = True
        ctx.pop("complaint_offer_reconfirmed", None)
        return {
            "context": ctx,
            "execution_status": "completed",
            "handoff": False,
        }
    if decision == "reconfirm":
        ctx["complaint_offer_pending"] = True
        ctx["complaint_offer_reconfirmed"] = True
        ctx["complaint_handoff_accept"] = False
        return {
            "messages": [AIMessage(content=complaint_reconfirm_text())],
            "context": ctx,
            "execution_status": "completed",
            "handoff": False,
        }
    text = _latest_user_text(state)
    ctx["complaint_offer_pending"] = True
    ctx["complaint_summary"] = text[:80]
    ctx["complaint_handoff_accept"] = False
    ctx.pop("complaint_offer_reconfirmed", None)
    return {
        "messages": [AIMessage(content=complaint_empathy_text(text))],
        "context": ctx,
        "execution_status": "completed",
        "handoff": False,
    }
