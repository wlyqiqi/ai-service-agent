"""Form Interrupt projections for the event protocol (event-spec §2.2).

Waiting on a Mutation Form is the checkpoint interrupt. ``form_required`` and
``form_rejected`` are derived from that interrupt for clients.
"""

from __future__ import annotations

_OTHER_INTERRUPT_TYPES = {"approval_required", "handoff_request", "confirmation_required"}


def is_form_interrupt(value) -> bool:
    """True when ``value`` is a Mutation Form Interrupt payload."""
    if not isinstance(value, dict):
        return False
    if value.get("type") in _OTHER_INTERRUPT_TYPES:
        return False
    return isinstance(value.get("tool"), str) and isinstance(value.get("order_candidates"), list)


def project_form_interrupt(interrupt) -> dict | None:
    """Map one Form Interrupt onto ``form_required`` or ``form_rejected``."""
    value = getattr(interrupt, "value", None)
    if not is_form_interrupt(value):
        return None
    errors = value.get("errors")
    if errors in (None, {}, [], ""):
        errors = None
    data = {
        "tool": value["tool"],
        "order_candidates": value["order_candidates"],
        "form_session_id": value.get("form_session_id") or getattr(interrupt, "id", None),
    }
    for key in ("fields", "prefills", "summary"):
        if key in value and value[key] is not None:
            data[key] = value[key]
    if errors:
        data["errors"] = errors
    schema = getattr(interrupt, "response_schema", None)
    if schema is not None:
        data["response_schema"] = schema
    return {"type": "form_rejected" if errors else "form_required", "data": data}
