import argparse
import json
import os
import statistics
import time
from collections import Counter
from pathlib import Path

from langchain_core.messages import HumanMessage

os.environ["FAKE_MODEL"] = "true"
os.environ["JEV_ROUTE_INTENT_ENABLED"] = "true"

from ai_service_agent.config import get_settings
from ai_service_agent.graph.route_policy import ROUTE_POLICY
from ai_service_agent.graph.router import make_router
from ai_service_agent.testing import ToolCallingFakeModel, route_intent_json_message


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dataset",
        default="tests/evals/router_benchmark/route_intent.jsonl",
    )
    parser.add_argument("--output", default="scripts/route_intent_eval_results.json")
    args = parser.parse_args()

    cases = [
        json.loads(line)
        for line in Path(args.dataset).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    unknown_intents = {
        case["expected_route_intent"] for case in cases
    } - set(ROUTE_POLICY)
    if unknown_intents:
        raise ValueError(f"Unknown expected route intents: {sorted(unknown_intents)}")

    model = ToolCallingFakeModel(
        responses=[
            route_intent_json_message(case["expected_route_intent"], 0.95)
            for case in cases
        ]
    )
    router = make_router(model)
    results = []
    for case in cases:
        state = {
            "messages": [HumanMessage(content=case["input"])],
            "user_id": "u1",
            "session_id": "route-intent-eval",
            "context": {},
        }
        started = time.perf_counter()
        output = router(state)
        latency_ms = (time.perf_counter() - started) * 1000
        predicted = output["route_intent"]
        results.append({
            "id": case["id"],
            "input": case["input"],
            "expected_route_intent": case["expected_route_intent"],
            "predicted_route_intent": predicted,
            "confidence": output["confidence"],
            "latency_ms": round(latency_ms, 1),
            "correct": predicted == case["expected_route_intent"],
        })

    confusion = Counter(
        f"{result['expected_route_intent']} -> {result['predicted_route_intent']}"
        for result in results
        if not result["correct"]
    )
    latencies = [result["latency_ms"] for result in results]
    report = {
        "settings": {
            "route_intent_enabled": get_settings().route_intent_enabled,
            "jev_enabled": get_settings().jev_enabled,
            "fake_model": get_settings().fake_model,
        },
        "summary": {
            "total": len(results),
            "route_intent_accuracy": round(
                sum(result["correct"] for result in results) / len(results), 4
            ),
            "mean_ms": round(statistics.mean(latencies), 1),
            "min_ms": round(min(latencies), 1),
            "max_ms": round(max(latencies), 1),
            "confusion": dict(confusion),
        },
        "results": results,
    }
    Path(args.output).write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
