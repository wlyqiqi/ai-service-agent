"""Select 共享纯函数（无业务语义）：供 Mutation Select / HighRisk Select 复用。

节点工厂见 mutation_select.make_mutation_select_node、
high_risk_select.make_high_risk_select_node。本模块不提供 make_select_node。
"""

import uuid

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import SystemMessage
from langchain_core.messages.utils import count_tokens_approximately, trim_messages
from langchain_core.tools import BaseTool

from ai_service_agent.config import get_settings
from ai_service_agent.graph.context.builders import build_recent_visible_context
from ai_service_agent.graph.context.policy import make_select_policy
from ai_service_agent.tools.params import is_absent_sentinel


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


def _is_uuid4(value: str) -> bool:
    try:
        return uuid.UUID(value).version == 4
    except (ValueError, AttributeError):
        return False


def _normalize_placeholder_args(args: dict, meta) -> dict:
    """将模型占位符归一为「未提供」，供缺参澄清（不把空参当 schema 拒绝）。

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
