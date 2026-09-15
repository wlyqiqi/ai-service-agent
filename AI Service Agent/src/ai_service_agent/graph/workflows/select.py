"""工具选择（OQ-2）：LLM json_mode 结构化输出 {tool, args} + 确定性校验兜底。

LLM 负责理解，确定性代码负责校验（AGENTS.md §18）：工具必须在档内 + args 必须过 schema。
校验失败 -> writer safety_rejected(rule=precheck) + 置 precheck_reason + 条件边路由 safety_final，不进入 interrupt。
"""

import json
import uuid
from typing import Callable

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.config import get_stream_writer

from ai_service_agent.tools.registry import ToolRegistry

SELECT_SYSTEM_TEMPLATE = (
    "你是客服操作选择器。根据用户意图，从允许的工具中选择一个并给出符合其 schema 的参数。\n"
    "只输出 JSON，形状为 {{\"tool\": \"<工具名>\", \"args\": {{...}}}}。\n"
    "允许的工具：\n{tool_list}\n"
    "args 只能包含该工具列出的字段名，不要输出 schema 之外的字段（输出未知字段会被拒绝）。\n"
    "示例：\n"
    "用户：我要把收货地址改成朝阳区\n"
    "输出：{{\"tool\": \"account_update\", \"args\": {{\"address\": \"朝阳区\"}}}}\n"
    "不要输出其它内容。"
)


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


def _bind_json(model):
    """json_mode 绑定（与 router.py 同契约）：真实模型 response_format json_object，fake 直接 JSON 字符串。"""
    if isinstance(model, FakeMessagesListChatModel):
        return model
    return model.bind(response_format={"type": "json_object"})


def make_select_node(model, tier: str, tools: list, registry: ToolRegistry) -> Callable[[dict], dict]:
    allowed = {t.name for t in tools}
    lines = []
    for t in tools:
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

    def select(state: dict) -> dict:
        last = ""
        for m in reversed(state.get("messages", [])):
            if getattr(m, "type", "") == "human":
                last = m.content if isinstance(m.content, str) else str(m.content)
                break
        resp = bound.invoke([
            SystemMessage(content=SELECT_SYSTEM_TEMPLATE.format(tool_list=tool_list)),
            HumanMessage(content=last),
        ])
        try:
            payload = json.loads(resp.content)
            tool_name = payload["tool"]
            args = dict(payload.get("args") or {})
        except Exception:
            return _reject(state, "select_parse_failed")
        args.setdefault("request_id", str(uuid.uuid4()))  # 幂等键（tool-spec §2）；先补再校验
        meta = registry.meta_for(tool_name) if isinstance(tool_name, str) else None
        if meta is None or tool_name not in allowed:
            return _reject(state, "unknown_tool")
        if meta.schema is not None:
            try:
                args = meta.schema.model_validate(args).model_dump()
            except Exception:
                return _reject(state, "invalid_args")
        return {
            "context": {
                **state.get("context", {}),
                "pending_tool": {"tool": tool_name, "args": args, "request_id": args["request_id"]},
            }
        }

    return select
