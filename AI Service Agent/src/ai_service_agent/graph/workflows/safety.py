"""安全预检（safety-spec.md §1，D4 v1 范围）与最小不变量 recheck（D6/D7）。

v1 覆盖：参数 schema 白名单 + min_risk_level 匹配 + user_id 登录态 + 数据边界（显式 owner_user_id）。
P4 增量：必填标识符非空（D2，预检层业务兜底）+ 退款时限 / 取消状态机（D3，事实来自 precheck_fetch 节点
与其余频次 / 额度 / 敏感操作复核仍留后续增量（skip(precheck_rule_pending)）。
决策节点返回普通 dict + 条件边路由（LangGraph 1.2.11 实测：Command(goto) 不抑制静态出边，
静态后继与 goto 目标会并发执行导致双写；路由一律走条件边）。
"""

from typing import Callable

from langchain_core.messages import AIMessage
from langgraph.config import get_stream_writer

from ai_service_agent.tools.orders import lookup_order_state
from ai_service_agent.tools.registry import ToolRegistry

PRECHECK_REJECT_TEXT = "该操作未能通过安全校验，已拒绝执行。"

RISK_ORDER = {"read": 0, "mutation": 1, "high_risk": 2}
from datetime import datetime, timezone

REFUND_WINDOW_DAYS = 7  # 无理由退款签收窗口（D3，CTRL-0086）


def _apply_business_rules(pending: dict, precheck_data: dict | None, user_id: str = "") -> str | None:
    """D3（P4）业务规则 v1（safety-spec §1.2）：确定性消费 precheck_data 事实，返回拒绝 reason 或 None。
    只裁决不 fetch（fetch 职责在 precheck_fetch 节点，事实层）；参数与事实订单不一致时不做裁决（防御）。"""
    if not precheck_data:
        return None
    tool_name = pending.get("tool")
    order_id = (pending.get("args") or {}).get("order_id")
    if precheck_data.get("order_id") != order_id:
        return None
    if precheck_data.get("user_id") and user_id and precheck_data["user_id"] != user_id:
        return "data_boundary"
    if tool_name == "create_invoice":
        amount = (pending.get("args") or {}).get("amount")
        total_amount = precheck_data.get("total_amount")
        invoiced_amount = precheck_data.get("invoiced_amount")
        if (
            isinstance(amount, (int, float)) and amount > 0
            and isinstance(total_amount, (int, float))
            and isinstance(invoiced_amount, (int, float))
            and amount > float(total_amount) - float(invoiced_amount) + 1e-9
        ):
            return "invoice_amount_exceeds"
    if tool_name == "refund_apply" and precheck_data.get("status") == "signed":
        signed_at = precheck_data.get("signed_at")
        if isinstance(signed_at, datetime):
            age_days = (datetime.now(timezone.utc) - signed_at).total_seconds() / 86400
            if age_days > REFUND_WINDOW_DAYS:
                return "refund_window_expired"
    if tool_name == "cancel_order" and precheck_data.get("status") == "cancelled":
        return "order_already_cancelled"
    return None


def make_precheck_fetch_node() -> Callable[[dict], dict]:
    """precheck_fetch（D3，P4）：位于 select 后、safety_check 前。pending_tool.args 含 order_id 时
    查询订单状态 -> 写 context["precheck_data"]（仅事实），不做任何授权或业务裁决；无 order_id / 订单不存在不写。"""
    def precheck_fetch(state: dict) -> dict:
        ctx = state.get("context", {})
        pending = ctx.get("pending_tool") or {}
        order_id = (pending.get("args") or {}).get("order_id")
        if not isinstance(order_id, str) or not order_id:
            return {}
        data = lookup_order_state(order_id)
        if data is None:
            return {}
        return {"context": {**ctx, "precheck_data": dict(data)}}
    return precheck_fetch


def _check(pending: dict, tier: str, registry: ToolRegistry, user_id: str) -> str | None:
    tool_name = pending.get("tool")
    meta = registry.meta_for(tool_name) if isinstance(tool_name, str) else None
    if meta is None:
        return "unknown_tool"
    if RISK_ORDER[tier] < RISK_ORDER[meta.min_risk_level]:
        return "tool_not_allowed_at_tier"
    args = pending.get("args") or {}
    if meta.schema is not None:
        try:
            meta.schema.model_validate(args)
        except Exception:
            return "invalid_args"
        # D2（P4）：必填标识符非空（safety-spec §1.2）——schema 校验的业务层兜底，
        # 防 select 之外来源的 args 绕过；pydantic 接受空串须显式判空，request_id 除外
        # （其缺失由 recheck 的 missing_request_id 兜底）。
        required = [
            n for n in meta.schema.model_fields
            if n != "request_id" and meta.schema.model_fields[n].is_required()
        ]
        if any(args.get(n) == "" for n in required):
            return "invalid_args"
    if not user_id:
        return "not_logged_in"
    if args.get("owner_user_id") and args["owner_user_id"] != user_id:
        return "data_boundary"
    return None


def make_safety_check_node(tier: str, registry: ToolRegistry) -> Callable[[dict], dict]:
    """预检节点：位于写操作执行前、interrupt 前（safety-spec §1.1）；失败不进入 interrupt 不调工具。"""
    def safety_check(state: dict) -> dict:
        ctx = state.get("context", {})
        reason = _check(ctx.get("pending_tool") or {}, tier, registry, state.get("user_id", ""))
        if reason is None:
            reason = _apply_business_rules(
                ctx.get("pending_tool") or {}, ctx.get("precheck_data"), state.get("user_id", "")
            )
        if reason is None:
            return {"context": {**ctx, "precheck": "pass"}}
        get_stream_writer()({"type": "safety_rejected", "data": {"rule": "precheck", "reason": reason}})
        return {"context": {**ctx, "precheck_reason": reason}, "execution_status": "completed"}
    return safety_check


def make_recheck_node(tier: str, registry: ToolRegistry) -> Callable[[dict], dict]:
    """恢复后、执行前最小不变量 recheck（D6/D7）：request_id 存在、工具仍在档、schema 仍有效。
    参数未变 / context 非陈旧：pending_tool 由本 turn select 写入并随 checkpoint 恢复，resume 不改写；
    完整语义校验见 safety-spec 后续增量。"""
    def recheck(state: dict) -> dict:
        ctx = state.get("context", {})
        pending = ctx.get("pending_tool") or {}
        reason = _check(pending, tier, registry, state.get("user_id", ""))
        if not pending.get("request_id"):
            reason = reason or "missing_request_id"
        if reason:
            get_stream_writer()({"type": "safety_rejected", "data": {"rule": "recheck", "reason": reason}})
            return {"context": {**ctx, "recheck_reason": reason}, "execution_status": "completed"}
        return {"context": {**ctx, "recheck": "ok"}}
    return recheck


def safety_final(state: dict) -> dict:
    reason = state.get("context", {}).get("precheck_reason", "precheck")
    return {"messages": [AIMessage(content=f"{PRECHECK_REJECT_TEXT}（{reason}）")], "execution_status": "completed"}
