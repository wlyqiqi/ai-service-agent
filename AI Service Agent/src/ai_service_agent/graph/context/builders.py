import re
from collections.abc import Sequence

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from ai_service_agent.graph.messages import message_text

_THINK_PATTERN = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)


def strip_reasoning(text: str) -> str:
    return _THINK_PATTERN.sub("", text).strip()


def visible_text(message: BaseMessage) -> str:
    return strip_reasoning(message_text(message))


def _is_human(message: BaseMessage) -> bool:
    return isinstance(message, HumanMessage)


def _is_final_ai(message: BaseMessage) -> bool:
    return isinstance(message, AIMessage) and not message.tool_calls


def last_human_index(messages: Sequence[BaseMessage]) -> int:
    for index in range(len(messages) - 1, -1, -1):
        if _is_human(messages[index]):
            return index
    return -1


def _completed_visible_turns(messages: Sequence[BaseMessage]) -> list[list[BaseMessage]]:
    turns: list[list[BaseMessage]] = []
    current: list[BaseMessage] = []

    for message in messages:
        if _is_human(message):
            if current:
                turns.append(current)
            current = [message]
        elif current:
            current.append(message)

    if current:
        turns.append(current)
    return turns


def _visible_turn_messages(turn: Sequence[BaseMessage]) -> list[BaseMessage]:
    final_ai = next((message for message in reversed(turn) if _is_final_ai(message)), None)
    visible = [HumanMessage(content=visible_text(turn[0]))]
    if final_ai is not None:
        visible.append(AIMessage(content=visible_text(final_ai)))
    return visible


def build_recent_visible_context(
    messages: Sequence[BaseMessage],
    *,
    previous_completed_turns: int,
) -> list[BaseMessage]:
    current_index = last_human_index(messages)
    if current_index < 0:
        return []

    prior_turns = _completed_visible_turns(messages[:current_index])
    selected_turns = prior_turns[-previous_completed_turns:]
    selected = [
        message
        for turn in selected_turns
        for message in _visible_turn_messages(turn)
    ]
    current = messages[current_index:]
    selected.extend(
        message
        for message in current
        if _is_human(message) or _is_final_ai(message)
    )
    return selected


def build_read_context(
    messages: Sequence[BaseMessage],
    *,
    previous_completed_turns: int,
) -> list[BaseMessage]:
    current_index = last_human_index(messages)
    if current_index < 0:
        return list(messages)

    prior_visible = build_recent_visible_context(
        messages[:current_index],
        previous_completed_turns=previous_completed_turns,
    )
    return [*prior_visible, *messages[current_index:]]
