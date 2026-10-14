"""Shared L3 lane criterion: form hang via interrupt / form_required (option A).

Root-graph parent status (routing, not waiting_confirmation) is locked in
test_mutation_graph.py::test_mutation_incomplete_args_open_form_interrupt.
"""

from ai_service_agent.evals.lane_criteria import mutation_form_incomplete_pass


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
