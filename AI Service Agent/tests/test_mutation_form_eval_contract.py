"""T8 / v2 contract: functional fixtures expect Form Interrupt, not old confirm.

Seam: fixture + harness helpers (scripts/functional_scenario_eval.py, dataset builder).
Does not re-test MutationGraph product path (covered by T1–T7).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FUNCTIONAL = ROOT / "tests" / "evals" / "functional_scenarios" / "master.jsonl"
REAL_MODEL = ROOT / "tests" / "evals" / "real_model" / "master.jsonl"


def _load_functional_eval():
    path = ROOT / "scripts" / "functional_scenario_eval.py"
    spec = importlib.util.spec_from_file_location("functional_scenario_eval_t8", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_functional_v2_has_sixty_cases_and_target_distribution():
    cases = _load_jsonl(FUNCTIONAL)
    assert len(cases) == 60
    assert {c["id"] for c in cases} == {f"F{i:02d}" for i in range(1, 61)}
    by_suite: dict[str, int] = {}
    for c in cases:
        by_suite[c["suite"]] = by_suite.get(c["suite"], 0) + 1
        assert c.get("dataset_version") == "v2"
    assert by_suite == {
        "guard": 5,
        "read": 10,
        "mutation": 14,
        "high_risk": 10,
        "fallback": 4,
        "handoff": 8,
        "multiturn": 5,
        "context": 4,
    }
    assert sum(1 for c in cases if c["split"] == "smoke") == 16


def test_functional_mutation_happy_path_expects_form_required_not_confirmation():
    cases = {c["id"]: c for c in _load_jsonl(FUNCTIONAL) if c["suite"] == "mutation"}
    for case_id in ("F16", "F17", "F18", "F19", "F20", "F21", "F25"):
        events = cases[case_id]["expected_events"]
        assert "form_required" in events, case_id
        assert "confirmation_required" not in events, case_id


def test_functional_tool_known_missing_fields_open_form_not_chat_clarify():
    cases = {c["id"]: c for c in _load_jsonl(FUNCTIONAL)}
    for case_id in ("F22", "F23", "F24", "F58"):
        case = cases[case_id]
        assert "form_required" in case["expected_events"], case_id
        assert "clarification_required" not in case["expected_events"], case_id
        assert "clarification_required" in case.get("forbidden_events", []), case_id


def test_functional_operation_clarify_and_highrisk_clarify_unchanged():
    cases = {c["id"]: c for c in _load_jsonl(FUNCTIONAL)}
    assert cases["F26"]["turns"][0]["message"] == "我要改一下订单"
    assert cases["F26"]["expected_events"] == ["clarification_required", "done"]
    assert "form_required" in cases["F26"]["forbidden_events"]
    for case_id in ("F35", "F36"):
        assert "clarification_required" in cases[case_id]["expected_events"]
        assert cases[case_id]["suite"] == "high_risk"


def test_functional_form_rejected_case_present():
    cases = {c["id"]: c for c in _load_jsonl(FUNCTIONAL)}
    events = cases["F28"]["expected_events"]
    assert "form_required" in events
    assert "form_rejected" in events
    assert "tool_call" in cases["F28"].get("forbidden_events", [])


def test_watched_events_include_form_interrupt_projections():
    mod = _load_functional_eval()
    types = mod.watched_events([
        {"type": "form_required", "data": {"tool": "update_address"}},
        {"type": "form_rejected", "data": {"errors": {"address": "bad"}}},
        {"type": "confirmation_required", "data": {"summary": "legacy"}},
        {"type": "done", "data": {}},
        {"type": "token", "data": {"text": "x"}},
    ])
    assert types == ["form_required", "form_rejected", "confirmation_required", "done"]


def test_build_form_resume_body_for_confirm_and_cancel():
    mod = _load_functional_eval()
    confirm = mod.build_form_resume_body(
        "sess-1",
        {"action": "confirm", "order_id": "ORD-PAID-1", "fields": {"address": "杭州市"}},
    )
    assert confirm == {
        "conversation_id": "sess-1",
        "action": "confirm",
        "order_id": "ORD-PAID-1",
        "fields": {"address": "杭州市"},
    }
    cancel = mod.build_form_resume_body("sess-1", {"action": "cancel"})
    assert cancel == {"conversation_id": "sess-1", "action": "cancel"}


def test_real_model_mutation_graph_cases_use_form_required():
    cases = {c["id"]: c for c in _load_jsonl(REAL_MODEL)}
    for case_id in (
        "REAL-MUTATION-001",
        "REAL-MUTATION-002",
        "REAL-MUTATION-003",
        "REAL-MUTATION-004",
        "REAL-MUTATION-005",
        "REAL-MUTATION-006",
    ):
        events = cases[case_id]["expected_events"]
        assert "form_required" in events, case_id
        assert "confirmation_required" not in events, case_id
        assert "clarification_required" not in events, case_id
    op = cases["REAL-MUTATION-007"]
    assert op["input"] == "我要改一下订单"
    assert op["expected_events"] == ["clarification_required", "done"]
    assert "form_required" in op.get("forbidden_events", [])
