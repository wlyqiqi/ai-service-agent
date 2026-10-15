import json

import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableBinding
from langchain_openai import ChatOpenAI

from ai_service_agent.graph.router import (
    _parse_route_intent_content,
    _router_model,
    make_deepseek_router,
)
from ai_service_agent.testing import ToolCallingFakeModel, route_intent_json_message


@pytest.mark.asyncio
async def test_router_parses_route_intent():
    model = ToolCallingFakeModel(responses=[route_intent_json_message("faq", 0.95)])
    node = make_deepseek_router(model)
    result = await node({"messages": [{"role": "human", "content": "产品怎么用"}]})
    assert result["route_intent"] == "faq"
    assert result["confidence"] == 0.95
    assert result["routing_backend"] == "deepseek"
    assert result["routing_fallback"] is True
    assert result["route_control"] == "policy"


@pytest.mark.asyncio
async def test_router_low_confidence_becomes_unknown():
    model = ToolCallingFakeModel(responses=[route_intent_json_message("faq", 0.3)])
    node = make_deepseek_router(model)
    result = await node({"messages": [{"role": "human", "content": "在吗"}]})
    assert result["route_intent"] == "unknown"
    assert result["confidence"] == 0.3


def test_router_real_model_binds_json_object():
    """json_mode 修复（2026-09-15 阻断项）：真实模型路径绑定
    response_format={"type":"json_object"}，模型在 content 返回 JSON 字符串；
    不把 schema 交给 create_agent（避免 ToolStrategy 工具包装导致真实模型解析失败）。"""
    model = ChatOpenAI(model="probe", api_key="probe", base_url="http://localhost")
    bound = _router_model(model)
    assert isinstance(bound, RunnableBinding)
    assert bound.kwargs.get("response_format") == {"type": "json_object"}


def test_router_fake_model_skips_bind():
    """fake model 不绑定：responses 即 json_mode 结构的 JSON 字符串，与真实路径结构一致。"""
    model = ToolCallingFakeModel(responses=[route_intent_json_message("faq", 0.9)])
    assert _router_model(model) is model


def test_router_parse_accepts_json_mode_string_and_dict():
    assert _parse_route_intent_content('{"route_intent": "faq", "confidence": 0.9}') == {
        "route_intent": "faq", "confidence": 0.9,
    }
    assert _parse_route_intent_content({"route_intent": "faq", "confidence": 0.9})["route_intent"] == "faq"


def test_router_parse_rejects_tool_strategy_wrapper_text():
    """回归钉：ToolStrategy 的 'Returning structured response' 描述性文本不是稳定契约
    （依赖结构化输出包装实现细节），必须显式失败，不得新增解析分支。"""
    content = "Returning structured response: route_intent='faq' confidence=0.9"
    with pytest.raises(json.JSONDecodeError):
        _parse_route_intent_content(content)


def test_router_system_prompt_deleted():
    from ai_service_agent import prompts
    assert not hasattr(prompts, "ROUTER_SYSTEM")


@pytest.mark.asyncio
async def test_deepseek_fallback_outputs_route_intent():
    model = ToolCallingFakeModel(responses=[AIMessage(content='{"route_intent":"faq","confidence":0.95}')])
    node = make_deepseek_router(model)
    state = {"messages": [{"role": "human", "content": "产品怎么用"}]}
    result = await node(state)
    assert result["route_intent"] == "faq"
    assert result["routing_backend"] == "deepseek"
    assert result["routing_fallback"] is True
    assert result["route_control"] == "policy"


@pytest.mark.asyncio
async def test_router_primary_failure_uses_fallback_model():
    class _Boom:
        model_name = "Qwen/Qwen3.6-35B-A3B"

        def with_structured_output(self, *args, **kwargs):
            class _Bound:
                async def ainvoke(self, messages, config=None):
                    raise TimeoutError("primary down")

            return _Bound()

    class _Ok:
        model_name = "deepseek-ai/DeepSeek-V4-Flash"

        def with_structured_output(self, *args, **kwargs):
            class _Bound:
                async def ainvoke(self, messages, config=None):
                    from ai_service_agent.graph.router import RouteIntentSchema

                    return RouteIntentSchema(route_intent="faq", confidence=0.91)

            return _Bound()

    node = make_deepseek_router(_Boom(), fallback_model=_Ok())
    result = await node({"messages": [{"role": "human", "content": "产品怎么用"}]})
    assert result["route_intent"] == "faq"
    assert result["routing_backend"] == "deepseek"
    assert result["routing_fallback"] is True
    assert result["route_control"] == "policy"
