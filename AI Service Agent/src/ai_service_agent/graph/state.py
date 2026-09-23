from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

from ai_service_agent.graph.approval import ApprovalRequest

EXECUTION_STATUSES = (
    "idle", "routing", "clarifying", "running",
    "waiting_confirmation", "waiting_approval",
    "waiting_handoff", "completed", "failed",
)


class CustomerServiceState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    user_id: str
    session_id: str
    capability: str
    risk_level: Literal["read", "mutation", "high_risk"]
    confidence: float
    guard_status: Literal["pass", "fail"] | None   # Guard 并行检测结果（graph-spec §2）
    context: dict
    pending_approval: ApprovalRequest | None       # 派生业务字段（approval-spec §2）
    execution_status: Literal[
        "idle", "routing", "clarifying", "running",
        "waiting_confirmation", "waiting_approval",
        "waiting_handoff", "completed", "failed",
    ]
    handoff: bool
    handoff_reason: str
