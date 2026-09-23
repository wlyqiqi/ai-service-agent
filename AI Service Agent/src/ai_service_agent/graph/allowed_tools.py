"""Deterministic candidate-tool boundary for route-intent execution.

The model may select a tool only after Policy narrows the candidate set. The
middleware filters model-visible tools; ToolNode wrappers reject calls that are
not in the same allowlist.
"""

import json
from collections.abc import Awaitable, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage


def _allowed_tools(state) -> set[str] | None:
    if not isinstance(state, dict) or "allowed_tools" not in state:
        return None
    value = state.get("allowed_tools") or []
    return {str(name) for name in value}


def _not_allowed(request) -> ToolMessage:
    tool_call = request.tool_call
    return ToolMessage(
        content=json.dumps(
            {"error": "tool_not_allowed", "tool": tool_call.get("name")},
            ensure_ascii=False,
        ),
        name=str(tool_call.get("name", "")),
        tool_call_id=str(tool_call.get("id", "")),
        status="error",
    )


class AllowedToolsMiddleware(AgentMiddleware):
    name = "allowed_tools_middleware"

    def wrap_model_call(self, request, handler):
        allowed = _allowed_tools(request.state)
        if allowed is not None:
            request = request.override(
                tools=[tool for tool in request.tools if getattr(tool, "name", "") in allowed]
            )
        return handler(request)

    async def awrap_model_call(self, request, handler):
        allowed = _allowed_tools(request.state)
        if allowed is not None:
            request = request.override(
                tools=[tool for tool in request.tools if getattr(tool, "name", "") in allowed]
            )
        return await handler(request)

    def wrap_tool_call(self, request, handler):
        allowed = _allowed_tools(request.state)
        if allowed is not None and request.tool_call.get("name") not in allowed:
            return _not_allowed(request)
        return handler(request)

    async def awrap_tool_call(self, request, handler):
        allowed = _allowed_tools(request.state)
        if allowed is not None and request.tool_call.get("name") not in allowed:
            return _not_allowed(request)
        return await handler(request)


def make_allowed_tool_boundary_wrapper_sync(next_wrapper=None):
    def wrapper(request, execute):
        allowed = _allowed_tools(request.state)
        if allowed is not None and request.tool_call.get("name") not in allowed:
            return _not_allowed(request)
        if next_wrapper is None:
            return execute(request)
        return next_wrapper(request, execute)

    return wrapper


def make_allowed_tool_boundary_wrapper_async(next_wrapper=None):
    async def wrapper(request, execute):
        allowed = _allowed_tools(request.state)
        if allowed is not None and request.tool_call.get("name") not in allowed:
            return _not_allowed(request)
        if next_wrapper is None:
            return await execute(request)
        return await next_wrapper(request, execute)

    return wrapper
