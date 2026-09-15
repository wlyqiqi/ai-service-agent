import json

from ai_service_agent.graph.approval import ApprovalRequest, make_approval_request


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
