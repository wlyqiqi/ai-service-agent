"""Shared Form Resume helpers for MutationGraph and HighRiskGraph collection."""

import uuid
from typing import Any, Literal

from pydantic import BaseModel, create_model

# Resume control / projection keys that are never tool business fields.
# Single source of truth: harness/form_gate.py extends this with inbound-only
# control keys (``confirmation``) when parsing form submissions.
FORM_RESUME_SKIP_FIELDS = frozenset(
    {"action", "request_id", "current_address", "abandon_footnote"}
)

# One scrub set for both lanes. Union of the keys each lane was clearing
# (form_errors, recheck_reason, confirm_result). confirm_result is in the
# union even though Mutation's read point is rewritten by form_interrupt
# before it is observed: one set, no per-lane optional keys.
# Empty-state inline scrub is a different operation and must not use this set
# (it also drops form_session_id; Mutation empty additionally drops pending_tool).
FORM_SCRUB_KEYS = frozenset({"form_errors", "recheck_reason", "confirm_result"})


def prepare_form_update(state: dict) -> dict:
    """Scrub stale Form keys, mint a session id, and mark waiting_confirmation.

    Both lanes call this. There is no parameter that scrubs a different set.
    """
    ctx = {
        key: item
        for key, item in dict(state.get("context") or {}).items()
        if key not in FORM_SCRUB_KEYS
    }
    # Mint form_session_id before Form Interrupt so resume re-runs of
    # form_interrupt keep the same session (interrupt-before is idempotent).
    # Recheck failures loop back to form_interrupt without prepare_form, so the
    # id stays stable for form_rejected; a later new form gets a fresh id here.
    ctx["form_session_id"] = str(uuid.uuid4())
    return {"execution_status": "waiting_confirmation", "context": ctx}


def form_resume_model(tool_schema: type[BaseModel] | None) -> type[BaseModel]:
    """Confirm Form Resume shape derived from the tool schema.

    Business fields stay optional so Form Cancel (``{"action": "cancel"}``) still
    validates. ``request_id`` is not a form field; the graph injects it.
    """
    definitions: dict[str, Any] = {
        "action": (Literal["confirm", "cancel"], ...),
    }
    if tool_schema is not None:
        for name, field in tool_schema.model_fields.items():
            if name in ("request_id", "action"):
                continue
            definitions[name] = (field.annotation | None, None)
    return create_model("MutationFormResume", **definitions)


def resume_action(resume) -> str | None:
    if isinstance(resume, BaseModel):
        action = resume.action
    elif isinstance(resume, dict):
        action = resume.get("action")
    else:
        return None
    return action if isinstance(action, str) else None


def resume_field_values(resume) -> dict:
    """Structured Confirm values. Ignores action and any client request_id."""
    if isinstance(resume, BaseModel):
        data = resume.model_dump()
    elif isinstance(resume, dict):
        data = dict(resume)
    else:
        return {}
    return {
        key: value
        for key, value in data.items()
        if key not in FORM_RESUME_SKIP_FIELDS
        and value is not None
    }


def schema_field_args(values: dict, tool_schema: type[BaseModel] | None) -> dict:
    """Keep only tool-schema business fields (drop Order Picker echoes the tool does not take)."""
    if tool_schema is None:
        return dict(values)
    allowed = {name for name in tool_schema.model_fields if name != "request_id"}
    return {key: value for key, value in values.items() if key in allowed}
