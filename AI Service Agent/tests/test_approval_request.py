import json

from ai_service_agent.graph.approval import (
    ApprovalRequest,
    make_approval_request,
    resolve_approval_outcome,
)


def test_make_approval_request_fields():
    req = make_approval_request("u1", "即将执行退款", expires_minutes=30)
    assert req["requester_user_id"] == "u1"
    assert req["summary"] == "即将执行退款"
    assert req["type"] == "high_risk"
    assert req["status"] == "pending"
    assert req["approval_id"]
    assert req["decision"] is None and req["decided_by"] is None and req["decided_at"] is None
    assert req["created_at"] < req["expires_at"]


def test_approval_request_is_plain_dict_json_serializable():
    """TypedDict = 普通 dict：checkpoint 序列化不依赖 pydantic（实测弃用警告，见 Task 1 设计依据）。"""
    req = make_approval_request("u1", "s", 30)
    assert isinstance(req, dict)
    assert json.dumps(req)  # 不抛异常


def test_resolve_outcome_approved_and_rejected():
    assert resolve_approval_outcome({"action": "approve"}) == "approved"
    assert resolve_approval_outcome({"action": "reject"}) == "rejected"


def test_resolve_outcome_fail_closed_on_unknown_action():
    assert resolve_approval_outcome({"action": "maybe"}) == "rejected"
    assert resolve_approval_outcome({}) == "rejected"
    assert resolve_approval_outcome(None) == "rejected"
    assert resolve_approval_outcome("approve") == "rejected"


def test_resolve_outcome_expired_wins_over_action():
    assert resolve_approval_outcome({"action": "approve", "result": "expired"}) == "expired"

