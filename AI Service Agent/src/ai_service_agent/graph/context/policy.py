from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import partial

from langchain_core.messages import BaseMessage

from ai_service_agent.config import Settings
from ai_service_agent.graph.context.builders import (
    build_read_context,
    build_recent_visible_context,
)


@dataclass(frozen=True)
class ContextPolicy:
    name: str
    max_total_tokens: int
    build_messages: Callable[[Sequence[BaseMessage]], list[BaseMessage]]


def make_router_policy(settings: Settings) -> ContextPolicy:
    return ContextPolicy(
        name="router",
        max_total_tokens=settings.router_total_input_max_tokens,
        build_messages=partial(build_recent_visible_context, previous_completed_turns=1),
    )


def make_fallback_policy(settings: Settings) -> ContextPolicy:
    return ContextPolicy(
        name="fallback",
        max_total_tokens=settings.fallback_total_input_max_tokens,
        build_messages=partial(build_recent_visible_context, previous_completed_turns=2),
    )


def make_read_policy(settings: Settings) -> ContextPolicy:
    return ContextPolicy(
        name="read",
        max_total_tokens=settings.read_total_input_max_tokens,
        build_messages=partial(build_read_context, previous_completed_turns=2),
    )


def make_select_policy(settings: Settings) -> ContextPolicy:
    return ContextPolicy(
        name="select",
        max_total_tokens=settings.select_total_input_max_tokens,
        build_messages=partial(build_recent_visible_context, previous_completed_turns=1),
    )
