"""ApprovalRequest（approval-spec.md §2）。

选 TypedDict（普通 dict）而非 pydantic：LangGraph 1.2.11 checkpoint 序列化 pydantic 实例
触发 `Deserializing unregistered type ... will be blocked in a future version` 弃用警告
（2026-09-15 实测），违反"不依赖会被未来版本破坏的实现细节"原则。TypedDict 满足
state-spec §1 `pending_approval: ApprovalRequest | None` 注解且天然 JSON 可序列化。
"""

from datetime import datetime, timedelta, timezone
from typing import TypedDict
from uuid import uuid4


class ApprovalRequest(TypedDict):
    approval_id: str
    type: str
    summary: str
    requester_user_id: str
    created_at: str
    expires_at: str
    status: str
    decision: str | None
    decided_by: str | None
    decided_at: str | None


def resume_decision(resume) -> dict | None:
    """Extract the first HITL decision from a resume payload, if any.

    Official vocabulary is ``{"decisions": [{"type": "approve" | "reject", "message": ...}]}``
    (``HumanInTheLoopMiddleware`` / Agent Inbox, see ADR-0006). We emit the older
    ``{"action": ..., "note": ...}`` shape ourselves and must keep accepting both for
    the compatibility window, so callers go through this helper rather than poking keys.
    """
    if not isinstance(resume, dict):
        return None
    decisions = resume.get("decisions")
    if isinstance(decisions, list) and decisions:
        first = decisions[0]
        if isinstance(first, dict) and isinstance(first.get("type"), str):
            return first
    return None


def _resume_action(resume) -> str | None:
    decision = resume_decision(resume)
    if decision is not None:
        return decision["type"]
    if not isinstance(resume, dict):
        return None
    action = resume.get("action")
    return action if isinstance(action, str) else None


def resume_reject_note(resume) -> str | None:
    """Rejection reason: HITL ``decisions[0].message`` or legacy ``note``."""
    decision = resume_decision(resume)
    if decision is not None:
        message = decision.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
        return None
    if isinstance(resume, dict):
        note = resume.get("note")
        if isinstance(note, str) and note.strip():
            return note.strip()
    return None


def resolve_approval_outcome(resume) -> str:
    """Map an interrupt resume payload to ``approved`` / ``rejected`` / ``expired``.

    Fail-closed: a missing or unknown action is treated as ``rejected``
    (approval-spec §3.1). ``result="expired"`` wins over any action.
    Accepts both the HITL ``{"decisions": [...]}`` shape and the legacy
    ``{"action": ...}`` shape (ADR-0006 compatibility window).
    """
    if isinstance(resume, dict) and resume.get("result") == "expired":
        return "expired"
    return "approved" if _resume_action(resume) == "approve" else "rejected"


def make_approval_request(requester_user_id: str, summary: str, expires_minutes: int) -> ApprovalRequest:
    now = datetime.now(timezone.utc)
    return {
        "approval_id": str(uuid4()),
        "type": "high_risk",
        "summary": summary,
        "requester_user_id": requester_user_id,
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=expires_minutes)).isoformat(),
        "status": "pending",
        "decision": None,
        "decided_by": None,
        "decided_at": None,
    }
