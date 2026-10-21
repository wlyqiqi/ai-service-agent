"""Clarify 节点（P4 D1 / #110）：确定性澄清文案，普通轮非 interrupt。

消费 select 写入的：
- `clarify_missing`（HighRisk 缺参 / 零候选）
- `tier_clarify`（写图档不匹配，或 HighRisk 无 tool_call）
- `operation_clarify`（Mutation select 无 tool_call 的 Operation Clarify 逃逸；入口层
  Hybrid Tool Resolution 仍优先在进子图前完成）

解析失败/未知工具/schema 错误仍走 select 的 _reject（safety_rejected）。
轮次计数存 context["clarify_count"]，完整成功 select 清零，
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
    operation_clarify_text,
)

def make_clarify_node() -> Callable[[dict], dict]:
    def clarify(state: dict) -> dict:
        ctx = state.get("context", {})
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
        elif ctx.get("operation_clarify"):
            # Select 无 tool_call（#110）：Mutation 内 Operation Clarify，与入口层同文案/事件。
            data = {"missing": [], "reason": "operation_unclear"}
            text = operation_clarify_text()
            ctx = {**ctx}
            ctx.pop("operation_clarify", None)
        else:
            missing = ctx.get("clarify_missing") or []
            candidates = ctx.get("cancellation_candidates")
            is_cancel_selection = candidates is not None and missing == ["order_id"]
            data = {"missing": missing}
            if is_cancel_selection:
                data["candidates"] = candidates
            if is_cancel_selection and candidates:
                options = "、".join(
                    f"{item['order_id']}（{item.get('product_summary', '商品信息不可用')}）"
                    for item in candidates
                )
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
