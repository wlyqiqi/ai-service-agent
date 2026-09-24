import argparse
import json
import math
import os
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

import httpx
from langchain_core.messages import HumanMessage

from ai_service_agent.config import get_settings
from ai_service_agent.evals.runner import policy_target


HIGH_RISK_OPERATIONS = {
    "cancel_order",
    "refund_apply",
    "account_security_action",
    "data_admin_action",
    "order_transfer_or_reship",
}

BASELINE_QUESTIONS = {
    "capability": {
        "type": "choice",
        "instructions": "选择用户当前请求的主要业务能力；不要根据历史消息猜测不存在的操作。",
        "criteria": {
            "faq": "产品使用、售后政策、商品参数、故障排查等知识问答",
            "order": "订单查询、物流、发票、售后、取消、退款等订单业务",
            "account": "用户资料，例如收货地址、手机号等联系方式",
            "smalltalk": "问候、感谢或非业务闲聊",
            "unknown": "无法判断、缺少上下文或不在支持范围内",
        },
    },
    "risk_level": {
        "type": "choice",
        "instructions": "判断满足当前请求所需的执行风险档位。",
        "criteria": {
            "read": "只查询或回答，不修改业务状态",
            "mutation": "普通资料或业务数据修改",
            "high_risk": "取消订单、退款申请等需要审批的操作",
        },
    },
}

EXPERIMENT_QUESTIONS = {
    **BASELINE_QUESTIONS,
    "operation": {
        "type": "choice",
        "instructions": "选择当前请求中最主要或风险最高的业务操作。多意图时选择风险最高的操作。",
        "criteria": {
            "cancel_order": {
                "definition": "取消已有订单；属于高风险操作",
                "examples": ["取消订单 10086", "这单不要了"],
                "not": ["取消预约", "取消提醒", "查订单是否已取消"],
            },
            "refund_apply": {
                "definition": "提交或重新提交退款申请；属于高风险操作",
                "examples": ["申请退款", "退了吧", "重新提交退款"],
                "not": ["查退款进度", "问退款政策"],
            },
            "account_security_action": {
                "definition": "账号安全相关的强制或敏感操作；属于高风险操作",
                "examples": ["强制重置账号", "强制解绑陌生设备", "冻结账号"],
                "not": ["修改手机号", "修改地址", "查询登录记录"],
            },
            "data_admin_action": {
                "definition": "批量删除、清空或导出用户/订单/维修/工单数据；属于高风险操作",
                "examples": ["清空订单历史", "删除维修记录", "导出全部数据"],
                "not": ["查询一条订单", "修改一条备注"],
            },
            "order_transfer_or_reship": {
                "definition": "订单所有权转移、过户或特殊补发；属于高风险操作",
                "examples": ["转到朋友名下", "跨境补发", "海外补发"],
                "not": ["修改收货地址", "查询物流"],
            },
            "ordinary_action": {
                "definition": "普通查询或普通写操作，不属于上述高风险操作",
                "examples": ["查物流", "改地址", "开发票", "取消上门预约"],
                "not": ["取消订单", "申请退款", "强制解绑设备"],
            },
            "unknown": "无法判断、缺少对象、缺少上下文或不在支持范围内",
        },
    },
}


def percentile(values: list[float], percent: float) -> float:
    ordered = sorted(values)
    index = (len(ordered) - 1) * percent / 100
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def latency_summary(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "p50_ms": round(percentile(values, 50), 1),
        "mean_ms": round(statistics.mean(values), 1),
        "p95_ms": round(percentile(values, 95), 1),
        "min_ms": round(min(values), 1),
        "max_ms": round(max(values), 1),
    }


def accuracy_summary(results: list[dict]) -> dict:
    total = len(results)
    if total == 0:
        return {
            "total": 0,
            "capability_accuracy": None,
            "risk_accuracy": None,
            "joint_accuracy": None,
            "operation_accuracy": None,
            "policy_target_accuracy": None,
            "high_risk_recall": None,
            "mutation_recall": None,
            "read_recall": None,
            "capability_confusion": {},
            "risk_confusion": {},
            "operation_confusion": {},
        }

    def recall(risk: str) -> float:
        expected = [r for r in results if r["expected_risk_level"] == risk]
        correct = [r for r in expected if r["final_risk_level"] == risk]
        return round(len(correct) / len(expected), 4) if expected else None

    def confusion(field: str) -> dict:
        return {
            f"{expected} -> {predicted}": count
            for (expected, predicted), count in Counter(
                (r[f"expected_{field}"], r[f"final_{field}"]) for r in results
            ).items()
        }

    return {
        "total": total,
        "capability_accuracy": round(sum(r["capability_correct"] for r in results) / total, 4),
        "risk_accuracy": round(sum(r["risk_correct"] for r in results) / total, 4),
        "joint_accuracy": round(sum(r["joint_correct"] for r in results) / total, 4),
        "operation_accuracy": round(sum(r["operation_correct"] for r in results) / total, 4),
        "policy_target_accuracy": round(sum(r["policy_target_correct"] for r in results) / total, 4),
        "high_risk_recall": recall("high_risk"),
        "mutation_recall": recall("mutation"),
        "read_recall": recall("read"),
        "capability_confusion": confusion("capability"),
        "risk_confusion": confusion("risk_level"),
        "operation_confusion": confusion("operation"),
    }


def answer(data: dict, name: str) -> tuple[str, float] | None:
    value = data.get("answers", {}).get(name)
    if not isinstance(value, dict) or not isinstance(value.get("choice"), str):
        return None
    try:
        return value["choice"], float(value.get("confidence"))
    except (TypeError, ValueError):
        return None


def request(
    client: httpx.Client,
    settings,
    text: str,
    questions: dict,
) -> tuple[dict | None, float, str | None]:
    state = {"messages": [{"role": "human", "content": text}]}
    started = time.perf_counter()
    try:
        response = client.post(
            settings.jev_api_url,
            headers={"Authorization": f"Bearer {settings.jev_api_key}"},
            json={"state": state, "model": settings.jev_model, "questions": questions},
            timeout=settings.jev_timeout_seconds,
        )
        response.raise_for_status()
        return response.json(), (time.perf_counter() - started) * 1000, None
    except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
        return None, (time.perf_counter() - started) * 1000, type(exc).__name__


def result_row(
    case: dict,
    output: dict | None,
    latency_ms: float,
    variant: str,
    error: str | None = None,
) -> dict:
    base = {
        "id": case["id"],
        "variant": variant,
        "input": case["input"],
        "expected_capability": case["expected_capability"],
        "expected_risk_level": case["expected_risk_level"],
        "expected_graph": case["expected_graph"],
        "expected_policy": case["expected_policy"],
        "expected_operation": case["expected_operation"],
        "latency_ms": round(latency_ms, 1),
        "error": error,
    }
    if output is None:
        return {
            **base,
            "accepted": False,
            "final_capability": "unknown",
            "final_risk_level": "read",
            "final_operation": "unknown",
            "capability": "unknown",
            "risk_level": "read",
            "operation": "unknown",
            "confidence": 0.0,
            "capability_correct": False,
            "risk_correct": False,
            "joint_correct": False,
            "operation_correct": False,
            "predicted_policy_target": "fallback",
            "policy_target_correct": False,
        }
    return {
        **base,
        **output,
        "final_capability": output["capability"],
        "final_risk_level": output["risk_level"],
        "final_operation": output["operation"],
        "capability_correct": output["capability"] == case["expected_capability"],
        "risk_correct": output["risk_level"] == case["expected_risk_level"],
        "joint_correct": (
            output["capability"] == case["expected_capability"]
            and output["risk_level"] == case["expected_risk_level"]
        ),
        "operation_correct": output["operation"] == case["expected_operation"],
        "predicted_policy_target": policy_target(output["capability"], output["risk_level"]),
        "policy_target_correct": (
            policy_target(output["capability"], output["risk_level"]) == case["expected_graph"]
        ),
    }


def evaluate_baseline(client: httpx.Client, settings, case: dict) -> tuple[dict, float]:
    data, latency_ms, error = request(client, settings, case["input"], BASELINE_QUESTIONS)
    if data is None:
        return result_row(case, None, latency_ms, "baseline", error), latency_ms

    capability = answer(data, "capability")
    risk = answer(data, "risk_level")
    if capability is None or risk is None:
        return result_row(case, None, latency_ms, "baseline", "invalid_answer"), latency_ms

    confidence = min(capability[1], risk[1])
    accepted = (
        capability[0] != "unknown"
        and confidence >= settings.jev_confidence_threshold
        and not (
            risk[0] in ("mutation", "high_risk")
            and risk[1] < settings.jev_risk_confidence_threshold
        )
    )
    output = {
        "accepted": accepted,
        "capability": capability[0] if accepted else "unknown",
        "risk_level": risk[0] if accepted else "read",
        "operation": "not_asked",
        "confidence": confidence if accepted else 0.0,
        "raw_capability": capability[0],
        "raw_risk_level": risk[0],
        "capability_confidence": capability[1],
        "risk_confidence": risk[1],
    }
    return result_row(case, output, latency_ms, "baseline"), latency_ms


def evaluate_operation(client: httpx.Client, settings, case: dict) -> tuple[dict, float]:
    data, latency_ms, error = request(client, settings, case["input"], EXPERIMENT_QUESTIONS)
    if data is None:
        return result_row(case, None, latency_ms, "operation", error), latency_ms

    capability = answer(data, "capability")
    operation = answer(data, "operation")
    risk = answer(data, "risk_level")
    if capability is None or operation is None or risk is None:
        return result_row(case, None, latency_ms, "operation", "invalid_answer"), latency_ms

    if operation[0] in HIGH_RISK_OPERATIONS:
        final_risk = "high_risk"
        risk_confidence = operation[1]
    else:
        final_risk = risk[0]
        risk_confidence = risk[1]

    confidence = min(capability[1], risk_confidence)
    accepted = (
        capability[0] != "unknown"
        and operation[0] != "unknown"
        and confidence >= settings.jev_confidence_threshold
        and not (
            final_risk in ("mutation", "high_risk")
            and risk_confidence < settings.jev_risk_confidence_threshold
        )
    )
    output = {
        "accepted": accepted,
        "capability": capability[0] if accepted else "unknown",
        "risk_level": final_risk if accepted else "read",
        "operation": operation[0] if accepted else "unknown",
        "confidence": confidence if accepted else 0.0,
        "raw_capability": capability[0],
        "raw_operation": operation[0],
        "raw_risk_level": risk[0],
        "capability_confidence": capability[1],
        "operation_confidence": operation[1],
        "risk_confidence": risk[1],
    }
    return result_row(case, output, latency_ms, "operation"), latency_ms


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dataset",
        default="tests/evals/router_benchmark/high_risk_micro.jsonl",
    )
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--output",
        default="scripts/router_operation_micro_results.json",
    )
    args = parser.parse_args()

    api_key = os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        raise RuntimeError("TYPESAFE_API_KEY is not set")

    cases = []
    with open(args.dataset, encoding="utf-8") as file:
        for line in file:
            if line.strip():
                cases.append(json.loads(line))
    if args.limit:
        cases = cases[: args.limit]

    get_settings.cache_clear()
    settings = get_settings()
    client = httpx.Client(timeout=settings.jev_timeout_seconds)
    baseline_results = []
    operation_results = []
    baseline_latencies = []
    operation_latencies = []

    for index, case in enumerate(cases, start=1):
        print(f"[{index}/{len(cases)}] {case['id']}", file=sys.stderr, flush=True)
        baseline_row, baseline_latency = evaluate_baseline(client, settings, case)
        operation_row, operation_latency = evaluate_operation(client, settings, case)
        baseline_results.append(baseline_row)
        operation_results.append(operation_row)
        baseline_latencies.append(baseline_latency)
        operation_latencies.append(operation_latency)

    accepted_baseline = [r for r in baseline_results if r["accepted"]]
    accepted_operation = [r for r in operation_results if r["accepted"]]
    output = {
        "dataset": args.dataset,
        "case_count": len(cases),
        "thresholds": {
            "confidence": settings.jev_confidence_threshold,
            "risk_confidence": settings.jev_risk_confidence_threshold,
            "timeout_seconds": settings.jev_timeout_seconds,
        },
        "baseline": {
            **accuracy_summary(baseline_results),
            "accepted_rate": round(sum(r["accepted"] for r in baseline_results) / len(cases), 4),
            "accepted_accuracy": accuracy_summary(accepted_baseline),
            "latency": latency_summary(baseline_latencies),
        },
        "operation": {
            **accuracy_summary(operation_results),
            "accepted_rate": round(sum(r["accepted"] for r in operation_results) / len(cases), 4),
            "accepted_accuracy": accuracy_summary(accepted_operation),
            "latency": latency_summary(operation_latencies),
        },
        "results": {
            "baseline": baseline_results,
            "operation": operation_results,
        },
    }
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in output.items() if k != "results"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
