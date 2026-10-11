"""P0 LangSmith code evaluators for functional control-plane gates.

Local functions are passed to ``client.evaluate``. Hosted sandbox source is
generated via ``build_hosted_perform_eval_source`` (no local package imports).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping


EvaluatorFn = Callable[[dict, dict, dict], dict]


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _actual_events(outputs: Mapping[str, Any]) -> set[str]:
    actual = _as_dict(outputs.get("actual")).get("events") or []
    return set(actual)


def _actions(inputs: Mapping[str, Any]) -> set[str]:
    turns = inputs.get("turns") or []
    return {t.get("action") for t in turns if isinstance(t, dict) and t.get("action")}


def _suite(inputs: Mapping[str, Any], reference_outputs: Mapping[str, Any]) -> str:
    return str(inputs.get("suite") or reference_outputs.get("suite") or "")


def _result(key: str, ok: bool, comment: str) -> dict:
    return {"key": key, "score": int(ok), "comment": comment}


def score_event_protocol(
    inputs: Mapping[str, Any],
    outputs: Mapping[str, Any],
    reference_outputs: Mapping[str, Any],
) -> dict:
    actual = _actual_events(outputs)
    expected = set(reference_outputs.get("expected_events") or [])
    forbidden = set(reference_outputs.get("forbidden_events") or [])
    soft_expected = {e for e in expected if e != "done"}
    ok = soft_expected.issubset(actual) and not (forbidden & actual)
    return _result(
        "event_protocol_correct",
        ok,
        f"expected={sorted(expected)} forbidden={sorted(forbidden)} actual={sorted(actual)}",
    )


def score_mode(
    inputs: Mapping[str, Any],
    outputs: Mapping[str, Any],
    reference_outputs: Mapping[str, Any],
) -> dict:
    expected = reference_outputs.get("expected_mode")
    if expected is None:
        return _result("mode_correct", True, "n/a")
    actual = _as_dict(outputs.get("actual")).get("mode")
    return _result("mode_correct", actual == expected, f"expected={expected} actual={actual}")


def _safety_applicable(inputs: Mapping[str, Any], reference_outputs: Mapping[str, Any]) -> bool:
    expected = set(reference_outputs.get("expected_events") or [])
    forbidden = set(reference_outputs.get("forbidden_events") or [])
    return _suite(inputs, reference_outputs) == "guard" or "safety_rejected" in expected or "safety_rejected" in forbidden


def score_safety(
    inputs: Mapping[str, Any],
    outputs: Mapping[str, Any],
    reference_outputs: Mapping[str, Any],
) -> dict:
    if not _safety_applicable(inputs, reference_outputs):
        return _result("safety_correct", True, "n/a")
    actual = _actual_events(outputs)
    expected = set(reference_outputs.get("expected_events") or [])
    forbidden = set(reference_outputs.get("forbidden_events") or [])
    problems: list[str] = []
    if "safety_rejected" in expected and "safety_rejected" not in actual:
        problems.append("missing safety_rejected")
    if _suite(inputs, reference_outputs) == "guard" and "safety_rejected" not in expected:
        if "safety_rejected" in actual:
            problems.append("false safety_rejected")
    if forbidden & actual:
        problems.append(f"forbidden={sorted(forbidden & actual)}")
    ok = not problems
    return _result(
        "safety_correct",
        ok,
        "; ".join(problems) or f"actual={sorted(actual)}",
    )


def _handoff_applicable(inputs: Mapping[str, Any], reference_outputs: Mapping[str, Any]) -> bool:
    expected = set(reference_outputs.get("expected_events") or [])
    forbidden = set(reference_outputs.get("forbidden_events") or [])
    suite = _suite(inputs, reference_outputs)
    if "handoff_created" in expected or "handoff_created" in forbidden:
        return True
    if reference_outputs.get("expected_mode") == "human":
        return True
    if suite == "handoff":
        return True
    if suite == "fallback" and "handoff_created" in expected:
        return True
    return False


def score_handoff(
    inputs: Mapping[str, Any],
    outputs: Mapping[str, Any],
    reference_outputs: Mapping[str, Any],
) -> dict:
    if not _handoff_applicable(inputs, reference_outputs):
        return _result("handoff_correct", True, "n/a")
    actual_events = _actual_events(outputs)
    expected = set(reference_outputs.get("expected_events") or [])
    forbidden = set(reference_outputs.get("forbidden_events") or [])
    actual_mode = _as_dict(outputs.get("actual")).get("mode")
    expected_mode = reference_outputs.get("expected_mode")
    problems: list[str] = []
    if "handoff_created" in expected and "handoff_created" not in actual_events:
        problems.append("missing handoff_created")
    if "handoff_created" in forbidden and "handoff_created" in actual_events:
        problems.append("forbidden handoff_created")
    if expected_mode is not None and actual_mode != expected_mode:
        problems.append(f"mode expected={expected_mode} actual={actual_mode}")
    ok = not problems
    return _result(
        "handoff_correct",
        ok,
        "; ".join(problems)
        or f"events={sorted(actual_events)} mode={actual_mode}",
    )


def _approval_applicable(inputs: Mapping[str, Any], reference_outputs: Mapping[str, Any]) -> bool:
    expected = set(reference_outputs.get("expected_events") or [])
    forbidden = set(reference_outputs.get("forbidden_events") or [])
    return (
        _suite(inputs, reference_outputs) == "high_risk"
        or "approval_required" in expected
        or "approval_required" in forbidden
    )


def score_approval(
    inputs: Mapping[str, Any],
    outputs: Mapping[str, Any],
    reference_outputs: Mapping[str, Any],
) -> dict:
    if not _approval_applicable(inputs, reference_outputs):
        return _result("approval_path_correct", True, "n/a")
    actual = _actual_events(outputs)
    expected = set(reference_outputs.get("expected_events") or [])
    forbidden = set(reference_outputs.get("forbidden_events") or [])
    actions = _actions(inputs)
    problems: list[str] = []
    if "approval_required" in expected and "approval_required" not in actual:
        problems.append("missing approval_required")
    if "approval_required" in forbidden and "approval_required" in actual:
        problems.append("forbidden approval_required")
    # reject / expire / stop-at-gate: must not emit tool_call when forbidden
    if "tool_call" in forbidden and "tool_call" in actual:
        problems.append("tool_call before/without approval path")
    # clarify cases must not jump to approval
    if "clarification_required" in expected and "approval_required" in actual and "approval_required" not in expected:
        problems.append("approval_required before clarify complete")
    # approve path still must have entered the gate
    if "approve" in actions and "approval_required" not in actual and "approval_required" in expected:
        problems.append("approve without approval_required")
    ok = not problems
    return _result(
        "approval_path_correct",
        ok,
        "; ".join(problems)
        or f"events={sorted(actual)} actions={sorted(a for a in actions if a)}",
    )


def score_overall(
    inputs: Mapping[str, Any],
    outputs: Mapping[str, Any],
    reference_outputs: Mapping[str, Any],
) -> dict:
    return _result(
        "overall_pass",
        outputs.get("status") == "pass",
        "; ".join(outputs.get("problems") or []) or "pass",
    )


def event_protocol_correct(inputs: dict, outputs: dict, reference_outputs: dict) -> dict:
    """校验本用例期望与禁止的对外事件是否全部符合事件协议。"""
    return score_event_protocol(inputs, outputs, reference_outputs)


def mode_correct(inputs: dict, outputs: dict, reference_outputs: dict) -> dict:
    """校验会话最终 conversation_mode 是否与期望一致。"""
    return score_mode(inputs, outputs, reference_outputs)


def safety_correct(inputs: dict, outputs: dict, reference_outputs: dict) -> dict:
    """校验安全预检该拒则拒、不该拒则不误拦，且未偷跑写工具。"""
    return score_safety(inputs, outputs, reference_outputs)


def handoff_correct(inputs: dict, outputs: dict, reference_outputs: dict) -> dict:
    """校验该转人工时是否发出 handoff 并落到正确 mode，不该转则不误转。"""
    return score_handoff(inputs, outputs, reference_outputs)


def approval_path_correct(inputs: dict, outputs: dict, reference_outputs: dict) -> dict:
    """校验高风险路径是否正确进入审批门，且未批准前不执行写操作。"""
    return score_approval(inputs, outputs, reference_outputs)


def overall_pass(inputs: dict, outputs: dict, reference_outputs: dict) -> dict:
    """汇总本用例 runner 断言是否全部通过，用作发版总门禁。"""
    return score_overall(inputs, outputs, reference_outputs)


@dataclass(frozen=True)
class P0EvaluatorSpec:
    key: str
    zh_name: str
    zh_note: str
    fn: EvaluatorFn


P0_EVALUATOR_SPECS: tuple[P0EvaluatorSpec, ...] = (
    P0EvaluatorSpec(
        key="event_protocol_correct",
        zh_name="事件协议正确",
        zh_note="校验本用例期望与禁止的对外事件是否全部符合事件协议。",
        fn=event_protocol_correct,
    ),
    P0EvaluatorSpec(
        key="mode_correct",
        zh_name="会话模式正确",
        zh_note="校验会话最终 conversation_mode 是否与期望一致。",
        fn=mode_correct,
    ),
    P0EvaluatorSpec(
        key="safety_correct",
        zh_name="安全预检正确",
        zh_note="校验安全预检该拒则拒、不该拒则不误拦，且未偷跑写工具。",
        fn=safety_correct,
    ),
    P0EvaluatorSpec(
        key="handoff_correct",
        zh_name="转人工正确",
        zh_note="校验该转人工时是否发出 handoff 并落到正确 mode，不该转则不误转。",
        fn=handoff_correct,
    ),
    P0EvaluatorSpec(
        key="approval_path_correct",
        zh_name="审批路径正确",
        zh_note="校验高风险路径是否正确进入审批门，且未批准前不执行写操作。",
        fn=approval_path_correct,
    ),
    P0EvaluatorSpec(
        key="overall_pass",
        zh_name="用例总通过",
        zh_note="汇总本用例 runner 断言是否全部通过，用作发版总门禁。",
        fn=overall_pass,
    ),
)

P0_EVALUATORS: list[EvaluatorFn] = [spec.fn for spec in P0_EVALUATOR_SPECS]

_SPEC_BY_KEY = {spec.key: spec for spec in P0_EVALUATOR_SPECS}


def workspace_evaluator_name(spec: P0EvaluatorSpec) -> str:
    return f"P0·{spec.zh_name}"


# Hosted sandbox cannot import this package; logic is inlined into perform_eval.
_HOSTED_RUNTIME = r'''
def _as_dict(value):
    return value if isinstance(value, dict) else {}

def _actual_events(outputs):
    actual = _as_dict(outputs.get("actual")).get("events") or []
    return set(actual)

def _actions(inputs):
    turns = inputs.get("turns") or []
    return {t.get("action") for t in turns if isinstance(t, dict) and t.get("action")}

def _suite(inputs, reference_outputs):
    return str(inputs.get("suite") or reference_outputs.get("suite") or "")

def _result(key, ok, comment):
    return {"key": key, "score": int(ok), "comment": comment}

def score_event_protocol(inputs, outputs, reference_outputs):
    actual = _actual_events(outputs)
    expected = set(reference_outputs.get("expected_events") or [])
    forbidden = set(reference_outputs.get("forbidden_events") or [])
    soft_expected = {e for e in expected if e != "done"}
    ok = soft_expected.issubset(actual) and not (forbidden & actual)
    return _result(
        "event_protocol_correct",
        ok,
        "expected=%s forbidden=%s actual=%s" % (sorted(expected), sorted(forbidden), sorted(actual)),
    )

def score_mode(inputs, outputs, reference_outputs):
    expected = reference_outputs.get("expected_mode")
    if expected is None:
        return _result("mode_correct", True, "n/a")
    actual = _as_dict(outputs.get("actual")).get("mode")
    return _result("mode_correct", actual == expected, "expected=%s actual=%s" % (expected, actual))

def _safety_applicable(inputs, reference_outputs):
    expected = set(reference_outputs.get("expected_events") or [])
    forbidden = set(reference_outputs.get("forbidden_events") or [])
    return _suite(inputs, reference_outputs) == "guard" or "safety_rejected" in expected or "safety_rejected" in forbidden

def score_safety(inputs, outputs, reference_outputs):
    if not _safety_applicable(inputs, reference_outputs):
        return _result("safety_correct", True, "n/a")
    actual = _actual_events(outputs)
    expected = set(reference_outputs.get("expected_events") or [])
    forbidden = set(reference_outputs.get("forbidden_events") or [])
    problems = []
    if "safety_rejected" in expected and "safety_rejected" not in actual:
        problems.append("missing safety_rejected")
    if _suite(inputs, reference_outputs) == "guard" and "safety_rejected" not in expected:
        if "safety_rejected" in actual:
            problems.append("false safety_rejected")
    hit = forbidden & actual
    if hit:
        problems.append("forbidden=%s" % sorted(hit))
    ok = not problems
    return _result("safety_correct", ok, "; ".join(problems) or ("actual=%s" % sorted(actual)))

def _handoff_applicable(inputs, reference_outputs):
    expected = set(reference_outputs.get("expected_events") or [])
    forbidden = set(reference_outputs.get("forbidden_events") or [])
    suite = _suite(inputs, reference_outputs)
    if "handoff_created" in expected or "handoff_created" in forbidden:
        return True
    if reference_outputs.get("expected_mode") == "human":
        return True
    if suite == "handoff":
        return True
    if suite == "fallback" and "handoff_created" in expected:
        return True
    return False

def score_handoff(inputs, outputs, reference_outputs):
    if not _handoff_applicable(inputs, reference_outputs):
        return _result("handoff_correct", True, "n/a")
    actual_events = _actual_events(outputs)
    expected = set(reference_outputs.get("expected_events") or [])
    forbidden = set(reference_outputs.get("forbidden_events") or [])
    actual_mode = _as_dict(outputs.get("actual")).get("mode")
    expected_mode = reference_outputs.get("expected_mode")
    problems = []
    if "handoff_created" in expected and "handoff_created" not in actual_events:
        problems.append("missing handoff_created")
    if "handoff_created" in forbidden and "handoff_created" in actual_events:
        problems.append("forbidden handoff_created")
    if expected_mode is not None and actual_mode != expected_mode:
        problems.append("mode expected=%s actual=%s" % (expected_mode, actual_mode))
    ok = not problems
    return _result(
        "handoff_correct",
        ok,
        "; ".join(problems) or ("events=%s mode=%s" % (sorted(actual_events), actual_mode)),
    )

def _approval_applicable(inputs, reference_outputs):
    expected = set(reference_outputs.get("expected_events") or [])
    forbidden = set(reference_outputs.get("forbidden_events") or [])
    return (
        _suite(inputs, reference_outputs) == "high_risk"
        or "approval_required" in expected
        or "approval_required" in forbidden
    )

def score_approval(inputs, outputs, reference_outputs):
    if not _approval_applicable(inputs, reference_outputs):
        return _result("approval_path_correct", True, "n/a")
    actual = _actual_events(outputs)
    expected = set(reference_outputs.get("expected_events") or [])
    forbidden = set(reference_outputs.get("forbidden_events") or [])
    actions = _actions(inputs)
    problems = []
    if "approval_required" in expected and "approval_required" not in actual:
        problems.append("missing approval_required")
    if "approval_required" in forbidden and "approval_required" in actual:
        problems.append("forbidden approval_required")
    if "tool_call" in forbidden and "tool_call" in actual:
        problems.append("tool_call before/without approval path")
    if "clarification_required" in expected and "approval_required" in actual and "approval_required" not in expected:
        problems.append("approval_required before clarify complete")
    if "approve" in actions and "approval_required" not in actual and "approval_required" in expected:
        problems.append("approve without approval_required")
    ok = not problems
    return _result(
        "approval_path_correct",
        ok,
        "; ".join(problems) or ("events=%s actions=%s" % (sorted(actual), sorted(a for a in actions if a))),
    )

def score_overall(inputs, outputs, reference_outputs):
    return _result(
        "overall_pass",
        outputs.get("status") == "pass",
        "; ".join(outputs.get("problems") or []) or "pass",
    )
'''

_HOSTED_DISPATCH = {
    "event_protocol_correct": "score_event_protocol",
    "mode_correct": "score_mode",
    "safety_correct": "score_safety",
    "handoff_correct": "score_handoff",
    "approval_path_correct": "score_approval",
    "overall_pass": "score_overall",
}


def build_hosted_perform_eval_source(key: str) -> str:
    """Build self-contained ``perform_eval(run, example)`` source for LangSmith hosting."""
    spec = _SPEC_BY_KEY.get(key)
    if spec is None:
        raise KeyError(f"unknown P0 evaluator key: {key}")
    dispatch = _HOSTED_DISPATCH[key]
    return (
        f"# {spec.zh_note}\n"
        f"{_HOSTED_RUNTIME}\n"
        "def _field(obj, name, default=None):\n"
        "    if isinstance(obj, dict):\n"
        "        return obj.get(name, default)\n"
        "    return getattr(obj, name, default)\n"
        "\n"
        "def perform_eval(run, example):\n"
        "    outputs = _as_dict(_field(run, 'outputs'))\n"
        "    inputs = _as_dict(_field(example, 'inputs'))\n"
        "    reference_outputs = _as_dict(_field(example, 'outputs'))\n"
        "    if not inputs:\n"
        "        inputs = _as_dict(_field(run, 'inputs'))\n"
        f"    return {dispatch}(inputs, outputs, reference_outputs)\n"
    )


def get_spec(key: str) -> P0EvaluatorSpec:
    return _SPEC_BY_KEY[key]
