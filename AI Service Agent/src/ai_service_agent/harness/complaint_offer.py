"""从 checkpointer 读取投诉询问标记，供入口层复用既有 handoff 路径。

不新建 interrupt 类型。同意转人工仍走 ``HandoffService.request_handoff``。
"""

from __future__ import annotations

import logging

logger = logging.getLogger("ai_service_agent")


def _context(values: dict | None) -> dict:
    return dict((values or {}).get("context") or {})


def complaint_offer_from_checkpoint(checkpoint: dict | None) -> tuple[bool, str]:
    """从 checkpointer 元组里的 checkpoint 读询问标记。无 checkpoint 按未挂起。

    ``/chat`` 用它避免为了 peek 再 ``make_ai_graph`` 一次（与 AI 回合必须共用
    唯一构建入口）。
    """
    if not checkpoint:
        return False, ""
    values = checkpoint.get("channel_values") or {}
    ctx = values.get("context") or {}
    if not isinstance(ctx, dict):
        return False, ""
    return bool(ctx.get("complaint_offer_pending")), str(ctx.get("complaint_summary") or "")


async def read_complaint_offer(graph, config) -> tuple[bool, str]:
    """返回 ``(complaint_offer_pending, complaint_summary)``。读失败按未挂起。"""
    if graph is None or getattr(graph, "checkpointer", None) is None:
        return False, ""
    try:
        snapshot = await graph.aget_state(config)
    except Exception:
        logger.exception("read complaint offer failed")
        return False, ""
    ctx = _context(getattr(snapshot, "values", None))
    return bool(ctx.get("complaint_offer_pending")), str(ctx.get("complaint_summary") or "")


def complaint_handoff_accept(values: dict | None) -> str | None:
    """图内兜底：用户已明确同意。返回 summary；未置位则 None。"""
    ctx = _context(values)
    if not ctx.get("complaint_handoff_accept"):
        return None
    return str(ctx.get("complaint_summary") or "")[:80]


async def clear_complaint_offer_flags(graph, config) -> None:
    """同意转人工后清掉询问标记，避免下一句被再次当成 offer 回复。"""
    if graph is None or getattr(graph, "checkpointer", None) is None:
        return
    try:
        snapshot = await graph.aget_state(config)
        ctx = _context(getattr(snapshot, "values", None))
        if not any(
            ctx.get(key)
            for key in (
                "complaint_offer_pending",
                "complaint_handoff_accept",
                "complaint_offer_decision",
                "complaint_offer_reconfirmed",
            )
        ):
            return
        ctx["complaint_offer_pending"] = False
        ctx["complaint_handoff_accept"] = False
        ctx.pop("complaint_offer_decision", None)
        ctx.pop("complaint_offer_reconfirmed", None)
        await graph.aupdate_state(config, {"context": ctx}, as_node="complaint_graph")
    except Exception:
        logger.exception("clear complaint offer flags failed")
