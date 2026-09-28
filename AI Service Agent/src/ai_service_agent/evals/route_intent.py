import json
import math
from collections import Counter, defaultdict
from pathlib import Path


MUTATION_OR_HIGH_RISK = {
    "order_mutation",
    "cancel_order_execute",
    "refund_apply_execute",
}


def load_cases(path: str | Path) -> list[dict]:
    cases = []
    for index, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        row = json.loads(line)
        if "current_user_message" not in row and "input" not in row:
            raise ValueError(
                f"route-intent eval row {index} requires current_user_message or input"
            )
        required = {
            key for key in ("id", "expected_route_intent")
            if key not in row
        }
        if required:
            raise ValueError(
                f"route-intent eval row {index} missing required keys: {sorted(required)}"
            )
        row.setdefault("current_user_message", row.get("input"))
        row.setdefault("recent_context", [])
        cases.append(row)

    ids = [case["id"] for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate route-intent eval id")
    return cases


def build_messages(case: dict) -> list[dict]:
    return [
        *case.get("recent_context", []),
        {"role": "human", "content": case["current_user_message"]},
    ]


def _percentile(values: list[float], percent: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * percent / 100) - 1))
    return ordered[index]


def _latency_summary(values: list[float]) -> dict:
    return {
        "n": len(values),
        "p50_ms": round(_percentile(values, 50), 1),
        "p95_ms": round(_percentile(values, 95), 1),
        "mean_ms": round(sum(values) / len(values), 1) if values else 0.0,
        "max_ms": round(max(values), 1) if values else 0.0,
    }


def evaluate_cases(router, cases: list[dict]) -> list[dict]:
    results = []
    for case in cases:
        output = router({
            "messages": build_messages(case),
            "user_id": "u-route-eval",
            "session_id": case["id"],
            "context": {},
        })
        predicted = output["route_intent"]
        results.append({
            "id": case["id"],
            "current_user_message": case["current_user_message"],
            "recent_context": case.get("recent_context", []),
            "expected_route_intent": case["expected_route_intent"],
            "predicted_route_intent": predicted,
            "confidence": output.get("confidence"),
            "routing_backend": output.get("routing_backend", "unknown"),
            "routing_fallback": bool(output.get("routing_fallback", False)),
            "latency_ms": output.get("latency_ms", 0.0),
            "correct": predicted == case["expected_route_intent"],
        })
    return results


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
            sum(row["correct"] for row in high_risk_expected) / len(high_risk_expected), 4
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
        "latency": _latency_summary([row.get("latency_ms", 0.0) for row in results]),
    }
