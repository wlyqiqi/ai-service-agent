import pytest
from langchain_core.messages import HumanMessage

from ai_service_agent.graph.workflows.select import make_select_node
from ai_service_agent.testing import ToolCallingFakeModel, select_json_message
from ai_service_agent.tools import DEFAULT_TOOL_REGISTRY


def test_mutation_select_rejects_tool_outside_allowed_tools():
    model = ToolCallingFakeModel(
        responses=[select_json_message(
            "update_address",
            {"order_id": "ORD-PAID-1", "address": "北京市朝阳区"},
        )]
    )
    registry = DEFAULT_TOOL_REGISTRY
    select = make_select_node(
        model,
        "mutation",
        registry.for_risk_level("mutation"),
        registry,
    )
    state = {
        "messages": [HumanMessage(content="帮我改地址")],
        "user_id": "u1",
        "context": {},
        "allowed_tools": ["update_phone"],
    }
    out = select(state)
    assert out["context"]["precheck_reason"] == "tool_not_allowed"


def test_mutation_select_prompt_only_contains_allowed_tools():
    captured = {}

    class _PromptCaptureModel(ToolCallingFakeModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            captured["prompt"] = messages[0].content
            return super()._generate(messages, stop, run_manager, **kwargs)

    model = _PromptCaptureModel(
        responses=[select_json_message(
            "update_phone",
            {"phone": "13800138000"},
        )]
    )
    registry = DEFAULT_TOOL_REGISTRY
    select = make_select_node(
        model,
        "mutation",
        registry.for_risk_level("mutation"),
        registry,
    )
    out = select({
        "messages": [HumanMessage(content="帮我换个手机号，新号 13800138000")],
        "user_id": "u1",
        "context": {},
        "allowed_tools": ["update_phone"],
    })
    assert "update_address" not in captured["prompt"]
    assert "update_phone" in captured["prompt"]
    assert out["context"]["pending_tool"]["tool"] == "update_phone"
