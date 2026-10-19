"""Inbound → Form Interrupt decision (single source of truth).

This module owns *only* the decision for an inbound message while a Mutation
Form Interrupt is pending: confirm / cancel / implicit cancel / refuse.

Executing the decision stays with the caller:

- ``Command(resume=...)`` and the two-phase Implicit Cancel (silent cancel
  resume, then a normal new turn on the same thread) are graph invocation
  orchestration — ``AGENTS.md §4`` keeps that out of a decision module.
- Re-projecting the form on ``REFUSE`` is transport projection.

Layering: harness may import graph; graph must never import harness.
``FORM_RESUME_SKIP_FIELDS`` therefore lives in
``graph/workflows/form_resume.py`` and is extended here with inbound-only
control keys.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from ai_service_agent.graph.workflows.form_resume import FORM_RESUME_SKIP_FIELDS

# Inbound form payloads may carry transport control keys that are never tool
# business fields. ``confirmation`` is Chatwoot's legacy confirm control.
FORM_INBOUND_SKIP_FIELDS = FORM_RESUME_SKIP_FIELDS | {"confirmation"}

_CONFIRM_ACTIONS = frozenset({"confirm", "confirmed"})
_CANCEL_ACTIONS = frozenset({"cancel", "canceled"})

# Tools whose Chatwoot form has no action select (#71): a submit carrying only
# business fields is the primary confirm gesture.
_PRIMARY_SUBMIT_TOOLS = frozenset({"update_address"})


class FormInboundKind(str, Enum):
    """Where the inbound came from."""

    FORM_SUBMIT = "form_submit"  # Chatwoot message_updated + submitted_values
    TEXT = "text"                # Plain chat text
    EXPLICIT = "explicit"        # HTTP button (/actions/confirm)


class FormDecisionKind(str, Enum):
    CONFIRM = "confirm"
    CANCEL = "cancel"
    IMPLICIT_CANCEL = "implicit_cancel"
    REFUSE = "refuse"


class PendingFormPolicy(str, Enum):
    """How a non-form inbound is handled while a form is pending.

    ``IMPLICIT_CANCEL`` is the project default (CONTEXT.md Implicit Cancel):
    cancel the stale interrupt silently, then route the message as a new turn.
    ``REFUSE`` keeps the interrupt and lets the transport re-project the form
    (strict Form-Only Write). Callers must choose it explicitly.
    """

    IMPLICIT_CANCEL = "implicit_cancel"
    REFUSE = "refuse"


@dataclass(frozen=True)
class FormDecision:
    kind: FormDecisionKind
    # Ready to pass to ``Command(resume=...)`` for CONFIRM / CANCEL.
    # ``IMPLICIT_CANCEL`` carries the cancel payload the caller resumes with.
    # ``REFUSE`` carries None: the interrupt stays pending.
    resume_payload: dict[str, Any] | None = None


def normalize_submitted_values(value: Any) -> dict[str, Any] | None:
    """Accept Chatwoot's object shape and the widget/agent-bot ``[{name, value}]`` array."""
    if isinstance(value, dict):
        return value
    if not isinstance(value, list):
        return None
    normalized: dict[str, Any] = {}
    for item in value:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("title")
        if name is not None and "value" in item:
            normalized[str(name)] = item["value"]
    return normalized or None


def _business_fields(values: dict[str, Any]) -> dict[str, Any]:
    """Drop control / projection keys; keep business values as submitted.

    Empty strings are preserved on purpose: the graph's recheck maps them to a
    field-level ``invalid_args`` error, which is the pre-refactor behaviour.
    """
    return {
        key: value
        for key, value in values.items()
        if key not in FORM_INBOUND_SKIP_FIELDS
    }


def _non_empty_business_fields(values: dict[str, Any]) -> dict[str, Any]:
    """Business fields with empty values removed (primary-submit detection only)."""
    return {
        key: value
        for key, value in _business_fields(values).items()
        if value not in (None, "")
    }


def _pending_decision(policy: PendingFormPolicy) -> FormDecision:
    if policy is PendingFormPolicy.REFUSE:
        return FormDecision(FormDecisionKind.REFUSE)
    return FormDecision(
        FormDecisionKind.IMPLICIT_CANCEL, {"action": "cancel"}
    )


def decide_form_inbound(
    interrupt_value: dict[str, Any] | None,
    *,
    kind: FormInboundKind,
    submitted_values: dict[str, Any] | None = None,
    fields: dict[str, Any] | None = None,
    pending_policy: PendingFormPolicy = PendingFormPolicy.IMPLICIT_CANCEL,
) -> FormDecision:
    """Decide how one inbound acts on a pending Form Interrupt.

    ``interrupt_value`` is the pending ``interrupt()`` payload. Plain text is
    never parsed into business fields (Form-Only Write): it can only resolve
    through ``pending_policy``.
    """
    if kind is FormInboundKind.EXPLICIT:
        explicit = dict(fields or {})
        action = explicit.get("action")
        if action in _CONFIRM_ACTIONS:
            return FormDecision(
                FormDecisionKind.CONFIRM,
                {"action": "confirm", **_business_fields(explicit)},
            )
        if action in _CANCEL_ACTIONS:
            return FormDecision(FormDecisionKind.CANCEL, {"action": "cancel"})
        return FormDecision(FormDecisionKind.REFUSE)

    if kind is FormInboundKind.FORM_SUBMIT:
        submitted = dict(submitted_values or {})
        action = submitted.get("confirmation") or submitted.get("action")
        business = _business_fields(submitted)
        tool = (interrupt_value or {}).get("tool")
        primary_submit = (
            not action
            and bool(_non_empty_business_fields(submitted))
            and submitted_values is not None
            and tool in _PRIMARY_SUBMIT_TOOLS
        )
        if action in _CONFIRM_ACTIONS or primary_submit:
            return FormDecision(
                FormDecisionKind.CONFIRM,
                {"action": "confirm", **business},
            )
        if action in _CANCEL_ACTIONS:
            return FormDecision(FormDecisionKind.CANCEL, {"action": "cancel"})
        # Malformed submit (no action, not a primary-submit tool): same policy
        # as a non-form inbound, matching the pre-refactor behaviour.
        return _pending_decision(pending_policy)

    # Plain text is never a Form Resume (Form-Only Write).
    return _pending_decision(pending_policy)
