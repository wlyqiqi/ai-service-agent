"""Upload functional scenarios master.jsonl to LangSmith as a dataset."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from langsmith import Client

from ai_service_agent.config import get_settings

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "tests" / "evals" / "functional_scenarios" / "master.jsonl"
DEFAULT_NAME = "ai-cs-functional-scenarios-v2"

INPUT_KEYS = {
    "id", "suite", "split", "user_id", "turns", "require_done",
    "context_probe", "note",
}
OUTPUT_KEYS = {
    "expected_events", "forbidden_events", "expected_tools",
    "expected_mode", "store_contains",
}


def load_cases(path: Path) -> list[dict]:
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(cases) != 60:
        raise ValueError(f"expected exactly 60 v2 cases, got {len(cases)}")
    if len({c["id"] for c in cases}) != len(cases):
        raise ValueError("duplicate ids")
    return cases


def to_example(case: dict) -> dict:
    inputs = {k: case.get(k) for k in INPUT_KEYS if k in case}
    outputs = {k: case.get(k) for k in OUTPUT_KEYS if k in case}
    return {
        "inputs": inputs,
        "outputs": outputs,
        "metadata": {
            "case_id": case["id"],
            "suite": case.get("suite"),
            "split": case.get("split"),
            "source": "tests/evals/functional_scenarios/master.jsonl",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--name", default=DEFAULT_NAME)
    parser.add_argument("--replace", action="store_true", help="delete existing dataset examples by recreating")
    args = parser.parse_args()

    settings = get_settings()
    if not settings.langsmith_api_key:
        raise RuntimeError("LANGSMITH_API_KEY required")

    cases = load_cases(args.dataset)
    examples = [to_example(c) for c in cases]
    client = Client()

    if client.has_dataset(dataset_name=args.name):
        if not args.replace:
            print(json.dumps({"status": "exists", "dataset_name": args.name, "hint": "pass --replace to recreate"}, ensure_ascii=False))
            return 0
        client.delete_dataset(dataset_name=args.name)

    dataset = client.create_dataset(
        dataset_name=args.name,
        description="Functional branch scenarios (>=52): single/multi-turn, handoff, AI/human context, context harness.",
    )
    # Attach smoke/full splits via metadata; LangSmith splits API optional
    client.create_examples(dataset_id=dataset.id, examples=examples)

    # Best-effort: tag smoke examples into a split if supported
    try:
        listed = list(client.list_examples(dataset_name=args.name))
        smoke_ids = [ex.id for ex in listed if (ex.metadata or {}).get("split") == "smoke"]
        if smoke_ids:
            client.update_examples(
                example_ids=smoke_ids,
                splits=[["smoke"] for _ in smoke_ids],
            )
        full_ids = [ex.id for ex in listed]
        if full_ids:
            client.update_examples(
                example_ids=full_ids,
                splits=[["full"] for _ in full_ids],
            )
    except Exception as exc:  # noqa: BLE001
        print(f"SPLIT_WARN {type(exc).__name__}: {exc}")

    print(json.dumps({
        "dataset_name": args.name,
        "dataset_id": str(dataset.id),
        "examples": len(examples),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
