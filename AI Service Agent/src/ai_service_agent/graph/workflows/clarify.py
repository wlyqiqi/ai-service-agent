"""Clarify 节点（P4 D1 / #110）：确定性澄清文案，普通轮非 interrupt。

消费 HighRisk select 写入的：
- `clarify_missing`（HighRisk 缺参 / 零候选）
- `tier_clarify`（HighRisk 档不匹配，或 HighRisk 无 tool_call）

Mutation Operation Clarify 由 mutation_select_llm 内联完成（ADR 0004），不再经本节点。
HighRisk 无 tool_call / 档不匹配仍经本节点（ADR 0005 P0 不内联 Operation Clarify）。
解析失败/未知工具/schema 错误仍走 select 的 _reject（safety_rejected）。
轮次计数存 context["clarify_count"]。换 route_intent / target_graph 时由 Policy Gate
在进入子图前清零；完整成功 select 亦清零。
超过 Settings.clarify_max_turns -> 确定性 fallback 文案 + completed（不再澄清）。
"""

from typing import Callable

from langchain_core.messages import AIMessage
from langgraph.config import get_stream_writer

from ai_service_agent.config import get_settings

from ai_service_agent.graph.workflows.copy import (
    clarify_candidates_text,
    clarify_fallback_text,
    clarify_missing_text,
    clarify_no_candidates_text,
    clarify_tier_text,
)

def make_clarify_node() -> Callable[[dict], dict]:
    def clarify(state: dict) -> dict:
        ctx = state.get("context", {})
        # 跨 route_intent / target_graph 的清零在 Policy Gate（进子图前）。
        count = int(ctx.get("clarify_count", 0))
        max_turns = get_settings().clarify_max_turns
        if count >= max_turns:
            return {
                "messages": [AIMessage(content=clarify_fallback_text())],
                "context": {**ctx, "clarify_count": count + 1},
                "execution_status": "completed",
            }
        if ctx.get("tier_clarify"):
            data = {"missing": [], "reason": "tier_mismatch"}
            text = clarify_tier_text()
        else:
            missing = ctx.get("clarify_missing") or []
            candidates = ctx.get("cancellation_candidates")
            is_cancel_selection = candidates is not None and missing == ["order_id"]
            data = {"missing": missing}
            if is_cancel_selection:
                data["candidates"] = candidates
            if is_cancel_selection and candidates:
                text = clarify_candidates_text(candidates)
            elif is_cancel_selection:
                text = clarify_no_candidates_text()
            else:
                text = clarify_missing_text(missing)
        get_stream_writer()({"type": "clarification_required", "data": data})
        return {
            "messages": [AIMessage(content=text)],
            "context": {**ctx, "clarify_count": count + 1},
            "execution_status": "clarifying",
        }

    return clarify
