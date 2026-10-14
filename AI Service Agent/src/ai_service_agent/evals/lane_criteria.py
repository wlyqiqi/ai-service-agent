"""Shared smoke-lane pass rules for Mutation Form hangs.

Parent ``execution_status`` stays ``routing`` while MutationGraph is interrupted;
the hang authority is SSE ``form_required`` plus checkpointer interrupts.
"""


def mutation_form_incomplete_pass(
    *,
    http_ok: bool,
    form_required: bool,
    clarification_required: bool,
    confirmation_required: bool,
    write_tool_count: int,
    done: bool,
    interrupts: list,
    thread_id_ok: bool,
) -> bool:
    return (
        http_ok
        and form_required
        and not clarification_required
        and not confirmation_required
        and write_tool_count == 0
        and done
        and bool(interrupts)
        and thread_id_ok
    )
