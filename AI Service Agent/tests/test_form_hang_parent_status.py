"""Shared L3 lane criterion: form hang via interrupt / form_required (option A).

Root-graph parent status (routing, not waiting_confirmation) is locked in
test_mutation_graph.py::test_mutation_incomplete_args_open_form_interrupt.
Transport must use checkpointer interrupts (pending_form_value), not that
parent status.
"""

from pathlib import Path
from types import SimpleNamespace

from ai_service_agent.evals.lane_criteria import mutation_form_incomplete_pass
from ai_service_agent.harness.form_events import pending_form_value

ROOT = Path(__file__).resolve().parents[1]


def test_lane_pass_when_parent_status_is_still_routing():
    """Parent checkpoint stays routing while the subgraph is interrupted."""
    assert mutation_form_incomplete_pass(
        http_ok=True,
        form_required=True,
        clarification_required=False,
        confirmation_required=False,
        write_tool_count=0,
        done=True,
        interrupts=[{"tool": "update_address"}],
        thread_id_ok=True,
    )


def test_lane_fail_without_interrupt_even_if_status_looks_waiting():
    assert not mutation_form_incomplete_pass(
        http_ok=True,
        form_required=False,
        clarification_required=False,
        confirmation_required=False,
        write_tool_count=0,
        done=True,
        interrupts=[],
        thread_id_ok=True,
    )


def test_pending_form_value_reads_interrupt_not_parent_status():
    """No interrupt is not a hang. A form interrupt is, whatever the parent status is."""
    assert pending_form_value([]) is None
    assert pending_form_value(()) is None
    approval = SimpleNamespace(value={"type": "approval_required", "approval_id": "a1"})
    assert pending_form_value([approval]) is None
    form = SimpleNamespace(
        value={"tool": "update_address", "order_candidates": [{"order_id": "ORD-1"}]}
    )
    assert pending_form_value([form])["tool"] == "update_address"


def test_harness_does_not_name_waiting_confirmation():
    """Transport sources must not branch on the parent status literal."""
    hits = []
    harness = ROOT / "src" / "ai_service_agent" / "harness"
    for path in harness.rglob("*.py"):
        if "waiting_confirmation" in path.read_text():
            hits.append(str(path.relative_to(harness)))
    assert hits == []
