import json
import math
from typing import Callable, Literal

import httpx
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from pydantic import BaseModel

from ai_service_agent.config import get_settings
from ai_service_agent.graph.route_policy import (
    ROUTE_POLICY,
    route_intent_is_confident,
)
from ai_service_agent.prompts import ROUTE_INTENT_SYSTEM, ROUTER_SYSTEM

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
            "判断用户当前 turn 的主要业务意图。"
            "只根据当前系统真实支持的业务分类；"
            "不支持的动作必须归为 unsupported_action。"
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
                "当前系统没有对应工具的动作。"
                "不要把取消预约、清空订单历史、订单所有权转移或人工流程类请求映射到 mutation。"
            ),
            "unknown": "无法判断、缺少上下文、模糊或不在支持范围内",
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
    messages = []
    for message in state.get("messages", [])[-6:]:
        if isinstance(message, dict):
            role = message.get("type", message.get("role", "human"))
            content = message.get("content", "")
        else:
            role = getattr(message, "type", "human")
            content = getattr(message, "content", "")
        messages.append({
            "role": str(role),
            "content": content if isinstance(content, str) else str(content),
        })
    return {"messages": messages}

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

    return {
        "route_intent": choice,
        "confidence": confidence,
        "execution_status": "routing",
        "routing_backend": "jev",
        "routing_fallback": False,
    }

def make_router(model) -> Callable[[dict], dict]:
    """生产用 create_agent + json_object 结构化输出；fake model 下解析其 JSON 文本。

    两条路径（真实 json_object / fake 直接 JSON 字符串）产出结构一致，均经 RouterSchema 校验。
    """
    settings = get_settings()
    if settings.route_intent_enabled:
        return make_route_intent_router(model)

    agent = create_agent(_router_model(model), tools=[], system_prompt=ROUTER_SYSTEM)

    def route(state: dict) -> dict:
        result = agent.invoke({"messages": state["messages"]})
        payload = _parse_router_content(result["messages"][-1].content)
        parsed = RouterSchema.model_validate(payload)
        conf = float(parsed.confidence)
        threshold = get_settings().router_conf_threshold
        return {
            "capability": parsed.capability if conf >= threshold else "unknown",
            "risk_level": parsed.risk_level,
            "confidence": conf,
            "execution_status": "routing",
        }

    return route

def make_route_intent_router(model) -> Callable[[dict], dict]:
    """Route Intent Router v2：Jev primary，DeepSeek fallback，Policy 仍负责路由。"""
    agent = create_agent(_router_model(model), tools=[], system_prompt=ROUTE_INTENT_SYSTEM)

    def deepseek_route(state: dict) -> dict:
        result = agent.invoke({"messages": state["messages"]})
        payload = _parse_route_intent_content(result["messages"][-1].content)
        parsed = RouteIntentSchema.model_validate(payload)
        intent = parsed.route_intent
        confidence = float(parsed.confidence)
        if intent not in ROUTE_POLICY or not route_intent_is_confident(intent, confidence):
            intent = "unknown"
        return {
            "route_intent": intent,
            "confidence": confidence,
            "execution_status": "routing",
            "routing_backend": "deepseek",
            "routing_fallback": True,
        }

    settings = get_settings()
    if not settings.jev_enabled or not settings.jev_api_key or settings.fake_model:
        return deepseek_route

    def route(state: dict) -> dict:
        try:
            with httpx.Client(timeout=settings.jev_timeout_seconds) as jev_client:
                candidate = _classify_route_intent_with_jev(jev_client, state, settings)
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            return deepseek_route(state)
        if candidate is not None:
            if route_intent_is_confident(
                candidate["route_intent"], candidate["confidence"]
            ):
                return candidate
            return {
                "route_intent": "unknown",
                "confidence": candidate["confidence"],
                "execution_status": "routing",
                "routing_backend": "jev",
                "routing_fallback": False,
            }
        return deepseek_route(state)

    return route
