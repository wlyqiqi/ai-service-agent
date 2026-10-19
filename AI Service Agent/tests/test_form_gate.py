"""harness/form_gate.py — inbound → Form Interrupt decision (pure functions)."""

import pytest

from ai_service_agent.harness.form_gate import (
    FORM_INBOUND_SKIP_FIELDS,
    FormDecisionKind,
    FormInboundKind,
    decide_form_inbound,
    normalize_submitted_values,
)

ADDRESS_FORM = {"tool": "update_address", "order_candidates": [{"order_id": "ORD-1"}]}
CANCEL_FORM = {"tool": "cancel_order", "order_candidates": [{"order_id": "ORD-1"}]}


# --- normalize_submitted_values -------------------------------------------


def test_normalize_accepts_object():
    values = {"action": "confirm", "address": "A"}
    assert normalize_submitted_values(values) is values


def test_normalize_accepts_name_value_array():
    submitted = [
        {"name": "action", "value": "confirm"},
        {"name": "address", "value": "A"},
    ]
    assert normalize_submitted_values(submitted) == {"action": "confirm", "address": "A"}


def test_normalize_accepts_title_value_array():
    assert normalize_submitted_values([{"title": "address", "value": "A"}]) == {"address": "A"}


@pytest.mark.parametrize("value", [None, [], [{"name": "address"}], "text", 3])
def test_normalize_returns_none_for_unusable_shapes(value):
    assert normalize_submitted_values(value) is None


# --- FORM_SUBMIT: explicit actions ---------------------------------------


@pytest.mark.parametrize("action", ["confirm", "confirmed"])
def test_form_submit_confirm_action(action):
    decision = decide_form_inbound(
        ADDRESS_FORM,
        kind=FormInboundKind.FORM_SUBMIT,
        submitted_values={"action": action, "address": "A"},
    )
    assert decision.kind is FormDecisionKind.CONFIRM
    assert decision.resume_payload == {"action": "confirm", "address": "A"}


@pytest.mark.parametrize("action", ["cancel", "canceled"])
def test_form_submit_cancel_action(action):
    decision = decide_form_inbound(
        ADDRESS_FORM,
        kind=FormInboundKind.FORM_SUBMIT,
        submitted_values={"action": action, "address": "A"},
    )
    assert decision.kind is FormDecisionKind.CANCEL
    assert decision.resume_payload == {"action": "cancel"}


def test_confirmation_field_counts_as_action():
    decision = decide_form_inbound(
        ADDRESS_FORM,
        kind=FormInboundKind.FORM_SUBMIT,
        submitted_values={"confirmation": "confirm", "address": "A"},
    )
    assert decision.kind is FormDecisionKind.CONFIRM
    assert decision.resume_payload == {"action": "confirm", "address": "A"}


# --- FORM_SUBMIT: primary submit (#71) -----------------------------------


def test_primary_submit_update_address_confirms():
    decision = decide_form_inbound(
        ADDRESS_FORM,
        kind=FormInboundKind.FORM_SUBMIT,
        submitted_values={"order_id": "ORD-1", "address": "A"},
    )
    assert decision.kind is FormDecisionKind.CONFIRM
    assert decision.resume_payload == {
        "action": "confirm",
        "order_id": "ORD-1",
        "address": "A",
    }


def test_primary_submit_drops_control_and_projection_fields():
    decision = decide_form_inbound(
        ADDRESS_FORM,
        kind=FormInboundKind.FORM_SUBMIT,
        submitted_values={
            "order_id": "ORD-1",
            "address": "A",
            "request_id": "client-forged",
            "current_address": "OLD",
            "abandon_footnote": "footnote",
            "confirmation": "",
        },
    )
    assert decision.resume_payload == {
        "action": "confirm",
        "order_id": "ORD-1",
        "address": "A",
    }


def test_primary_submit_keeps_empty_string_for_field_level_rejection():
    """Empty business values must survive: recheck maps them to invalid_args."""
    decision = decide_form_inbound(
        ADDRESS_FORM,
        kind=FormInboundKind.FORM_SUBMIT,
        submitted_values={"order_id": "ORD-1", "address": ""},
    )
    assert decision.kind is FormDecisionKind.CONFIRM
    assert decision.resume_payload == {"action": "confirm", "order_id": "ORD-1", "address": ""}


def test_primary_submit_only_for_update_address():
    """A non-primary tool with no action is not a confirm gesture."""
    decision = decide_form_inbound(
        CANCEL_FORM,
        kind=FormInboundKind.FORM_SUBMIT,
        submitted_values={"order_id": "ORD-1", "reason": "x"},
    )
    assert decision.kind is FormDecisionKind.IMPLICIT_CANCEL


def test_primary_submit_requires_a_business_value():
    decision = decide_form_inbound(
        ADDRESS_FORM,
        kind=FormInboundKind.FORM_SUBMIT,
        submitted_values={"current_address": "OLD", "abandon_footnote": "f"},
    )
    assert decision.kind is FormDecisionKind.IMPLICIT_CANCEL


# --- TEXT: policy driven, never parsed as fields -------------------------


def test_text_default_policy_is_implicit_cancel():
    decision = decide_form_inbound(ADDRESS_FORM, kind=FormInboundKind.TEXT)
    assert decision.kind is FormDecisionKind.IMPLICIT_CANCEL
    assert decision.resume_payload == {"action": "cancel"}


def test_text_always_implicit_cancels():
    """There is exactly one pending-form policy; no per-entry refuse switch."""
    decision = decide_form_inbound(ADDRESS_FORM, kind=FormInboundKind.TEXT)
    assert decision.kind is FormDecisionKind.IMPLICIT_CANCEL
    assert decision.resume_payload == {"action": "cancel"}


def test_malformed_submit_implicit_cancels():
    decision = decide_form_inbound(
        CANCEL_FORM,
        kind=FormInboundKind.FORM_SUBMIT,
        submitted_values={"note": "no action"},
    )
    assert decision.kind is FormDecisionKind.IMPLICIT_CANCEL
    assert decision.resume_payload == {"action": "cancel"}


# --- EXPLICIT: HTTP button ----------------------------------------------


def test_explicit_confirm_with_fields():
    decision = decide_form_inbound(
        ADDRESS_FORM,
        kind=FormInboundKind.EXPLICIT,
        fields={"action": "confirm", "order_id": "ORD-1", "address": "A"},
    )
    assert decision.kind is FormDecisionKind.CONFIRM
    assert decision.resume_payload == {
        "action": "confirm",
        "order_id": "ORD-1",
        "address": "A",
    }


def test_explicit_cancel_ignores_fields():
    decision = decide_form_inbound(
        ADDRESS_FORM,
        kind=FormInboundKind.EXPLICIT,
        fields={"action": "cancel", "address": "A"},
    )
    assert decision.kind is FormDecisionKind.CANCEL
    assert decision.resume_payload == {"action": "cancel"}


def test_explicit_invalid_action_raises():
    """HTTP callers validate the action first; an invalid one is a programming error."""
    with pytest.raises(ValueError, match="invalid explicit form action"):
        decide_form_inbound(
            ADDRESS_FORM, kind=FormInboundKind.EXPLICIT, fields={"action": "maybe"}
        )


# --- single source of truth ---------------------------------------------


def test_inbound_skip_fields_extend_resume_skip_fields():
    assert FORM_INBOUND_SKIP_FIELDS == {
        "action",
        "confirmation",
        "request_id",
        "current_address",
        "abandon_footnote",
    }
