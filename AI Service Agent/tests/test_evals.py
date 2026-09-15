import json

from ai_service_agent.evals import runner

BASE = {"expected_policy": "allow", "requires_human": False, "reason": "", "metadata": {"category": "read", "risk": "low"}}
CASE_READ = {"id": "T-01", "input": "查订单 ORD-1", "expected_capability": "order", "expected_risk_level": "read", "expected_graph": "read", **BASE}
CASE_MUT = {"id": "T-02", "input": "改收货地址", "expected_capability": "order", "expected_risk_level": "mutation", "expected_graph": "mutation", **BASE}
CASE_HANDOFF = {"id": "T-03", "input": "麻烦帮我转人工客服", "expected_capability": "unknown", "expected_risk_level": "read", "expected_graph": "handoff", **BASE}
CASE_HANDOFF_PENDING = {"id": "T-04", "input": "气死了！垃圾！", "expected_capability": "unknown", "expected_risk_level": "read", "expected_graph": "handoff", **BASE}


def _write(tmp_path, cases):
    p = tmp_path / "master.jsonl"
    p.write_text("\n".join(json.dumps(c, ensure_ascii=False) for c in cases), encoding="utf-8")
    return tmp_path


def test_load_cases_rejects_missing_keys(tmp_path):
    bad = {"id": "X", "input": "hi"}
    d = _write(tmp_path, [bad])
    try:
        runner.load_cases(d / "master.jsonl")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_policy_dimension_pass_and_skip(tmp_path):
    d = _write(tmp_path, [CASE_READ, CASE_MUT])
    res = runner.evaluate_policy(runner.load_cases(d / "master.jsonl"))
    assert res[0]["id"] == "T-01" and res[0]["status"] == "pass"
    assert res[1]["id"] == "T-02" and res[1]["status"] == "skip"
    assert res[1]["skip_reason"] == "mutation_pending"


def test_handoff_dimension_hit_miss_and_no_false_trigger(tmp_path):
    d = _write(tmp_path, [CASE_READ, CASE_HANDOFF, CASE_HANDOFF_PENDING])
    res = runner.evaluate_handoff(runner.load_cases(d / "master.jsonl"))
    by_id = {r["id"]: r for r in res}
    assert by_id["T-01"]["status"] == "pass"        # 非 handoff case 不得误触发
    assert by_id["T-03"]["status"] == "pass"        # 关键词命中 -> 触发
    assert by_id["T-04"]["status"] == "skip"        # 规则未实现（Guard/情绪，OQ-3）
    assert by_id["T-04"]["skip_reason"] == "handoff_rule_pending"


def test_summarize_and_determinism(tmp_path):
    d = _write(tmp_path, [CASE_READ, CASE_MUT])
    r1 = runner.summarize(runner.evaluate_policy(runner.load_cases(d / "master.jsonl")))
    r2 = runner.summarize(runner.evaluate_policy(runner.load_cases(d / "master.jsonl")))
    assert r1 == r2
    assert r1["total"] == 2 and r1["pass"] == 1 and r1["fail"] == 0
    assert r1["skip"] == {"mutation_pending": 1}
