"""工具选择（OQ-2）：LLM json_mode 结构化输出 {tool, args} + 确定性校验兜底。

LLM 负责理解，确定性代码负责校验（AGENTS.md §18）：工具必须在档内 + args 必须过 schema。
校验失败 -> writer safety_rejected(rule=precheck) + 置 precheck_reason + 条件边路由 safety_final，不进入 interrupt。
"""

import json
import uuid
from typing import Callable

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import ValidationError
from langgraph.config import get_stream_writer

from ai_service_agent.graph.workflows.safety import RISK_ORDER
from ai_service_agent.tools.orders import find_own_cancellable_paid_orders, find_own_refundable_orders
from ai_service_agent.tools.registry import ToolRegistry
from ai_service_agent.prompts import SELECT_SYSTEM_TEMPLATE

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

def make_select_node(model, tier: str, tools: list, registry: ToolRegistry) -> Callable[[dict], dict]:
    # B2b（P5 D2）：写图 select 档内严格——允许集与系统提示仅含 min_risk_level == tier 的工具；
    # 只读工具不进入写图 select（ToolNode 分层注入不变，select 是写图唯一工具选择闸）。
    tier_tools = [
        t for t in tools
        if (meta := registry.meta_for(t.name)) is not None and meta.min_risk_level == tier
    ]
    allowed = {t.name for t in tier_tools}
    lines = []
    for t in tier_tools:
        line = f"- {t.name}: {t.description}"
        contract = _describe_args_schema(registry.meta_for(t.name))
        if contract:
            line += f"\n  参数：{contract}"
        lines.append(line)
    tool_list = "\n".join(lines)
    bound = _bind_json(model)

    def _reject(state: dict, reason: str) -> dict:
        get_stream_writer()({"type": "safety_rejected", "data": {"rule": "precheck", "reason": reason}})
        # 普通 dict 返回 + 条件边路由（LangGraph 1.2.11 实测：Command(goto) 不会抑制静态出边，
        # 静态后继与 goto 目标会并发执行导致双写；决策路由一律走条件边）
        return {"context": {**state.get("context", {}), "precheck_reason": reason}, "execution_status": "completed"}

    def _clarify(state: dict, missing: list[str]) -> dict:
        # 参数不完整 != 拒绝（D1）：只写 clarify_missing，由条件边路由到 clarify 节点（普通轮）。
        # 不发 safety_rejected；execution_status 由 clarify 节点置 clarifying。
        return {"context": {**state.get("context", {}), "clarify_missing": missing}}

    def _tier_clarify(state: dict, tool_name: str) -> dict:
        # B2b：已注册低档工具在写图内被选中 = 意图与风险档不匹配 -> 澄清（普通轮）。
        # 不 reject（工具真实存在）、不进闸门、不执行。
        return {"context": {**state.get("context", {}), "tier_clarify": tool_name}}

    def _incomplete_fields(args: dict, meta) -> list[str]:
        """参数完备性（D1）：必填业务字段缺失/为空串，或全可选工具全部业务字段为空 -> 返回缺失字段；完整 -> []。"""
        if meta.schema is None:
            return []
        fields = [n for n in meta.schema.model_fields if n != "request_id"]
        required = [n for n in fields if meta.schema.model_fields[n].is_required()]
        missing = [n for n in required if not args.get(n)]
        if missing:
            return missing
        if not required and all(not args.get(n) for n in fields):
            return fields
        return []

    def select(state: dict) -> dict:
        current_context = {**state.get("context", {})}
        current_context.pop("cancellation_candidates", None)
        state = {**state, "context": current_context}
        last = ""
        for m in reversed(state.get("messages", [])):
            if getattr(m, "type", "") == "human":
                last = m.content if isinstance(m.content, str) else str(m.content)
                break
        resp = bound.invoke([
            SystemMessage(content=SELECT_SYSTEM_TEMPLATE.format(
                tool_list=tool_list,
                examples=_build_examples(tier_tools, registry),
            )),
            HumanMessage(content=last),
        ])
        try:
            payload = json.loads(resp.content)
            tool_name = payload["tool"]
            args = dict(payload.get("args") or {})
        except Exception:
            return _reject(state, "select_parse_failed")
        if tool_name == "cancel_order" and not args.get("order_id"):
            candidates = find_own_cancellable_paid_orders(state.get("user_id", ""))
            if isinstance(candidates, dict):
                return _reject(state, candidates["error"])
            if len(candidates) == 1:
                args["order_id"] = candidates[0]["order_id"]
            else:
                ctx = {
                    **state.get("context", {}),
                    "clarify_missing": ["order_id"],
                    "cancellation_candidates": candidates,
                }
                return {"context": ctx}
        if tool_name == "refund_apply" and not args.get("order_id"):
            candidates = find_own_refundable_orders(state.get("user_id", ""))
            if isinstance(candidates, dict):
                return _reject(state, candidates["error"])
            if len(candidates) == 1:
                args["order_id"] = candidates[0]["order_id"]
            elif len(candidates) > 1:
                ctx = {
                    **state.get("context", {}),
                    "clarify_missing": ["order_id"],
                    "cancellation_candidates": candidates,
                }
                return {"context": ctx}
            else:
                return _clarify(state, ["order_id"])
        args.setdefault("request_id", str(uuid.uuid4()))  # 幂等键（tool-spec §2）；先补再校验
        meta = registry.meta_for(tool_name) if isinstance(tool_name, str) else None
        if meta is None:
            return _reject(state, "unknown_tool")
        if tool_name not in allowed:
            if RISK_ORDER[meta.min_risk_level] < RISK_ORDER[tier]:
                return _tier_clarify(state, tool_name)
            return _reject(state, "unknown_tool")
        if meta.schema is not None:
            try:
                args = meta.schema.model_validate(args).model_dump()
            except ValidationError as exc:
                errors = exc.errors()
                missing = [e["loc"][0] for e in errors if e["type"] == "missing" and e["loc"]]
                # 只有"全部为缺必填"才走 clarify；混有类型/格式错误仍按 invalid_args 拒绝
                if missing and len(missing) == len(errors):
                    missing = [f for f in missing if f != "request_id"]
                    if missing:
                        return _clarify(state, missing)
                return _reject(state, "invalid_args")
            incomplete = _incomplete_fields(args, meta)
            if incomplete:
                return _clarify(state, incomplete)
        ctx = {**state.get("context", {})}
        ctx.pop("clarify_missing", None)
        ctx.pop("tier_clarify", None)
        ctx.pop("cancellation_candidates", None)
        ctx["clarify_count"] = 0  # 完整成功 select 清零（D1 轮次语义）
        ctx["pending_tool"] = {"tool": tool_name, "args": args, "request_id": args["request_id"]}
        return {"context": ctx}

    return select
