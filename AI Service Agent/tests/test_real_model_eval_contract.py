import json
from pathlib import Path

from ai_service_agent.graph.route_policy import resolve_route_policy


DATASET = Path(__file__).resolve().parents[1] / "tests" / "evals" / "real_model" / "master.jsonl"


def load_dataset(path: Path = DATASET) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_real_model_dataset_size_and_unique_ids():
    cases = load_dataset()

    assert 30 <= len(cases) <= 40
    assert len({case["id"] for case in cases}) == len(cases)


def test_real_model_dataset_contract():
    cases = load_dataset()
    base_required = {
        "id",
        "execution_mode",
        "category",
        "user_id",
        "input",
        "expected_route_intent",
        "expected_target_graph",
    }
    graph_required = base_required | {"expected_route_intent", "expected_target_graph", "expected_events"}

    for case in cases:
        assert base_required <= case.keys(), case["id"]
        if case["execution_mode"] in {"graph", "handoff"}:
            assert graph_required <= case.keys(), case["id"]
        assert case["execution_mode"] in {"router", "graph", "handoff"}, case["id"]
        if case["expected_route_intent"] is not None:
            policy = resolve_route_policy(case["expected_route_intent"])
            assert policy.target_graph == case["expected_target_graph"], case["id"]


def test_real_model_dataset_covers_required_layers():
    categories = {case["category"] for case in load_dataset()}

    assert {"router_intent", "router_multi_turn", "read_order", "read_shipment", "read_faq"} <= categories
    assert {"mutation_phone", "mutation_address", "mutation_clarify"} <= categories
    assert {"high_risk_refund", "high_risk_cancel", "high_risk_precheck"} <= categories
