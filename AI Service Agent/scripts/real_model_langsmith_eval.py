"""Run the real-model eval through LangSmith Datasets and Experiments."""

import argparse
import importlib.util
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import httpx
from langsmith import Client

from ai_service_agent.config import get_settings


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
DEFAULT_DATASET_NAME = "ai-service-agent-real-model-eval-v1"


def load_runner_module():
    path = SCRIPTS / "real_model_eval.py"
    spec = importlib.util.spec_from_file_location("real_model_eval_runner", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load runner: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def route_correct(inputs: dict, outputs: dict, reference_outputs: dict) -> dict:
    expected = reference_outputs.get("expected_route_intent")
    if expected is None:
        return {"key": "route_correct", "score": 1, "comment": "not_applicable"}
    actual = outputs.get("actual", {}).get("route_intent")
    return {
        "key": "route_correct",
        "score": int(actual == expected),
        "comment": f"expected={expected} actual={actual}",
    }


def tool_correct(inputs: dict, outputs: dict, reference_outputs: dict) -> dict:
    expected = set(reference_outputs.get("expected_tools") or [])
    actual = set(outputs.get("actual", {}).get("tools") or [])
    return {
        "key": "tool_correct",
        "score": int(actual == expected),
        "comment": f"expected={sorted(expected)} actual={sorted(actual)}",
    }


def event_protocol_correct(inputs: dict, outputs: dict, reference_outputs: dict) -> dict:
    actual_events = set(outputs.get("actual", {}).get("events") or [])
    expected = set(reference_outputs.get("expected_events") or [])
    forbidden = set(reference_outputs.get("forbidden_events") or [])
    passed = all(event_type in actual_events for event_type in expected)
    passed = passed and not any(event_type in actual_events for event_type in forbidden)
    return {
        "key": "event_protocol_correct",
        "score": int(passed),
        "comment": f"expected={sorted(expected)} forbidden={sorted(forbidden)} actual={sorted(actual_events)}",
    }


def overall_pass(inputs: dict, outputs: dict, reference_outputs: dict) -> dict:
    return {
        "key": "overall_pass",
        "score": int(outputs.get("status") == "pass"),
        "comment": "; ".join(outputs.get("problems") or []) or "pass",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset-name", default=DEFAULT_DATASET_NAME)
    parser.add_argument("--experiment-prefix", default=None)
    parser.add_argument("--project", default=None)
    parser.add_argument("--categories", nargs="*")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    settings = get_settings()
    if not settings.model_api_key or not settings.model_endpoint:
        raise RuntimeError("real-model eval requires ARK_API_KEY and ARK_BASE_URL")
    if not settings.langsmith_api_key:
        raise RuntimeError("real-model eval requires LANGSMITH_API_KEY")

    if args.project:
        os.environ["LANGSMITH_PROJECT"] = args.project

    client = Client()
    examples = list(client.list_examples(dataset_name=args.dataset_name))
    if args.categories:
        wanted = set(args.categories)
        examples = [example for example in examples if example.inputs.get("category") in wanted]
    examples.sort(key=lambda example: example.inputs.get("id", ""))
    if args.limit:
        examples = examples[: args.limit]
    if not examples:
        raise RuntimeError("no examples selected")

    prefix = args.experiment_prefix or f"real-model-{settings.model_name}-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}"
    runner = load_runner_module()

    with tempfile.TemporaryDirectory(
        prefix="langsmith-real-model-eval-",
        ignore_cleanup_errors=True,
    ) as temp_dir:
        db_path = str(Path(temp_dir) / "eval.db")
        load_seed = runner.load_seed_script()
        load_seed.build_database(db_path)
        runner._DB_PATH = db_path
        settings.mock_ecommerce_db_path = db_path
        app = runner.create_app(
            db_path,
            model=runner.make_model(),
            simulate_approval=False,
            auth_enabled=False,
            chatwoot=runner.NullChatwoot(),
            profile_db_path=db_path,
        )
        server = runner.start_server(app, "127.0.0.1", 8765)
        try:
            snapshot_path = str(Path(temp_dir) / "eval-clean.db")
            runner.create_eval_snapshot(db_path, snapshot_path)
            router = runner.make_eval_router()
            references = {
                example.inputs.get("id"): example.outputs or {}
                for example in examples
            }

            with httpx.Client(timeout=180) as http_client:
                def target(inputs: dict) -> dict:
                    case = {
                        **inputs,
                        **references.get(inputs.get("id"), {}),
                    }
                    if case["execution_mode"] == "router":
                        result = runner.evaluate_router(case, router)
                    elif case["execution_mode"] == "handoff":
                        runner.reset_eval_database(snapshot_path, db_path)
                        result = runner.evaluate_handoff(http_client, case)
                    else:
                        runner.reset_eval_database(snapshot_path, db_path)
                        result = runner.evaluate_graph(http_client, db_path, case)
                    return {
                        "status": result["status"],
                        "problems": result["problems"],
                        "actual": result["actual"],
                    }

                results = client.evaluate(
                    target,
                    data=examples,
                    evaluators=[route_correct, tool_correct, event_protocol_correct, overall_pass],
                    experiment_prefix=prefix,
                    max_concurrency=1,
                    num_repetitions=1,
                    metadata={
                        "ark_model": settings.model_name,
                        "ark_base_url": settings.model_endpoint,
                        "jev_enabled": settings.jev_enabled,
                        "dataset_source": "tests/evals/real_model/master.jsonl",
                    },
                )

            rows = []
            passed = 0
            for row in results:
                output = row["run"].outputs or {}
                is_pass = output.get("status") == "pass"
                passed += int(is_pass)
                rows.append({
                    "id": row["example"].inputs.get("id"),
                    "category": row["example"].inputs.get("category"),
                    "status": output.get("status"),
                    "problems": output.get("problems"),
                    "actual": output.get("actual"),
                })

            summary = {
                "experiment_name": results.experiment_name,
                "experiment_url": results.url,
                "cases": len(rows),
                "passed": passed,
                "failed": len(rows) - passed,
            }
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            if args.out:
                args.out.write_text(
                    json.dumps({"summary": summary, "results": rows}, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
        finally:
            server.should_exit = True

    return 0 if rows and passed == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
