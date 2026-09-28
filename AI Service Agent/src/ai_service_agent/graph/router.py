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
from ai_service_agent.graph.route_policy import (
    ROUTE_POLICY,
    route_intent_is_confident,
)
from ai_service_agent.prompts import ROUTE_INTENT_SYSTEM, ROUTER_SYSTEM

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


class RouterSchema(BaseModel):
    capability: str
    risk_level: Literal["read", "mutation", "high_risk"]
    confidence: float

class RouteIntentSchema(BaseModel):
    route_intent: str
    confidence: float

_JEV_ROUTE_INTENT_QUESTION = {
    "route_intent": {
        "type": "choice",
        "instructions": (
            "判断 current_user_message 的主要业务意图。"
            "current_user_message 是唯一主判断对象。"
            "recent_context 仅用于解析当前消息中的指代和省略，例如“那”“这个”“它”“刚才那个订单”。"
            "不得把 recent_context 中已经完成的历史意图当作当前意图。"
            "只选择当前系统支持的业务分类。"
            "用户意图清晰但系统没有对应能力时选择 unsupported_action。"
            "无法确定当前意图时选择 unknown。"
        ),
        "criteria": {
            "faq": "产品使用、售后政策、商品参数、故障排查等知识问答",
            "smalltalk": "问候、感谢或非业务闲聊",
            "order_query": "根据订单号查询某一笔订单",
            "my_orders": "查询当前用户名下的订单列表",
            "shipment_query": "查询物流或配送进度",
            "account_profile_update": (
                "修改收货地址、联系电话或手机号。"
                "即使输入包含订单号，也不能因订单号进入 order_mutation。"
            ),
            "order_mutation": "修改订单备注、开发票或创建售后工单",
            "cancel_order_execute": "用户明确要求执行取消订单",
            "refund_apply_execute": "用户明确要求执行退款申请",
            "unsupported_action": (
                "用户意图清晰但当前系统没有对应工具可执行。"
                "不要把取消预约、清空订单历史、订单所有权转移或人工流程类请求映射到 mutation。"
                "这些动作归为 unsupported_action，不归 unknown。"
            ),
            "unknown": "无法理解 current_user_message，缺少当前用户意图，或即使结合 recent_context 仍无法确定意图。系统不支持的可理解操作应归为 unsupported_action，不归 unknown。",
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

def _parse_router_content(content) -> dict:
    """解析 Router 模型输出：只接受 JSON 字符串 / dict。

    ToolStrategy 的 "Returning structured response: ..." 描述性文本不是稳定契约，
    必须让 json.loads 显式失败（回归钉，见 test_router.py），不得新增解析分支。
    """
    if isinstance(content, str):
        return json.loads(content)
    if isinstance(content, dict):
        return content
    return RouterSchema.model_validate(content).model_dump()

def _parse_route_intent_content(content) -> dict:
    if isinstance(content, str):
        return json.loads(content)
    if isinstance(content, dict):
        return content
    return RouteIntentSchema.model_validate(content).model_dump()

def _jev_state(state: dict) -> dict:
    messages = list(state.get("messages", []))
    last_message = messages[-1]
    if isinstance(last_message, dict):
        last_content = last_message.get("content", "")
    else:
        last_content = getattr(last_message, "content", "")

    recent_context = []
    for message in messages[-3:-1]:
        if isinstance(message, dict):
            role = message.get("type", message.get("role", "human"))
            content = message.get("content", "")
        else:
            role = getattr(message, "type", "human")
            content = getattr(message, "content", "")
        recent_context.append({
            "role": str(role),
            "content": content if isinstance(content, str) else str(content),
        })

    return {
        "current_user_message": last_content if isinstance(last_content, str) else str(last_content),
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

def make_deepseek_router(model, *, route_intent_enabled: bool) -> Callable[[dict], Awaitable[dict]]:
    """DeepSeek fallback node; retry policy is attached by the root graph."""
    system_prompt = ROUTE_INTENT_SYSTEM if route_intent_enabled else ROUTER_SYSTEM
    agent = create_agent(_router_model(model), tools=[], system_prompt=system_prompt)

    if not route_intent_enabled:
        async def deepseek_route(state: dict, config: RunnableConfig | None = None) -> dict:
            result = await agent.ainvoke({"messages": state["messages"]}, config=config)
            payload = _parse_router_content(result["messages"][-1].content)
            parsed = RouterSchema.model_validate(payload)
            confidence = float(parsed.confidence)
            threshold = get_settings().router_conf_threshold
            return {
                "capability": parsed.capability if confidence >= threshold else "unknown",
                "risk_level": parsed.risk_level,
                "confidence": confidence,
                "execution_status": "routing",
                "route_control": "policy",
            }

        return deepseek_route

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
