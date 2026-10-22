"""Unit tests for Read quality-plane helpers (no live LLM / DeepEval judge)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_service_agent.evals import deepeval_read as dr

ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE = ROOT / "tests" / "evals" / "knowledge"


def test_load_default_phase1_categories():
    cases = dr.load_knowledge_cases(
        KNOWLEDGE,
        categories=("direct_fact", "paraphrase", "multi_hop"),
        answerable_only=True,
    )
    cats = {c["category"] for c in cases}
    assert cats == {"direct_fact", "paraphrase", "multi_hop"}
    assert len(cases) == 26 + 16 + 21
    assert all(c.get("answerable", True) for c in cases)


def test_load_ids_and_limit(tmp_path):
    p = tmp_path / "direct_fact.jsonl"
    rows = [
        {
            "id": "KB-T1",
            "category": "direct_fact",
            "question": "Q1",
            "answerable": True,
            "expected_facts": ["a"],
        },
        {
            "id": "KB-T2",
            "category": "direct_fact",
            "question": "Q2",
            "answerable": True,
            "expected_facts": ["b"],
        },
    ]
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    got = dr.load_knowledge_cases(tmp_path, categories=("direct_fact",), ids={"KB-T2"})
    assert [c["id"] for c in got] == ["KB-T2"]
    limited = dr.load_knowledge_cases(tmp_path, categories=("direct_fact",), limit=1)
    assert len(limited) == 1


def test_l0_rejects_empty_tech_greeting():
    assert dr.check_l0("")[0] is False
    assert dr.check_l0("抱歉，系统遇到技术问题，请稍后再试。")[0] is False
    assert dr.check_l0("您好！有什么可以帮您？")[0] is False
    ok, reason = dr.check_l0("JoyAir A1 炸篮容量为 4.5L。（来源：JoyAir_A1智能空气炸锅.md）")
    assert ok is True
    assert reason == "ok"


def test_l1_facts_requires_numbers_and_tokens():
    answer = "JoyAir A1 Pro 的额定功率是 1700 W，整机保修 12 个月。"
    ok, hit, miss = dr.check_l1_facts(answer, ["JoyAir A1 Pro 额定功率 1700 W"])
    assert ok is True
    assert miss == []
    assert hit

    ok2, _, miss2 = dr.check_l1_facts(answer, ["JoyAir A1 Pro 额定功率 2100 W"])
    assert ok2 is False
    assert miss2


def test_l1_facts_chinese_phrasing_variant():
    """Multi-hop style: keyword overlap across split sentences (not full NL entailment)."""
    answer = (
        "JoyClean V1 的 HEPA 滤网建议每 6 个月更换一次。"
        "滤网属于易耗件，正常损耗不在保修范围内。"
    )
    ok, hit, miss = dr.check_l1_facts(
        answer,
        ["V1 HEPA 滤网每 6 个月更换", "滤网等易耗件不在保修范围"],
    )
    assert ok is True, (hit, miss)
    assert miss == []


def test_l1_abstention_for_unanswerable():
    case = {"answerable": False}
    good = dr.check_l1(case, "知识库未查询到槟城上门维修的明确信息，建议以官方渠道确认为准。")
    assert good["pass"] is True
    bad = dr.check_l1(case, "可以，槟城全部支持免费上门维修。")
    assert bad["pass"] is False


def test_decide_pass_phase1_faithfulness_hard():
    l1 = {"pass": True}
    metrics_ok = {
        "FaithfulnessMetric": {"score": 0.9, "success": True},
        "GEval_Citations": {"score": 0.2, "success": False},
    }
    ok, reasons = dr.decide_pass(
        l0_ok=True,
        l1=l1,
        metrics=metrics_ok,
        citations_hard=False,
        must_cite_source=True,
    )
    assert ok is True
    assert reasons == []

    ok2, reasons2 = dr.decide_pass(
        l0_ok=True,
        l1=l1,
        metrics=metrics_ok,
        citations_hard=True,
        must_cite_source=True,
    )
    assert ok2 is False
    assert "citations_fail" in reasons2


def test_build_input_messages_includes_conversation_context():
    case = {
        "question": "还是连不上",
        "conversation_context": ["我的 JoyClean S1 连不上 Wi-Fi。", "我已经重启过设备了。"],
    }
    msgs = dr.build_input_messages(case)
    assert msgs[-1].content == "还是连不上"
    assert any(getattr(m, "content", None) == "我的 JoyClean S1 连不上 Wi-Fi。" for m in msgs)


def test_summarize_counts():
    summary = dr.summarize(
        [
            {"pass": True, "category": "direct_fact"},
            {"pass": False, "category": "direct_fact"},
            {"pass": True, "category": "multi_hop", "error": None},
        ]
    )
    assert summary["total"] == 3
    assert summary["pass"] == 2
    assert summary["fail"] == 1
    assert summary["by_category"]["direct_fact"]["pass"] == 1
