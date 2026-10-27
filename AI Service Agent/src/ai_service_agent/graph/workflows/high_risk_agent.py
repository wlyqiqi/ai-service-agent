"""HighRiskGraph（graph-spec §6.3，D7）：显式 workflow，非 ReAct。

select -> 安全预检 -> prepare_approval（ApprovalRequest + 状态）-> approval interrupt
-> resume(approve/reject/expired) -> recheck -> execute / 拒绝 / 过期收尾。
审批决策在图内；Approval Service 只做持久化与前置校验（approval-spec §3.1）。

路由一律用条件边（LangGraph 1.2.11 实测：Command(goto) 不抑制静态出边，静态后继与
goto 目标会并发执行导致共享 LastValue 通道双写 InvalidUpdateError；决策节点返回普通
dict，由条件边函数路由，规避该冲突——与 mutation_agent.py 同裁定）。
"""

import uuid

import langgraph.types as _lg_types
from langchain_core.messages import AIMessage
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.runtime import Runtime

from ai_service_agent.config import get_settings
from ai_service_agent.domain.orders import OrderRepositoryUnavailable
from ai_service_agent.graph.approval import make_approval_request, resolve_approval_outcome
from ai_service_agent.graph.messages import read_tool_error
from ai_service_agent.graph.runtime_context import GraphContext
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
    approval_summary,
    clarify_no_candidates_text,
    confirmation_summary,
    operation_cancelled_text,
    recheck_failed_text,
    RUNTIME_ERROR_TEXT,
)
from ai_service_agent.graph.workflows.form_resume import (
    FORM_SCRUB_KEYS,
    form_resume_model,
    prepare_form_update,
    resume_action,
    resume_field_values,
    schema_field_args,
)
from ai_service_agent.graph.workflows.safety import (
    incomplete_business_fields,
    make_precheck_fetch_node,
    make_recheck_node,
    make_safety_check_node,
    safety_final,
)
from ai_service_agent.graph.workflows.clarify import make_clarify_node
from ai_service_agent.graph.workflows.high_risk_select import make_high_risk_select_node
from ai_service_agent.tools.registry import ToolRegistry


def make_high_risk_graph(
    model,
    tools: list,
    registry: ToolRegistry,
    tool_hooks=None,
    order_repository=None,
):
    def _order_repository(runtime: Runtime[GraphContext] | None):
        if runtime is not None and runtime.context is not None:
            repository = getattr(runtime.context, "order_repository", None)
            if repository is not None:
                return repository
        return order_repository

    def prepare_form(state: dict) -> dict:
        return prepare_form_update(state)

    def form_interrupt(state: dict, runtime: Runtime[GraphContext] | None = None) -> dict:
        """Missing-arg collection for cancel/refund. Confirm continues to approval, not a write."""
        ctx = dict(state.get("context") or {})
        pending = ctx["pending_tool"]
        form_session_id = ctx.get("form_session_id") or str(uuid.uuid4())
        repository = _order_repository(runtime)
        user_id = state.get("user_id") or ""
        candidates: list[dict] = []
        if repository is not None and user_id:
            try:
                candidates = repository.list_actionable_orders(user_id, pending["tool"])
            except (OrderRepositoryUnavailable, ValueError):
                candidates = []
        if not candidates:
            next_ctx = {
                key: item
                for key, item in ctx.items()
                if key not in ("form_errors", "recheck_reason", "confirm_result", "form_session_id")
            }
            next_ctx["confirm_result"] = "empty"
            return {
                "messages": [AIMessage(content=clarify_no_candidates_text())],
                "execution_status": "completed",
                "context": next_ctx,
            }
        meta = registry.meta_for(pending["tool"])
        schema = None if meta is None else meta.schema
        prefills = {
            key: value
            for key, value in (pending.get("args") or {}).items()
            if key != "request_id" and value not in (None, "")
        }
        value = {
            "tool": pending["tool"],
            "order_candidates": candidates,
            "summary": confirmation_summary(pending["tool"], pending["args"]),
            "form_session_id": form_session_id,
            "prefills": prefills,
        }
        resume = _lg_types.interrupt(value, response_schema=form_resume_model(schema))
        action = resume_action(resume)
        accepted = action == "confirm"
        next_ctx = {
            key: item
            for key, item in ctx.items()
            if key not in FORM_SCRUB_KEYS
        }
        next_ctx["form_session_id"] = form_session_id
        next_ctx["confirm_result"] = "confirm" if accepted else "cancel"
        if not accepted:
            next_ctx.pop("form_session_id", None)
            return {"context": next_ctx, "execution_status": "completed"}
        request_id = pending.get("request_id") or str(uuid.uuid4())
        next_ctx["pending_tool"] = {
            "tool": pending["tool"],
            "args": {
                **schema_field_args(resume_field_values(resume), schema),
                "request_id": request_id,
            },
            "request_id": request_id,
        }
        return {"context": next_ctx, "execution_status": "waiting_confirmation"}

    def prepare_approval(state: dict) -> dict:
        pending = state["context"]["pending_tool"]
        ctx = state.get("context", {})
        summary, unavailable_reason = approval_summary(
            pending["tool"],
            pending.get("args") or {},
            ctx.get("precheck_data"),
            state.get("user_id", ""),
        )
        if unavailable_reason:
            return {
                "context": {**ctx, "precheck_reason": unavailable_reason},
                "execution_status": "completed",
            }
        req = make_approval_request(state.get("user_id", ""), summary, get_settings().approval_expires_minutes)
        return {
            "pending_approval": req,
            "context": {**ctx, "approval_summary": summary},
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
        outcome = resolve_approval_outcome(resume)
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

    def finalize(state: dict) -> dict:
        """单收尾节点：按固定优先级产出文案与 execution_status（逐条等价于原 6 节点）。

        判定顺序（严格）：
        1. 工具返回 error（成功执行路径 / 执行期异常）-> approval_failed_text，failed
        2. execution_status == "failed"（无工具 error）-> RUNTIME_ERROR_TEXT，failed
        3. approval_result == "rejected" -> approval_rejected_text
        4. approval_result == "expired"  -> approval_expired_text
        5. confirm_result == "cancel"    -> operation_cancelled_text
        6. recheck_reason 非空           -> recheck_failed_text
        7. 其余                          -> approval_approved_text
        除 1/2 外 execution_status 一律 completed。

        防御：pending_tool 可能不存在（high_risk_select_llm 崩溃时 error_handler 直接 goto 本节点），
        故 tool 一律用 `(context.get("pending_tool") or {}).get("tool", "")` 读取。
        """
        ctx = state.get("context") or {}
        pending = ctx.get("pending_tool") or {}
        tool = pending.get("tool", "")
        status = state.get("execution_status")

        # 工具结果只在「本轮确实执行过工具」或「执行期异常」时读取：
        #   - 成功路径：approval_result == "approved" 且未 abort（recheck_reason 为空）
        #   - 执行期异常：error_handler 已置 failed 后 goto 本节点
        # 其余收尾路径（rejected / expired / form cancel / recheck abort）不读，
        # 避免把历史 turn 残留的 ToolMessage error 误判为本轮失败。
        executed = ctx.get("approval_result") == "approved" and not ctx.get("recheck_reason")
        tool_error = (
            read_tool_error(state.get("messages", []), tool)
            if tool and (executed or status == "failed")
            else None
        )
        if tool_error:
            return {
                "messages": [AIMessage(content=approval_failed_text(tool, tool_error))],
                "execution_status": "failed",
            }
        if status == "failed":
            return {
                "messages": [AIMessage(content=RUNTIME_ERROR_TEXT)],
                "execution_status": "failed",
            }
        if ctx.get("approval_result") == "rejected":
            return {
                "messages": [AIMessage(content=approval_rejected_text(tool))],
                "execution_status": "completed",
            }
        if ctx.get("approval_result") == "expired":
            return {
                "messages": [AIMessage(content=approval_expired_text(tool))],
                "execution_status": "completed",
            }
        if ctx.get("confirm_result") == "cancel":
            return {
                "messages": [AIMessage(content=operation_cancelled_text())],
                "execution_status": "completed",
            }
        if ctx.get("recheck_reason"):
            return {
                "messages": [AIMessage(content=recheck_failed_text())],
                "execution_status": "completed",
            }
        return {
            "messages": [AIMessage(content=approval_approved_text(tool))],
            "execution_status": "completed",
        }

    def _select_target(state: dict) -> str:
        ctx = state.get("context", {})
        if "clarify_missing" in ctx or ctx.get("tier_clarify"):
            return "clarify"
        pending = ctx.get("pending_tool")
        if pending:
            meta = registry.meta_for(pending.get("tool")) if isinstance(pending.get("tool"), str) else None
            if incomplete_business_fields(pending.get("args") or {}, meta):
                return "prepare_form"
            return "precheck_fetch"
        return "safety_final"

    def _form_target(state: dict) -> str:
        result = state.get("context", {}).get("confirm_result")
        if result == "confirm":
            return "precheck_fetch"
        if result == "empty":
            return "end"
        return "finalize_form_cancelled"

    def _safety_target(state: dict) -> str:
        return "safety_final" if state.get("context", {}).get("precheck_reason") else "prepare_approval"

    def _approval_target(state: dict) -> str:
        outcome = state.get("context", {}).get("approval_result")
        if outcome == "approved":
            return "recheck"
        return "finalize_rejected" if outcome == "rejected" else "finalize_expired"

    def _recheck_target(state: dict) -> str:
        return "finalize_aborted" if state.get("context", {}).get("recheck_reason") else "execute"

    builder = StateGraph(CustomerServiceState, context_schema=GraphContext)
    handler = make_subgraph_error_handler("finalize")
    builder.set_node_defaults(error_handler=handler)
    builder.add_node(
        "high_risk_select_llm",
        make_high_risk_select_node(model, tools, registry, order_repository),
        retry_policy=RETRY_LLM,
        timeout=make_timeout_policy(get_settings().llm_node_timeout_seconds),
    )
    builder.add_node("clarify", make_clarify_node())
    builder.add_node("prepare_form", prepare_form)
    builder.add_node("form_interrupt", form_interrupt)
    builder.add_node("precheck_fetch", make_precheck_fetch_node(order_repository))
    builder.add_node("safety_check", make_safety_check_node("high_risk", registry))
    builder.add_node("safety_final", safety_final)
    builder.add_node("prepare_approval", prepare_approval)
    builder.add_node("approval_pause", approval_pause)
    builder.add_node("recheck", make_recheck_node("high_risk", registry, order_repository))
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
    builder.add_node("finalize", finalize)
    builder.add_edge(START, "high_risk_select_llm")
    builder.add_conditional_edges(
        "high_risk_select_llm", _select_target,
        {
            "clarify": "clarify",
            "prepare_form": "prepare_form",
            "precheck_fetch": "precheck_fetch",
            "safety_final": "safety_final",
        },
    )
    builder.add_edge("clarify", END)
    builder.add_edge("prepare_form", "form_interrupt")
    builder.add_conditional_edges(
        "form_interrupt",
        _form_target,
        {
            "precheck_fetch": "precheck_fetch",
            "finalize_form_cancelled": "finalize",
            "end": END,
        },
    )
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
        {"recheck": "recheck", "finalize_rejected": "finalize", "finalize_expired": "finalize"},
    )
    builder.add_conditional_edges(
        "recheck", _recheck_target, {"execute": "execute", "finalize_aborted": "finalize"}
    )
    builder.add_edge("execute", "tools")
    builder.add_edge("tools", "finalize")
    builder.add_edge("finalize", END)
    builder.add_edge("safety_final", END)
    return builder.compile()
