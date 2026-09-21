"""Clarify 节点（P4 D1）：select 参数不完整 -> 确定性澄清文案，普通轮非 interrupt。

只服务"参数不完整"（缺必填/空必填/全空业务字段）；解析失败/未知工具/schema 错误仍走 select 的
_reject（safety_rejected）。轮次计数存 context["clarify_count"]，完整成功 select 清零，
超过 Settings.clarify_max_turns -> 确定性 fallback 文案 + completed（不再澄清）。
"""

from typing import Callable

from langchain_core.messages import AIMessage
from langgraph.config import get_stream_writer

from ai_service_agent.config import get_settings

CLARIFY_FALLBACK_TEXT = "抱歉，暂时无法确定您的需求，请换个说法或联系人工客服。"

TIER_CLARIFY_TEXT = "您的请求涉及较高风险操作，但我无法确定具体要执行哪一项业务（如：申请退款、取消订单），请明确说明。"

FIELD_LABELS = {
    "order_id": "订单号",
    "amount": "金额",
    "reason": "原因",
    "phone": "手机号",
    "email": "邮箱",
    "address": "收货地址",
    "text": "检索内容",
    "company": "公司名",
    "tax_no": "税号",
    "note": "订单备注",
}


def clarify_text(missing: list[str]) -> str:
    """确定性模板：字段名 -> 中文 label（未映射字段回落原字段名）。"""
    labels = [FIELD_LABELS.get(f, f) for f in missing]
    return "请提供：" + "、".join(labels) + "。"


def make_clarify_node() -> Callable[[dict], dict]:
    def clarify(state: dict) -> dict:
        ctx = state.get("context", {})
        count = int(ctx.get("clarify_count", 0))
        max_turns = get_settings().clarify_max_turns
        if count >= max_turns:
            return {
                "messages": [AIMessage(content=CLARIFY_FALLBACK_TEXT)],
                "context": {**ctx, "clarify_count": count + 1},
                "execution_status": "completed",
            }
        if ctx.get("tier_clarify"):
            data = {"missing": [], "reason": "tier_mismatch"}
            text = TIER_CLARIFY_TEXT
        else:
            missing = ctx.get("clarify_missing") or []
            candidates = ctx.get("cancellation_candidates")
            is_cancel_selection = candidates is not None and missing == ["order_id"]
            data = {"missing": missing, "candidates": candidates or []}
            if is_cancel_selection and candidates:
                options = "、".join(
                    f"{item['order_id']}（{item.get('product_summary', '商品信息不可用')}）"
                    for item in candidates
                )
                text = f"您有多笔可取消订单，请选择订单号：{options}。"
            elif is_cancel_selection:
                text = "当前没有可取消的已付款订单。"
            else:
                text = clarify_text(missing)
        get_stream_writer()({"type": "clarification_required", "data": data})
        return {
            "messages": [AIMessage(content=text)],
            "context": {**ctx, "clarify_count": count + 1},
            "execution_status": "clarifying",
        }

    return clarify
