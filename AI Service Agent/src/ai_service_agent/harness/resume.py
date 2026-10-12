from datetime import datetime, timezone

from langchain_core.messages import BaseMessage
from langgraph.types import Command

from ai_service_agent.config import get_settings

# 唯一 resume schema 见 graph-spec.md §3 / approval-spec.md §3.3
HANDOFF_RESUME_ACTION = "handoff_resumed"


def resolve_resume_payload(interrupt_value, latest_user_text: str = "") -> dict | None:
    """入口层 resume 调度：由挂起中断构造 resume payload（唯一 schema 见 graph-spec §3）。

    handoff_request -> {"action": "handoff_resumed", "note": <latest>}；
    confirmation_required -> confirm/cancel 关键词映射（未命中 fail-closed cancel，OQ-1）；
    Form Interrupt 不在此映射（纯文本不是 Form Resume）；
    无中断 / 未知类型 -> None（调用方按普通输入新起 run）。
    P3 Task 5 扩展：approval 中断在 approval_dispatch 单独处理（不在此映射）。
    """
    if not isinstance(interrupt_value, dict):
        return None
    t = interrupt_value.get("type")
    if t == "handoff_request":
        return {"action": HANDOFF_RESUME_ACTION, "note": latest_user_text}
    if t == "confirmation_required":
        s = get_settings()
        if any(k in latest_user_text for k in s.confirm_keywords):
            return {"action": "confirm"}
        # 未命中关键词 fail-closed：按取消处理（OQ-1；绝不默认放行）
        return {"action": "cancel"}
    return None


def approval_dispatch(interrupt_value, now=None) -> str | None:
    """approval_required 中断的入口判定（OQ-4）：
    'pending'（保持挂起：不 resume、不新起 run，等审批完成/过期）
    'expired'（以 {"result":"expired"} 恢复，HighRiskGraph 过期收尾）；其它中断 -> None。
    """
    if not isinstance(interrupt_value, dict) or interrupt_value.get("type") != "approval_required":
        return None
    expires_at = interrupt_value.get("expires_at")
    if not expires_at:
        return "pending"
    now = now or datetime.now(timezone.utc)
    try:
        expires = datetime.fromisoformat(expires_at)
    except ValueError:
        return "pending"
    return "expired" if now >= expires else "pending"
