"""安全预检（safety-spec.md §1，D4 v1 范围）与最小不变量 recheck（D6/D7）。

v1 只覆盖：参数 schema 白名单 + min_risk_level 匹配 + user_id 登录态 + 数据边界（显式 owner_user_id）。
业务规则（频次/额度/状态机）、敏感操作复核留后续增量（不在 P3 强制，skip(precheck_rule_pending)）。
决策节点返回普通 dict + 条件边路由（LangGraph 1.2.11 实测：Command(goto) 不抑制静态出边，
静态后继与 goto 目标会并发执行导致双写；路由一律走条件边）。
"""

from typing import Callable

from langchain_core.messages import AIMessage
from langgraph.config import get_stream_writer

from ai_service_agent.tools.registry import ToolRegistry

PRECHECK_REJECT_TEXT = "该操作未能通过安全校验，已拒绝执行。"

RISK_ORDER = {"read": 0, "mutation": 1, "high_risk": 2}


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
    if not user_id:
        return "not_logged_in"
    if args.get("owner_user_id") and args["owner_user_id"] != user_id:
        return "data_boundary"
    return None


def make_safety_check_node(tier: str, registry: ToolRegistry) -> Callable[[dict], dict]:
    """预检节点：位于写操作执行前、interrupt 前（safety-spec §1.1）；失败不进入 interrupt 不调工具。"""
    def safety_check(state: dict) -> dict:
        reason = _check(state.get("context", {}).get("pending_tool") or {}, tier, registry, state.get("user_id", ""))
        if reason is None:
            return {"context": {**state.get("context", {}), "precheck": "pass"}}
        get_stream_writer()({"type": "safety_rejected", "data": {"rule": "precheck", "reason": reason}})
        return {"context": {**state.get("context", {}), "precheck_reason": reason}, "execution_status": "completed"}
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
