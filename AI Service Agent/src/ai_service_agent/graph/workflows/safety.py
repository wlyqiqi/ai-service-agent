"""安全预检（safety-spec.md §1，D4 v1 范围）与最小不变量 recheck（D6/D7）。

v1 覆盖：参数 schema 白名单 + min_risk_level 匹配 + user_id 登录态 + 数据边界（显式 owner_user_id）。
P4 增量：必填标识符非空（D2，预检层业务兜底）+ 退款时限 / 取消状态机（D3，事实来自 precheck_fetch 节点
与其余频次 / 额度 / 敏感操作复核仍留后续增量（skip(precheck_rule_pending)）。
决策节点返回普通 dict + 条件边路由（LangGraph 1.2.11 实测：Command(goto) 不抑制静态出边，
静态后继与 goto 目标会并发执行导致双写；路由一律走条件边）。
"""

import asyncio
from typing import Awaitable, Callable

from langchain_core.messages import AIMessage
from langgraph.config import get_stream_writer
from langgraph.runtime import Runtime

from ai_service_agent.domain.orders import OrderRepository, OrderRepositoryUnavailable
from ai_service_agent.tools.orders import is_address_editable_status
from ai_service_agent.tools.registry import ToolRegistry
from ai_service_agent.graph.runtime_context import GraphContext
from ai_service_agent.graph.workflows.copy import precheck_reject_text

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
        status = precheck_data.get("status")
        if status in ("cancelled", "refunded", "refunding"):
            return "invoice_not_allowed"
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
    if tool_name == "update_address" and not is_address_editable_status(
        precheck_data.get("status")
    ):
        return "address_not_editable"
    if tool_name == "refund_apply" and precheck_data.get("status") == "signed":
        signed_at = precheck_data.get("signed_at")
        if isinstance(signed_at, datetime):
            age_days = (datetime.now(timezone.utc) - signed_at).total_seconds() / 86400
            if age_days > REFUND_WINDOW_DAYS:
                return "refund_window_expired"
    if tool_name == "refund_apply":
        status = precheck_data.get("status")
        if status == "data_source_unavailable":
            return "data_source_unavailable"
        if status == "not_found":
            return "order_not_found"
        if status in ("refunded", "refunding"):
            return "order_already_refunded" if status == "refunded" else "order_already_refunding"
        if status not in ("paid", "signed"):
            return "refund_not_allowed"
        amount = (pending.get("args") or {}).get("amount")
        total_amount = precheck_data.get("total_amount")
        occupied = precheck_data.get("occupied_refund_amount", 0.0)
        if (
            isinstance(amount, (int, float)) and amount > 0
            and isinstance(total_amount, (int, float))
            and amount > float(total_amount) - float(occupied) + 1e-9
        ):
            return "refund_amount_exceeds"
    if tool_name == "cancel_order":
        status = precheck_data.get("status")
        if status == "data_source_unavailable":
            return "data_source_unavailable"
        if status == "not_found":
            return "order_not_found"
        if status == "cancelled":
            return "order_already_cancelled"
        if status != "paid":
            return "cancel_not_allowed"
    return None


def _repository(runtime: Runtime[GraphContext] | None, fallback: OrderRepository | None) -> OrderRepository:
    if runtime is not None and runtime.context is not None:
        repository = getattr(runtime.context, "order_repository", None)
        if repository is not None:
            return repository
    if fallback is None:
        raise OrderRepositoryUnavailable("order_repository_not_configured")
    return fallback


def make_precheck_fetch_node(
    order_repository: OrderRepository | None = None,
) -> Callable[[dict], Awaitable[dict]]:
    """precheck_fetch（D3，P4）：位于 select 后、safety_check 前。pending_tool.args 含 order_id 时
    查询订单状态 -> 写 context["precheck_data"]（仅事实），不做任何授权或业务裁决；无 order_id / 订单不存在不写。"""
    async def precheck_fetch(
        state: dict,
        runtime: Runtime[GraphContext] | None = None,
    ) -> dict:
        ctx = state.get("context", {})
        pending = ctx.get("pending_tool") or {}
        order_id = (pending.get("args") or {}).get("order_id")
        if not isinstance(order_id, str) or not order_id:
            return {}
        try:
            snapshot = await asyncio.to_thread(
                _repository(runtime, order_repository).get_order_snapshot,
                order_id,
                state.get("user_id", ""),
            )
        except OrderRepositoryUnavailable:
            return {
                "context": {
                    **ctx,
                    "precheck_data": {
                        "order_id": order_id,
                        "status": "data_source_unavailable",
                    },
                }
            }
        if snapshot is None:
            return {
                "context": {
                    **ctx,
                    "precheck_data": {"order_id": order_id, "status": "not_found"},
                }
            }
        return {"context": {**ctx, "precheck_data": snapshot.to_precheck_data()}}
    return precheck_fetch


def incomplete_business_fields(args: dict, meta) -> list[str]:
    """Missing required business fields (or all-empty optional tool). Shared by select + safety."""
    if meta is None or meta.schema is None:
        return []
    fields = [name for name in meta.schema.model_fields if name != "request_id"]
    required = [name for name in fields if meta.schema.model_fields[name].is_required()]
    missing = [name for name in required if not args.get(name)]
    if missing:
        return missing
    if not required and all(not args.get(name) for name in fields):
        return fields
    return []


def _incomplete_business_fields(pending: dict, registry: ToolRegistry) -> bool:
    """True when Mutation Form still needs required fields (defer schema reject until Form Resume)."""
    tool_name = pending.get("tool")
    meta = registry.meta_for(tool_name) if isinstance(tool_name, str) else None
    return bool(incomplete_business_fields(pending.get("args") or {}, meta))


def _check(
    pending: dict,
    tier: str,
    registry: ToolRegistry,
    user_id: str,
    *,
    defer_incomplete_fields: bool = False,
) -> str | None:
    tool_name = pending.get("tool")
    meta = registry.meta_for(tool_name) if isinstance(tool_name, str) else None
    if meta is None:
        return "unknown_tool"
    if RISK_ORDER[tier] < RISK_ORDER[meta.min_risk_level]:
        return "tool_not_allowed_at_tier"
    args = pending.get("args") or {}
    skip_schema = defer_incomplete_fields and _incomplete_business_fields(pending, registry)
    if meta.schema is not None and not skip_schema:
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
    """预检节点：位于写操作执行前、interrupt 前（safety-spec §1.1）；失败不进入 interrupt 不调工具。

    Mutation 缺参时放行至 Form Interrupt；字段/业务完备性在 Form Resume 后的 recheck 裁决。
    """
    def safety_check(state: dict) -> dict:
        ctx = state.get("context", {})
        ctx = {key: value for key, value in ctx.items() if key != "precheck_reason"}
        pending = ctx.get("pending_tool") or {}
        defer = tier == "mutation" and _incomplete_business_fields(pending, registry)
        reason = _check(
            pending,
            tier,
            registry,
            state.get("user_id", ""),
            defer_incomplete_fields=defer,
        )
        if reason is None and not defer:
            reason = _apply_business_rules(pending, ctx.get("precheck_data"), state.get("user_id", ""))
        if reason is None:
            return {"context": {**ctx, "precheck": "pass"}}
        get_stream_writer()({"type": "safety_rejected", "data": {"rule": "precheck", "reason": reason}})
        return {"context": {**ctx, "precheck_reason": reason}, "execution_status": "completed"}
    return safety_check


def form_errors_from_reason(
    reason: str,
    pending: dict,
    precheck_data: dict | None = None,
) -> dict:
    """Map recheck/validate failure onto Mutation Form ``errors`` (event-spec §2.2)."""
    status = None
    if isinstance(precheck_data, dict):
        status = precheck_data.get("status")
    text = precheck_reject_text(reason, status)
    if reason == "invalid_args":
        args = pending.get("args") or {}
        empty = [key for key, value in args.items() if key != "request_id" and value == ""]
        if len(empty) == 1:
            return {empty[0]: text}
        if empty:
            return {key: text for key in empty}
    if reason in (
        "address_not_editable",
        "order_not_found",
        "order_state_changed",
        "invoice_not_allowed",
        "invoice_amount_exceeds",
        "data_boundary",
    ):
        return {"order_id": text}
    return {"_form": text}


def make_recheck_node(
    tier: str,
    registry: ToolRegistry,
    order_repository: OrderRepository | None = None,
    *,
    form_reinterrupt: bool = False,
) -> Callable[[dict], dict]:
    """恢复后、执行前最小不变量 recheck（D6/D7）：request_id 存在、工具仍在档、schema 仍有效。
    Mutation Confirm 会先用 Form Resume 字段值改写 pending_tool.args（系统 request_id 保留）；
    版本冲突只比较同一 order_id。换了一笔 Order Picker 订单时按新订单重做业务预检。
    ``form_reinterrupt=True``（Mutation Form）：失败写入 ``form_errors``，不发
    ``safety_rejected``、不结束 Form Interrupt，由条件边回到 form_interrupt 再挂起。
    完整语义校验见 safety-spec 后续增量。"""
    def recheck(state: dict, runtime: Runtime[GraphContext] | None = None) -> dict:
        ctx = state.get("context", {})
        ctx = {
            key: value
            for key, value in ctx.items()
            if key not in ("recheck_reason", "form_errors")
        }
        pending = ctx.get("pending_tool") or {}
        reason = _check(pending, tier, registry, state.get("user_id", ""))
        if not pending.get("request_id"):
            reason = reason or "missing_request_id"
        fresh_data = None
        if reason is None and (pending.get("args") or {}).get("order_id"):
            try:
                snapshot = _repository(runtime, order_repository).get_order_snapshot(
                    (pending["args"]).get("order_id", ""),
                    state.get("user_id", ""),
                )
            except OrderRepositoryUnavailable:
                reason = "data_source_unavailable"
            else:
                if snapshot is None:
                    reason = "order_not_found"
                else:
                    fresh_data = snapshot.to_precheck_data()
                    previous = ctx.get("precheck_data")
                    current_order_id = (pending.get("args") or {}).get("order_id")
                    if isinstance(previous, dict) and previous.get("version"):
                        previous_order_id = previous.get("order_id")
                        version_applies = previous_order_id in (None, current_order_id)
                        if version_applies and snapshot.version != previous.get("version"):
                            reason = "order_state_changed"
                    if reason is None:
                        reason = _apply_business_rules(pending, fresh_data, state.get("user_id", ""))
                    if reason is None:
                        ctx = {**ctx, "precheck_data": fresh_data}
        if reason:
            if form_reinterrupt:
                errors = form_errors_from_reason(
                    reason, pending, fresh_data if fresh_data is not None else ctx.get("precheck_data"),
                )
                return {
                    "context": {**ctx, "recheck_reason": reason, "form_errors": errors},
                    "execution_status": "waiting_confirmation",
                }
            get_stream_writer()({"type": "safety_rejected", "data": {"rule": "recheck", "reason": reason}})
            return {"context": {**ctx, "recheck_reason": reason}, "execution_status": "completed"}
        return {"context": {**ctx, "recheck": "ok"}}
    return recheck


def safety_final(state: dict) -> dict:
    context = state.get("context", {})
    reason = context.get("precheck_reason", "precheck")
    facts = context.get("precheck_data") or {}
    status = facts.get("status") if isinstance(facts, dict) else None
    return {"messages": [AIMessage(content=precheck_reject_text(reason, status))], "execution_status": "completed"}
