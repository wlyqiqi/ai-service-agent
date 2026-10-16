"""Tell a real seat Take over apart from Chatwoot's bot-failure auto-open.

Official Agent Bot status is pending=bot / open=human, but Chatwoot also
calls ``conversation.open!`` when the Agent Bot webhook times out
(``Net::ReadTimeout``). That system open is followed by an activity whose
English copy is:

    Conversation was marked open by system due to an error with the agent bot.

The status webhook is built before that activity exists, and the assignee
stays the Agent Bot (or unset). The Take over button opens first and
self-assigns second, so the positive signal is a User assignee — on the
open payload, on a later ``conversation_updated``, or on the self-assign
activity — not status ``open`` by itself.
"""

from __future__ import annotations

BOT_ERROR_MARK = "error with the agent bot"
_ASSIGN_MARKS = ("self-assigned this conversation", "assigned to ")


def _as_dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _meta(data: dict) -> dict:
    meta = _as_dict(data.get("meta"))
    if meta:
        return meta
    return _as_dict(_as_dict(data.get("conversation")).get("meta"))


def _is_activity(message: dict) -> bool:
    kind = message.get("message_type")
    if isinstance(kind, str):
        return kind.lower() == "activity"
    return kind == 2


def _activity_texts(data: dict) -> list[str]:
    texts: list[str] = []

    def take(message: object) -> None:
        if not isinstance(message, dict) or not _is_activity(message):
            return
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            texts.append(content)

    take(data)
    messages = data.get("messages")
    if isinstance(messages, list):
        for item in messages:
            take(item)
    nested = _as_dict(data.get("conversation")).get("messages")
    if isinstance(nested, list):
        for item in nested:
            take(item)
    return texts


def is_bot_error_signal(data: dict) -> bool:
    """Activity copy Chatwoot posts after an Agent Bot webhook failure."""
    blob = "\n".join(_activity_texts(data)).lower()
    return BOT_ERROR_MARK in blob


def human_assignee_present(data: dict) -> bool:
    """True when the payload shows a human seat, not the Agent Bot."""
    meta = _meta(data)
    assignee_type = meta.get("assignee_type")
    if isinstance(assignee_type, str):
        return assignee_type.lower() == "user"
    assignee = _as_dict(meta.get("assignee"))
    kind = assignee.get("type")
    if isinstance(kind, str):
        return kind.lower() == "user"
    for change in data.get("changed_attributes") or []:
        if not isinstance(change, dict):
            continue
        assignee_id = change.get("assignee_id")
        if isinstance(assignee_id, dict) and assignee_id.get("current_value") not in (None, "", 0):
            return True
        changed_type = change.get("assignee_type")
        if isinstance(changed_type, dict):
            current = changed_type.get("current_value")
            if isinstance(current, str) and current.lower() == "user":
                return True
    return False


def is_assignment_activity(data: dict) -> bool:
    """Self-assign / Assigned-to activity. The bot-error sentence is not one."""
    if is_bot_error_signal(data):
        return False
    blob = "\n".join(_activity_texts(data)).lower()
    return any(mark in blob for mark in _ASSIGN_MARKS)


def human_takeover_signal(data: dict) -> bool:
    """Real seat Take over. A User assignee wins over a stale bot-error sentence."""
    if human_assignee_present(data):
        return True
    if is_bot_error_signal(data):
        return False
    return is_assignment_activity(data)
