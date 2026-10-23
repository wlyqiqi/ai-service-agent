import pytest
from langchain_core.messages import HumanMessage

from ai_service_agent.graph.workflows.mutation_select import make_mutation_select_node
from ai_service_agent.testing import ToolCallingFakeModel, select_json_message
from ai_service_agent.tools import DEFAULT_TOOL_REGISTRY


@pytest.mark.asyncio
async def test_mutation_select_rejects_tool_outside_allowed_tools():
    model = ToolCallingFakeModel(
        responses=[select_json_message(
            "update_address",
            {"order_id": "ORD-PAID-1", "address": "北京市朝阳区"},
        )]
    )
    registry = DEFAULT_TOOL_REGISTRY
    select = make_mutation_select_node(
        model,
        registry.for_risk_level("mutation"),
        registry,
    )
    state = {
        "messages": [HumanMessage(content="帮我改地址")],
        "user_id": "u1",
        "context": {},
        "allowed_tools": ["update_phone"],
    }
    out = await select(state)
    assert out["context"]["precheck_reason"] == "tool_not_allowed"


@pytest.mark.asyncio
async def test_mutation_select_prompt_only_contains_allowed_tools():
    captured = {}

    class _PromptCaptureModel(ToolCallingFakeModel):
        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            captured["prompt"] = messages[0].content
            return await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)

    model = _PromptCaptureModel(
        responses=[select_json_message(
            "update_phone",
            {"phone": "13800138000"},
        )]
    )
    registry = DEFAULT_TOOL_REGISTRY
    select = make_mutation_select_node(
        model,
        registry.for_risk_level("mutation"),
        registry,
    )
    out = await select({
        "messages": [HumanMessage(content="帮我换个手机号，新号 13800138000")],
        "user_id": "u1",
        "context": {},
        "allowed_tools": ["update_phone"],
    })
    assert "update_address" not in captured["prompt"]
    assert "update_phone" in captured["prompt"]
    assert out["context"]["pending_tool"]["tool"] == "update_phone"
