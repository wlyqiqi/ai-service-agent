from pathlib import Path

from ai_service_agent.evals.context_policy import load_cases, run_dataset


ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "tests" / "evals" / "context_harness" / "context_policy.jsonl"


def test_context_policy_dataset_matches_current_harness():
    report = run_dataset(DATASET)

    assert report["dataset"] == str(DATASET)
    assert report["summary"] == {"total": 20, "pass": 20, "fail": 0}


def test_context_policy_dataset_covers_all_policies():
    cases = load_cases(DATASET)
    policies = {case["policy"] for case in cases}

    assert policies == {"router", "fallback", "read", "select"}
