"""占位串归一化：哨兵识别、读路径 middleware、写路径幂等键。"""

import importlib.util
import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.prebuilt.tool_node import ToolCallRequest

from ai_service_agent.graph.agents.read_agent import make_read_graph
from ai_service_agent.graph.agents.tool_args import ToolArgumentNormalizationMiddleware
from ai_service_agent.testing import ToolCallingFakeModel
from ai_service_agent.tools import orders as orders_mod
from ai_service_agent.tools.orders import my_orders
from ai_service_agent.tools.params import is_absent_sentinel, normalize_absent_args
from conftest import ainvoke_graph

ROOT = Path(__file__).resolve().parents[1]


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


def _seed_db(tmp_path: Path) -> Path:
    spec = importlib.util.spec_from_file_location(
        "seed_mock_db_arg_norm", ROOT / "scripts" / "seed_mock_db.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    db_path = tmp_path / "orders.db"
    module.build_database(str(db_path))
    return db_path


def _use_orders_db(monkeypatch, db_path: Path) -> None:
    class _Settings:
        mock_ecommerce_db_path = str(db_path)

    monkeypatch.setattr(orders_mod, "get_settings", lambda: _Settings())


class _SpyHook:
    def __init__(self):
        self.args = None

    def on_before(self, context):
        self.args = dict(context.args)

    def on_after(self, context, outcome):
        return None


def test_my_orders_status_null_returns_all_orders_for_the_caller(monkeypatch, tmp_path):
    _use_orders_db(monkeypatch, _seed_db(tmp_path))
    hook = _SpyHook()
    model = ToolCallingFakeModel(responses=[
        AIMessage(
            content="",
            tool_calls=[{
                "name": "my_orders",
                "args": {"status": "null"},
                "id": "call-orders",
                "type": "tool_call",
            }],
        ),
        AIMessage(content="这是您的订单。"),
    ])
    graph = make_read_graph(model, tools=[my_orders], tool_hooks=[hook])
    out = ainvoke_graph(
        graph,
        {
            "messages": [HumanMessage(content="我的订单")],
            "user_id": "u1",
            "session_id": "orders-null",
            "context": {},
        },
        {"configurable": {"thread_id": "orders-null"}},
    )
    tool_message = next(message for message in out["messages"] if getattr(message, "type", None) == "tool")
    payload = json.loads(tool_message.content)
    assert payload["count"] == 5
    assert "error" not in payload
    assert hook.args == {}
