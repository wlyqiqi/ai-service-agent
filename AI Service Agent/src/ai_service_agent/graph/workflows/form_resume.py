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


def _is_uuid4(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return uuid.UUID(value).version == 4
    except ValueError:
        return False


def coerce_request_id(*candidates: object) -> str:
    """First uuid4 among ``candidates``, otherwise a new uuid4.

    Select and Form Resume confirm call this before execute → tools.
    Empty string, the sentinel null, and any other non-uuid4 are not
    idempotency keys. ``tools/write.py`` may still replace an empty string
    with a fresh uuid4; that direct-call safety net is not the MutationGraph
    path — a retry would miss the previous row.
    """
    for value in candidates:
        if isinstance(value, str) and _is_uuid4(value):
            return value
    return str(uuid.uuid4())


def stamp_pending_tool_request_id(pending: dict) -> dict:
    """Copy one uuid4 onto ``pending_tool.request_id`` and ``args.request_id``.

    An empty args dict, or an empty args request_id, must not reach ToolNode:
    ``write.py`` would mint a new id on every call and break
    ``(tool_name, request_id)`` replay. A uuid4 already stored by select or
    confirm is kept, including across recheck → form re-entry.
    """
    args = dict(pending.get("args") or {})
    request_id = coerce_request_id(pending.get("request_id"), args.get("request_id"))
    args["request_id"] = request_id
    stamped = dict(pending)
    stamped["args"] = args
    stamped["request_id"] = request_id
    return stamped
