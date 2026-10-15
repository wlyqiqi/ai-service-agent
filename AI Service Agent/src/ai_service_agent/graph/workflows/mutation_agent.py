"""MutationGraph（graph-spec §6.2）：显式 workflow，非 ReAct。

select（tool 已知即可，业务字段可不全）-> 安全预检（缺参时放行至表单）
-> prepare_form -> Form Interrupt -> Form Resume(confirm/cancel)
-> recheck -> execute(写工具) / 取消收尾。
不再走聊天 clarification_required（缺业务字段）或 confirmation_required。
Confirm 后校验/业务预检失败：条件边回到 form_interrupt，同一 form_session_id
再次 Form Interrupt（SSE 投影 form_rejected），不走 safety_rejected 收尾。
Confirm 的写库参数来自 Form Resume 字段值（含 Order Picker 的 order_id），
再加系统 request_id，由 execute 构造单次 tool_call，经 ToolNode 写一次。
select 留下的 pending_tool.args 是 prefills，不是 Confirm 真源（Form-Only Write）。
Cancel 哨兵不写库。确认结果写回 context（graph-spec §9），不新增 State 字段。

路由一律用条件边（LangGraph 1.2.11 实测：Command(goto) 不抑制静态出边，静态后继与
goto 目标会并发执行导致共享 LastValue 通道双写 InvalidUpdateError；决策节点返回普通
dict，由条件边函数路由，规避该冲突）。
"""

import json
import uuid
from typing import Any, Literal

import langgraph.types as _lg_types
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.runtime import Runtime
from pydantic import BaseModel, create_model

from ai_service_agent.domain.orders import OrderRepositoryUnavailable
from ai_service_agent.graph.runtime_context import GraphContext
from ai_service_agent.graph.state import CustomerServiceState
from ai_service_agent.graph.allowed_tools import (
    make_allowed_tool_boundary_wrapper_async,
    make_allowed_tool_boundary_wrapper_sync,
)
from ai_service_agent.config import get_settings
from ai_service_agent.graph.fault_tolerance import RETRY_LLM, make_subgraph_error_handler, make_timeout_policy
from ai_service_agent.harness.tool_hooks import make_tool_hook_wrapper, make_tool_hook_wrapper_sync
from ai_service_agent.graph.workflows.copy import (
    completion_text,
    confirmation_summary,
    mutation_tool_failed_text,
    operation_cancelled_text,
    update_address_empty_state_text,
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
from ai_service_agent.tools.registry import ToolRegistry


def _form_resume_model(tool_schema: type[BaseModel] | None) -> type[BaseModel]:
    """Confirm Form Resume shape derived from the tool schema.

    Business fields stay optional so Form Cancel (``{"action": "cancel"}``) still
    validates. ``request_id`` is not a form field; the graph injects it.
    """
    definitions: dict[str, Any] = {
        "action": (Literal["confirm", "cancel"], ...),
    }
    if tool_schema is not None:
        for name, field in tool_schema.model_fields.items():
            if name in ("request_id", "action"):
                continue
            definitions[name] = (field.annotation | None, None)
    return create_model("MutationFormResume", **definitions)


def _resume_action(resume) -> str | None:
    if isinstance(resume, BaseModel):
        action = resume.action
    elif isinstance(resume, dict):
        action = resume.get("action")
    else:
        return None
    return action if isinstance(action, str) else None


def _resume_field_values(resume) -> dict:
    """Structured Confirm values. Ignores action and any client request_id."""
    if isinstance(resume, BaseModel):
        data = resume.model_dump()
    elif isinstance(resume, dict):
        data = dict(resume)
    else:
        return {}
    return {
        key: value
        for key, value in data.items()
        if key not in ("action", "request_id", "current_address", "abandon_footnote")
        and value is not None
    }


def make_mutation_graph(
    model,
    tools: list,
    registry: ToolRegistry,
    tool_hooks=None,
    order_repository=None,
):
    def prepare_form(state: dict) -> dict:
        # Mint form_session_id before Form Interrupt so resume re-runs of
        # form_interrupt keep the same session (interrupt-before is idempotent).
        # Recheck failures loop back to form_interrupt without prepare_form, so the
        # id stays stable for form_rejected; a later new form gets a fresh id here.
        ctx = {
            key: item
            for key, item in dict(state.get("context") or {}).items()
            if key not in ("form_errors", "recheck_reason")
        }
        ctx["form_session_id"] = str(uuid.uuid4())
        return {"execution_status": "waiting_confirmation", "context": ctx}

    def _order_repository(runtime: Runtime[GraphContext] | None):
        if runtime is not None and runtime.context is not None:
            repository = getattr(runtime.context, "order_repository", None)
            if repository is not None:
                return repository
        return order_repository

    def form_interrupt(state: dict, runtime: Runtime[GraphContext] | None = None) -> dict:
        # interrupt once per invocation; validate/precheck failures loop back via
        # conditional edge with form_errors (Closed MutationGraph / event-spec §2.2).
        ctx = dict(state.get("context") or {})
        pending = ctx["pending_tool"]
        form_session_id = ctx.get("form_session_id") or str(uuid.uuid4())
        errors = ctx.get("form_errors")
        if errors in (None, {}, [], ""):
            errors = None
        candidates: list[dict] = []
        repository = _order_repository(runtime)
        user_id = state.get("user_id") or ""
        if repository is not None and user_id:
            try:
                candidates = repository.list_order_picker_candidates(user_id, pending["tool"])
            except (OrderRepositoryUnavailable, ValueError):
                candidates = []
        current_address = ""
        if pending["tool"] == "update_address" and repository is not None and user_id:
            try:
                profile = repository.get_user_profile(user_id) or {}
                current_address = str(profile.get("address") or "")
            except OrderRepositoryUnavailable:
                current_address = ""
        # #71 empty state: no eligible paid orders → plain text, no empty Form Interrupt.
        if pending["tool"] == "update_address" and not candidates and errors is None:
            next_ctx = {
                key: item
                for key, item in ctx.items()
                if key
                not in (
                    "form_errors",
                    "recheck_reason",
                    "confirm_result",
                    "form_session_id",
                    "pending_tool",
                )
            }
            next_ctx["confirm_result"] = "empty"
            return {
                "messages": [AIMessage(content=update_address_empty_state_text())],
                "execution_status": "completed",
                "context": next_ctx,
            }
        meta = registry.meta_for(pending["tool"])
        value = {
            "tool": pending["tool"],
            "order_candidates": candidates,
            "summary": confirmation_summary(pending["tool"], pending["args"]),
            "form_session_id": form_session_id,
        }
        if pending["tool"] == "update_address":
            value["current_address"] = current_address
        if errors:
            value["errors"] = errors
        resume = _lg_types.interrupt(
            value,
            response_schema=_form_resume_model(None if meta is None else meta.schema),
        )
        action = _resume_action(resume)
        accepted = action == "confirm"
        next_ctx = {
            key: item
            for key, item in ctx.items()
            if key not in ("form_errors", "recheck_reason", "confirm_result")
        }
        next_ctx["form_session_id"] = form_session_id
        next_ctx["confirm_result"] = "confirm" if accepted else "cancel"
        if not accepted:
            next_ctx.pop("form_session_id", None)
            return {"context": next_ctx, "execution_status": "completed"}
        request_id = pending.get("request_id") or str(uuid.uuid4())
        next_ctx["pending_tool"] = {
            "tool": pending["tool"],
            "args": {**_resume_field_values(resume), "request_id": request_id},
            "request_id": request_id,
        }
        return {"context": next_ctx, "execution_status": "waiting_confirmation"}

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
                "messages": [AIMessage(content=mutation_tool_failed_text(pending["tool"], tool_error))],
                "execution_status": "failed",
            }
        return {"messages": [AIMessage(content=completion_text(pending["tool"], pending["args"]))], "execution_status": "completed"}

    def finalize_rejected(state: dict) -> dict:
        return {"messages": [AIMessage(content=operation_cancelled_text())], "execution_status": "completed"}

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
        return "safety_final" if state.get("context", {}).get("precheck_reason") else "prepare_form"

    def _confirm_target(state: dict) -> str:
        result = state.get("context", {}).get("confirm_result")
        if result == "confirm":
            return "recheck"
        if result == "empty":
            # Empty-state message already written; skip cancel copy (#71).
            return "end"
        return "finalize_rejected"

    def _recheck_target(state: dict) -> str:
        # Closed MutationGraph: same form session re-interrupt (form_rejected), not abort.
        return "form_interrupt" if state.get("context", {}).get("recheck_reason") else "execute"

    builder = StateGraph(CustomerServiceState, context_schema=GraphContext).set_node_defaults(
        error_handler=make_subgraph_error_handler("finalize_failed"),
    )
    builder.add_node(
        "select_llm",
        make_select_node(model, "mutation", tools, registry),
        retry_policy=RETRY_LLM,
        timeout=make_timeout_policy(get_settings().llm_node_timeout_seconds),
    )
    builder.add_node("clarify", make_clarify_node())
    builder.add_node("precheck_fetch", make_precheck_fetch_node(order_repository))
    builder.add_node("safety_check", make_safety_check_node("mutation", registry))
    builder.add_node("safety_final", safety_final)
    builder.add_node("prepare_form", prepare_form)
    builder.add_node("form_interrupt", form_interrupt)
    builder.add_node(
        "recheck",
        make_recheck_node("mutation", registry, order_repository, form_reinterrupt=True),
    )
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
    builder.add_node("finalize_rejected", finalize_rejected)
    builder.add_node("finalize_failed", finalize_failed)
    builder.add_edge(START, "select_llm")
    builder.add_conditional_edges(
        "select_llm", _select_target,
        {"clarify": "clarify", "precheck_fetch": "precheck_fetch", "safety_final": "safety_final"},
    )
    builder.add_edge("clarify", END)
    builder.add_edge("precheck_fetch", "safety_check")
    builder.add_conditional_edges(
        "safety_check", _safety_target, {"prepare_form": "prepare_form", "safety_final": "safety_final"}
    )
    builder.add_edge("prepare_form", "form_interrupt")
    builder.add_conditional_edges(
        "form_interrupt",
        _confirm_target,
        {"recheck": "recheck", "finalize_rejected": "finalize_rejected", "end": END},
    )
    builder.add_conditional_edges(
        "recheck", _recheck_target, {"execute": "execute", "form_interrupt": "form_interrupt"}
    )
    builder.add_edge("execute", "tools")
    builder.add_edge("tools", "finalize_ok")
    builder.add_edge("finalize_ok", END)
    builder.add_edge("finalize_rejected", END)
    builder.add_edge("safety_final", END)
    builder.add_edge("finalize_failed", END)
    return builder.compile()
