"""Create the LangSmith real-model eval dataset from the local JSONL source."""

import argparse
import json
from pathlib import Path

from langsmith import Client

from ai_service_agent.config import get_settings


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "tests" / "evals" / "real_model" / "master.jsonl"
DEFAULT_DATASET_NAME = "ai-service-agent-real-model-eval-v1"

INPUT_KEYS = {
    "id",
    "execution_mode",
    "category",
    "user_id",
    "input",
    "recent_context",
    "confirm",
    "approve",
}
OUTPUT_KEYS = {
    "expected_route_intent",
    "expected_target_graph",
    "expected_tools",
    "expected_events",
    "forbidden_events",
}


def load_cases(path: Path) -> list[dict]:
    cases = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            cases.append(json.loads(line))
    if not 30 <= len(cases) <= 40:
        raise ValueError(f"expected 30-40 cases, got {len(cases)}")
    if len({case["id"] for case in cases}) != len(cases):
        raise ValueError("dataset has duplicate case ids")
    return cases


def to_langsmith_example(case: dict) -> dict:
    inputs = {key: case.get(key) for key in INPUT_KEYS if key in case}
    outputs = {key: case.get(key, []) for key in OUTPUT_KEYS}
    return {
        "inputs": inputs,
        "outputs": outputs,
        "metadata": {
            "source": "tests/evals/real_model/master.jsonl",
            "case_id": case["id"],
            "category": case["category"],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--name", default=DEFAULT_DATASET_NAME)
    args = parser.parse_args()

    settings = get_settings()
    if not settings.langsmith_api_key:
        raise RuntimeError("LangSmith API key is required")

    cases = load_cases(args.dataset)
    examples = [to_langsmith_example(case) for case in cases]
    client = Client()

    if client.has_dataset(dataset_name=args.name):
        print(f"DATASET_EXISTS {args.name}")
        return 0

    dataset = client.create_dataset(
        dataset_name=args.name,
        description="40 real-model E2E cases for router, read, mutation, high-risk and handoff flows.",
    )
    client.create_examples(dataset_id=dataset.id, examples=examples)
    print(json.dumps({
        "dataset_name": args.name,
        "dataset_id": str(dataset.id),
        "examples": len(examples),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
