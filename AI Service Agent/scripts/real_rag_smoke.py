r"""真实 LightRAG + ARK 冒烟：ReadGraph 知识问答，验证引用来源与如实告知。

前置：LightRAG-chengyuan 容器运行（localhost:9621，auth disabled）；.env 配 ARK 真实模型。
运行：$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy";
      uv run --no-sync python scripts/real_rag_smoke.py
"""

import asyncio
import sys
import time
from typing import Annotated

from langchain_core.messages import HumanMessage
from langchain_core.tools import tool

from ai_service_agent.config import make_model
from ai_service_agent.graph.agents.read_agent import make_read_graph
from ai_service_agent.tools.kb import kb_search

CASES = [
    {"q": "JoyAir A1 的炸篮容量是多少？", "expect": "来源", "note": "direct_fact 应引用来源"},
    {"q": "JoyClean V1 支持 Wi-Fi 连接吗？", "expect": "来源", "note": "direct_fact 应引用来源"},
    {"q": "空气炸锅忘记密码怎么配网？", "expect": "来源", "note": "指南类应检索到并引用来源"},
    {"q": "你们的注册资本是多少？", "expect": "unanswerable", "note": "知识缺口应如实告知未查询到"},
]


def extract_sources(out) -> list[str]:
    """从工具结果中提取参考来源文件名（kb_search 返回「参考来源：...」行）。"""
    names: list[str] = []
    for msg in out["messages"]:
        content = getattr(msg, "content", None)
        if isinstance(content, str) and "参考来源" in content:
            for line in content.splitlines():
                line = line.strip()
                if line.startswith("参考来源："):
                    names.extend(n.strip() for n in line[len("参考来源："):].split("、") if n.strip())
    return names


def last_ai_content(out) -> str:
    """取最后一条 AI 文本消息（从后往前找，避免依赖末条位置）。"""
    for msg in reversed(out["messages"]):
        if getattr(msg, "type", None) == "ai":
            content = getattr(msg, "content", None)
            if content:
                return content
    return ""


def make_timed_kb_search(case_times: list[float]):
    """真实 kb_search 的计时包装：单独记录检索耗时（不影响工具返回值与图内行为）。"""
    @tool
    def timed_kb_search(text: Annotated[str, "用户的完整问题原文，保留原始表述，不要改写或摘要"]) -> str:
        """澄元智能家电知识库检索（真实 kb_search 的计时包装）。"""
        t0 = time.perf_counter()
        try:
            return kb_search.func(text)
        finally:
            case_times.append(time.perf_counter() - t0)
    return timed_kb_search


async def main() -> int:
    all_ok = True
    total_t0 = time.perf_counter()
    for i, case in enumerate(CASES, 1):
        tool_times: list[float] = []
        graph = make_read_graph(make_model(), [make_timed_kb_search(tool_times)])
        state = {
            "messages": [HumanMessage(content=case["q"])],
            "user_id": "u1",
            "session_id": "s1",
            "context": {},
        }
        t0 = time.perf_counter()
        out = await graph.ainvoke(state)
        elapsed = time.perf_counter() - t0
        kb_elapsed = sum(tool_times)
        last = last_ai_content(out)
        sources = extract_sources(out)
        if case["expect"] == "来源":
            ok = bool(sources) and "来源" in last
        else:
            ok = "未查询到" in last or "无法" in last or "不清楚" in last
        print(f"\n[CASE {i}] Q: {case['q']}")
        print(f"A: {last}")
        if sources:
            print(f"来源文件名: {'、'.join(sources)}")
        print(f"检索耗时(kb_search): {kb_elapsed:.2f}s；总耗时: {elapsed:.2f}s")
        print(f"判定: {'PASS' if ok else 'FAIL'}（{case['note']}）")
        all_ok = all_ok and ok
    total = time.perf_counter() - total_t0
    print(f"\n总计: {total:.2f}s；结果: {'ALL PASS' if all_ok else 'HAS FAIL'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(asyncio.run(main()))