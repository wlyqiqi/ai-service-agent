import asyncio
import json
import math
from collections.abc import Awaitable
from typing import Callable, Literal

import httpx
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.messages import SystemMessage
from langchain_core.messages.utils import count_tokens_approximately, trim_messages
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel

from ai_service_agent.config import get_settings
from ai_service_agent.graph.context.policy import make_router_policy
from ai_service_agent.graph.messages import message_text
from ai_service_agent.graph.route_policy import (
    ROUTE_POLICY,
    route_intent_is_confident,
)
from ai_service_agent.prompts import ROUTE_INTENT_QUESTION, ROUTE_INTENT_SYSTEM

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

_JEV_ROUTE_INTENT_QUESTION = ROUTE_INTENT_QUESTION

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
    """Parse model output into route intent dict with fence stripping fallback."""
    if isinstance(content, str):
        text = content.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            body = "\n".join(lines[1:-1]) if len(lines) > 2 else text
            text = body.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end > start:
                return json.loads(text[start:end + 1])
            raise
    if isinstance(content, dict):
        return content
    return RouteIntentSchema.model_validate(content).model_dump()

def _jev_state(state: dict) -> dict:
    router_messages = build_router_messages(state)
    current_user_message = message_text(router_messages[-1]) if router_messages else ""
    recent_context = [
        {
            "role": "ai" if getattr(message, "type", "human") == "ai" else "human",
            "content": message_text(message),
        }
        for message in router_messages[:-1]
    ]

    return {
        "current_user_message": current_user_message,
        "recent_context": recent_context,
    }


def build_router_messages(state: dict) -> list:
    """Router 动态 messages：可见 turn 窗口 + 输入 token 预算裁剪（system 由调用方另加）。"""
    messages = list(state.get("messages") or [])
    if not messages:
        return [HumanMessage(content="")]
    settings = get_settings()
    policy = make_router_policy(settings)
    system_message = SystemMessage(content=ROUTE_INTENT_SYSTEM)
    fixed_tokens = count_tokens_approximately([system_message], tools=[])
    dynamic_budget = max(1, policy.max_total_tokens - fixed_tokens)
    dynamic_messages = policy.build_messages(messages)
    if not dynamic_messages:
        return [HumanMessage(content="")]
    return trim_messages(
        dynamic_messages,
        strategy="last",
        max_tokens=dynamic_budget,
        token_counter=count_tokens_approximately,
        start_on="human",
        end_on=("human", "tool"),
        allow_partial=False,
    )

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

def _backend_label(model) -> str:
    name = str(getattr(model, "model_name", None) or getattr(model, "model", "") or "")
    lower = name.lower()
    if "qwen" in lower:
        return "qwen"
    if "deepseek" in lower:
        return "deepseek"
    return "router"


def _finalize_route_intent(intent: str, confidence: float, backend: str, fallback: bool) -> dict:
    if intent not in ROUTE_POLICY or not route_intent_is_confident(intent, confidence):
        intent = "unknown"
    return _route_intent_result(intent, confidence, backend, fallback)


def make_deepseek_router(model, fallback_model=None) -> Callable[[dict], Awaitable[dict]]:
    """LLM Route Intent 节点（JEV 关闭时的主路由；JEV 失败时的兜底节点同此）。

    Real model: with_structured_output(function_calling)。主模型失败时可再试
    fallback_model（如 Qwen → DeepSeek-V4-Flash）。Fake：create_agent + JSON 解析。
    """
    if isinstance(model, FakeMessagesListChatModel):
        agent = create_agent(_router_model(model), tools=[], system_prompt=ROUTE_INTENT_SYSTEM)

        async def deepseek_route(state: dict, config: RunnableConfig | None = None) -> dict:
            try:
                result = await agent.ainvoke({"messages": build_router_messages(state)}, config=config)
                payload = _parse_route_intent_content(result["messages"][-1].content)
            except (json.JSONDecodeError, TypeError, ValueError):
                return _route_intent_result("unknown", 0.0, "deepseek", True)
            parsed = RouteIntentSchema.model_validate(payload)
            return _finalize_route_intent(
                parsed.route_intent, float(parsed.confidence), "deepseek", True
            )
    else:
        primary = model.with_structured_output(RouteIntentSchema, method="function_calling")
        secondary = None
        if fallback_model is not None and not isinstance(fallback_model, FakeMessagesListChatModel):
            secondary = fallback_model.with_structured_output(
                RouteIntentSchema, method="function_calling"
            )
        primary_backend = _backend_label(model)
        fallback_backend = _backend_label(fallback_model) if fallback_model is not None else "deepseek"

        async def deepseek_route(state: dict, config: RunnableConfig | None = None) -> dict:
            messages = [SystemMessage(content=ROUTE_INTENT_SYSTEM), *build_router_messages(state)]
            parsed = None
            used_fallback = False
            backend = primary_backend
            try:
                parsed = await primary.ainvoke(messages, config=config)
            except Exception:
                parsed = None
            if parsed is None and secondary is not None:
                used_fallback = True
                backend = fallback_backend
                try:
                    parsed = await secondary.ainvoke(messages, config=config)
                except Exception:
                    parsed = None
            if parsed is None:
                return _route_intent_result("unknown", 0.0, backend, True)
            return _finalize_route_intent(
                parsed.route_intent,
                float(parsed.confidence),
                backend,
                used_fallback or backend == "deepseek",
            )

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
