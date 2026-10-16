"""Classifier: system bot-error open vs a real seat Take over."""

from ai_service_agent.harness.handoff_signal import (
    human_assignee_present,
    human_takeover_signal,
    is_assignment_activity,
    is_bot_error_signal,
)

BOT_ERROR = "Conversation was marked open by system due to an error with the agent bot."


def test_bot_error_activity_is_not_a_takeover():
    payload = {
        "event": "message_created",
        "message_type": "activity",
        "content": BOT_ERROR,
        "conversation": {"id": 1, "status": "open", "meta": {"assignee_type": "AgentBot"}},
    }
    assert is_bot_error_signal(payload) is True
    assert human_assignee_present(payload) is False
    assert human_takeover_signal(payload) is False
    assert is_assignment_activity(payload) is False


def test_user_assignee_on_open_is_takeover():
    payload = {
        "event": "conversation_status_changed",
        "id": 5,
        "status": "open",
        "meta": {"assignee_type": "User", "assignee": {"id": 7, "name": "Qi", "type": "user"}},
    }
    assert human_takeover_signal(payload) is True
    assert is_bot_error_signal(payload) is False


def test_agent_bot_assignee_is_not_human():
    payload = {
        "status": "open",
        "meta": {"assignee_type": "AgentBot", "assignee": {"id": 3, "name": "Bot"}},
    }
    assert human_assignee_present(payload) is False
    assert human_takeover_signal(payload) is False


def test_nested_conversation_meta_counts():
    payload = {
        "event": "conversation_status_changed",
        "conversation": {
            "id": 9,
            "status": "open",
            "meta": {"assignee_type": "User", "assignee": {"id": 4, "name": "Seat"}},
        },
    }
    assert human_assignee_present(payload) is True


def test_changed_assignee_id_counts_as_human():
    payload = {
        "status": "open",
        "changed_attributes": [
            {"status": {"previous_value": "pending", "current_value": "open"}},
            {"assignee_id": {"previous_value": None, "current_value": 12}},
        ],
    }
    assert human_assignee_present(payload) is True


def test_self_assign_activity_is_takeover_without_meta():
    payload = {
        "event": "message_created",
        "message_type": 2,
        "content": "Qi self-assigned this conversation",
        "conversation": {"id": 5, "status": "open"},
    }
    assert is_assignment_activity(payload) is True
    assert human_takeover_signal(payload) is True


def test_assigned_to_activity_is_takeover():
    payload = {
        "message_type": "activity",
        "content": "Assigned to Qi by Qi",
        "conversation": {"status": "open"},
    }
    assert human_takeover_signal(payload) is True


def test_customer_text_is_not_an_activity_signal():
    payload = {
        "message_type": "incoming",
        "content": "Conversation was marked open by system due to an error with the agent bot.",
    }
    assert is_bot_error_signal(payload) is False
    assert human_takeover_signal(payload) is False


def test_user_assignee_wins_over_stale_bot_error_sentence():
    payload = {
        "message_type": "activity",
        "content": BOT_ERROR,
        "conversation": {
            "status": "open",
            "meta": {"assignee_type": "User", "assignee": {"id": 1, "name": "Qi", "type": "user"}},
        },
    }
    assert is_bot_error_signal(payload) is True
    assert human_takeover_signal(payload) is True


def test_bare_open_is_neither():
    payload = {"event": "conversation_status_changed", "id": 5, "status": "open"}
    assert human_takeover_signal(payload) is False
    assert is_bot_error_signal(payload) is False
