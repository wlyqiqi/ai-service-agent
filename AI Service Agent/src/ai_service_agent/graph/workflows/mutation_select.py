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
from ai_service_agent.graph.workflows.copy import (
    CAPABILITY_LABELS,
    clarify_fallback_text,
    join_cn,
    operation_clarify_text,
)
from ai_service_agent.graph.workflows.safety import incomplete_business_fields
from ai_service_agent.graph.workflows.select import (
    _bind_json,
    _budgeted_select_messages,
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

# Fake（json）与 Real（bind_tools）决策对齐，输出形态按路径分流。HighRisk select 仍用
# select._build_examples，不读这里。
# 行为用例：
# 1. 改地址 / 改电话 / 备注 / 开票 / 售后且话术完整 → 选对应工具，只填句中已有字段。
# 2. 开票句无金额 → create_invoice 不含 amount（schema 字段为 company / tax_no）。
# 3. 缺参仍选工具：档案类（地址优先，否则电话）与订单写（备注优先，否则开票 / 售后）各一条。
# 4. 「改一下订单」→ 不选工具，只列举当前工具对应能力。
# 5. 「退款」等列表外 → 不选工具、不映射，说明当前能办哪些。
# 6. 同时有地址和电话时，「改联系方式」→ 追问；同时有开票和备注且混说 → 追问。
_MUTATION_EXAMPLE_TEXTS: dict[str, tuple[str, dict]] = {
    "update_address": (
        "订单 ORD-10086 收货地址改成北京市朝阳区望京SOHO",
        {"order_id": "ORD-10086", "address": "北京市朝阳区望京SOHO"},
    ),
    "update_phone": (
        "帮我换个手机号，新号 13800138000",
        {"phone": "13800138000"},
    ),
    "update_order_note": (
        "给订单 ORD-10086 加个备注：放驿站就行",
        {"order_id": "ORD-10086", "note": "放驿站就行"},
    ),
    "create_invoice": (
        "订单 ORD-10086 开票，抬头星辰科技有限公司，税号 91110108MA01AB12CD",
        {
            "order_id": "ORD-10086",
            "company": "星辰科技有限公司",
            "tax_no": "91110108MA01AB12CD",
        },
    ),
    "create_service_ticket": (
        "订单 ORD-10086 申请售后，屏幕有裂痕",
        {"order_id": "ORD-10086", "reason": "屏幕有裂痕"},
    ),
}

_MUTATION_MISSING_USER_TEXTS = {
    "update_address": "我想改个收货地址",
    "update_phone": "我要换绑手机号",
    "update_order_note": "给订单加个备注",
    "create_invoice": "给我开一张发票",
    "create_service_ticket": "我要申请售后",
}

_PROFILE_MISSING_PRIORITY = ("update_address", "update_phone")
_ORDER_MISSING_PRIORITY = ("update_order_note", "create_invoice", "create_service_ticket")


def _first_present(priority: tuple[str, ...], names: set[str]) -> str | None:
    for name in priority:
        if name in names:
            return name
    return None


def _known_args(tool_name: str, args: dict, registry: ToolRegistry) -> dict:
    """保留 schema 字段（不含 request_id），丢掉示例里多出来的键。"""
    meta = registry.meta_for(tool_name)
    if meta is None or meta.schema is None:
        return {key: value for key, value in args.items() if key != "request_id"}
    allowed = set(meta.schema.model_fields) - {"request_id"}
    return {key: value for key, value in args.items() if key in allowed}


def _fallback_example(tool, registry: ToolRegistry) -> tuple[str, dict] | None:
    meta = registry.meta_for(tool.name)
    if meta is None or meta.schema is None:
        return None
    required = [
        name
        for name, info in meta.schema.model_fields.items()
        if name != "request_id" and info.is_required()
    ]
    if not required:
        return None
    return (tool.description, {name: f"{name}示例" for name in required})


def _fmt_tool_call(name: str, args: dict) -> str:
    if not args:
        return f"{name}()"
    parts = [
        f"{key}={json.dumps(value, ensure_ascii=False)}"
        for key, value in args.items()
    ]
    return f"{name}({', '.join(parts)})"


def _labels_for(tools: list, registry: ToolRegistry) -> list[str]:
    labels = []
    for tool in tools:
        label = CAPABILITY_LABELS.get(tool.name)
        if not label:
            meta = registry.meta_for(tool.name)
            desc = (meta.description if meta is not None else "") or tool.name
            label = desc.strip().rstrip("。")
        labels.append(label)
    return labels


def _vague_content(labels: list[str]) -> str:
    if not labels:
        return "请直接说一下您要办理的事项。"
    if len(labels) == 1:
        return f"目前可以帮您{labels[0]}。请说一下具体要怎么办。"
    if len(labels) == 2:
        return f"您是要{labels[0]}，还是{labels[1]}？请直接说一下。"
    return f"您是要{'、'.join(labels[:-1])}，还是{labels[-1]}？请直接说一下。"


def _refund_content(labels: list[str]) -> str:
    return (
        "退款不在当前能办的范围内。目前可以帮您"
        f"{join_cn(labels)}，请直接说要办哪一项。"
    )


def _usable_clarify_text(content: str) -> str:
    """模型给的追问话术可用则用；若它误把 JSON 载荷当 content 返回，丢弃走模板。

    提示词已不向真模型注入 JSON few-shot，但这是软约束——模型仍可能自己吐
    ``{"tool": ...}``。这段内容会直接进 ``messages`` 给用户，必须挡在这里。
    """
    text = (content or "").strip()
    if not text.startswith("{"):
        return text
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return text
    if isinstance(payload, dict) and "tool" in payload:
        return ""
    return text


def _render_pair(user: str, json_body: str, real_body: str, mode: str) -> str:
    if mode == "json":
        return f"用户：{user}\n输出：{json_body}"
    return f"用户：{user}\n行为：{real_body}"


def _json_tool(name: str, args: dict) -> str:
    return json.dumps({"tool": name, "args": args}, ensure_ascii=False)


def _json_clarify(content: str) -> str:
    return json.dumps({"tool": None, "content": content}, ensure_ascii=False)


def _build_mutation_examples(tier_tools: list, registry: ToolRegistry, mode: str) -> str:
    """按当前工具集生成 Mutation Select few-shot。

    mode="json" 保持 {"tool": ...}；mode="bind_tools" 只描述 tool_call 或纯文本，
    不出现 JSON tool 载荷。列表里没有的工具，对应正例不注入。
    """
    if mode not in ("json", "bind_tools"):
        raise ValueError(f"unknown mutation example mode: {mode!r}")
    names = {tool.name for tool in tier_tools}
    parts: list[str] = []

    for tool in tier_tools:
        example = _MUTATION_EXAMPLE_TEXTS.get(tool.name)
        if example is None:
            example = _fallback_example(tool, registry)
            if example is None:
                continue
        user_text, args = example
        args = _known_args(tool.name, args, registry)
        parts.append(
            _render_pair(
                user_text,
                _json_tool(tool.name, args),
                f"发起 tool_call：{_fmt_tool_call(tool.name, args)}",
                mode,
            )
        )

    for name in (
        _first_present(_PROFILE_MISSING_PRIORITY, names),
        _first_present(_ORDER_MISSING_PRIORITY, names),
    ):
        if name is None:
            continue
        user_text = _MUTATION_MISSING_USER_TEXTS.get(name, f"我要使用{name}")
        parts.append(
            _render_pair(
                f"{user_text}（参数不完整）",
                _json_tool(name, {}),
                (
                    f"发起 tool_call：{_fmt_tool_call(name, {})}"
                    "（参数不完整，只填已知参数；缺参交后续表单，不要改用纯文本追问）"
                ),
                mode,
            )
        )

    labels = _labels_for(tier_tools, registry)
    vague = _vague_content(labels)
    refund = _refund_content(labels)
    parts.append(
        _render_pair(
            "帮我改一下订单",
            _json_clarify(vague),
            f"不发起 tool_call，纯文本追问：{vague}",
            mode,
        )
    )
    parts.append(
        _render_pair(
            "我要退款",
            _json_clarify(refund),
            f"不发起 tool_call，纯文本说明当前能办的并请用户重说：{refund}",
            mode,
        )
    )

    if "update_address" in names and "update_phone" in names:
        contact = (
            f"您是要{CAPABILITY_LABELS['update_address']}，"
            f"还是{CAPABILITY_LABELS['update_phone']}？"
        )
        parts.append(
            _render_pair(
                "帮我改一下联系方式",
                _json_clarify(contact),
                f"不发起 tool_call，纯文本追问：{contact}",
                mode,
            )
        )
    if "create_invoice" in names and "update_order_note" in names:
        mixed = (
            f"您是要{CAPABILITY_LABELS['create_invoice']}，"
            f"还是{CAPABILITY_LABELS['update_order_note']}？请说一下先办哪一项。"
        )
        parts.append(
            _render_pair(
                "订单 ORD-10086 既要开票又要加备注，我还没定先办哪个",
                _json_clarify(mixed),
                f"不发起 tool_call，纯文本追问：{mixed}",
                mode,
            )
        )

    text = "\n\n".join(parts)
    if mode == "bind_tools" and '{"tool":' in text:
        raise ValueError("bind_tools mutation examples must not include JSON tool few-shots")
    return text


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

    def _operation_clarify_inline(
        state: dict, content: str, tool_names: list[str]
    ) -> dict:
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
        text = _usable_clarify_text(content) or operation_clarify_text(tool_names)
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
        examples = _build_mutation_examples(
            selected_tools,
            registry,
            "json" if is_fake else "bind_tools",
        )
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
                        state,
                        str(payload.get("content") or payload.get("clarify") or ""),
                        [t.name for t in selected_tools],
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
                return _operation_clarify_inline(
                    state, str(result.content or ""), [t.name for t in selected_tools]
                )
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
