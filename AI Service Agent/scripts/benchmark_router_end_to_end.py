import argparse
import json
import math
import os
import statistics
import time
from collections import Counter
from pathlib import Path

import httpx
from langchain_core.messages import HumanMessage

from ai_service_agent.config import get_settings, make_model
from ai_service_agent.evals.runner import load_cases, policy_target
from ai_service_agent.graph.router import _classify_with_jev, make_router


def percentile(values: list[float], percent: float) -> float:
    ordered = sorted(values)
    index = (len(ordered) - 1) * percent / 100
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def latency_summary(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "p50_ms": round(percentile(values, 50), 1),
        "mean_ms": round(statistics.mean(values), 1),
        "p95_ms": round(percentile(values, 95), 1),
        "p99_ms": round(percentile(values, 99), 1),
        "min_ms": round(min(values), 1),
        "max_ms": round(max(values), 1),
    }


def accuracy_summary(results: list[dict]) -> dict:
    total = len(results)
    if total == 0:
        return {
            "total": 0,
            "capability_accuracy": None,
            "risk_accuracy": None,
            "joint_accuracy": None,
            "policy_target_accuracy": None,
            "high_risk_recall": None,
            "mutation_recall": None,
            "read_recall": None,
            "capability_confusion": {},
            "risk_confusion": {},
        }

    def recall(risk: str) -> float:
        expected = [r for r in results if r["expected_risk_level"] == risk]
        correct = [r for r in expected if r["predicted_risk_level"] == risk]
        return round(len(correct) / len(expected), 4) if expected else None

    return {
        "total": total,
        "capability_accuracy": round(sum(r["capability_correct"] for r in results) / total, 4),
        "risk_accuracy": round(sum(r["risk_correct"] for r in results) / total, 4),
        "joint_accuracy": round(sum(r["joint_correct"] for r in results) / total, 4),
        "policy_target_accuracy": round(sum(r["policy_target_correct"] for r in results) / total, 4),
        "high_risk_recall": recall("high_risk"),
        "mutation_recall": recall("mutation"),
        "read_recall": recall("read"),
        "capability_confusion": {
            f"{expected} -> {predicted}": count
            for (expected, predicted), count in Counter(
                (r["expected_capability"], r["predicted_capability"]) for r in results
            ).items()
        },
        "risk_confusion": {
            f"{expected} -> {predicted}": count
            for (expected, predicted), count in Counter(
                (r["expected_risk_level"], r["predicted_risk_level"]) for r in results
            ).items()
        },
    }


def evaluate(router_fn, cases: list[dict], backend: str) -> dict:
    results = []
    for case in cases:
        state = {
            "messages": [HumanMessage(content=case["input"])],
            "user_id": "u1",
            "session_id": "benchmark",
            "context": {},
        }
        started = time.perf_counter()
        output = router_fn(state)
        elapsed = (time.perf_counter() - started) * 1000
        result = {
            "id": case["id"],
            "backend": backend,
            "input": case["input"],
            "expected_capability": case["expected_capability"],
            "expected_risk_level": case["expected_risk_level"],
            "expected_graph": case["expected_graph"],
            "predicted_capability": output["capability"],
            "predicted_risk_level": output["risk_level"],
            "confidence": output["confidence"],
            "latency_ms": round(elapsed, 1),
            "capability_correct": output["capability"] == case["expected_capability"],
            "risk_correct": output["risk_level"] == case["expected_risk_level"],
            "joint_correct": (
                output["capability"] == case["expected_capability"]
                and output["risk_level"] == case["expected_risk_level"]
            ),
            "predicted_policy_target": policy_target(output["capability"], output["risk_level"]),
            "policy_target_correct": (
                policy_target(output["capability"], output["risk_level"]) == case["expected_graph"]
            ),
        }
        results.append(result)
    return {
        "summary": {**accuracy_summary(results), "latency": latency_summary([r["latency_ms"] for r in results])},
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default="tests/evals/control/master.jsonl")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--output", default="scripts/router_end_to_end_results.json")
    args = parser.parse_args()

    cases = load_cases(args.dataset)
    # Router benchmark excludes cases intercepted before Router consumption:
    # explicit handoff is resolved at the entry layer; guard_reject consumes guard, not router.
    cases = [case for case in cases if case["expected_policy"] not in {"handoff", "guard_reject"}]
    if args.limit:
        cases = cases[: args.limit]

    os.environ.pop("JEV_ENABLED", None)
    get_settings.cache_clear()
    deepseek_router = make_router(make_model())

    api_key = os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        raise RuntimeError("TYPESAFE_API_KEY is not set")
    os.environ["JEV_ENABLED"] = "true"
    get_settings.cache_clear()
    settings = get_settings()

    deepseek = evaluate(deepseek_router, cases, "deepseek")

    jev_client = httpx.Client(timeout=settings.jev_timeout_seconds)
    jev_raw_results = []
    jev_end_to_end_results = []
    fallback_latency = 0.0
    for case in cases:
        state = {
            "messages": [HumanMessage(content=case["input"])],
            "user_id": "u1",
            "session_id": "benchmark",
            "context": {},
        }
        jev_started = time.perf_counter()
        jev_result = None
        jev_error = None
        try:
            jev_result = _classify_with_jev(jev_client, state, settings)
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
            jev_error = type(exc).__name__
        jev_latency = (time.perf_counter() - jev_started) * 1000

        if jev_result is not None:
            final_result = jev_result
            final_backend = "jev"
            fallback_latency = None
        else:
            fallback_started = time.perf_counter()
            final_result = deepseek_router(state)
            fallback_latency = (time.perf_counter() - fallback_started) * 1000
            final_backend = "deepseek_fallback"

        total_latency = jev_latency + (fallback_latency or 0)
        base = {
            "id": case["id"],
            "input": case["input"],
            "expected_capability": case["expected_capability"],
            "expected_risk_level": case["expected_risk_level"],
            "expected_graph": case["expected_graph"],
        }
        if jev_result is not None:
            jev_raw_results.append({
                **base,
                "backend": "jev",
                "predicted_capability": jev_result["capability"],
                "predicted_risk_level": jev_result["risk_level"],
                "confidence": jev_result["confidence"],
                "latency_ms": round(jev_latency, 1),
                "capability_correct": jev_result["capability"] == case["expected_capability"],
                "risk_correct": jev_result["risk_level"] == case["expected_risk_level"],
                "joint_correct": (
                    jev_result["capability"] == case["expected_capability"]
                    and jev_result["risk_level"] == case["expected_risk_level"]
                ),
                "predicted_policy_target": policy_target(jev_result["capability"], jev_result["risk_level"]),
                "policy_target_correct": (
                    policy_target(jev_result["capability"], jev_result["risk_level"]) == case["expected_graph"]
                ),
            })
        else:
            jev_raw_results.append({
                **base,
                "backend": "fallback_or_error",
                "jev_error": jev_error,
                "latency_ms": round(jev_latency, 1),
            })

        jev_end_to_end_results.append({
            **base,
            "backend": final_backend,
            "predicted_capability": final_result["capability"],
            "predicted_risk_level": final_result["risk_level"],
            "confidence": final_result["confidence"],
            "latency_ms": round(total_latency, 1),
            "jev_primary_latency_ms": round(jev_latency, 1),
            "fallback_latency_ms": round(fallback_latency, 1) if fallback_latency is not None else None,
            "capability_correct": final_result["capability"] == case["expected_capability"],
            "risk_correct": final_result["risk_level"] == case["expected_risk_level"],
            "joint_correct": (
                final_result["capability"] == case["expected_capability"]
                and final_result["risk_level"] == case["expected_risk_level"]
            ),
            "predicted_policy_target": policy_target(final_result["capability"], final_result["risk_level"]),
            "policy_target_correct": (
                policy_target(final_result["capability"], final_result["risk_level"]) == case["expected_graph"]
            ),
        })

    jev_raw_accepted = [r for r in jev_raw_results if r["backend"] == "jev"]
    output = {
        "dataset": args.dataset,
        "case_count": len(cases),
        "thresholds": {
            "jev_confidence_threshold": settings.jev_confidence_threshold,
            "jev_risk_confidence_threshold": settings.jev_risk_confidence_threshold,
            "jev_timeout_seconds": settings.jev_timeout_seconds,
        },
        "deepseek": deepseek["summary"],
        "jev_accepted_only": {
            **accuracy_summary(jev_raw_accepted),
            "latency": latency_summary([r["latency_ms"] for r in jev_raw_accepted]),
        },
        "jev_end_to_end": {
            **accuracy_summary(jev_end_to_end_results),
            "fallback_rate": round(
                sum(r["backend"] == "deepseek_fallback" for r in jev_end_to_end_results) / len(cases), 4
            ),
            "latency": latency_summary([r["latency_ms"] for r in jev_end_to_end_results]),
        },
        "results": {
            "deepseek": deepseek["results"],
            "jev_raw": jev_raw_results,
            "jev_end_to_end": jev_end_to_end_results,
        },
    }
    Path(args.output).write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in output.items() if k != "results"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
