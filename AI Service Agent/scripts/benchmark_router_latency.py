import json
import math
import os
import statistics
import time

import httpx

from ai_service_agent.config import make_model
from ai_service_agent.graph.router import make_router


SAMPLES = [
    "你好，在吗？",
    "这个锅可以放洗碗机吗？",
    "帮我查一下订单 ORD-10086 的物流。",
    "我想把收货地址改成北京市朝阳区望京SOHO。",
    "订单 ORD-10086 需要开发票，抬头星辰科技有限公司。",
    "订单 ORD-10086 不要了，帮我取消。",
    "订单 ORD-10086 有质量问题，申请退款 199 元。",
    "帮我处理一下。",
    "人工智能客服一般是怎么实现的？",
    "我要转人工。",
]

JEV_QUESTIONS = {
    "capability": {
        "type": "choice",
        "instructions": "Classify the latest user message for a Chinese e-commerce customer service system. Choose one primary business capability. Use unknown only when the intent is not clear, not actionable, or not covered by the other options.",
        "criteria": {
            "faq": "Product usage, policy, or general knowledge question",
            "order": "Order lookup, shipment, invoice, cancellation, refund, or other order-related work",
            "account": "Account profile fields such as address or phone number",
            "smalltalk": "Greeting, thanks, or non-business conversation",
            "unknown": "Unclear, non-actionable, or unclassified request",
        },
    },
    "risk_level": {
        "type": "choice",
        "instructions": "Classify the execution risk of satisfying the user's current request. If there is no actionable request, use read. Do not infer a higher risk than the request itself.",
        "criteria": {
            "read": "Lookup or answer only; no business state changes",
            "mutation": "Ordinary modification of account or order data",
            "high_risk": "Cancellation, refund, or another operation that requires approval",
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


def summarize(latencies: list[float]) -> dict:
    return {
        "n": len(latencies),
        "p50_ms": round(percentile(latencies, 50), 1),
        "mean_ms": round(statistics.mean(latencies), 1),
        "p95_ms": round(percentile(latencies, 95), 1),
        "min_ms": round(min(latencies), 1),
        "max_ms": round(max(latencies), 1),
    }


def run_deepseek_sample(router, message: str) -> dict:
    started = time.perf_counter()
    result = router({"messages": [{"type": "human", "content": message}]})
    elapsed = (time.perf_counter() - started) * 1000
    return {"message": message, "result": result, "latency_ms": round(elapsed, 1)}


def run_jev_sample(client: httpx.Client, api_key: str, message: str) -> dict:
    payload = {
        "state": message,
        "model": "jev-latest",
        "questions": JEV_QUESTIONS,
    }
    started = time.perf_counter()
    response = client.post(
        "https://api.typesafe.ai/v1/systemone",
        headers={"Authorization": f"Bearer {api_key}"},
        json=payload,
        timeout=30,
    )
    elapsed = (time.perf_counter() - started) * 1000
    response.raise_for_status()
    body = response.json()
    answers = body["answers"]
    return {
        "message": message,
        "capability": answers["capability"]["choice"],
        "capability_confidence": answers["capability"]["confidence"],
        "risk_level": answers["risk_level"]["choice"],
        "risk_confidence": answers["risk_level"]["confidence"],
        "usage": body.get("usage"),
        "latency_ms": round(elapsed, 1),
    }


def main() -> None:
    api_key = os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        raise RuntimeError("TYPESAFE_API_KEY is not set")

    router = make_router(make_model())
    with httpx.Client() as client:
        run_jev_sample(client, api_key, "预热请求")

        deepseek_results = []
        jev_results = []
        for message in SAMPLES:
            deepseek_results.append(run_deepseek_sample(router, message))
            jev_results.append(run_jev_sample(client, api_key, message))

    deepseek_latencies = [item["latency_ms"] for item in deepseek_results]
    jev_latencies = [item["latency_ms"] for item in jev_results]
    output = {
        "samples": len(SAMPLES),
        "deepseek": {
            "summary": summarize(deepseek_latencies),
            "results": deepseek_results,
        },
        "jev": {
            "summary": summarize(jev_latencies),
            "results": jev_results,
        },
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
