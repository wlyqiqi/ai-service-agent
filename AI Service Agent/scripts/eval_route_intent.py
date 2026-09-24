"""Offline or live route-intent evaluation for Router v2."""

import argparse
import json
import math
import time
from collections import Counter, defaultdict
from pathlib import Path

from ai_service_agent.config import make_model
from ai_service_agent.graph.router import make_router


MUTATION_OR_HIGH_RISK = {
    "order_mutation", "cancel_order_execute", "refund_apply_execute"
}


def load_cases(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    ids = [row["id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate route-intent eval id")
    return rows


def percentile(values: list[float], percent: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * percent / 100) - 1))
    return ordered[index]


def latency_summary(values: list[float]) -> dict:
    return {
        "n": len(values),
        "p50_ms": round(percentile(values, 50), 1),
        "p95_ms": round(percentile(values, 95), 1),
        "mean_ms": round(sum(values) / len(values), 1) if values else 0.0,
        "max_ms": round(max(values), 1) if values else 0.0,
    }


def summarize(results: list[dict]) -> dict:
    total = len(results)
    if not total:
        return {"total": 0}

    expected_by_intent: dict[str, list[dict]] = defaultdict(list)
    for result in results:
        expected_by_intent[result["expected_route_intent"]].append(result)

    def recall(intent: str) -> float | None:
        rows = expected_by_intent.get(intent, [])
        return round(sum(row["correct"] for row in rows) / len(rows), 4) if rows else None

    account_rows = expected_by_intent.get("account_profile_update", [])
    account_confusion = sum(
        row["predicted_route_intent"] == "order_mutation" for row in account_rows
    )
    unsupported_rows = expected_by_intent.get("unsupported_action", [])
    unsupported_leakage = sum(
        row["predicted_route_intent"] in MUTATION_OR_HIGH_RISK
        for row in unsupported_rows
    )
    backend_counts = Counter(row["routing_backend"] for row in results)
    confusion = Counter(
        (row["expected_route_intent"], row["predicted_route_intent"])
        for row in results if not row["correct"]
    )

    high_risk_expected = [
        row for row in results
        if row["expected_route_intent"] in {"cancel_order_execute", "refund_apply_execute"}
    ]
    high_risk_correct = sum(row["correct"] for row in high_risk_expected)

    return {
        "total": total,
        "route_intent_accuracy": round(sum(row["correct"] for row in results) / total, 4),
        "account_profile_update_recall": recall("account_profile_update"),
        "account_profile_update_to_order_mutation_rate": round(
            account_confusion / len(account_rows), 4
        ) if account_rows else None,
        "cancel_order_execute_recall": recall("cancel_order_execute"),
        "refund_apply_execute_recall": recall("refund_apply_execute"),
        "high_risk_recall": round(
            high_risk_correct / len(high_risk_expected), 4
        ) if high_risk_expected else None,
        "unsupported_action_mutation_or_high_risk_leakage": round(
            unsupported_leakage / len(unsupported_rows), 4
        ) if unsupported_rows else None,
        "fallback_rate": round(sum(row["routing_fallback"] for row in results) / total, 4),
        "backend_counts": dict(backend_counts),
        "confusion": {
            f"{expected} -> {predicted}": count
            for (expected, predicted), count in confusion.items()
        },
        "latency": latency_summary([row["latency_ms"] for row in results]),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("tests/evals/router_benchmark/route_intent.jsonl"),
    )
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    router = make_router(make_model())
    results = []
    for case in load_cases(args.dataset):
        started = time.perf_counter()
        output = router({
            "messages": [{"role": "human", "content": case["input"]}],
            "user_id": "u-route-eval",
            "session_id": case["id"],
            "context": {},
        })
        latency_ms = (time.perf_counter() - started) * 1000
        predicted = output["route_intent"]
        results.append({
            "id": case["id"],
            "input": case["input"],
            "expected_route_intent": case["expected_route_intent"],
            "predicted_route_intent": predicted,
            "confidence": output.get("confidence"),
            "routing_backend": output.get("routing_backend", "unknown"),
            "routing_fallback": bool(output.get("routing_fallback", False)),
            "latency_ms": round(latency_ms, 1),
            "correct": predicted == case["expected_route_intent"],
        })

    report = {"summary": summarize(results), "results": results}
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    if args.out:
        args.out.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
