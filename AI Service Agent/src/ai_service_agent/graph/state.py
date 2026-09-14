from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

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
    context: dict
    execution_status: Literal[
        "idle", "routing", "clarifying", "running",
        "waiting_confirmation", "waiting_approval",
        "waiting_handoff", "completed", "failed",
    ]
    handoff: bool
