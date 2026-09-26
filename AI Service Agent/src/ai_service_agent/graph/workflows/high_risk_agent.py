"""HighRiskGraph（graph-spec §6.3，D7）：显式 workflow，非 ReAct。

select -> 安全预检 -> prepare_approval（ApprovalRequest + 状态）-> approval interrupt
-> resume(approve/reject/expired) -> recheck -> execute / 拒绝 / 过期收尾。
审批决策在图内；Approval Service 只做持久化与前置校验（approval-spec §3.1）。

路由一律用条件边（LangGraph 1.2.11 实测：Command(goto) 不抑制静态出边，静态后继与
goto 目标会并发执行导致共享 LastValue 通道双写 InvalidUpdateError；决策节点返回普通
dict，由条件边函数路由，规避该冲突——与 mutation_agent.py 同裁定）。
"""

import json

import langgraph.types as _lg_types
from langchain_core.messages import AIMessage
from langchain_core.messages import ToolMessage
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from ai_service_agent.config import get_settings
from ai_service_agent.graph.approval import make_approval_request
from ai_service_agent.graph.state import CustomerServiceState
from ai_service_agent.graph.allowed_tools import (
    make_allowed_tool_boundary_wrapper_async,
    make_allowed_tool_boundary_wrapper_sync,
)
from ai_service_agent.graph.fault_tolerance import RETRY_LLM, make_subgraph_error_handler, make_timeout_policy
from ai_service_agent.harness.tool_hooks import make_tool_hook_wrapper, make_tool_hook_wrapper_sync
from ai_service_agent.graph.workflows.copy import (
    approval_approved_text,
    approval_expired_text,
    approval_failed_text,
    approval_rejected_text,
    recheck_failed_text,
    RUNTIME_ERROR_TEXT,
)
from ai_service_agent.graph.workflows.safety import (
    make_precheck_fetch_node,
    make_recheck_node,
    make_safety_check_node,
    safety_final,
)
from ai_service_agent.graph.workflows.clarify import make_clarify_node
from ai_service_agent.graph.workflows.select import make_select_node
from ai_service_agent.tools.orders import order_items_summary
from ai_service_agent.tools.registry import ToolRegistry


def make_high_risk_graph(model, tools: list, registry: ToolRegistry, tool_hooks=None):
    def prepare_approval(state: dict) -> dict:
        pending = state["context"]["pending_tool"]
        if pending["tool"] == "cancel_order":
            order_id = pending["args"].get("order_id", "")
            facts = state.get("context", {}).get("precheck_data")
            valid_paid_facts = (
                isinstance(facts, dict)
                and facts.get("order_id") == order_id
                and facts.get("status") == "paid"
                and facts.get("user_id") == state.get("user_id", "")
            )
            if valid_paid_facts:
                summary = (
                    f"取消订单 {facts['order_id']}｜"
                    f"金额 {facts['total_amount']}｜"
                    f"商品：{order_items_summary(order_id)}｜"
                    f"申请人 {state.get('user_id', '')}｜"
                    f"状态 已付款"
                )
            else:
                return {
                    "context": {
                        **state.get("context", {}),
                        "precheck_reason": "cancel_order_facts_unavailable",
                    },
                    "execution_status": "completed",
                }
        elif pending["tool"] == "refund_apply":
            order_id = pending["args"].get("order_id", "")
            facts = state.get("context", {}).get("precheck_data")
            valid_refund_facts = (
                isinstance(facts, dict)
                and facts.get("order_id") == order_id
                and facts.get("user_id") == state.get("user_id", "")
                and facts.get("status") in ("paid", "signed")
            )
            if valid_refund_facts:
                amount = pending["args"].get("amount")
                occupied = float(facts.get("occupied_refund_amount", 0.0))
                resolved_amount = (
                    round(float(amount), 2)
                    if amount is not None
                    else round(float(facts["total_amount"]) - occupied, 2)
                )
                summary = (
                    f"退款申请 {facts['order_id']}｜"
                    f"退款金额 {resolved_amount}｜"
                    f"订单总额 {facts['total_amount']}｜"
                    f"已占用退款 {occupied}｜"
                    f"商品：{order_items_summary(order_id)}｜"
                    f"申请人 {state.get('user_id', '')}｜"
                    f"状态 {'已付款' if facts['status'] == 'paid' else '已签收'}"
                )
            else:
                summary = f"即将执行「{pending['tool']}」，参数：{pending['args']}"
        else:
            summary = f"即将执行「{pending['tool']}」，参数：{pending['args']}"
        req = make_approval_request(state.get("user_id", ""), summary, get_settings().approval_expires_minutes)
        return {
            "pending_approval": req,
            "context": {**state.get("context", {}), "approval_summary": summary},
            "execution_status": "waiting_approval",
        }

    def _prepare_target(state: dict) -> str:
        return "safety_final" if state.get("context", {}).get("precheck_reason") else "approval_pause"

    def approval_pause(state: dict) -> dict:
        req = state.get("pending_approval") or {}
        resume = _lg_types.interrupt({
            "type": "approval_required",
            "approval_id": req.get("approval_id", ""),
            "summary": req.get("summary", ""),
            "expires_at": req.get("expires_at", ""),
            "requester_user_id": req.get("requester_user_id", ""),
            "created_at": req.get("created_at", ""),
        })
        action = resume.get("action") if isinstance(resume, dict) else None
        if isinstance(resume, dict) and resume.get("result") == "expired":
            outcome = "expired"
        else:
            outcome = "approved" if action == "approve" else "rejected"  # 未知 action fail-closed
        get_stream_writer()({"type": "approval_updated", "data": {"approval_id": req.get("approval_id", ""), "status": outcome}})
        ctx = {**state.get("context", {}), "approval_result": outcome}
        if outcome == "approved":
            return {"context": ctx}
        return {"context": ctx, "execution_status": "completed"}

    def execute(state: dict) -> dict:
        pending = state["context"]["pending_tool"]
        return {
            "messages": [AIMessage(
                content="",
                tool_calls=[{
                    "name": pending["tool"],
                    "args": pending["args"],
                    "id": f"call-{pending['request_id']}",
                    "type": "tool_call",
                }],
            )]
        }

    def finalize_ok(state: dict) -> dict:
        pending = state["context"]["pending_tool"]
        tool_error = None
        for message in reversed(state.get("messages", [])):
            if not isinstance(message, ToolMessage) or message.name != pending["tool"]:
                continue
            result = message.content
            if isinstance(result, str):
                try:
                    result = json.loads(result)
                except json.JSONDecodeError:
                    result = None
            if isinstance(result, dict) and result.get("error"):
                tool_error = result["error"]
            break
        if tool_error:
            return {
                "messages": [AIMessage(content=approval_failed_text(pending["tool"], tool_error))],
                "execution_status": "failed",
            }
        return {"messages": [AIMessage(content=approval_approved_text(pending["tool"]))], "execution_status": "completed"}

    def finalize_rejected(state: dict) -> dict:
        pending = state["context"]["pending_tool"]
        return {"messages": [AIMessage(content=approval_rejected_text(pending["tool"]))], "execution_status": "completed"}

    def finalize_expired(state: dict) -> dict:
        pending = state["context"]["pending_tool"]
        return {"messages": [AIMessage(content=approval_expired_text(pending["tool"]))], "execution_status": "completed"}

    def finalize_aborted(state: dict) -> dict:
        return {"messages": [AIMessage(content=recheck_failed_text())], "execution_status": "completed"}

    def finalize_failed(state: dict) -> dict:
        return {"messages": [AIMessage(content=RUNTIME_ERROR_TEXT)], "execution_status": "failed"}

    def _select_target(state: dict) -> str:
        ctx = state.get("context", {})
        if "clarify_missing" in ctx or ctx.get("tier_clarify"):
            return "clarify"
        if "pending_tool" in ctx:
            return "precheck_fetch"
        return "safety_final"

    def _safety_target(state: dict) -> str:
        return "safety_final" if state.get("context", {}).get("precheck_reason") else "prepare_approval"

    def _approval_target(state: dict) -> str:
        outcome = state.get("context", {}).get("approval_result")
        if outcome == "approved":
            return "recheck"
        return "finalize_rejected" if outcome == "rejected" else "finalize_expired"

    def _recheck_target(state: dict) -> str:
        return "finalize_aborted" if state.get("context", {}).get("recheck_reason") else "execute"

    builder = StateGraph(CustomerServiceState)
    handler = make_subgraph_error_handler("finalize_failed")
    builder.set_node_defaults(error_handler=handler)
    builder.add_node(
        "select_llm",
        make_select_node(model, "high_risk", tools, registry),
        retry_policy=RETRY_LLM,
        timeout=make_timeout_policy(get_settings().llm_node_timeout_seconds),
    )
    builder.add_node("clarify", make_clarify_node())
    builder.add_node("precheck_fetch", make_precheck_fetch_node())
    builder.add_node("safety_check", make_safety_check_node("high_risk", registry))
    builder.add_node("safety_final", safety_final)
    builder.add_node("prepare_approval", prepare_approval)
    builder.add_node("approval_pause", approval_pause)
    builder.add_node("recheck", make_recheck_node("high_risk", registry))
    builder.add_node("execute", execute)
    hook_sync = make_tool_hook_wrapper_sync(tool_hooks) if tool_hooks else None
    hook_async = make_tool_hook_wrapper(tool_hooks) if tool_hooks else None
    boundary_sync = make_allowed_tool_boundary_wrapper_sync(next_wrapper=hook_sync)
    boundary_async = make_allowed_tool_boundary_wrapper_async(next_wrapper=hook_async)
    builder.add_node(
        "tools",
        ToolNode(
            tools,
            handle_tool_errors=False,
            wrap_tool_call=boundary_sync,
            awrap_tool_call=boundary_async,
        ),
    )
    builder.add_node("finalize_ok", finalize_ok)
    builder.add_node("finalize_failed", finalize_failed)
    builder.add_node("finalize_rejected", finalize_rejected)
    builder.add_node("finalize_expired", finalize_expired)
    builder.add_node("finalize_aborted", finalize_aborted)
    builder.add_edge(START, "select_llm")
    builder.add_conditional_edges(
        "select_llm", _select_target,
        {"clarify": "clarify", "precheck_fetch": "precheck_fetch", "safety_final": "safety_final"},
    )
    builder.add_edge("clarify", END)
    builder.add_edge("precheck_fetch", "safety_check")
    builder.add_conditional_edges(
        "safety_check", _safety_target, {"prepare_approval": "prepare_approval", "safety_final": "safety_final"}
    )
    builder.add_conditional_edges(
        "prepare_approval", _prepare_target,
        {"approval_pause": "approval_pause", "safety_final": "safety_final"},
    )
    builder.add_conditional_edges(
        "approval_pause", _approval_target,
        {"recheck": "recheck", "finalize_rejected": "finalize_rejected", "finalize_expired": "finalize_expired"},
    )
    builder.add_conditional_edges(
        "recheck", _recheck_target, {"execute": "execute", "finalize_aborted": "finalize_aborted"}
    )
    builder.add_edge("execute", "tools")
    builder.add_edge("tools", "finalize_ok")
    builder.add_edge("finalize_ok", END)
    builder.add_edge("finalize_failed", END)
    builder.add_edge("finalize_rejected", END)
    builder.add_edge("finalize_expired", END)
    builder.add_edge("finalize_aborted", END)
    builder.add_edge("safety_final", END)
    return builder.compile()
