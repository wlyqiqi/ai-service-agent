"""Offline or live route-intent evaluation for Router v2."""

import argparse
import json
import asyncio
import time
from pathlib import Path

from ai_service_agent.evals.route_intent import build_messages, load_cases, summarize
from ai_service_agent.graph.router import make_jev_router


async def run_eval(args) -> int:
    router = make_jev_router()
    results = []
    cases = load_cases(args.dataset)
    for case in cases:
        started = time.perf_counter()
        output = await router({
            "messages": build_messages(case),
            "user_id": "u-route-eval",
            "session_id": case["id"],
            "context": {},
        })
        latency_ms = (time.perf_counter() - started) * 1000
        predicted = output["route_intent"]
        results.append({
            "id": case["id"],
            "current_user_message": case["current_user_message"],
            "recent_context": case["recent_context"],
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("tests/evals/router_benchmark/route_intent.jsonl"),
    )
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    return asyncio.run(run_eval(args))


if __name__ == "__main__":
    raise SystemExit(main())
