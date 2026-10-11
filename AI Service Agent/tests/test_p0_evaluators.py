"""Offline unit tests for P0 LangSmith evaluators (no network)."""

from __future__ import annotations

import re

from ai_service_agent.evals.p0_evaluators import (
    P0_EVALUATOR_SPECS,
    P0_EVALUATORS,
    approval_path_correct,
    build_hosted_perform_eval_source,
    event_protocol_correct,
    handoff_correct,
    mode_correct,
    overall_pass,
    safety_correct,
    workspace_evaluator_name,
)


_CN = re.compile(r"[\u4e00-\u9fff]")


def test_each_evaluator_has_chinese_docstring():
    assert len(P0_EVALUATORS) == 6
    for spec in P0_EVALUATOR_SPECS:
        doc = (spec.fn.__doc__ or "").strip()
        assert doc, f"{spec.key} missing docstring"
        assert _CN.search(doc), f"{spec.key} docstring not Chinese: {doc}"
        assert doc == spec.zh_note
        assert workspace_evaluator_name(spec).startswith("P0·")


def test_event_protocol_pass_and_fail():
    ref = {
        "expected_events": ["safety_rejected", "done"],
        "forbidden_events": ["tool_call"],
    }
    ok = event_protocol_correct(
        {},
        {"actual": {"events": ["safety_rejected", "done", "token"]}},
        ref,
    )
    assert ok["key"] == "event_protocol_correct" and ok["score"] == 1

    soft = event_protocol_correct(
        {},
        {"actual": {"events": ["safety_rejected"]}},  # done soft-missing ok
        ref,
    )
    assert soft["score"] == 1

    missing = event_protocol_correct(
        {},
        {"actual": {"events": ["done"]}},
        ref,
    )
    assert missing["score"] == 0

    forbidden = event_protocol_correct(
        {},
        {"actual": {"events": ["safety_rejected", "tool_call", "done"]}},
        ref,
    )
    assert forbidden["score"] == 0


def test_mode_correct():
    assert mode_correct({}, {"actual": {"mode": "ai"}}, {"expected_mode": "ai"})["score"] == 1
    assert mode_correct({}, {"actual": {"mode": "human"}}, {"expected_mode": "ai"})["score"] == 0
    assert mode_correct({}, {"actual": {"mode": "ai"}}, {})["comment"] == "n/a"


def test_safety_correct_reject_and_false_reject():
    # S01-like: must reject
    must = safety_correct(
        {"suite": "guard"},
        {"actual": {"events": ["safety_rejected", "done"]}},
        {"expected_events": ["safety_rejected", "done"], "forbidden_events": ["tool_call"]},
    )
    assert must["score"] == 1

    miss = safety_correct(
        {"suite": "guard"},
        {"actual": {"events": ["done"]}},
        {"expected_events": ["safety_rejected", "done"], "forbidden_events": ["tool_call"]},
    )
    assert miss["score"] == 0

    # S03-like: must not false-reject
    ok = safety_correct(
        {"suite": "guard"},
        {"actual": {"events": ["done"]}},
        {"expected_events": ["done"], "forbidden_events": ["tool_call"]},
    )
    assert ok["score"] == 1

    false = safety_correct(
        {"suite": "guard"},
        {"actual": {"events": ["safety_rejected", "done"]}},
        {"expected_events": ["done"], "forbidden_events": ["tool_call"]},
    )
    assert false["score"] == 0

    # non-guard n/a
    na = safety_correct(
        {"suite": "read"},
        {"actual": {"events": ["done"]}},
        {"expected_events": ["done"], "forbidden_events": []},
    )
    assert na["score"] == 1 and na["comment"] == "n/a"


def test_handoff_correct():
    must = handoff_correct(
        {"suite": "handoff"},
        {"actual": {"events": ["handoff_created", "done"], "mode": "human"}},
        {
            "expected_events": ["handoff_created", "done"],
            "forbidden_events": [],
            "expected_mode": "human",
        },
    )
    assert must["score"] == 1

    forbid = handoff_correct(
        {"suite": "handoff"},
        {"actual": {"events": ["handoff_created", "done"], "mode": "ai"}},
        {
            "expected_events": ["done"],
            "forbidden_events": ["handoff_created"],
            "expected_mode": "ai",
        },
    )
    assert forbid["score"] == 0

    # resolve: handoff happened, final mode ai
    resolved = handoff_correct(
        {"suite": "handoff", "turns": [{"message": "转人工"}, {"action": "resolve"}]},
        {"actual": {"events": ["handoff_created", "done"], "mode": "ai"}},
        {
            "expected_events": ["handoff_created", "done"],
            "forbidden_events": [],
            "expected_mode": "ai",
        },
    )
    assert resolved["score"] == 1

    na = handoff_correct(
        {"suite": "read"},
        {"actual": {"events": ["done"], "mode": "ai"}},
        {"expected_events": ["done"], "expected_mode": "ai"},
    )
    assert na["comment"] == "n/a"


def test_approval_path_correct():
    approve = approval_path_correct(
        {"suite": "high_risk", "turns": [{"message": "退款"}, {"action": "approve"}]},
        {"actual": {"events": ["approval_required", "done"]}},
        {"expected_events": ["approval_required", "done"], "forbidden_events": []},
    )
    assert approve["score"] == 1

    reject = approval_path_correct(
        {"suite": "high_risk", "turns": [{"message": "退款"}, {"action": "reject"}]},
        {"actual": {"events": ["approval_required", "tool_call", "done"]}},
        {
            "expected_events": ["approval_required", "done"],
            "forbidden_events": ["tool_call"],
        },
    )
    assert reject["score"] == 0

    clarify = approval_path_correct(
        {"suite": "high_risk", "turns": [{"message": "退了吧"}]},
        {"actual": {"events": ["clarification_required", "approval_required", "done"]}},
        {
            "expected_events": ["clarification_required", "done"],
            "forbidden_events": ["approval_required", "tool_call"],
        },
    )
    assert clarify["score"] == 0

    na = approval_path_correct(
        {"suite": "read"},
        {"actual": {"events": ["done"]}},
        {"expected_events": ["done"]},
    )
    assert na["comment"] == "n/a"


def test_overall_pass():
    assert overall_pass({}, {"status": "pass", "problems": []}, {})["score"] == 1
    fail = overall_pass({}, {"status": "fail", "problems": ["event missing: x"]}, {})
    assert fail["score"] == 0
    assert "event missing" in fail["comment"]


def test_hosted_perform_eval_source():
    for spec in P0_EVALUATOR_SPECS:
        src = build_hosted_perform_eval_source(spec.key)
        assert "def perform_eval(run, example):" in src
        assert spec.key in src
        assert spec.zh_note in src
        assert "import ai_service_agent" not in src
        # executable and returns expected key
        ns: dict = {}
        exec(src, ns, ns)  # noqa: S102 — intentional offline sanity for hosted source
        result = ns["perform_eval"](
            {"outputs": {"status": "pass", "actual": {"events": ["done"], "mode": "ai"}}},
            {
                "inputs": {"suite": "read", "turns": [{"message": "hi"}]},
                "outputs": {"expected_events": ["done"], "forbidden_events": [], "expected_mode": "ai"},
            },
        )
        assert result["key"] == spec.key
        assert result["score"] in (0, 1)
