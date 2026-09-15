from ai_service_agent.graph.state import CustomerServiceState, EXECUTION_STATUSES


def test_execution_status_has_waiting_handoff():
    assert "waiting_handoff" in EXECUTION_STATUSES
    assert len(EXECUTION_STATUSES) == 9


def test_state_requires_session_id():
    s: CustomerServiceState = {"messages": [], "user_id": "u1", "session_id": "s1"}
    assert s["session_id"] == "s1"

def test_state_p3_fields():
    s: CustomerServiceState = {
        "messages": [], "user_id": "u1", "session_id": "s1",
        "guard_status": None, "pending_approval": None,
    }
    assert s["guard_status"] is None
    assert s["pending_approval"] is None
