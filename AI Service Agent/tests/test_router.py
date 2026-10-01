import pytest
from langchain_core.messages import AIMessage

from ai_service_agent.graph.router import make_deepseek_router
from ai_service_agent.testing import ToolCallingFakeModel


@pytest.mark.asyncio
async def test_router_parses_structured_output():
    model = ToolCallingFakeModel(
        responses=[AIMessage(content='{"capability": "order", "risk_level": "read", "confidence": 0.9}')]
    )
    node = make_deepseek_router(model, route_intent_enabled=False)
    out = await node({"messages": [], "user_id": "u1", "session_id": "s1", "context": {}})
    assert out["capability"] == "order"
    assert out["risk_level"] == "read"
    assert out["confidence"] == 0.9


@pytest.mark.asyncio
async def test_router_low_confidence_goes_unknown():
    model = ToolCallingFakeModel(
        responses=[AIMessage(content='{"capability": "order", "risk_level": "read", "confidence": 0.3}')]
    )
    node = make_deepseek_router(model, route_intent_enabled=False)
    out = await node({"messages": [], "user_id": "u1", "session_id": "s1", "context": {}})
    assert out["capability"] == "unknown"

import json

import pytest
from langchain_core.runnables import RunnableBinding
from langchain_openai import ChatOpenAI

from ai_service_agent.graph.router import _router_model, make_deepseek_router
from ai_service_agent.testing import ToolCallingFakeModel, router_json_message


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
    model = ToolCallingFakeModel(responses=[router_json_message("faq", "read", 0.9)])
    assert _router_model(model) is model


def test_router_parse_accepts_json_mode_string_and_dict():
    assert _parse_router_content('{"capability": "order", "risk_level": "read", "confidence": 0.9}') == {
        "capability": "order", "risk_level": "read", "confidence": 0.9,
    }
    assert _parse_router_content({"capability": "order", "risk_level": "read", "confidence": 0.9})["capability"] == "order"


def test_router_parse_rejects_tool_strategy_wrapper_text():
    """回归钉：ToolStrategy 的 'Returning structured response' 描述性文本不是稳定契约
    （依赖结构化输出包装实现细节），必须显式失败，不得新增解析分支。"""
    content = "Returning structured response: capability='order' risk_level='read' confidence=0.9"
    with pytest.raises(json.JSONDecodeError):
        _parse_router_content(content)


@pytest.mark.asyncio
async def test_router_parses_structured_output():
    model = ToolCallingFakeModel(
        responses=[router_json_message("order", "read", 0.9)]
    )
    node = make_deepseek_router(model, route_intent_enabled=False)
    out = await node({"messages": [], "user_id": "u1", "session_id": "s1", "context": {}})
    assert out["capability"] == "order"
    assert out["risk_level"] == "read"
    assert out["confidence"] == 0.9


@pytest.mark.asyncio
async def test_router_low_confidence_goes_unknown():
    model = ToolCallingFakeModel(
        responses=[router_json_message("order", "read", 0.3)]
    )
    node = make_deepseek_router(model, route_intent_enabled=False)
    out = await node({"messages": [], "user_id": "u1", "session_id": "s1", "context": {}})
    assert out["capability"] == "unknown"


def test_router_system_prompt_deleted():
    from ai_service_agent import prompts
    assert not hasattr(prompts, "ROUTER_SYSTEM")


@pytest.mark.asyncio
async def test_deepseek_fallback_outputs_route_intent():
    from ai_service_agent.testing import ToolCallingFakeModel
    from langchain_core.messages import AIMessage
    from ai_service_agent.graph.router import make_deepseek_router

    model = ToolCallingFakeModel(responses=[AIMessage(content='{"route_intent":"faq","confidence":0.95}')])
    node = make_deepseek_router(model)
    state = {"messages": [{"role": "human", "content": "产品怎么用"}]}
    result = await node(state)
    assert result["route_intent"] == "faq"
    assert result["routing_backend"] == "deepseek"
    assert result["routing_fallback"] is True
    assert result["route_control"] == "policy"
