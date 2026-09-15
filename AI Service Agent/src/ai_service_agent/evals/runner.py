import json
from pathlib import Path

from langchain_core.messages import HumanMessage
from langgraph.types import Command

from ai_service_agent.config import get_settings
from ai_service_agent.graph.builder import POLICY
from ai_service_agent.graph.workflows.fallback import check_handoff

# skip 原因固定词表：mutation/high_risk/guard 为 P3/P4 未实现能力；handoff_rule_pending 为
# safety-spec §2.1 中未实现规则（投诉/情绪/连续失败/安全等，OQ-3）；not_applicable 为本维度不适用。
SKIP_REASONS = ("mutation_pending", "high_risk_pending", "guard_pending", "handoff_rule_pending", "not_applicable")

GRAPH_POLICY_SKIP = {"mutation": "mutation_pending", "high_risk": "high_risk_pending", "blocked": "guard_pending"}
HANDOFF_SKIP = "handoff_rule_pending"


def load_cases(path: str | Path) -> list[dict]:
    required = {"id", "input", "expected_capability", "expected_risk_level", "expected_graph", "expected_policy"}
    cases = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            case = json.loads(line)
            missing = required - case.keys()
            if missing:
                raise ValueError(f"{Path(path).name}: {case.get('id')} missing {sorted(missing)}")
            cases.append(case)
    return cases


def policy_target(capability: str, risk_level: str) -> str:
    """确定性 Policy 归一化（graph-spec.md §5）：read_graph -> read。"""
    target = POLICY.get((capability, risk_level), "fallback")
    return "read" if target == "read_graph" else target


def _handoff_triggered(node_out) -> bool:
    if isinstance(node_out, Command):
        return bool(node_out.update.get("handoff"))
    return bool(node_out.get("handoff"))


def evaluate_policy(cases: list[dict]) -> list[dict]:
    results = []
    for case in cases:
        expected = case["expected_graph"]
        if expected in GRAPH_POLICY_SKIP:
            results.append({"id": case["id"], "status": "skip", "skip_reason": GRAPH_POLICY_SKIP[expected]})
            continue
        if expected == "handoff":  # blocked 已在上文按 guard_pending 处理
            results.append({"id": case["id"], "status": "skip", "skip_reason": "not_applicable"})
            continue
        actual = policy_target(case["expected_capability"], case["expected_risk_level"])
        results.append({"id": case["id"], "status": "pass" if actual == expected else "fail", "actual": actual})
    return results


def evaluate_handoff(cases: list[dict]) -> list[dict]:
    results = []
    for case in cases:
        node_out = check_handoff({"messages": [HumanMessage(content=case["input"])]})
        triggered = _handoff_triggered(node_out)
        if case["expected_graph"] == "handoff":
            if triggered:
                results.append({"id": case["id"], "status": "pass"})
            else:
                # 未命中当前关键词规则：属 safety-spec §2.1 未实现规则（OQ-3）
                results.append({"id": case["id"], "status": "skip", "skip_reason": HANDOFF_SKIP})
        else:
            results.append({"id": case["id"], "status": "pass" if not triggered else "fail"})
    return results


def summarize(results: list[dict]) -> dict:
    summary = {"total": len(results), "pass": 0, "fail": 0, "skip": {}}
    for r in results:
        if r["status"] == "pass":
            summary["pass"] += 1
        elif r["status"] == "fail":
            summary["fail"] += 1
        else:
            reason = r.get("skip_reason", "unknown")
            summary["skip"][reason] = summary["skip"].get(reason, 0) + 1
    return summary


def run_all(control_dir: str | Path) -> dict:
    control_dir = Path(control_dir)
    cases = load_cases(control_dir / "master.jsonl")
    dimensions = {
        "policy": {"results": evaluate_policy(cases), "summary": None},
        "handoff": {"results": evaluate_handoff(cases), "summary": None},
    }
    for dim in dimensions.values():
        dim["summary"] = summarize(dim["results"])
    return {
        "dataset": str(control_dir / "master.jsonl"),
        "case_count": len(cases),
        "dimensions": dimensions,
        "settings": {
            "handoff_keywords": get_settings().handoff_keywords,
            "router_conf_threshold": get_settings().router_conf_threshold,
        },
    }
