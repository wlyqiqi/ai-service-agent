"""占位串归一化：哨兵识别、读路径 middleware、写路径幂等键。"""

import pytest
from langgraph.prebuilt.tool_node import ToolCallRequest

from ai_service_agent.graph.agents.tool_args import ToolArgumentNormalizationMiddleware
from ai_service_agent.tools.params import is_absent_sentinel, normalize_absent_args


def test_absent_sentinels_match_case_and_whitespace():
    for value in ("null", " NULL ", "None", "nil", "undefined", "n/a", "na", "", "   "):
        assert is_absent_sentinel(value)


def test_dash_and_real_values_are_kept():
    assert is_absent_sentinel("-") is False
    assert is_absent_sentinel("paid") is False
    assert is_absent_sentinel(None) is False
    assert is_absent_sentinel(0) is False


def test_normalize_absent_args_drops_sentinels_and_honors_skip():
    normalized = normalize_absent_args(
        {
            "status": "null",
            "note": "-",
            "request_id": "null",
            "amount": 0,
            "flag": False,
        },
        skip=frozenset({"request_id"}),
    )
    assert normalized == {
        "note": "-",
        "request_id": "null",
        "amount": 0,
        "flag": False,
    }


def _tool_request(args: dict) -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={"name": "my_orders", "args": dict(args), "id": "call-1", "type": "tool_call"},
        tool=None,
        state={},
        runtime=None,
    )


def test_middleware_strips_placeholder_args_before_the_tool_runs():
    request = _tool_request({"status": "NULL", "limit_note": "-"})
    seen = {}

    def handler(forwarded):
        seen["request"] = forwarded
        seen["args"] = dict(forwarded.tool_call["args"])
        return "ran"

    assert ToolArgumentNormalizationMiddleware().wrap_tool_call(request, handler) == "ran"
    assert seen["args"] == {"limit_note": "-"}
    assert seen["request"] is not request
    assert request.tool_call["args"]["status"] == "NULL"


def test_middleware_passes_clean_args_through_without_override():
    request = _tool_request({"status": "paid"})

    def handler(forwarded):
        assert forwarded is request
        return "ran"

    assert ToolArgumentNormalizationMiddleware().wrap_tool_call(request, handler) == "ran"


@pytest.mark.asyncio
async def test_async_middleware_strips_placeholder_args():
    request = _tool_request({"order_id": " null "})
    seen = {}

    async def handler(forwarded):
        seen["args"] = dict(forwarded.tool_call["args"])
        return "ran"

    result = await ToolArgumentNormalizationMiddleware().awrap_tool_call(request, handler)
    assert result == "ran"
    assert seen["args"] == {}
