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
    route_intent: str
    target_graph: Literal[
        "read_graph", "mutation_graph", "high_risk_graph", "fallback"
    ]
    execution_class: Literal[
        "read_only", "standard_write", "high_risk"
    ]
    allowed_tools: list[str]
    requires_approval: bool
    confidence: float
    routing_backend: str | None
    routing_fallback: bool
    route_control: Literal["policy", "deepseek_fallback"] | None
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
