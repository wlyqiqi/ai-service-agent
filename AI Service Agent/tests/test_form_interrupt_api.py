"""Offline smoke for Form Interrupt / Form Resume (Mutation Form T1).

Seam: installed langgraph public HITL API used by later Mutation Form tickets.
Does not exercise MutationGraph write behavior.
"""

from __future__ import annotations

import importlib.metadata
import inspect
from pathlib import Path
from typing import Any, Optional

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, ValidationError
from typing_extensions import TypedDict

ROOT = Path(__file__).resolve().parents[1]


class FormResumeModel(BaseModel):
    """Typed Form Resume payload (Confirm path)."""

    action: str
    order_id: str
    address: str = ""


class FormState(TypedDict):
    answer: Optional[Any]


def test_langgraph_version_supports_form_interrupt_response_schema():
    version = importlib.metadata.version("langgraph")
    parts = tuple(int(p) for p in version.split(".")[:3])
    assert parts >= (1, 2, 12), (
        f"Form Interrupt response_schema requires langgraph>=1.2.12; installed {version}"
    )


def test_interrupt_signature_accepts_response_schema():
    params = inspect.signature(interrupt).parameters
    assert "response_schema" in params
    assert "value" in params


def test_form_interrupt_pause_and_form_resume_roundtrip():
    """Form Interrupt surfaces value + response_schema; Form Resume returns validated model."""

    def collect_form(state: FormState) -> FormState:
        # Interrupt value carries form UI payload only; SSE type is a harness projection
        # (form_required vs form_rejected), not a field inside value (event-spec §2.2).
        value = {
            "tool": "update_address",
            "order_candidates": [{"order_id": "ORD-1"}],
            "prefills": {"address": "北京市朝阳区"},
        }
        answer = interrupt(value, response_schema=FormResumeModel)
        return {"answer": answer}

    graph = (
        StateGraph(FormState)
        .add_node("collect_form", collect_form)
        .add_edge(START, "collect_form")
        .compile(checkpointer=MemorySaver())
    )
    config = {"configurable": {"thread_id": "form-interrupt-smoke"}}

    paused = graph.invoke({"answer": None}, config)
    pending = paused["__interrupt__"][0]
    assert pending.value["tool"] == "update_address"
    assert pending.value["order_candidates"] == [{"order_id": "ORD-1"}]
    assert "errors" not in pending.value or not pending.value.get("errors")
    assert pending.response_schema == FormResumeModel.model_json_schema()

    resumed = graph.invoke(
        Command(resume={"action": "confirm", "order_id": "ORD-1", "address": "北京市朝阳区"}),
        config,
    )
    assert isinstance(resumed["answer"], FormResumeModel)
    assert resumed["answer"].action == "confirm"
    assert resumed["answer"].order_id == "ORD-1"
    assert resumed["answer"].address == "北京市朝阳区"


def test_form_interrupt_reissue_with_errors_is_form_rejected_shape():
    """Same Form Interrupt API can carry errors for form_rejected projection later."""

    def collect_form(state: FormState) -> FormState:
        answer = interrupt(
            {
                "tool": "update_address",
                "order_candidates": [{"order_id": "ORD-1"}],
                "errors": {"address": "地址不可修改"},
            },
            response_schema=FormResumeModel,
        )
        return {"answer": answer}

    graph = (
        StateGraph(FormState)
        .add_node("collect_form", collect_form)
        .add_edge(START, "collect_form")
        .compile(checkpointer=MemorySaver())
    )
    config = {"configurable": {"thread_id": "form-interrupt-rejected"}}

    paused = graph.invoke({"answer": None}, config)
    pending = paused["__interrupt__"][0]
    assert pending.value["errors"]["address"] == "地址不可修改"
    assert pending.response_schema == FormResumeModel.model_json_schema()


def test_form_resume_invalid_payload_raises_validation_error():
    def collect_form(state: FormState) -> FormState:
        answer = interrupt(
            {"tool": "update_address", "order_candidates": [{"order_id": "ORD-1"}]},
            response_schema=FormResumeModel,
        )
        return {"answer": answer}

    graph = (
        StateGraph(FormState)
        .add_node("collect_form", collect_form)
        .add_edge(START, "collect_form")
        .compile(checkpointer=MemorySaver())
    )
    config = {"configurable": {"thread_id": "form-interrupt-invalid"}}

    graph.invoke({"answer": None}, config)
    with pytest.raises(ValidationError) as excinfo:
        graph.invoke(Command(resume={"action": "confirm"}), config)
    assert "order_id" in str(excinfo.value).lower()


def test_event_spec_defines_form_required_and_form_rejected_mapping():
    """Event protocol must define form_* projections of Form Interrupt (issue #15)."""
    text = (ROOT / "docs" / "specs" / "event-spec.md").read_text(encoding="utf-8")
    assert "`form_required`" in text
    assert "`form_rejected`" in text
    assert "Form Interrupt" in text
    assert "Form Resume" in text
    assert "Interrupt.response_schema" in text
    assert "value.errors" in text
    assert "不是第二套挂起源" in text or "不是第二套" in text
