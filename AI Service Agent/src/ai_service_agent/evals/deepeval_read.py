"""Read 质量面评测（DeepEval）：独立于 control 确定性 runner。

数据：tests/evals/knowledge/*.jsonl
被测：ReadGraph + LLM_*（SUT）
Judge：DEEPEVAL_JUDGE_*（缺省回落 LLM_*，并打印警告）
  Judge 默认开启 thinking（enable_thinking + reasoning_effort=high）；SUT/runtime LLM 节点保持关闭。

判定：
  L0 坏答短路（硬）→ L1 expected_facts / 拒答启发式（硬）
  → Faithfulness（硬）
  → AnswerRelevancy / G-Eval Tone / G-Eval Citations（软；默认跳过以省 Judge 调用）
  → Citations 可在 --citations-hard 且 must_cite_source 时升为硬门（即使未开 --soft-metrics）

用法：
  # 硬门扫面（默认）：L0/L1/Faithfulness；按类分批
  python -m ai_service_agent.evals.deepeval_read --category direct_fact \\
    --out tmp/reports/deepeval_read_direct_fact.json
  # 补跑软指标
  python -m ai_service_agent.evals.deepeval_read --soft-metrics --limit 10 \\
    --out tmp/reports/deepeval_read_soft_pilot.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import unicodedata
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from langchain_core.messages import AIMessage, HumanMessage

# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATA_DIR = ROOT / "tests" / "evals" / "knowledge"
DEFAULT_CATEGORIES = ("direct_fact", "paraphrase", "multi_hop")
PHASE1_CATEGORIES = DEFAULT_CATEGORIES

TECH_ERROR_MARKERS = ("技术问题", "运行时错误", "RUNTIME_ERROR")
GREETING_ONLY_RE = re.compile(
    r"^(您好|你好|嗨|哈喽|hello|hi)[！!。.\s]*"
    r"(有什么可以帮|需要什么帮助|请问有什么|很高兴为您)[^。.!！？?]{0,40}"
    r"[。.!！？?]?$",
    re.IGNORECASE,
)
ABSTENTION_MARKERS = (
    "未查询到",
    "暂无",
    "没有相关",
    "无法确认",
    "不确定",
    "知识库暂不可用",
    "检索结果：空",
    "检索结果：不可用",
    "建议以官方",
    "请以官方",
    "无法从现有",
    "现有资料未",
)

_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
_ENG_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+/-]{1,}")
_ZH_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")


# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------


def load_knowledge_cases(
    data_dir: Path | str,
    *,
    categories: tuple[str, ...] | list[str] | None = None,
    ids: set[str] | None = None,
    limit: int | None = None,
    answerable_only: bool | None = None,
) -> list[dict[str, Any]]:
    """Load knowledge JSONL cases (category files or master.jsonl)."""
    root = Path(data_dir)
    if not root.exists():
        raise FileNotFoundError(f"knowledge data dir not found: {root}")

    wanted = {c.strip() for c in (categories or ()) if c and c.strip()}
    rows: list[dict[str, Any]] = []

    if wanted:
        paths = []
        for cat in wanted:
            p = root / f"{cat}.jsonl"
            if not p.exists():
                raise FileNotFoundError(f"category file missing: {p}")
            paths.append(p)
    else:
        master = root / "master.jsonl"
        if not master.exists():
            raise FileNotFoundError(f"master.jsonl missing under {root}")
        paths = [master]

    seen: set[str] = set()
    for path in paths:
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            raw = line.strip()
            if not raw:
                continue
            try:
                case = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: invalid JSON") from exc
            cid = str(case.get("id") or "").strip()
            if not cid:
                raise ValueError(f"{path}:{line_no}: missing id")
            if cid in seen:
                continue
            if ids is not None and cid not in ids:
                continue
            if wanted and case.get("category") not in wanted:
                continue
            if answerable_only is True and not case.get("answerable", True):
                continue
            if answerable_only is False and case.get("answerable", True):
                continue
            if "question" not in case:
                raise ValueError(f"{path}:{line_no}: missing question")
            seen.add(cid)
            rows.append(case)
            if limit is not None and len(rows) >= limit:
                return rows
    return rows


# ---------------------------------------------------------------------------
# L0 / L1
# ---------------------------------------------------------------------------


def check_l0(actual_output: str) -> tuple[bool, str]:
    """Hard gate: empty / tech-error / pure greeting → fail (no judge)."""
    text = (actual_output or "").strip()
    if not text:
        return False, "empty_answer"
    for marker in TECH_ERROR_MARKERS:
        if marker in text:
            return False, f"tech_error:{marker}"
    compact = re.sub(r"\s+", "", text)
    if len(compact) <= 40 and GREETING_ONLY_RE.match(text.strip()):
        return False, "greeting_only"
    # Ultra-short non-answer (e.g. bare 您好)
    if len(compact) <= 8 and re.fullmatch(r"(您好|你好|嗨|哈喽|hello|hi)[！!。.]?", text.strip(), re.I):
        return False, "greeting_only"
    return True, "ok"


def _zh_pieces(text: str) -> list[str]:
    """Chinese keyword pieces: short runs kept whole; longer runs → bigrams/trigrams."""
    pieces: list[str] = []
    for run in _ZH_RUN_RE.findall(text or ""):
        if len(run) <= 6:
            pieces.append(run)
            continue
        for n in (3, 2):
            for i in range(len(run) - n + 1):
                pieces.append(run[i : i + n])
    # Prefer longer / more distinctive pieces; drop ultra-common glue bigrams lightly
    # by de-duplicating while preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for p in pieces:
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
    return out


def _fact_signals(fact: str) -> tuple[list[str], list[str]]:
    """Return (numbers, other_signals) for expected_fact matching."""
    text = fact or ""
    numbers = _NUMBER_RE.findall(text)
    others = _ENG_TOKEN_RE.findall(text) + _zh_pieces(text)
    # Drop signals that are pure duplicates of numbers already checked.
    others = [t for t in others if t not in numbers]
    return numbers, others


def _normalize_match_text(text: str) -> str:
    """Normalize harmless formatting differences before deterministic matching."""
    normalized = unicodedata.normalize("NFKC", text or "").casefold()
    normalized = re.sub(r"\s+", "", normalized)
    normalized = re.sub(r"[‐‑‒–—−~～]", "-", normalized)
    normalized = re.sub(r"(?<=\d)至(?=\d)", "-", normalized)
    return normalized


def _fact_label(fact: str | dict[str, Any]) -> str:
    if isinstance(fact, dict):
        return str(fact.get("label") or "").strip()
    return str(fact).strip()


def _condition_hit(condition: str | dict[str, Any], answer_norm: str) -> bool:
    if isinstance(condition, str):
        needle = _normalize_match_text(condition)
        return bool(needle) and needle in answer_norm
    if not isinstance(condition, dict):
        raise TypeError("structured fact condition must be str or dict")

    any_of = condition.get("any_of")
    if any_of is not None:
        if not isinstance(any_of, list) or not any_of:
            raise ValueError("any_of must be a non-empty list")
        if any(_condition_hit(candidate, answer_norm) for candidate in any_of):
            return True

    regex = condition.get("regex")
    if regex is not None:
        if not isinstance(regex, str) or not regex:
            raise ValueError("regex must be a non-empty string")
        if re.search(regex, answer_norm, re.IGNORECASE | re.DOTALL):
            return True

    all_of = condition.get("all_of")
    if all_of is not None:
        if not isinstance(all_of, list) or not all_of:
            raise ValueError("all_of must be a non-empty list")
        return all(_condition_hit(item, answer_norm) for item in all_of)

    return False


def _number_hit(number: str, answer_norm: str) -> bool:
    token = _normalize_match_text(str(number))
    if not token:
        return False
    # Exact numeric token boundaries prevent ``1`` from matching ``1.75`` or ``10``.
    pattern = rf"(?<![\d.]){re.escape(token)}(?![\d.])"
    return re.search(pattern, answer_norm) is not None


def _structured_fact_hit(fact: dict[str, Any], actual_output: str) -> bool:
    answer_norm = _normalize_match_text(actual_output)

    for number in fact.get("numbers") or []:
        if not _number_hit(number, answer_norm):
            return False

    for pattern in fact.get("must_not") or []:
        normalized_pattern = _normalize_match_text(str(pattern))
        if re.search(normalized_pattern, answer_norm, re.IGNORECASE | re.DOTALL):
            return False

    all_of = fact.get("all_of")
    any_of = fact.get("any_of")
    if all_of is not None:
        if not isinstance(all_of, list) or not all_of:
            raise ValueError("structured fact all_of must be a non-empty list")
        if not all(_condition_hit(condition, answer_norm) for condition in all_of):
            return False
    if any_of is not None:
        if not isinstance(any_of, list) or not any_of:
            raise ValueError("structured fact any_of must be a non-empty list")
        if not any(_condition_hit(condition, answer_norm) for condition in any_of):
            return False

    if all_of is None and any_of is None:
        label = _normalize_match_text(_fact_label(fact))
        return bool(label) and label in answer_norm
    return True


def fact_hit(fact: str | dict[str, Any], actual_output: str) -> bool:
    """Keyword/number heuristic for one expected_fact against the answer.

    Chinese facts are matched via short runs / character n-grams so that
    ``滤网等易耗件不在保修范围`` can hit answers that phrase the same idea
    with intervening words (e.g. 「易耗件……不在保修范围内」). Structured
    facts provide explicit synonym groups, exact numbers, and contradiction
    guards while retaining the string heuristic for older datasets.
    """
    if isinstance(fact, dict):
        return _structured_fact_hit(fact, actual_output or "")
    fact = str(fact or "")
    answer = actual_output or ""
    answer_norm = answer.casefold()
    numbers, others = _fact_signals(fact)
    if not numbers and not others:
        return (fact or "") in answer

    for num in numbers:
        if num not in answer and num.casefold() not in answer_norm:
            return False

    if not others:
        return bool(numbers)

    hits = 0
    for tok in others:
        if tok.casefold() in answer_norm or tok in answer:
            hits += 1
    # Majority of non-numeric signals (at least 1).
    need = max(1, (len(others) + 1) // 2)
    return hits >= need


def check_l1_facts(
    actual_output: str, expected_facts: list[str | dict[str, Any]] | None
) -> tuple[bool, list[str], list[str]]:
    """Hard gate for answerable cases: each expected_fact should hit."""
    facts = [f for f in (expected_facts or []) if _fact_label(f)]
    if not facts:
        return True, [], []
    hit: list[str] = []
    miss: list[str] = []
    for fact in facts:
        label = _fact_label(fact)
        if fact_hit(fact, actual_output):
            hit.append(label)
        else:
            miss.append(label)
    return (len(miss) == 0), hit, miss


def check_l1_abstention(actual_output: str) -> tuple[bool, str]:
    """For answerable=false: require an abstention / insufficient-info signal."""
    text = actual_output or ""
    for marker in ABSTENTION_MARKERS:
        if marker in text:
            return True, marker
    return False, "no_abstention_signal"


def check_l1(case: dict[str, Any], actual_output: str) -> dict[str, Any]:
    if case.get("answerable", True):
        ok, hit, miss = check_l1_facts(actual_output, case.get("expected_facts"))
        return {
            "pass": ok,
            "mode": "expected_facts",
            "hit": hit,
            "miss": miss,
            "reason": "ok" if ok else f"missing_facts:{len(miss)}",
        }
    ok, reason = check_l1_abstention(actual_output)
    return {
        "pass": ok,
        "mode": "abstention",
        "hit": [reason] if ok else [],
        "miss": [] if ok else [reason],
        "reason": "ok" if ok else reason,
    }


# ---------------------------------------------------------------------------
# ReadGraph execution
# ---------------------------------------------------------------------------


def last_ai_content(messages: list[Any]) -> str:
    for msg in reversed(messages):
        if getattr(msg, "type", None) == "ai" and getattr(msg, "content", None):
            return str(msg.content)
    return ""


def retrieval_chunks(messages: list[Any]) -> list[str]:
    chunks: list[str] = []
    for msg in messages:
        if getattr(msg, "type", None) != "tool":
            continue
        content = getattr(msg, "content", None)
        if not content:
            continue
        text = str(content)
        if "检索结果" in text or "参考来源" in text:
            chunks.append(text)
    return chunks


def build_input_messages(case: dict[str, Any]) -> list[Any]:
    """Map conversation_context + question → LangChain messages."""
    msgs: list[Any] = []
    for i, turn in enumerate(case.get("conversation_context") or []):
        text = str(turn).strip()
        if not text:
            continue
        # Alternate user turns; insert a light stub AI ack so multi-turn reads naturally.
        msgs.append(HumanMessage(content=text))
        msgs.append(AIMessage(content="好的，我记下了。"))
        _ = i
    msgs.append(HumanMessage(content=str(case["question"])))
    return msgs


async def run_read_graph(case: dict[str, Any]) -> tuple[str, list[str]]:
    from ai_service_agent.config import make_read_model
    from ai_service_agent.graph.agents.read_agent import make_read_graph
    from ai_service_agent.tools.kb import kb_search

    graph = make_read_graph(make_read_model(), [kb_search])
    out = await graph.ainvoke(
        {
            "messages": build_input_messages(case),
            "user_id": "deepeval-read",
            "session_id": f"deepeval-read-{case.get('id', 'x')}",
            "context": {},
        }
    )
    messages = out.get("messages") or []
    actual = last_ai_content(messages)
    ctx = retrieval_chunks(messages)
    if not ctx:
        # Faithfulness needs retrieval_context; fall back to a direct kb call.
        try:
            fallback = kb_search.invoke({"text": str(case["question"])})
            if fallback:
                ctx = [str(fallback)]
        except Exception:
            pass
    return actual, ctx


# ---------------------------------------------------------------------------
# Judge + metrics
# ---------------------------------------------------------------------------


def _load_env() -> dict[str, str | None]:
    file_env = dotenv_values(ROOT / ".env")
    # Process env wins over .env file for CI / overrides.
    merged: dict[str, str | None] = dict(file_env)
    for key in (
        "LLM_API_KEY",
        "LLM_BASE_URL",
        "LLM_MODEL",
        "LLM_AUTH_HEADER",
        "DEEPEVAL_JUDGE_API_KEY",
        "DEEPEVAL_JUDGE_BASE_URL",
        "DEEPEVAL_JUDGE_MODEL",
        "DEEPEVAL_JUDGE_AUTH_HEADER",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
    ):
        if key in os.environ:
            merged[key] = os.environ[key]
    return merged


# Judge thinking on + effort high (OpenAI SDK → extra_body; OfficeACE / SiliconFlow compatible).
JUDGE_THINKING_GENERATION_KWARGS: dict[str, Any] = {
    "extra_body": {
        "enable_thinking": True,
        "reasoning_effort": "high",
    }
}


def make_judge_model():
    """Build DeepEval LocalModel from DEEPEVAL_JUDGE_* (fallback LLM_*).

    Prefer a complete ``DEEPEVAL_JUDGE_*`` profile (e.g. OfficeACE ``deepseek-v4-flash``)
    separate from SiliconFlow runtime ``LLM_*``. Judge enables thinking at
    ``reasoning_effort=high``. Runtime SUT stays ``enable_thinking=False``.
    """
    from deepeval.models import LocalModel

    env = _load_env()
    judge_model = (env.get("DEEPEVAL_JUDGE_MODEL") or "").strip()
    judge_base = (env.get("DEEPEVAL_JUDGE_BASE_URL") or "").strip()
    judge_key = (env.get("DEEPEVAL_JUDGE_API_KEY") or "").strip()
    judge_auth = (env.get("DEEPEVAL_JUDGE_AUTH_HEADER") or "").strip()

    sut_model = (env.get("LLM_MODEL") or "").strip() or "deepseek-v4-flash"
    sut_base = (env.get("LLM_BASE_URL") or "").strip()
    sut_key = (env.get("LLM_API_KEY") or "").strip()
    sut_auth = (env.get("LLM_AUTH_HEADER") or "").strip()

    model = judge_model or sut_model
    base_url = judge_base or sut_base
    api_key = judge_key or sut_key
    auth = judge_auth or sut_auth

    if not judge_model or not judge_base or not judge_key:
        warnings.warn(
            "DEEPEVAL_JUDGE_* incomplete; falling back to LLM_* for judge "
            "(prefer a stronger/separate judge than SUT flash).",
            UserWarning,
            stacklevel=2,
        )

    if not base_url or not api_key:
        raise RuntimeError(
            "Judge credentials missing: set DEEPEVAL_JUDGE_* or LLM_BASE_URL/LLM_API_KEY"
        )

    # DeepEval / OpenAI-compatible clients often read these.
    os.environ.setdefault("OPENAI_API_KEY", api_key)
    os.environ.setdefault("OPENAI_BASE_URL", base_url)

    kwargs: dict[str, Any] = {}
    if auth:
        kwargs["default_headers"] = {"Authorization": auth}

    return LocalModel(
        model=model,
        base_url=base_url,
        api_key=api_key,
        temperature=0,
        generation_kwargs=dict(JUDGE_THINKING_GENERATION_KWARGS),
        **kwargs,
    )


def _metric_result(metric: Any) -> dict[str, Any]:
    return {
        "score": getattr(metric, "score", None),
        "success": bool(getattr(metric, "success", False)),
        "threshold": getattr(metric, "threshold", None),
        "reason": getattr(metric, "reason", None),
    }


def metrics_to_run(*, soft_metrics: bool, citations_hard: bool) -> frozenset[str]:
    """Which DeepEval metrics to invoke (Judge LLM calls).

    Phase-1 hard path always includes Faithfulness. Soft metrics
    (Relevancy / Tone / Citations) are off by default to cut Judge cost.
    ``citations_hard`` still forces Citations even when soft metrics are off.
    """
    out: set[str] = {"FaithfulnessMetric"}
    if soft_metrics:
        out.update({"AnswerRelevancyMetric", "GEval_Tone", "GEval_Citations"})
    elif citations_hard:
        out.add("GEval_Citations")
    return frozenset(out)


def measure_deepeval_metrics(
    *,
    case: dict[str, Any],
    actual_output: str,
    retrieval_context: list[str],
    judge: Any,
    faithfulness_threshold: float,
    relevancy_threshold: float,
    tone_threshold: float,
    citations_threshold: float,
    soft_metrics: bool = False,
    citations_hard: bool = False,
) -> dict[str, Any]:
    from deepeval.metrics import AnswerRelevancyMetric, FaithfulnessMetric, GEval
    from deepeval.test_case import LLMTestCase, SingleTurnParams

    tc = LLMTestCase(
        input=str(case["question"]),
        actual_output=actual_output,
        expected_output=case.get("expected_answer"),
        retrieval_context=retrieval_context or None,
    )
    out: dict[str, Any] = {}
    planned = metrics_to_run(soft_metrics=soft_metrics, citations_hard=citations_hard)
    skip_soft = {"skipped": True, "reason": "soft_metrics_disabled"}

    # L2 Faithfulness (hard) — needs retrieval_context
    if "FaithfulnessMetric" in planned:
        if retrieval_context:
            try:
                m = FaithfulnessMetric(
                    threshold=faithfulness_threshold,
                    model=judge,
                    async_mode=False,
                )
                m.measure(tc)
                out["FaithfulnessMetric"] = _metric_result(m)
            except Exception as exc:  # noqa: BLE001 — report per-metric errors
                out["FaithfulnessMetric"] = {"error": str(exc)}
        else:
            out["FaithfulnessMetric"] = {
                "skipped": True,
                "reason": "no_retrieval_context",
            }

    # L3 Answer Relevancy (soft)
    if "AnswerRelevancyMetric" in planned:
        try:
            m = AnswerRelevancyMetric(
                threshold=relevancy_threshold,
                model=judge,
                async_mode=False,
            )
            m.measure(tc)
            out["AnswerRelevancyMetric"] = _metric_result(m)
        except Exception as exc:  # noqa: BLE001
            out["AnswerRelevancyMetric"] = {"error": str(exc)}
    else:
        out["AnswerRelevancyMetric"] = dict(skip_soft)

    # L4a Tone (soft)
    if "GEval_Tone" in planned:
        try:
            tone = GEval(
                name="Tone",
                criteria=(
                    "Evaluate whether the assistant reply is polite, professional, and "
                    "appropriate for Chengyuan (澄元) customer service in Chinese. "
                    "Friendly greetings, brief policy clarifications, and closing offers "
                    "to help further are acceptable and should not lower the score. "
                    "Lower the score only for rude, commanding, dismissive, or clearly "
                    "unprofessional tone."
                ),
                evaluation_params=[SingleTurnParams.ACTUAL_OUTPUT],
                threshold=tone_threshold,
                model=judge,
                async_mode=False,
            )
            tone.measure(tc)
            out["GEval_Tone"] = _metric_result(tone)
        except Exception as exc:  # noqa: BLE001
            out["GEval_Tone"] = {"error": str(exc)}
    else:
        out["GEval_Tone"] = dict(skip_soft)

    # L4b Citations — skip when no retrieval; soft unless caller hardens
    must_cite = bool(case.get("must_cite_source"))
    if "GEval_Citations" not in planned:
        out["GEval_Citations"] = {**skip_soft, "must_cite_source": must_cite}
    elif not retrieval_context:
        out["GEval_Citations"] = {
            "skipped": True,
            "reason": "no_retrieval_context",
            "must_cite_source": must_cite,
        }
    else:
        try:
            citations = GEval(
                name="Citations",
                criteria=(
                    "Evaluate whether the assistant reply cites or recognizably points "
                    "to retrieved knowledge sources (document filenames, "
                    "（来源：…） markers, policy names, or clear source references). "
                    "If retrieval context is present but the reply has no source cue, "
                    "score low. Mentions of product/policy names that clearly map to "
                    "retrieved docs may count as partial citation."
                ),
                evaluation_params=[
                    SingleTurnParams.ACTUAL_OUTPUT,
                    SingleTurnParams.RETRIEVAL_CONTEXT,
                ],
                threshold=citations_threshold,
                model=judge,
                async_mode=False,
            )
            citations.measure(tc)
            result = _metric_result(citations)
            result["must_cite_source"] = must_cite
            out["GEval_Citations"] = result
        except Exception as exc:  # noqa: BLE001
            out["GEval_Citations"] = {
                "error": str(exc),
                "must_cite_source": must_cite,
            }

    return out


def decide_pass(
    *,
    l0_ok: bool,
    l1: dict[str, Any],
    metrics: dict[str, Any],
    citations_hard: bool,
    must_cite_source: bool,
) -> tuple[bool, list[str]]:
    """Phase-1 pass: L0 ∧ L1 ∧ Faithfulness; optional Citations hard."""
    reasons: list[str] = []
    if not l0_ok:
        reasons.append("l0_fail")
    if not l1.get("pass"):
        reasons.append("l1_fail")

    faith = metrics.get("FaithfulnessMetric") or {}
    if faith.get("skipped"):
        reasons.append("faithfulness_skipped")
    elif "error" in faith:
        reasons.append("faithfulness_error")
    elif faith.get("score") is None or not faith.get("success"):
        reasons.append("faithfulness_fail")

    if citations_hard and must_cite_source:
        cit = metrics.get("GEval_Citations") or {}
        if cit.get("skipped"):
            reasons.append("citations_skipped")
        elif "error" in cit:
            reasons.append("citations_error")
        elif not cit.get("success"):
            reasons.append("citations_fail")

    return (len(reasons) == 0), reasons


# ---------------------------------------------------------------------------
# Batch runner
# ---------------------------------------------------------------------------


def _load_resume(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    rows = data.get("results") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return {}
    return {str(r["id"]): r for r in rows if isinstance(r, dict) and "id" in r}


def _write_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(results)
    passed = sum(1 for r in results if r.get("pass"))
    failed = sum(1 for r in results if r.get("pass") is False)
    errors = sum(1 for r in results if r.get("error"))
    by_cat: dict[str, dict[str, int]] = {}
    for r in results:
        cat = str(r.get("category") or "unknown")
        bucket = by_cat.setdefault(cat, {"total": 0, "pass": 0, "fail": 0})
        bucket["total"] += 1
        if r.get("pass"):
            bucket["pass"] += 1
        elif r.get("pass") is False:
            bucket["fail"] += 1
    return {
        "total": total,
        "pass": passed,
        "fail": failed,
        "error": errors,
        "pass_rate": (passed / total) if total else 0.0,
        "by_category": by_cat,
    }


async def evaluate_case(
    case: dict[str, Any],
    *,
    judge: Any,
    faithfulness_threshold: float,
    relevancy_threshold: float,
    tone_threshold: float,
    citations_threshold: float,
    citations_hard: bool,
    soft_metrics: bool = False,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": case["id"],
        "category": case.get("category"),
        "question": case.get("question"),
        "answerable": case.get("answerable", True),
        "must_cite_source": bool(case.get("must_cite_source")),
        "expected_answer": case.get("expected_answer"),
        "expected_facts": case.get("expected_facts"),
    }
    try:
        actual, ctx = await run_read_graph(case)
    except Exception as exc:  # noqa: BLE001
        row.update(
            {
                "error": f"read_graph:{exc}",
                "pass": False,
                "fail_reasons": ["read_graph_error"],
                "metrics": {},
            }
        )
        return row

    row["actual_output"] = actual
    row["retrieval_chunk_count"] = len(ctx)

    l0_ok, l0_reason = check_l0(actual)
    row["l0"] = {"pass": l0_ok, "reason": l0_reason}
    if not l0_ok:
        row.update(
            {
                "l1": {"pass": False, "reason": "skipped_after_l0"},
                "metrics": {},
                "pass": False,
                "fail_reasons": ["l0_fail"],
            }
        )
        return row

    l1 = check_l1(case, actual)
    row["l1"] = l1

    metrics = measure_deepeval_metrics(
        case=case,
        actual_output=actual,
        retrieval_context=ctx,
        judge=judge,
        faithfulness_threshold=faithfulness_threshold,
        relevancy_threshold=relevancy_threshold,
        tone_threshold=tone_threshold,
        citations_threshold=citations_threshold,
        soft_metrics=soft_metrics,
        citations_hard=citations_hard,
    )
    row["metrics"] = metrics

    ok, reasons = decide_pass(
        l0_ok=l0_ok,
        l1=l1,
        metrics=metrics,
        citations_hard=citations_hard,
        must_cite_source=bool(case.get("must_cite_source")),
    )
    row["pass"] = ok
    row["fail_reasons"] = reasons
    return row


async def run_all(
    *,
    data_dir: Path,
    categories: tuple[str, ...] | None,
    ids: set[str] | None,
    limit: int | None,
    answerable_only: bool | None,
    out_path: Path,
    resume: bool,
    pause_s: float,
    faithfulness_threshold: float,
    relevancy_threshold: float,
    tone_threshold: float,
    citations_threshold: float,
    citations_hard: bool,
    soft_metrics: bool = False,
) -> dict[str, Any]:
    cases = load_knowledge_cases(
        data_dir,
        categories=categories,
        ids=ids,
        limit=limit,
        answerable_only=answerable_only,
    )
    if not cases:
        raise SystemExit("no cases matched filters")

    prior = _load_resume(out_path) if resume else {}
    results: list[dict[str, Any]] = []
    judge = None

    for i, case in enumerate(cases, 1):
        cid = str(case["id"])
        if cid in prior and prior[cid].get("pass") is not None and not prior[cid].get("error"):
            # Resume: keep completed rows (including intentional fails).
            print(f"[{i}/{len(cases)}] {cid} resume-skip", flush=True)
            results.append(prior[cid])
            continue

        print(
            f"[{i}/{len(cases)}] {cid} cat={case.get('category')} "
            f"must_cite={bool(case.get('must_cite_source'))}",
            flush=True,
        )
        print(f"Q: {case.get('question')}", flush=True)

        if judge is None:
            judge = make_judge_model()

        row = await evaluate_case(
            case,
            judge=judge,
            faithfulness_threshold=faithfulness_threshold,
            relevancy_threshold=relevancy_threshold,
            tone_threshold=tone_threshold,
            citations_threshold=citations_threshold,
            citations_hard=citations_hard,
            soft_metrics=soft_metrics,
        )
        results.append(row)

        status = "PASS" if row.get("pass") else "FAIL"
        faith = (row.get("metrics") or {}).get("FaithfulnessMetric") or {}
        print(
            f"  {status} reasons={row.get('fail_reasons')} "
            f"faith={faith.get('score', faith.get('error', faith.get('skipped')))}",
            flush=True,
        )

        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "data_dir": str(data_dir),
            "categories": list(categories) if categories else None,
            "soft_metrics": soft_metrics,
            "thresholds": {
                "faithfulness": faithfulness_threshold,
                "relevancy": relevancy_threshold,
                "tone": tone_threshold,
                "citations": citations_threshold,
                "citations_hard": citations_hard,
            },
            "summary": summarize(results),
            "results": results,
        }
        _write_report(out_path, payload)

        if pause_s > 0 and i < len(cases):
            time.sleep(pause_s)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_dir": str(data_dir),
        "categories": list(categories) if categories else None,
        "soft_metrics": soft_metrics,
        "thresholds": {
            "faithfulness": faithfulness_threshold,
            "relevancy": relevancy_threshold,
            "tone": tone_threshold,
            "citations": citations_threshold,
            "citations_hard": citations_hard,
        },
        "summary": summarize(results),
        "results": results,
    }
    _write_report(out_path, report)
    return report


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Read quality-plane eval (DeepEval). "
            "Default: hard gates only (L0/L1/Faithfulness). "
            "Use --soft-metrics for Relevancy/Tone/Citations."
        )
    )
    p.add_argument(
        "--data",
        default=str(DEFAULT_DATA_DIR),
        help="knowledge dataset directory (default: tests/evals/knowledge)",
    )
    p.add_argument(
        "--category",
        default=",".join(DEFAULT_CATEGORIES),
        help=(
            "comma-separated categories "
            f"(default: {','.join(DEFAULT_CATEGORIES)}; empty = master.jsonl)"
        ),
    )
    p.add_argument("--ids", default="", help="comma-separated case ids")
    p.add_argument("--limit", type=int, default=None, help="max cases after filters")
    p.add_argument(
        "--answerable-only",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="only answerable=true cases (default: true)",
    )
    p.add_argument(
        "--out",
        default=None,
        help="JSON report path (default: tmp/reports/deepeval_read_<ts>.json)",
    )
    p.add_argument("--resume", action="store_true", help="skip ids already in --out")
    p.add_argument(
        "--pause-s",
        type=float,
        default=0.0,
        help="sleep seconds between cases (rate-limit pacing; default 0)",
    )
    p.add_argument("--faithfulness-threshold", type=float, default=0.7)
    p.add_argument("--relevancy-threshold", type=float, default=0.5)
    p.add_argument("--tone-threshold", type=float, default=0.5)
    p.add_argument("--citations-threshold", type=float, default=0.5)
    p.add_argument(
        "--citations-hard",
        action="store_true",
        help=(
            "when must_cite_source=true, Citations success required for pass "
            "(also forces running Citations even if --no-soft-metrics)"
        ),
    )
    p.add_argument(
        "--soft-metrics",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "run AnswerRelevancy + G-Eval Tone/Citations (default: off; "
            "hard-only Phase-1 to cut Judge LLM calls)"
        ),
    )
    return p


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    args = build_parser().parse_args(argv)
    cats_raw = (args.category or "").strip()
    categories: tuple[str, ...] | None
    if cats_raw:
        categories = tuple(c.strip() for c in cats_raw.split(",") if c.strip())
    else:
        categories = None

    ids: set[str] | None = None
    if args.ids.strip():
        ids = {x.strip() for x in args.ids.split(",") if x.strip()}

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = Path(args.out) if args.out else (ROOT / "tmp" / "reports" / f"deepeval_read_{ts}.json")

    report = asyncio.run(
        run_all(
            data_dir=Path(args.data),
            categories=categories,
            ids=ids,
            limit=args.limit,
            answerable_only=args.answerable_only,
            out_path=out_path,
            resume=args.resume,
            pause_s=args.pause_s,
            faithfulness_threshold=args.faithfulness_threshold,
            relevancy_threshold=args.relevancy_threshold,
            tone_threshold=args.tone_threshold,
            citations_threshold=args.citations_threshold,
            citations_hard=args.citations_hard,
            soft_metrics=args.soft_metrics,
        )
    )
    summary = report["summary"]
    print(
        f"\nwrote {out_path}\n"
        f"summary: total={summary['total']} pass={summary['pass']} "
        f"fail={summary['fail']} pass_rate={summary['pass_rate']:.2%}",
        flush=True,
    )
    return 0 if summary["fail"] == 0 and summary["error"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
