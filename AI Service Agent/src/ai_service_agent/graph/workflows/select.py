"""工具选择（OQ-2）：LLM 结构化输出 {tool, args} + 确定性校验兜底。

LLM 负责理解，确定性代码负责校验：工具必须在档内。
Mutation：tool 已知即写 pending_tool（字段可不全 → Form Interrupt prefills）。
HighRisk：缺参写 clarify_missing → 聊天 clarify；完整则 pending_tool。
真 schema 类型/未知字段错误 -> safety_rejected，不进入 interrupt。
"""

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.messages.utils import count_tokens_approximately, trim_messages
from langchain_core.tools import BaseTool
from langchain_core.runnables import RunnableConfig
from pydantic import ValidationError
from langgraph.config import get_stream_writer
from langgraph.runtime import Runtime

from ai_service_agent.domain.orders import OrderRepository, OrderRepositoryUnavailable
from ai_service_agent.graph.workflows.safety import RISK_ORDER, incomplete_business_fields
from ai_service_agent.tools.params import is_absent_sentinel
from ai_service_agent.tools.registry import ToolRegistry
from ai_service_agent.graph.runtime_context import GraphContext
from ai_service_agent.prompts import SELECT_BINDTOOLS_SYSTEM, SELECT_SYSTEM_TEMPLATE
from ai_service_agent.config import get_settings
from ai_service_agent.graph.context.builders import build_recent_visible_context
from ai_service_agent.graph.context.policy import make_select_policy

def _describe_args_schema(meta) -> str:
    """从 ToolMeta.schema 自动列出参数字段契约（request_id 由系统自动注入，不要求模型输出）。"""
    if meta is None or meta.schema is None:
        return ""
    parts = []
    for name, info in meta.schema.model_fields.items():
        if name == "request_id":
            continue
        parts.append(f"{name}({'必填' if info.is_required() else '可选'})")
    return "，".join(parts)

_EXAMPLE_TEXTS = {
    # 真实业务话术 + 严格对应 schema 的参数（示例生成用；args 不得含 schema 之外字段）
    "update_address": ("订单 ORD-10086 收货地址改成北京市朝阳区望京SOHO", {"order_id": "ORD-10086", "address": "北京市朝阳区望京SOHO"}),
    "update_phone": ("帮我换个手机号，新号 13800138000", {"phone": "13800138000"}),
    "update_order_note": ("给订单 ORD-10086 加个备注：放驿站就行", {"order_id": "ORD-10086", "note": "放驿站就行"}),
    "create_invoice": ("订单 ORD-10086 开票，抬头星辰科技有限公司，税号 91110108MA01AB12CD",
                       {"order_id": "ORD-10086", "amount": 199.0, "company": "星辰科技有限公司", "tax_no": "91110108MA01AB12CD"}),
    "create_service_ticket": ("订单 ORD-10086 申请售后，屏幕有裂痕", {"order_id": "ORD-10086", "reason": "屏幕有裂痕"}),
    "cancel_order": ("订单 ORD-10086 不要了，帮我取消", {"order_id": "ORD-10086", "reason": "不想要了"}),
    "refund_apply": ("订单 ORD-10086 申请退款，金额 199 元", {"order_id": "ORD-10086", "amount": 199.0, "reason": "质量问题"}),
}

_EXAMPLE_MISSING_USER_TEXTS = {
    "update_address": "我想改个收货地址",
    "update_phone": "我要换绑手机号",
    "update_order_note": "给订单加个备注",
    "create_invoice": "给我开一张发票",
    "create_service_ticket": "我要申请售后",
    "cancel_order": "我要取消订单",
    "refund_apply": "我要申请退款",
}


def _build_examples(tier_tools: list, registry: ToolRegistry) -> str:
    """按档位生成 select 示例：每工具一个真实业务示例（话术+参数对齐 schema）+ 一个缺参示例。"""
    parts = []
    for t in tier_tools:
        example = _EXAMPLE_TEXTS.get(t.name)
        if example is None:
            meta = registry.meta_for(t.name)
            if meta is None or meta.schema is None:
                continue
            required = [
                n for n in meta.schema.model_fields
                if n != "request_id" and meta.schema.model_fields[n].is_required()
            ]
            if not required:
                continue
            example = (t.description, {n: f"{n}示例" for n in required})
        user_text, args = example
        parts.append(
            f"用户：{user_text}\n"
            f"输出：{{\"tool\": \"{t.name}\", \"args\": {json.dumps(args, ensure_ascii=False)}}}"
        )
    if tier_tools:
        first = tier_tools[0]
        missing_text = _EXAMPLE_MISSING_USER_TEXTS.get(first.name, f"我要使用{first.name}")
        parts.append(
            f"用户：{missing_text}（参数不完整）\n"
            f"输出：{{\"tool\": \"{first.name}\", \"args\": {{}}}}"
        )
    return "\n\n".join(parts)


def _bind_json(model):
    """json_mode 绑定（与 router.py 同契约）：真实模型 response_format json_object，fake 直接 JSON 字符串。"""
    if isinstance(model, FakeMessagesListChatModel):
        return model
    return model.bind(response_format={"type": "json_object"})


def _is_uuid4(value: str) -> bool:
    try:
        return uuid.UUID(value).version == 4
    except (ValueError, AttributeError):
        return False


def _normalize_placeholder_args(args: dict, meta) -> dict:
    """将模型占位符归一为「未提供」，供 D1 缺参澄清（不把空参当 schema 拒绝）。

    - None / 空白字符串 / 缺省哨兵（"null" 等）→ 删除字段
    - int/float <= 0 → 删除字段（如 amount=0 的开票占位）
    真类型错误（如 amount=\"abc\"）保留，仍由 schema 走 invalid_args。
    request_id 原样保留；非 UUID 在进入缺参表单前由 select 换成新的 uuid4。
    """
    if meta is None or meta.schema is None:
        return dict(args)
    fields = set(meta.schema.model_fields) - {"request_id"}
    normalized: dict = {}
    for key, value in args.items():
        if key == "request_id":
            normalized[key] = value
            continue
        if key not in fields:
            normalized[key] = value
            continue
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if is_absent_sentinel(value):
            continue
        if isinstance(value, bool):
            normalized[key] = value
            continue
        if isinstance(value, (int, float)) and value <= 0:
            continue
        normalized[key] = value
    return normalized


def _budgeted_select_messages(
    state: dict,
    selected_tools: list,
    system_message: SystemMessage,
) -> list:
    settings = get_settings()
    policy = make_select_policy(settings)
    countable_tools = [
        tool for tool in selected_tools if isinstance(tool, (BaseTool, dict))
    ]
    fixed_tokens = count_tokens_approximately(
        [system_message],
        tools=countable_tools,
    )
    dynamic_budget = max(1, policy.max_total_tokens - fixed_tokens)
    dynamic_messages = build_recent_visible_context(
        state.get("messages", []),
        previous_completed_turns=1,
    )
    selected = trim_messages(
        dynamic_messages,
        strategy="last",
        max_tokens=dynamic_budget,
        token_counter=count_tokens_approximately,
        start_on="human",
        end_on=("human", "tool"),
        allow_partial=False,
    )
    return [system_message, *selected]


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


def make_select_node(
    model,
    tier: str,
    tools: list,
    registry: ToolRegistry,
    order_repository: OrderRepository | None = None,
) -> Callable[[dict], Awaitable[dict]]:
    # B2b（P5 D2）：写图 select 档内严格——允许集与系统提示仅含 min_risk_level == tier 的工具；
    # 只读工具不进入写图 select（ToolNode 分层注入不变，select 是写图唯一工具选择闸）。
    tier_tools = [
        t for t in tools
        if (meta := registry.meta_for(t.name)) is not None and meta.min_risk_level == tier
    ]
    tier_allowed = {t.name for t in tier_tools}
    lines_by_name = {}
    for t in tier_tools:
        line = f"- {t.name}: {t.description}"
        contract = _describe_args_schema(registry.meta_for(t.name))
        if contract:
            line += f"\n  参数：{contract}"
        lines_by_name[t.name] = line
    is_fake = isinstance(model, FakeMessagesListChatModel)
    if is_fake:
        bound = _bind_json(model)
    else:
        bound = model.bind_tools(tier_tools, tool_choice="any")

    def _reject(state: dict, reason: str) -> dict:
        try:
            get_stream_writer()({"type": "safety_rejected", "data": {"rule": "precheck", "reason": reason}})
        except RuntimeError:
            pass
        # 普通 dict 返回 + 条件边路由（LangGraph 1.2.11 实测：Command(goto) 不会抑制静态出边，
        # 静态后继与 goto 目标会并发执行导致双写；决策路由一律走条件边）
        return {"context": {**state.get("context", {}), "precheck_reason": reason}, "execution_status": "completed"}

    def _clarify(state: dict, missing: list[str]) -> dict:
        # 参数不完整 != 拒绝（D1）：只写 clarify_missing，由条件边路由到 clarify 节点（普通轮）。
        # 不发 safety_rejected；execution_status 由 clarify 节点置 clarifying。
        # Mutation 主路径不再走此分支（见 _pending_for_form）；HighRisk 仍用聊天补参。
        return {"context": {**state.get("context", {}), "clarify_missing": missing}}

    def _tier_clarify(state: dict, tool_name: str) -> dict:
        # B2b：已注册低档工具在写图内被选中 = 意图与风险档不匹配 -> 澄清（普通轮）。
        # 不 reject（工具真实存在）、不进闸门、不执行。
        return {"context": {**state.get("context", {}), "tier_clarify": tool_name}}

    def _partial_args_for_form(args: dict, meta) -> dict:
        """Mutation Form prefills：保留已知 schema 字段 + request_id，不要求一次填齐。"""
        known = set(meta.schema.model_fields)
        partial = {key: value for key, value in args.items() if key in known}
        partial.setdefault("request_id", str(uuid.uuid4()))
        return partial

    def _pending_for_form(state: dict, tool_name: str, args: dict) -> dict:
        """Mutation：tool 已知即落 pending_tool，缺参由 Form Interrupt 收集（Form-Only Write）。"""
        ctx = {**state.get("context", {})}
        ctx.pop("clarify_missing", None)
        ctx.pop("tier_clarify", None)
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
        messages = _budgeted_select_messages(
            state,
            selected_tools,
            SystemMessage(content=SELECT_SYSTEM_TEMPLATE.format(
                tool_list=tool_list,
                examples=_build_examples(selected_tools, registry),
            )),
        )
        if is_fake:
            resp = await bound.ainvoke(messages, config=config)
            try:
                payload = json.loads(resp.content)
                tool_name = payload["tool"]
                args = dict(payload.get("args") or {})
            except Exception:
                return _reject(state, "select_parse_failed")
        else:
            messages = _budgeted_select_messages(
                state,
                selected_tools,
                SystemMessage(content=SELECT_BINDTOOLS_SYSTEM),
            )
            result = await bound.ainvoke(messages, config=config)
            if not result.tool_calls:
                return _reject(state, "select_parse_failed")
            tool_call = result.tool_calls[0]
            tool_name = tool_call["name"]
            args = dict(tool_call.get("args") or {})

        if tier == "high_risk" and tool_name in ("cancel_order", "refund_apply"):
            if args.get("order_id") and str(args["order_id"]) not in _visible_text(state):
                # The model may only narrow a user-provided identifier; it may
                # never invent one or silently choose from a candidate list.
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
            if RISK_ORDER[meta.min_risk_level] < RISK_ORDER[tier]:
                return _tier_clarify(state, tool_name)
            return _reject(state, "unknown_tool")
        if meta.schema is not None:
            args = _normalize_placeholder_args(args, meta)
            try:
                args = meta.schema.model_validate(args).model_dump()
            except ValidationError as exc:
                errors = exc.errors()
                missing = [e["loc"][0] for e in errors if e["type"] == "missing" and e["loc"]]
                # 只有"全部为缺必填"才走缺参路径；混有类型/格式错误仍按 invalid_args 拒绝
                if missing and len(missing) == len(errors):
                    missing = [f for f in missing if f != "request_id"]
                    if missing:
                        if tier == "mutation":
                            return _pending_for_form(
                                state, tool_name, _partial_args_for_form(args, meta)
                            )
                        return _clarify(state, missing)
                return _reject(state, "invalid_args")
            incomplete = incomplete_business_fields(args, meta)
            if incomplete:
                if tier == "mutation":
                    return _pending_for_form(
                        state, tool_name, _partial_args_for_form(args, meta)
                    )
                return _clarify(state, incomplete)
        ctx = {**state.get("context", {})}
        ctx.pop("clarify_missing", None)
        ctx.pop("tier_clarify", None)
        ctx.pop("cancellation_candidates", None)
        ctx["clarify_count"] = 0  # 完整成功 select 清零（D1 轮次语义）
        ctx["pending_tool"] = {"tool": tool_name, "args": args, "request_id": args["request_id"]}
        return {"context": ctx}

    return select
