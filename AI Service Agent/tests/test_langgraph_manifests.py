import json
from pathlib import Path


def _load_manifest(name: str) -> dict:
    return json.loads(Path(__file__).resolve().parents[1].joinpath(name).read_text(encoding="utf-8"))


def test_production_manifest_only_exposes_root_agent():
    manifest = _load_manifest("langgraph.json")

    assert list(manifest["graphs"]) == ["agent"]
    assert manifest["graphs"]["agent"].endswith("harness/graph.py:app")


def test_studio_manifest_only_adds_debugging_subgraphs():
    production = _load_manifest("langgraph.json")
    studio = _load_manifest("langgraph.studio.json")

    assert set(studio["graphs"]) == {"agent", "read_graph", "mutation_graph", "high_risk_graph", "fallback"}
    assert all(studio["graphs"][name] == path for name, path in production["graphs"].items())
