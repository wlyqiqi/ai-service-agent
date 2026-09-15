from langchain_core.messages import BaseMessage
from langgraph.types import Command

# 唯一 resume schema 见 graph-spec.md §3 / approval-spec.md §3.3
HANDOFF_RESUME_ACTION = "handoff_resumed"


def resolve_resume_payload(interrupt_value, latest_user_text: str = "") -> dict | None:
    """入口层 resume 调度：由挂起中断构造 resume payload。

    handoff_request -> {"action": "handoff_resumed", "note": <latest_user_text>}；
    无中断 / 未知类型 -> None（调用方按普通输入新起 run）。
    P3/P4 扩展：approval / confirmation 中断在此映射为同一 schema 的 approve/reject/confirm/cancel。
    """
    if not isinstance(interrupt_value, dict):
        return None
    if interrupt_value.get("type") == "handoff_request":
        return {"action": HANDOFF_RESUME_ACTION, "note": latest_user_text}
    return None
