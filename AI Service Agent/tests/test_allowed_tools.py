import json

from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool

from ai_service_agent.graph.allowed_tools import (
    AllowedToolsMiddleware,
    make_allowed_tool_boundary_wrapper_sync,
)


@tool
def alpha_tool() -> dict:
    """Alpha tool."""
    return {"tool": "alpha_tool"}


@tool
def beta_tool() -> dict:
    """Beta tool."""
    return {"tool": "beta_tool"}


def _model_request(allowed_tools):
    return ModelRequest(
        model=None,
        messages=[AIMessage(content="hello")],
        tools=[alpha_tool, beta_tool],
        state={"allowed_tools": allowed_tools},
    )


def test_middleware_filters_model_tools():
    captured = {}

    def handler(request):
        captured["tools"] = request.tools
        return AIMessage(content="ok")

    middleware = AllowedToolsMiddleware()
    middleware.wrap_model_call(_model_request(["beta_tool"]), handler)
    assert [tool.name for tool in captured["tools"]] == ["beta_tool"]


def test_middleware_keeps_all_tools_when_boundary_absent():
    captured = {}

    def handler(request):
        captured["tools"] = request.tools
        return AIMessage(content="ok")

    request = ModelRequest(
        model=None,
        messages=[AIMessage(content="hello")],
        tools=[alpha_tool, beta_tool],
        state={},
    )
    AllowedToolsMiddleware().wrap_model_call(request, handler)
    assert len(captured["tools"]) == 2


def test_tool_boundary_rejects_disallowed_tool():
    request = type(
        "Request",
        (),
        {
            "tool_call": {"name": "beta_tool", "id": "call-1"},
            "state": {"allowed_tools": ["alpha_tool"]},
        },
    )()

    def execute(req):
        raise AssertionError("disallowed tool must not execute")

    result = make_allowed_tool_boundary_wrapper_sync()(request, execute)
    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert json.loads(result.content)["error"] == "tool_not_allowed"


def test_tool_boundary_allows_tool_and_calls_next_wrapper():
    request = type(
        "Request",
        (),
        {
            "tool_call": {"name": "alpha_tool", "id": "call-1"},
            "state": {"allowed_tools": ["alpha_tool"]},
        },
    )()

    def execute(req):
        return "executed"

    def next_wrapper(req, execute):
        return execute(req)

    result = make_allowed_tool_boundary_wrapper_sync(next_wrapper=next_wrapper)(request, execute)
    assert result == "executed"
