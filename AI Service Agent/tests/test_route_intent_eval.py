import json
from pathlib import Path

import pytest

from ai_service_agent.evals.route_intent import (
    build_messages,
    evaluate_cases,
    load_cases,
    summarize,
)


def _fake_router(cases: dict[str, dict]):
    calls = []

    def router(state: dict) -> dict:
        calls.append(state)
        current = state["messages"][-1]["content"]
        result = cases[current]
        return {
            "route_intent": result["route_intent"],
            "confidence": result["confidence"],
            "execution_status": "routing",
            "routing_backend": "jev",
            "routing_fallback": False,
            "route_control": "policy",
        }

    return router, calls


def test_build_messages_puts_current_user_message_last():
    messages = build_messages({
        "current_user_message": "那帮我取消",
        "recent_context": [
            {"role": "human", "content": "帮我查订单 ORD-1"},
            {"role": "ai", "content": "订单 ORD-1 已支付"},
        ],
    })

    assert messages == [
        {"role": "human", "content": "帮我查订单 ORD-1"},
        {"role": "ai", "content": "订单 ORD-1 已支付"},
        {"role": "human", "content": "那帮我取消"},
    ]


def test_load_cases_supports_single_turn_and_multi_turn(tmp_path):
    path = tmp_path / "cases.jsonl"
    path.write_text(
        "\n".join([
            json.dumps({"id": "ST-1", "input": "谢谢你", "expected_route_intent": "smalltalk"}, ensure_ascii=False),
            json.dumps({
                "id": "MT-1",
                "current_user_message": "那帮我取消",
                "recent_context": [{"role": "human", "content": "帮我查订单 ORD-1"}],
                "expected_route_intent": "cancel_order_execute",
            }, ensure_ascii=False),
        ]),
        encoding="utf-8",
    )

    cases = load_cases(path)

    assert [case["id"] for case in cases] == ["ST-1", "MT-1"]
    assert cases[0]["current_user_message"] == "谢谢你"
    assert cases[0]["recent_context"] == []
    assert cases[1]["current_user_message"] == "那帮我取消"


def test_load_cases_rejects_duplicate_ids(tmp_path):
    path = tmp_path / "cases.jsonl"
    row = {"id": "MT-1", "input": "那帮我取消", "expected_route_intent": "cancel_order_execute"}
    path.write_text("\n".join([json.dumps(row), json.dumps(row)]), encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate"):
        load_cases(path)


def test_multi_turn_eval_exercises_context_without_fallback():
    cases = [
        {
            "id": "MT-001",
            "current_user_message": "那帮我取消",
            "recent_context": [
                {"role": "human", "content": "帮我查订单 ORD-1"},
                {"role": "ai", "content": "订单 ORD-1 已支付"},
            ],
            "expected_route_intent": "cancel_order_execute",
        },
        {
            "id": "MT-002",
            "current_user_message": "看看我的订单",
            "recent_context": [
                {"role": "human", "content": "我要改手机号"},
                {"role": "ai", "content": "已修改"},
            ],
            "expected_route_intent": "my_orders",
        },
    ]
    router, calls = _fake_router({
        "那帮我取消": {"route_intent": "cancel_order_execute", "confidence": 0.98},
        "看看我的订单": {"route_intent": "my_orders", "confidence": 0.99},
    })

    results = evaluate_cases(router, cases)

    assert len(calls) == 2
    assert calls[0]["messages"][-1]["content"] == "那帮我取消"
    assert calls[0]["messages"][:-1] == cases[0]["recent_context"]
    assert [result["correct"] for result in results] == [True, True]
    assert all(result["routing_backend"] == "jev" for result in results)
    assert all(result["routing_fallback"] is False for result in results)


def test_multi_turn_summary_tracks_unsupported_action_leakage():
    results = [
        {"expected_route_intent": "unsupported_action", "predicted_route_intent": "order_mutation", "routing_backend": "jev", "routing_fallback": False, "correct": False},
        {"expected_route_intent": "unsupported_action", "predicted_route_intent": "cancel_order_execute", "routing_backend": "jev", "routing_fallback": False, "correct": False},
        {"expected_route_intent": "my_orders", "predicted_route_intent": "my_orders", "routing_backend": "jev", "routing_fallback": False, "correct": True},
    ]

    summary = summarize(results)

    assert summary["route_intent_accuracy"] == 0.3333
    assert summary["unsupported_action_mutation_or_high_risk_leakage"] == 1.0
    assert summary["fallback_rate"] == 0.0


def test_multi_turn_dataset_covers_required_categories():
    path = Path(__file__).resolve().parents[1] / "tests" / "evals" / "router_benchmark" / "route_intent_multi_turn.jsonl"
    cases = load_cases(path)
    categories = {case["category"] for case in cases}

    assert len(cases) == 8
    assert len({case["id"] for case in cases}) == len(cases)
    assert {
        "reference_resolution",
        "parameter_clarification",
        "intent_switch",
        "unsupported_boundary",
        "ambiguous",
    } <= categories
    assert all(len(case["recent_context"]) <= 2 for case in cases)
