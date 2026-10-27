"""Shared Form scrub set: helper contract and per-lane read-point equivalence.

Seeding ``confirm_result="cancel"`` on a form-opening turn must not emit
``operation_cancelled_text``. Form Interrupt rewrites that key before either
lane's read point, so clearing it in prepare is not a behavior change.
"""

import importlib.util
import tempfile
import uuid
from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver

from ai_service_agent.domain.orders import SQLiteOrderRepository
from ai_service_agent.graph.builder import build_graph
from ai_service_agent.graph.workflows.copy import operation_cancelled_text
from ai_service_agent.graph.workflows.form_resume import FORM_SCRUB_KEYS, prepare_form_update
from ai_service_agent.testing import ToolCallingFakeModel, router_json_message, select_json_message
from conftest import ainvoke_graph

ROOT = Path(__file__).resolve().parents[1]
STALE_SESSION = "stale-form-session"
SENTINELS = {
    "form_errors": {"address": "stale-error"},
    "recheck_reason": "stale-recheck",
    "confirm_result": "cancel",
}


def _seeded_context() -> dict:
    return {
        **SENTINELS,
        "form_session_id": STALE_SESSION,
        "pending_tool": {"tool": "keep-me", "args": {}},
        "unrelated": "kept",
    }


@pytest.mark.parametrize("sentinel_key", sorted(FORM_SCRUB_KEYS))
def test_prepare_form_update_clears_every_sentinel_and_mints_session(sentinel_key):
    state = {"context": _seeded_context()}
    update = prepare_form_update(state)

    assert update["execution_status"] == "waiting_confirmation"
    ctx = update["context"]
    assert sentinel_key not in ctx
    for key in FORM_SCRUB_KEYS:
        assert key not in ctx
    assert SENTINELS["confirm_result"] not in ctx.values()
    fresh = ctx["form_session_id"]
    assert fresh != STALE_SESSION
    uuid.UUID(fresh)
    assert ctx["pending_tool"]["tool"] == "keep-me"
    assert ctx["unrelated"] == "kept"
    assert state["context"]["form_session_id"] == STALE_SESSION
    assert state["context"][sentinel_key] == SENTINELS[sentinel_key]


def _load_seed(db_path: Path):
    spec = importlib.util.spec_from_file_location(
        "seed_mock_db_form_scrub", ROOT / "scripts" / "seed_mock_db.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.build_database(str(db_path))


def _use_db(monkeypatch, db_path: Path):
    from ai_service_agent.graph import builder as builder_mod
    from ai_service_agent.tools import orders as orders_mod
    from ai_service_agent.tools import write as write_mod

    class _Settings:
        mock_ecommerce_db_path = str(db_path)

    monkeypatch.setattr(write_mod, "get_settings", lambda: _Settings())
    monkeypatch.setattr(orders_mod, "get_settings", lambda: _Settings())
    monkeypatch.setattr(
        builder_mod, "SQLiteOrderRepository", lambda path: SQLiteOrderRepository(str(db_path))
    )


def _assert_form_turn_ignores_stale_cancel(out, *, tool: str):
    pending = out.get("__interrupt__", [])
    assert pending, out
    assert pending[0].value.get("tool") == tool
    assert pending[0].value.get("type") != "approval_required"
    assert pending[0].value.get("form_session_id") not in (None, "", STALE_SESSION)
    uuid.UUID(pending[0].value["form_session_id"])
    cancelled = operation_cancelled_text()
    for message in out.get("messages") or []:
        content = getattr(message, "content", "")
        text = content if isinstance(content, str) else str(content)
        assert cancelled not in text


def test_mutation_form_turn_ignores_seeded_confirm_cancel(monkeypatch):
    """One Mutation turn with a stale cancel must open the form, not cancel copy."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "form-scrub-mutation.db"
        _load_seed(db_path)
        _use_db(monkeypatch, db_path)
        model = ToolCallingFakeModel(
            responses=[
                router_json_message("account_profile_update", 0.9),
                select_json_message("update_address", {}),
            ]
        )
        graph = build_graph(model, tools=[], checkpointer=MemorySaver())
        thread_id = "form-scrub-mutation"
        out = ainvoke_graph(
            graph,
            {
                "messages": [HumanMessage(content="我要改个地址")],
                "user_id": "u1",
                "session_id": thread_id,
                "context": {
                    "confirm_result": "cancel",
                    "form_errors": {"address": "stale-error"},
                    "recheck_reason": "stale-recheck",
                    "form_session_id": STALE_SESSION,
                },
            },
            {"configurable": {"thread_id": thread_id}},
        )
        _assert_form_turn_ignores_stale_cancel(out, tool="update_address")
        _assert_lane_scrubbed(graph, {"configurable": {"thread_id": thread_id}})


def test_high_risk_form_turn_ignores_seeded_confirm_cancel(monkeypatch):
    """One HighRisk turn with a stale cancel must open the form, not cancel copy."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "form-scrub-high-risk.db"
        _load_seed(db_path)
        _use_db(monkeypatch, db_path)
        model = ToolCallingFakeModel(
            responses=[
                router_json_message("refund_apply_execute", 0.9),
                select_json_message("refund_apply", {"amount": 10.0}),
            ]
        )
        graph = build_graph(model, tools=[], checkpointer=MemorySaver())
        thread_id = "form-scrub-high-risk"
        out = ainvoke_graph(
            graph,
            {
                "messages": [HumanMessage(content="申请退款")],
                "user_id": "u1",
                "session_id": thread_id,
                "context": {
                    "confirm_result": "cancel",
                    "form_errors": {"order_id": "stale-error"},
                    "recheck_reason": "stale-recheck",
                    "form_session_id": STALE_SESSION,
                },
            },
            {"configurable": {"thread_id": thread_id}},
        )
        _assert_form_turn_ignores_stale_cancel(out, tool="refund_apply")
        _assert_lane_scrubbed(graph, {"configurable": {"thread_id": thread_id}})


def _assert_lane_scrubbed(graph, config):
    """prepare_form has already written the shared scrub into the lane checkpoint."""
    snapshot = graph.get_state(config, subgraphs=True)
    contexts = _contexts(snapshot)
    scrubbed = [
        ctx
        for ctx in contexts
        if ctx.get("form_session_id") not in (None, "", STALE_SESSION)
        and "pending_tool" in ctx
    ]
    assert scrubbed, [sorted(ctx) for ctx in contexts]
    for ctx in scrubbed:
        for key in FORM_SCRUB_KEYS:
            assert key not in ctx


def _contexts(snapshot) -> list[dict]:
    found: list[dict] = []

    def walk(snap):
        values = getattr(snap, "values", None) or {}
        ctx = values.get("context") if isinstance(values, dict) else None
        if isinstance(ctx, dict):
            found.append(ctx)
        for task in getattr(snap, "tasks", ()) or ():
            state = getattr(task, "state", None)
            if state is not None:
                walk(state)

    walk(snapshot)
    return found
