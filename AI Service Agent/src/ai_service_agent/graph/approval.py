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
