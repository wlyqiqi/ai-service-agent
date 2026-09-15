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

from pathlib import Path

CASE_FALLBACK = {"id": "T-05", "input": "随便聊聊", "expected_capability": "unknown", "expected_risk_level": "read", "expected_graph": "fallback", **BASE}


def test_routing_dimension_fixtures(tmp_path):
    d = _write(tmp_path, [CASE_READ, CASE_FALLBACK, CASE_HANDOFF, CASE_HANDOFF_PENDING])
    res = runner.evaluate_routing(runner.load_cases(d / "master.jsonl"))
    by_id = {r["id"]: r for r in res}
    assert by_id["T-01"]["status"] == "pass"            # read -> ReadGraph 完成
    assert by_id["T-05"]["status"] == "pass"            # fallback -> explain 完成
    assert by_id["T-03"]["status"] == "pass"            # handoff 关键词命中 -> interrupt
    assert by_id["T-04"]["status"] == "skip"
    assert by_id["T-04"]["skip_reason"] == "handoff_rule_pending"


def test_run_all_includes_three_dimensions(tmp_path):
    d = _write(tmp_path, [CASE_READ, CASE_MUT])
    report = runner.run_all(d)
    assert set(report["dimensions"].keys()) == {"policy", "handoff", "routing"}
    assert report["case_count"] == 2
    assert report["dimensions"]["routing"]["summary"]["pass"] == 1
    assert report["dimensions"]["routing"]["summary"]["skip"] == {"mutation_pending": 1}
    assert report["settings"]["handoff_keywords"]  # settings 快照存在


def test_cli_runs_on_real_control_dataset(tmp_path):
    import os
    import subprocess
    import sys

    repo = Path(__file__).resolve().parents[1]
    out = tmp_path / "report.json"
    env = {**os.environ, "PYTHONPATH": str(repo / "src")}
    proc = subprocess.run(
        [
            sys.executable, "-m", "ai_service_agent.evals",
            "--data", str(repo / "tests" / "evals" / "control"),
            "--out", str(out),
        ],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["case_count"] == 100
    assert report["dimensions"]["policy"]["summary"]["pass"] == 33
