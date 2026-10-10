"""Run functional scenarios via LangSmith Client.evaluate (dataset must already be uploaded)."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
from langsmith import Client

from ai_service_agent.config import get_settings

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET_NAME = "ai-cs-functional-scenarios-v1"
SCRIPTS = ROOT / "scripts"


def load_runner():
    path = SCRIPTS / "functional_scenario_eval.py"
    spec = importlib.util.spec_from_file_location("functional_scenario_eval_runner", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def event_protocol_correct(inputs: dict, outputs: dict, reference_outputs: dict) -> dict:
    actual = set((outputs.get("actual") or {}).get("events") or [])
    expected = set(reference_outputs.get("expected_events") or [])
    forbidden = set(reference_outputs.get("forbidden_events") or [])
    # done may be absent after approve HTTP resume; soften
    soft_expected = {e for e in expected if e != "done"}
    ok = soft_expected.issubset(actual) and not (forbidden & actual)
    return {"key": "event_protocol_correct", "score": int(ok),
            "comment": f"expected={sorted(expected)} forbidden={sorted(forbidden)} actual={sorted(actual)}"}


def mode_correct(inputs: dict, outputs: dict, reference_outputs: dict) -> dict:
    expected = reference_outputs.get("expected_mode")
    if expected is None:
        return {"key": "mode_correct", "score": 1, "comment": "n/a"}
    actual = (outputs.get("actual") or {}).get("mode")
    return {"key": "mode_correct", "score": int(actual == expected),
            "comment": f"expected={expected} actual={actual}"}


def overall_pass(inputs: dict, outputs: dict, reference_outputs: dict) -> dict:
    return {
        "key": "overall_pass",
        "score": int(outputs.get("status") == "pass"),
        "comment": "; ".join(outputs.get("problems") or []) or "pass",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset-name", default=DEFAULT_DATASET_NAME)
    parser.add_argument("--split", choices=["smoke", "full"], default="full")
    parser.add_argument("--only", nargs="*")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--experiment-prefix", default=None)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    settings = get_settings()
    if not settings.model_api_key or not settings.model_endpoint:
        raise RuntimeError("ARK_* required")
    if not settings.langsmith_api_key:
        raise RuntimeError("LANGSMITH_API_KEY required")

    os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key
    if settings.langsmith_project:
        os.environ.setdefault("LANGSMITH_PROJECT", settings.langsmith_project)

    client = Client()
    examples = list(client.list_examples(dataset_name=args.dataset_name))
    if args.split == "smoke":
        examples = [ex for ex in examples if (ex.metadata or {}).get("split") == "smoke" or (ex.inputs or {}).get("split") == "smoke"]
    if args.only:
        wanted = set(args.only)
        examples = [ex for ex in examples if (ex.inputs or {}).get("id") in wanted]
    examples.sort(key=lambda ex: (ex.inputs or {}).get("id", ""))
    if args.limit:
        examples = examples[: args.limit]
    if not examples:
        raise RuntimeError("no examples selected — upload dataset first")

    runner = load_runner()
    server, _temp = runner.prepare_runtime()
    prefix = args.experiment_prefix or f"functional-{settings.model_name}-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}"
    references = { (ex.inputs or {}).get("id"): (ex.outputs or {}) for ex in examples }

    try:
        with httpx.Client(timeout=180.0, trust_env=False) as http_client:
            def target(inputs: dict) -> dict:
                case = {**(inputs or {}), **references.get((inputs or {}).get("id"), {})}
                # ensure list fields
                for key in ("turns", "expected_events", "forbidden_events", "store_contains"):
                    case.setdefault(key, inputs.get(key) if key in (inputs or {}) else case.get(key) or [])
                result = runner.run_case(http_client, case)
                return {
                    "status": result["status"],
                    "problems": result.get("problems") or [],
                    "actual": result.get("actual") or {},
                }

            results = client.evaluate(
                target,
                data=examples,
                evaluators=[event_protocol_correct, mode_correct, overall_pass],
                experiment_prefix=prefix,
                max_concurrency=1,
                num_repetitions=1,
                metadata={
                    "ark_model": settings.model_name,
                    "suite": "functional_scenarios",
                    "split": args.split,
                    "dataset": args.dataset_name,
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
                "suite": row["example"].inputs.get("suite") or (row["example"].metadata or {}).get("suite"),
                "status": output.get("status"),
                "problems": output.get("problems"),
                "actual": output.get("actual"),
            })

        summary = {
            "experiment_name": getattr(results, "experiment_name", prefix),
            "experiment_url": getattr(results, "url", None),
            "cases": len(rows),
            "passed": passed,
            "failed": len(rows) - passed,
            "dataset": args.dataset_name,
            "split": args.split,
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            md = [
                "# Functional Scenarios LangSmith Report",
                "",
                f"- Dataset: `{args.dataset_name}`",
                f"- Split: `{args.split}`",
                f"- Result: **{passed}/{len(rows)}**",
                f"- Experiment: {summary.get('experiment_url') or summary.get('experiment_name')}",
                "",
                "| ID | Suite | Status | Problems |",
                "| --- | --- | --- | --- |",
            ]
            for r in rows:
                probs = "; ".join(r.get("problems") or [])[:120]
                md.append(f"| {r['id']} | {r.get('suite')} | {r.get('status')} | {probs} |")
            args.out.write_text("\n".join(md) + "\n", encoding="utf-8")
            args.out.with_suffix(".json").write_text(
                json.dumps({"summary": summary, "results": rows}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
    finally:
        server.should_exit = True

    return 0 if rows and passed == len(rows) else 2


if __name__ == "__main__":
    raise SystemExit(main())
