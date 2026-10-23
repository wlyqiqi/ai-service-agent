"""HighRisk Select：HighRiskGraph 专属高风险操作选择节点（CONTEXT.md / ADR 0005）。

一次调用要么发出本档 tool_call → pending_tool / clarify_missing，要么无 tool_call
→ tier_clarify（走既有 clarify 节点）。P0 不内联 Operation Clarify。
不与 Mutation / Read 的 select 共享节点、提示词或模型实例。
"""

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer
from langgraph.runtime import Runtime
from pydantic import ValidationError

from ai_service_agent.domain.orders import OrderRepository, OrderRepositoryUnavailable
from ai_service_agent.graph.runtime_context import GraphContext
from ai_service_agent.graph.workflows.safety import RISK_ORDER, incomplete_business_fields
from ai_service_agent.graph.workflows.select import (
    _bind_json,
    _budgeted_select_messages,
    _describe_args_schema,
    _is_uuid4,
    _normalize_placeholder_args,
)
from ai_service_agent.prompts import (
    HIGH_RISK_SELECT_BINDTOOLS_TEMPLATE,
    HIGH_RISK_SELECT_SYSTEM_TEMPLATE,
)
from ai_service_agent.tools.registry import ToolRegistry

_TIER = "high_risk"

_EXAMPLE_TEXTS = {
    "cancel_order": (
        "订单 ORD-10086 不要了，帮我取消",
        {"order_id": "ORD-10086", "reason": "不想要了"},
    ),
    "refund_apply": (
        "订单 ORD-10086 申请退款，金额 199 元",
        {"order_id": "ORD-10086", "amount": 199.0, "reason": "质量问题"},
    ),
}

_EXAMPLE_MISSING_USER_TEXTS = {
    "cancel_order": "我要取消订单",
    "refund_apply": "我要申请退款",
}


def _fmt_tool_call(name: str, args: dict) -> str:
    if not args:
        return f"{name}()"
    parts = [
        f"{key}={json.dumps(value, ensure_ascii=False)}"
        for key, value in args.items()
    ]
    return f"{name}({', '.join(parts)})"


def _render_pair(user: str, json_body: str, real_body: str, mode: str) -> str:
    if mode == "json":
        return f"用户：{user}\n输出：{json_body}"
    return f"用户：{user}\n行为：{real_body}"


def _json_tool(name: str, args: dict) -> str:
    return json.dumps({"tool": name, "args": args}, ensure_ascii=False)


def _build_examples(tier_tools: list, registry: ToolRegistry, mode: str) -> str:
    """按本档工具生成 HighRisk Select few-shot。

    mode="json" 给出 {"tool": ...}；mode="bind_tools" 只描述 tool_call。
    不出现 JSON tool 载荷（与 Mutation Select 对齐）。
    """
    if mode not in ("json", "bind_tools"):
        raise ValueError(f"unknown high_risk example mode: {mode!r}")
    parts = []
    for t in tier_tools:
        example = _EXAMPLE_TEXTS.get(t.name)
        if example is None:
            meta = registry.meta_for(t.name)
            if meta is None or meta.schema is None:
                continue
            required = [
                n
                for n in meta.schema.model_fields
                if n != "request_id" and meta.schema.model_fields[n].is_required()
            ]
            if not required:
                continue
            example = (t.description, {n: f"{n}示例" for n in required})
        user_text, args = example
        parts.append(
            _render_pair(
                user_text,
                _json_tool(t.name, args),
                f"发起 tool_call：{_fmt_tool_call(t.name, args)}",
                mode,
            )
        )
    if tier_tools:
        first = tier_tools[0]
        missing_text = _EXAMPLE_MISSING_USER_TEXTS.get(first.name, f"我要使用{first.name}")
        parts.append(
            _render_pair(
                f"{missing_text}（参数不完整）",
                _json_tool(first.name, {}),
                f"发起 tool_call：{_fmt_tool_call(first.name, {})}",
                mode,
            )
        )
        parts.append(
            _render_pair(
                "帮我处理一下订单",
                _json_tool("", {}),
                "不发起 tool_call（由系统澄清）",
                mode,
            )
        )
    return "\n\n".join(parts)


def _visible_text(state: dict) -> str:
    parts: list[str] = []
    for message in state.get("messages", []):
        if isinstance(message, dict):
            parts.append(str(message.get("content", "")))
        else:
            parts.append(str(getattr(message, "content", "")))
    return "\n".join(parts)


def _order_repository(
    runtime: Runtime[GraphContext] | None,
    fallback: OrderRepository | None,
) -> OrderRepository | None:
    if runtime is not None and runtime.context is not None:
        repository = getattr(runtime.context, "order_repository", None)
        if repository is not None:
            return repository
    return fallback


def make_high_risk_select_node(
    model,
    tools: list,
    registry: ToolRegistry,
    order_repository: OrderRepository | None = None,
) -> Callable[[dict], Awaitable[dict]]:
    """HighRisk Select（图内名 high_risk_select_llm）。无 tier 参数。"""
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
        # auto: allow no tool_call → tier clarify (#110 / ADR 0005)
        bound = model.bind_tools(tier_tools, tool_choice="auto")

    def _reject(state: dict, reason: str) -> dict:
        try:
            get_stream_writer()({"type": "safety_rejected", "data": {"rule": "precheck", "reason": reason}})
        except RuntimeError:
            pass
        return {
            "context": {**state.get("context", {}), "precheck_reason": reason},
            "execution_status": "completed",
        }

    def _clarify(state: dict, missing: list[str]) -> dict:
        return {"context": {**state.get("context", {}), "clarify_missing": missing}}

    def _tier_clarify(state: dict, tool_name: str) -> dict:
        return {"context": {**state.get("context", {}), "tier_clarify": tool_name}}

    def _no_tool_clarify(state: dict, content: str = "") -> dict:
        """tool_choice=auto 且模型未发起 tool_call：写 tier_clarify，交 clarify 节点产出话术。"""
        del content  # P0 不消费 LLM 追问文案。
        ctx = {**state.get("context", {})}
        ctx.pop("clarify_missing", None)
        ctx.pop("pending_tool", None)
        ctx.pop("operation_clarify", None)
        ctx["tier_clarify"] = True
        return {"context": ctx}

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
        runtime: Runtime[GraphContext] | None = None,
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
        examples = _build_examples(
            selected_tools, registry, "json" if is_fake else "bind_tools"
        )
        if is_fake:
            messages = _budgeted_select_messages(
                state,
                selected_tools,
                SystemMessage(content=HIGH_RISK_SELECT_SYSTEM_TEMPLATE.format(
                    tool_list=tool_list,
                    examples=examples,
                )),
            )
            resp = await bound.ainvoke(messages, config=config)
            try:
                payload = json.loads(resp.content)
                tool_name = payload.get("tool")
                if not tool_name:
                    return _no_tool_clarify(
                        state, str(payload.get("content") or payload.get("clarify") or "")
                    )
                args = dict(payload.get("args") or {})
            except Exception:
                return _reject(state, "select_parse_failed")
        else:
            messages = _budgeted_select_messages(
                state,
                selected_tools,
                SystemMessage(content=HIGH_RISK_SELECT_BINDTOOLS_TEMPLATE.format(
                    tool_list=tool_list,
                    examples=examples,
                )),
            )
            result = await bound.ainvoke(messages, config=config)
            if not result.tool_calls:
                return _no_tool_clarify(state, str(result.content or ""))
            tool_call = result.tool_calls[0]
            tool_name = tool_call["name"]
            args = dict(tool_call.get("args") or {})

        if tool_name in ("cancel_order", "refund_apply"):
            if args.get("order_id") and str(args["order_id"]) not in _visible_text(state):
                args.pop("order_id", None)

        if tool_name in ("cancel_order", "refund_apply") and not args.get("order_id"):
            repository = _order_repository(runtime, order_repository)
            candidates: list[dict] = []
            if repository is not None:
                try:
                    candidates = await asyncio.to_thread(
                        repository.list_actionable_orders,
                        state.get("user_id", ""),
                        tool_name,
                    )
                except (OrderRepositoryUnavailable, ValueError):
                    candidates = []
            if not candidates:
                return _clarify(state, ["order_id"])
            meta = registry.meta_for(tool_name)
            if meta is not None and meta.schema is not None:
                return _pending_for_form(
                    state, tool_name, _partial_args_for_form(args, meta)
                )
            return {
                "context": {
                    **state.get("context", {}),
                    "clarify_missing": ["order_id"],
                    "cancellation_candidates": candidates,
                }
            }
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
            if RISK_ORDER[meta.min_risk_level] < RISK_ORDER[_TIER]:
                return _tier_clarify(state, tool_name)
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
                        return _clarify(state, missing)
                return _reject(state, "invalid_args")
            incomplete = incomplete_business_fields(args, meta)
            if incomplete:
                return _clarify(state, incomplete)
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
