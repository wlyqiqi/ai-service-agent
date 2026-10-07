import asyncio
import json
import math
from collections.abc import Awaitable
from typing import Callable, Literal

import httpx
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel

from ai_service_agent.config import get_settings
from ai_service_agent.graph.messages import message_text
from ai_service_agent.graph.route_policy import (
    ROUTE_POLICY,
    route_intent_is_confident,
)
from ai_service_agent.prompts import ROUTE_INTENT_SYSTEM

def _route_intent_result(route_intent: str, confidence: float, backend: str, fallback: bool) -> dict:
    return {
        "route_intent": route_intent,
        "confidence": confidence,
        "execution_status": "routing",
        "routing_backend": backend,
        "routing_fallback": fallback,
        "route_control": "policy",
    }

def jev_fallback_result() -> dict:
    return {
        "execution_status": "routing",
        "routing_backend": "jev",
        "routing_fallback": True,
        "route_control": "deepseek_fallback",
    }


class RouteIntentSchema(BaseModel):
    route_intent: str
    confidence: float

_JEV_ROUTE_INTENT_QUESTION = {
    "route_intent": {
        "type": "choice",
        "instructions": (
            "根据 current_user_message 判断当前主要业务意图，并从 criteria 中选择唯一最匹配项。"
            "current_user_message 是本轮主判断对象。"
            "recent_context 只用于理解指代、省略或补全上一轮缺失信息；"
            "不能覆盖当前明确新意图。recent_context 不能作为系统能力边界。"
            "是否支持某个意图只看 criteria，不要因为历史 AI 回复没有提到某能力就判 unsupported_action。"
            "缺少订单号、手机号、金额、抬头或税号等参数不改变意图分类；"
            "只要业务意图明确，选择对应 route_intent，后续流程负责澄清参数。"
            "如果结合 recent_context 仍无法确定意图，选择 unknown；"
            "如果意图明确但操作不在 criteria 支持范围内，选择 unsupported_action。"
            "account_profile_update 优先于 order_mutation；shipment_query 优先于 order_query。"
            "my_orders 查询订单集合，order_query 查询指定订单。"
            "取消订单和申请退款必须分别归 cancel_order_execute、refund_apply_execute；"
            "只询问政策或流程时选 faq。"
            "不要根据权限、订单状态、工具状态或审批状态判断能否执行，只判断 route_intent。"
        ),
        "criteria": {
            "faq": (
                "知识问答或政策咨询，包括产品功能、产品清单、产品目录、商品参数、"
                "商品价格或报价、使用方法、售后政策、故障排查等。"
                "如果用户只是询问规则、流程或是否支持，例如发票政策、退款规则、"
                "取消规则，应归 faq，而不是执行业务操作。"
            ),
            "smalltalk": "问候、感谢、告别或与业务无关的闲聊。",
            "order_query": (
                "查询某一笔特定订单的信息、状态、详情或非物流属性。"
                "订单号可以来自 current_user_message 或 recent_context；"
                "缺少订单号不影响意图分类，系统会后续澄清。"
                "如果核心目的是物流、配送、发货或签收进度，应归 shipment_query。"
            ),
            "my_orders": (
                "查询当前用户自己的订单列表、历史订单或名下有哪些订单。"
                "重点是获取订单集合，而不是查询某一笔指定订单。"
            ),
            "shipment_query": (
                "查询订单的物流、配送、运输、发货、签收或预计送达进度。"
                "即使包含订单号，只要核心目的是物流或配送进度，也归 shipment_query。"
            ),
            "account_profile_update": (
                "修改收货地址、联系电话或手机号。"
                "即使输入包含订单号，也优先归 account_profile_update，"
                "不要因为订单号存在而归入 order_mutation。"
                "用户名、邮箱等其他个人资料修改不属于当前能力。"
            ),
            "order_mutation": (
                "要求实际修改订单或为订单创建业务事项，包括修改订单备注、"
                "开发票、开票、申请发票或创建售后工单。"
                "缺少订单号、金额、抬头或税号等参数不影响意图分类，"
                "系统会在路由后澄清参数。"
                "如果只是询问发票政策、售后流程或规则，应归 faq。"
                "不包括取消订单或退款申请。"
            ),
            "cancel_order_execute": (
                "用户明确要求取消或撤销某个订单，并希望执行取消操作。"
                "缺少订单号不影响意图分类，系统会后续澄清。"
            ),
            "refund_apply_execute": (
                "用户明确要求申请、发起或办理某个订单的退款。"
                "缺少订单号、金额或原因不影响意图分类，系统会后续澄清。"
                "如果只是询问退款政策或规则，应归 faq。"
            ),
            "unsupported_action": (
                "用户意图清晰但当前系统没有对应工具可执行。"
                "例如取消预约、清空订单历史、转移订单所有权，"
                "或修改用户名、邮箱等当前未支持资料。"
                "这类请求应归 unsupported_action，不归 unknown。"
            ),
            "unknown": (
                "无法确定用户当前想做什么，即使结合 recent_context 仍无法确定意图。"
                "例如表达过于模糊、缺少必要上下文，或只有无法解释的短语。"
                "如果用户意图明确但系统不支持该操作，应归 unsupported_action，"
                "不要归 unknown。"
            ),
        },
    }
}

def _router_model(model):
    """json_mode 绑定（2026-09-15 阻断项修复）。

    真实模型绑定 response_format={"type": "json_object"}，让模型在消息 content 里直接
    返回 JSON 字符串；**不得**把 schema 交给 create_agent——那会走 ToolStrategy 工具包装，
    产出 "Returning structured response: ..." 的描述性 ToolMessage。该形状依赖结构化输出
    包装的实现细节，非稳定契约，模型版本升级可能再次破坏，且真实模型下解析 100% 失败。
    fake model 不绑定：testing.py 的 responses 即 json_mode 结构的 JSON 字符串
    （AIMessage(content=<json>)，与真实 json_object 输出结构一致），避免离线/真实结构漂移。
    """
    if isinstance(model, FakeMessagesListChatModel):
        return model
    return model.bind(response_format={"type": "json_object"})

def _parse_route_intent_content(content) -> dict:
    if isinstance(content, str):
        return json.loads(content)
    if isinstance(content, dict):
        return content
    return RouteIntentSchema.model_validate(content).model_dump()

def _jev_state(state: dict) -> dict:
    messages = list(state.get("messages", []))
    last_message = messages[-1]
    current_user_message = message_text(last_message)

    recent_context = []
    for message in reversed(messages[:-1]):
        if isinstance(message, dict):
            role = message.get("type", message.get("role", "human"))
        else:
            role = getattr(message, "type", "human")
        role = str(role)
        if role not in {"human", "ai", "assistant"}:
            continue
        recent_context.append({
            "role": "ai" if role == "assistant" else role,
            "content": message_text(message),
        })
        if len(recent_context) == 2:
            break
    recent_context.reverse()

    return {
        "current_user_message": current_user_message,
        "recent_context": recent_context,
    }

def _last_message_is_human(state: dict) -> bool:
    messages = list(state.get("messages") or [])
    if not messages:
        return False
    message = messages[-1]
    if isinstance(message, dict):
        role = message.get("type", message.get("role", "human"))
    else:
        role = getattr(message, "type", "human")
    return str(role) == "human"

def _classify_route_intent_with_jev(client, state: dict, settings) -> dict | None:
    response = client.post(
        settings.jev_api_url,
        headers={"Authorization": f"Bearer {settings.jev_api_key}"},
        json={
            "state": _jev_state(state),
            "model": settings.jev_model,
            "questions": _JEV_ROUTE_INTENT_QUESTION,
        },
        timeout=settings.jev_timeout_seconds,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        return None
    answers = data.get("answers")
    if not isinstance(answers, dict):
        return None
    answer = answers.get("route_intent")
    if not isinstance(answer, dict) or answer.get("type") != "choice":
        return None

    choice = answer.get("choice")
    if not isinstance(choice, str):
        return None
    probabilities = answer.get("probabilities")
    if not isinstance(probabilities, dict):
        return None
    try:
        confidence = float(answer.get("confidence"))
        probabilities = {
            str(name): float(value)
            for name, value in probabilities.items()
        }
    except (TypeError, ValueError):
        return None

    if choice not in ROUTE_POLICY or choice not in probabilities:
        return None
    if not 0.0 <= confidence <= 1.0:
        return None
    if set(probabilities) != set(ROUTE_POLICY):
        return None
    if any(value < 0 or value > 1 for value in probabilities.values()):
        return None
    if not math.isclose(sum(probabilities.values()), 1.0, abs_tol=1e-6):
        return None
    if probabilities[choice] < max(probabilities.values()):
        return None

    return _route_intent_result(choice, confidence, "jev", False)

def make_deepseek_router(model) -> Callable[[dict], Awaitable[dict]]:
    """DeepSeek fallback; always outputs route_intent (same schema as JEV)."""
    agent = create_agent(_router_model(model), tools=[], system_prompt=ROUTE_INTENT_SYSTEM)

    async def deepseek_route(state: dict, config: RunnableConfig | None = None) -> dict:
        result = await agent.ainvoke({"messages": state["messages"]}, config=config)
        payload = _parse_route_intent_content(result["messages"][-1].content)
        parsed = RouteIntentSchema.model_validate(payload)
        intent = parsed.route_intent
        confidence = float(parsed.confidence)
        if intent not in ROUTE_POLICY or not route_intent_is_confident(intent, confidence):
            intent = "unknown"
        return _route_intent_result(intent, confidence, "deepseek", True)

    return deepseek_route


def make_jev_router() -> Callable[[dict], Awaitable[dict]]:
    """JEV primary node; failures transition to fallback without chain retry."""
    settings = get_settings()

    async def route(state: dict, config: RunnableConfig | None = None) -> dict:
        if not _last_message_is_human(state):
            return _route_intent_result("unknown", 0.0, "none", False)
        if not _jev_state(state)["current_user_message"]:
            return _route_intent_result("unknown", 0.0, "none", False)
        try:
            def call_jev():
                with httpx.Client(timeout=settings.jev_timeout_seconds) as jev_client:
                    return _classify_route_intent_with_jev(jev_client, state, settings)

            candidate = await asyncio.to_thread(call_jev)
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            return jev_fallback_result()
        if candidate is None:
            return jev_fallback_result()
        if route_intent_is_confident(
            candidate["route_intent"], candidate["confidence"]
        ):
            return candidate
        return _route_intent_result("unknown", candidate["confidence"], "jev", False)

    return route
