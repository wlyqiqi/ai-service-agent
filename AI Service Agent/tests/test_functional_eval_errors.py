"""Unit tests for functional eval stream-error helpers (no network)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace


def _load_runner():
    path = Path(__file__).resolve().parents[1] / "scripts" / "functional_scenario_eval.py"
    spec = importlib.util.spec_from_file_location("functional_scenario_eval_errors", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_error_summaries_prefer_detail():
    mod = _load_runner()
    events = [{
        "type": "error",
        "data": {
            "code": "OpenAIAuthenticationError",
            "message": "user facing",
            "detail": "Error code: 401 - Token is invalid.",
        },
    }]
    assert mod.error_summaries(events) == [
        "stream error: OpenAIAuthenticationError: Error code: 401 - Token is invalid."
    ]
    payloads = mod.error_payloads(events)
    assert payloads[0]["code"] == "OpenAIAuthenticationError"
    assert payloads[0]["detail"].startswith("Error code: 401")


def test_mode_changed_not_counted_as_problem():
    """S39/handoff: ModeChanged after handoff_created is design behavior, not a fail."""
    mod = _load_runner()
    events = [
        {"type": "handoff_created", "data": {"handoff_id": "h1"}},
        {
            "type": "error",
            "data": {"code": "ModeChanged", "message": "AI 回复已丢弃（会话已转人工）"},
        },
        {"type": "done", "data": {"final": ""}},
    ]
    assert mod.error_summaries(events) == []
    assert mod._problems_with_stream_errors(events, []) == []
    # Still visible in actual.errors for observability.
    assert any(p["code"] == "ModeChanged" for p in mod.error_payloads(events))
    assert mod.error_summaries(events, include_expected=True) == [
        "stream error: ModeChanged: AI 回复已丢弃（会话已转人工）"
    ]


def test_unexpected_errors_still_enrich_problems():
    mod = _load_runner()
    events = [{
        "type": "error",
        "data": {"code": "runtime_error", "message": "boom", "detail": "NodeTimeoutError"},
    }]
    problems = mod._problems_with_stream_errors(events, ["mode expected=human actual=ai"])
    assert problems[0] == "stream error: runtime_error: NodeTimeoutError"
    assert "mode expected=human actual=ai" in problems


def test_preflight_rejects_ark_key_on_siliconflow():
    mod = _load_runner()
    settings = SimpleNamespace(
        model_api_key="ark-test-token",
        model_endpoint="https://api.siliconflow.cn/v1",
    )
    try:
        mod.preflight_model_auth(settings)
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "SiliconFlow" in str(exc)
        assert "ark-" in str(exc)


def test_preflight_allows_aligned_siliconflow_sk():
    mod = _load_runner()
    settings = SimpleNamespace(
        model_api_key="sk-test-token",
        model_endpoint="https://api.siliconflow.cn/v1",
    )
    mod.preflight_model_auth(settings)
