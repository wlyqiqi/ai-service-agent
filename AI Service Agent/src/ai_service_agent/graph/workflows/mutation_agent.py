"""MutationGraph（graph-spec §6.2，D6）：显式 workflow，非 ReAct。

select -> 安全预检 -> prepare_confirm（发 confirmation_required + 状态）-> confirm interrupt
-> resume(confirm/cancel) -> recheck -> execute(写工具) / 拒绝收尾。
确认结果写回 context（graph-spec §9），不新增 State 字段。

路由一律用条件边（LangGraph 1.2.11 实测：Command(goto) 不抑制静态出边，静态后继与
goto 目标会并发执行导致共享 LastValue 通道双写 InvalidUpdateError；决策节点返回普通
dict，由条件边函数路由，规避该冲突）。
"""

import json

import langgraph.types as _lg_types
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

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
from ai_service_agent.tools.registry import ToolRegistry


def make_mutation_graph(model, tools: list, registry: ToolRegistry, tool_hooks=None):
    def prepare_confirm(state: dict) -> dict:
        pending = state["context"]["pending_tool"]
        summary = confirmation_summary(pending["tool"], pending["args"])
        get_stream_writer()({"type": "confirmation_required", "data": {"summary": summary}})
        return {"context": {**state.get("context", {}), "confirm_summary": summary}, "execution_status": "waiting_confirmation"}

    def confirm_pause(state: dict) -> dict:
        resume = _lg_types.interrupt({"type": "confirmation_required", "summary": state["context"]["confirm_summary"]})
        action = resume.get("action") if isinstance(resume, dict) else None
        accepted = action == "confirm"
        get_stream_writer()({"type": "confirmation_updated", "data": {"action": "confirm" if accepted else "cancel"}})
        ctx = {**state.get("context", {}), "confirm_result": "confirm" if accepted else "cancel"}
        if not accepted:
            return {"context": ctx, "execution_status": "completed"}
        return {"context": ctx}

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
        return "safety_final" if state.get("context", {}).get("precheck_reason") else "prepare_confirm"

    def _confirm_target(state: dict) -> str:
        return "recheck" if state.get("context", {}).get("confirm_result") == "confirm" else "finalize_rejected"

    def _recheck_target(state: dict) -> str:
        return "finalize_aborted" if state.get("context", {}).get("recheck_reason") else "execute"

    builder = StateGraph(CustomerServiceState).set_node_defaults(
        error_handler=make_subgraph_error_handler("finalize_failed"),
    )
    builder.add_node(
        "select_llm",
        make_select_node(model, "mutation", tools, registry),
        retry_policy=RETRY_LLM,
        timeout=make_timeout_policy(get_settings().llm_node_timeout_seconds),
    )
    builder.add_node("clarify", make_clarify_node())
    builder.add_node("precheck_fetch", make_precheck_fetch_node())
    builder.add_node("safety_check", make_safety_check_node("mutation", registry))
    builder.add_node("safety_final", safety_final)
    builder.add_node("prepare_confirm", prepare_confirm)
    builder.add_node("confirm_pause", confirm_pause)
    builder.add_node("recheck", make_recheck_node("mutation", registry))
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
    builder.add_node("finalize_aborted", finalize_aborted)
    builder.add_node("finalize_failed", finalize_failed)
    builder.add_edge(START, "select_llm")
    builder.add_conditional_edges(
        "select_llm", _select_target,
        {"clarify": "clarify", "precheck_fetch": "precheck_fetch", "safety_final": "safety_final"},
    )
    builder.add_edge("clarify", END)
    builder.add_edge("precheck_fetch", "safety_check")
    builder.add_conditional_edges(
        "safety_check", _safety_target, {"prepare_confirm": "prepare_confirm", "safety_final": "safety_final"}
    )
    builder.add_edge("prepare_confirm", "confirm_pause")
    builder.add_conditional_edges(
        "confirm_pause", _confirm_target, {"recheck": "recheck", "finalize_rejected": "finalize_rejected"}
    )
    builder.add_conditional_edges(
        "recheck", _recheck_target, {"execute": "execute", "finalize_aborted": "finalize_aborted"}
    )
    builder.add_edge("execute", "tools")
    builder.add_edge("tools", "finalize_ok")
    builder.add_edge("finalize_ok", END)
    builder.add_edge("finalize_rejected", END)
    builder.add_edge("finalize_aborted", END)
    builder.add_edge("safety_final", END)
    builder.add_edge("finalize_failed", END)
    return builder.compile()
