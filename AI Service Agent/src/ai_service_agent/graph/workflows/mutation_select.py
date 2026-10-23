"""Mutation Select：MutationGraph 专属写操作选择节点（CONTEXT.md / ADR 0004 follow-on）。

一次调用要么发出本档 tool_call → pending_tool（缺字段交 Form），要么自然语言
Operation Clarify。不与 HighRisk select 共享节点工厂、提示词或模型实例。
"""

from collections.abc import Awaitable, Callable
import json
import uuid

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer
from pydantic import ValidationError

from ai_service_agent.config import get_settings
from ai_service_agent.graph.workflows.copy import clarify_fallback_text, operation_clarify_text
from ai_service_agent.graph.workflows.safety import incomplete_business_fields
from ai_service_agent.graph.workflows.select import (
    _bind_json,
    _budgeted_select_messages,
    _build_examples,
    _describe_args_schema,
    _is_uuid4,
    _normalize_placeholder_args,
)
from ai_service_agent.prompts import (
    MUTATION_SELECT_BINDTOOLS_TEMPLATE,
    MUTATION_SELECT_SYSTEM_TEMPLATE,
)
from ai_service_agent.tools.registry import ToolRegistry

_TIER = "mutation"


def make_mutation_select_node(
    model,
    tools: list,
    registry: ToolRegistry,
) -> Callable[[dict], Awaitable[dict]]:
    """Mutation 档写工具选择；无 tier 参数，固定 mutation 允许集。"""
    tier_tools = [
        t
        for t in tools
        if (meta := registry.meta_for(t.name)) is not None and meta.min_risk_level == _TIER
    ]
    lines_by_name = {}
    for t in tier_tools:
        meta = registry.meta_for(t.name)
        line = f"- {t.name}: {meta.description}"
        contract = _describe_args_schema(meta)
        if contract:
            line += f"\n  参数：{contract}"
        lines_by_name[t.name] = line
    is_fake = isinstance(model, FakeMessagesListChatModel)
    if is_fake:
        bound = _bind_json(model)
    else:
        bound = model.bind_tools(tier_tools, tool_choice="auto")

    def _reject(state: dict, reason: str) -> dict:
        try:
            get_stream_writer()(
                {"type": "safety_rejected", "data": {"rule": "precheck", "reason": reason}}
            )
        except RuntimeError:
            pass
        return {
            "context": {**state.get("context", {}), "precheck_reason": reason},
            "execution_status": "completed",
        }

    def _operation_clarify_inline(state: dict, content: str) -> dict:
        ctx = {**state.get("context", {})}
        ctx.pop("clarify_missing", None)
        ctx.pop("pending_tool", None)
        ctx.pop("tier_clarify", None)
        ctx.pop("operation_clarify", None)
        ctx.pop("cancellation_candidates", None)
        count = int(ctx.get("clarify_count", 0))
        max_turns = get_settings().clarify_max_turns
        if count >= max_turns:
            return {
                "messages": [AIMessage(content=clarify_fallback_text())],
                "context": {**ctx, "clarify_count": count + 1},
                "execution_status": "completed",
            }
        text = (content or "").strip() or operation_clarify_text()
        try:
            get_stream_writer()(
                {
                    "type": "clarification_required",
                    "data": {"missing": [], "reason": "operation_unclear"},
                }
            )
        except RuntimeError:
            pass
        return {
            "messages": [AIMessage(content=text)],
            "context": {**ctx, "clarify_count": count + 1},
            "execution_status": "clarifying",
        }

    def _partial_args_for_form(args: dict, meta) -> dict:
        known = set(meta.schema.model_fields)
        partial = {key: value for key, value in args.items() if key in known}
        partial.setdefault("request_id", str(uuid.uuid4()))
        return partial

    def _pending_for_form(state: dict, tool_name: str, args: dict) -> dict:
        ctx = {**state.get("context", {})}
        ctx.pop("clarify_missing", None)
        ctx.pop("tier_clarify", None)
        ctx.pop("operation_clarify", None)
        ctx.pop("cancellation_candidates", None)
        ctx["clarify_count"] = 0
        ctx["pending_tool"] = {
            "tool": tool_name,
            "args": args,
            "request_id": args["request_id"],
        }
        return {"context": ctx}

    async def select(
        state: dict,
        config: RunnableConfig | None = None,
        runtime=None,
    ) -> dict:
        current_context = {**state.get("context", {})}
        current_context.pop("cancellation_candidates", None)
        state = {**state, "context": current_context}
        route_allowed = state.get("allowed_tools")
        if route_allowed is None:
            selected_tools = tier_tools
        else:
            route_allowed = {str(name) for name in route_allowed}
            selected_tools = [t for t in tier_tools if t.name in route_allowed]
        allowed = {t.name for t in selected_tools}
        tool_list = "\n".join(lines_by_name[t.name] for t in selected_tools)
        examples = _build_examples(selected_tools, registry)
        if is_fake:
            messages = _budgeted_select_messages(
                state,
                selected_tools,
                SystemMessage(
                    content=MUTATION_SELECT_SYSTEM_TEMPLATE.format(
                        tool_list=tool_list,
                        examples=examples,
                    )
                ),
            )
            resp = await bound.ainvoke(messages, config=config)
            try:
                payload = json.loads(resp.content)
                tool_name = payload.get("tool")
                if not tool_name:
                    return _operation_clarify_inline(
                        state, str(payload.get("content") or payload.get("clarify") or "")
                    )
                args = dict(payload.get("args") or {})
            except Exception:
                return _reject(state, "select_parse_failed")
        else:
            messages = _budgeted_select_messages(
                state,
                selected_tools,
                SystemMessage(
                    content=MUTATION_SELECT_BINDTOOLS_TEMPLATE.format(
                        tool_list=tool_list,
                        examples=examples,
                    )
                ),
            )
            result = await bound.ainvoke(messages, config=config)
            if not result.tool_calls:
                return _operation_clarify_inline(state, str(result.content or ""))
            tool_call = result.tool_calls[0]
            tool_name = tool_call["name"]
            args = dict(tool_call.get("args") or {})

        existing = args.get("request_id")
        if not isinstance(existing, str) or not _is_uuid4(existing):
            args["request_id"] = str(uuid.uuid4())
        meta = registry.meta_for(tool_name) if isinstance(tool_name, str) else None
        if meta is None:
            return _reject(state, "unknown_tool")
        route_allowed = state.get("allowed_tools")
        if route_allowed is not None and tool_name not in route_allowed:
            return _reject(state, "tool_not_allowed")
        if tool_name not in allowed:
            return _reject(state, "unknown_tool")
        if meta.schema is not None:
            args = _normalize_placeholder_args(args, meta)
            try:
                args = meta.schema.model_validate(args).model_dump()
            except ValidationError as exc:
                errors = exc.errors()
                missing = [e["loc"][0] for e in errors if e["type"] == "missing" and e["loc"]]
                if missing and len(missing) == len(errors):
                    missing = [f for f in missing if f != "request_id"]
                    if missing:
                        return _pending_for_form(
                            state, tool_name, _partial_args_for_form(args, meta)
                        )
                return _reject(state, "invalid_args")
            incomplete = incomplete_business_fields(args, meta)
            if incomplete:
                return _pending_for_form(
                    state, tool_name, _partial_args_for_form(args, meta)
                )
        ctx = {**state.get("context", {})}
        ctx.pop("clarify_missing", None)
        ctx.pop("tier_clarify", None)
        ctx.pop("operation_clarify", None)
        ctx.pop("cancellation_candidates", None)
        ctx["clarify_count"] = 0
        ctx["pending_tool"] = {
            "tool": tool_name,
            "args": args,
            "request_id": args["request_id"],
        }
        return {"context": ctx}

    return select
