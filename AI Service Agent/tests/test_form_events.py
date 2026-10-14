"""Unit tests for Form Interrupt SSE projection (event-spec §2.2)."""

from types import SimpleNamespace

from ai_service_agent.harness.form_events import is_form_interrupt, project_form_interrupt


def _form_value(**extra):
    base = {
        "tool": "update_address",
        "order_candidates": [{"order_id": "ORD-PAID-1", "status": "paid"}],
        "form_session_id": "fs-1",
        "summary": "改地址",
    }
    base.update(extra)
    return base


def test_is_form_interrupt_rejects_other_types():
    assert is_form_interrupt(_form_value()) is True
    assert is_form_interrupt({"type": "approval_required", "tool": "x", "order_candidates": []}) is False
    assert is_form_interrupt({"type": "handoff_request"}) is False
    assert is_form_interrupt("not-a-dict") is False


def test_project_form_interrupt_without_errors_is_form_required():
    interrupt = SimpleNamespace(
        value=_form_value(),
        id="interrupt-fallback-id",
        response_schema={"type": "object"},
    )
    projected = project_form_interrupt(interrupt)
    assert projected == {
        "type": "form_required",
        "data": {
            "tool": "update_address",
            "order_candidates": [{"order_id": "ORD-PAID-1", "status": "paid"}],
            "form_session_id": "fs-1",
            "summary": "改地址",
            "response_schema": {"type": "object"},
        },
    }


def test_project_form_interrupt_with_errors_is_form_rejected():
    interrupt = SimpleNamespace(
        value=_form_value(errors={"order_id": "该订单当前不可修改收货地址"}),
        id="interrupt-fallback-id",
        response_schema=None,
    )
    projected = project_form_interrupt(interrupt)
    assert projected["type"] == "form_rejected"
    assert projected["data"]["form_session_id"] == "fs-1"
    assert projected["data"]["errors"] == {"order_id": "该订单当前不可修改收货地址"}
    assert "response_schema" not in projected["data"]


def test_project_form_interrupt_empty_errors_treated_as_required():
    interrupt = SimpleNamespace(value=_form_value(errors={}), id="x", response_schema=None)
    assert project_form_interrupt(interrupt)["type"] == "form_required"
