"""占位串归一化：哨兵识别、读路径 middleware、写路径幂等键。"""

import importlib.util
import json
import sqlite3
import uuid
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from ai_service_agent.domain.orders import SQLiteOrderRepository
from ai_service_agent.graph import builder as builder_mod
from ai_service_agent.graph.agents.read_agent import make_read_graph
from ai_service_agent.graph.agents.tool_args import ToolArgumentNormalizationMiddleware
from ai_service_agent.graph.builder import build_graph
from ai_service_agent.graph.workflows.mutation_select import make_mutation_select_node
from ai_service_agent.testing import ToolCallingFakeModel, router_json_message, select_json_message
from ai_service_agent.tools import DEFAULT_TOOL_REGISTRY
from ai_service_agent.tools import orders as orders_mod
from ai_service_agent.tools import write as write_mod
from ai_service_agent.tools.kb import kb_search
from ai_service_agent.tools.orders import my_orders, order_lookup, shipment_query
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

    settings = _Settings()
    monkeypatch.setattr(orders_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(write_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(
        builder_mod,
        "SQLiteOrderRepository",
        lambda path: SQLiteOrderRepository(str(db_path)),
    )


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


def _read_tool_payload(graph, text: str, thread_id: str) -> dict:
    out = ainvoke_graph(
        graph,
        {
            "messages": [HumanMessage(content=text)],
            "user_id": "u1",
            "session_id": thread_id,
            "context": {},
        },
        {"configurable": {"thread_id": thread_id}},
    )
    tool_message = next(message for message in out["messages"] if getattr(message, "type", None) == "tool")
    return json.loads(tool_message.content)


def test_shipment_query_order_id_null_returns_the_callers_shipments(monkeypatch, tmp_path):
    _use_orders_db(monkeypatch, _seed_db(tmp_path))
    model = ToolCallingFakeModel(responses=[
        AIMessage(
            content="",
            tool_calls=[{
                "name": "shipment_query",
                "args": {"order_id": "null"},
                "id": "call-ship",
                "type": "tool_call",
            }],
        ),
        AIMessage(content="这是您的物流。"),
    ])
    graph = make_read_graph(model, tools=[shipment_query])
    payload = _read_tool_payload(graph, "我的快递到哪了", "ship-null")
    assert payload["count"] >= 1
    assert "error" not in payload
    assert all(item["order_id"] for item in payload["shipments"])


def test_order_lookup_order_id_null_is_a_missing_argument_not_not_found(monkeypatch, tmp_path):
    _use_orders_db(monkeypatch, _seed_db(tmp_path))
    model = ToolCallingFakeModel(responses=[
        AIMessage(
            content="",
            tool_calls=[{
                "name": "order_lookup",
                "args": {"order_id": "null"},
                "id": "call-lookup",
                "type": "tool_call",
            }],
        ),
        AIMessage(content="请提供订单号。"),
    ])
    graph = make_read_graph(model, tools=[order_lookup])
    out = ainvoke_graph(
        graph,
        {
            "messages": [HumanMessage(content="查一下订单")],
            "user_id": "u1",
            "session_id": "lookup-null",
            "context": {},
        },
        {"configurable": {"thread_id": "lookup-null"}},
    )
    tool_message = next(message for message in out["messages"] if getattr(message, "type", None) == "tool")
    body = tool_message.content
    assert "not_found" not in body
    assert "null" not in body
    assert "order_id" in body
    assert "missing" in body.lower() or "required" in body.lower()


def _mutation_select(args: dict):
    model = ToolCallingFakeModel(responses=[select_json_message("update_order_note", args)])
    return make_mutation_select_node(
        model,
        DEFAULT_TOOL_REGISTRY.for_risk_level("mutation"),
        DEFAULT_TOOL_REGISTRY,
    )


def test_select_replaces_non_uuid_request_id_before_the_write_is_pending():
    import asyncio

    node = _mutation_select({
        "order_id": "ORD-1",
        "note": "放驿站",
        "request_id": "null",
    })
    out = asyncio.run(node({
        "messages": [HumanMessage(content="给订单加备注")],
        "user_id": "u1",
        "context": {},
    }))
    request_id = out["context"]["pending_tool"]["request_id"]
    assert request_id != "null"
    assert uuid.UUID(request_id).version == 4
    assert out["context"]["pending_tool"]["args"]["request_id"] == request_id
    assert out["context"]["pending_tool"]["args"]["note"] == "放驿站"


def test_select_keeps_an_existing_uuid4_request_id():
    import asyncio

    kept = "11111111-1111-4111-8111-111111111111"
    node = _mutation_select({
        "order_id": "ORD-1",
        "note": "放驿站",
        "request_id": kept,
    })
    import asyncio
    out = asyncio.run(node({
        "messages": [HumanMessage(content="给订单加备注")],
        "user_id": "u1",
        "context": {},
    }))
    assert out["context"]["pending_tool"]["request_id"] == kept


def _confirm_note(order_id: str, note: str) -> dict:
    return {"action": "confirm", "order_id": order_id, "note": note}


def _note_for(db_path: Path, order_id: str) -> str | None:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT note FROM orders WHERE order_id=?", (order_id,)).fetchone()
    finally:
        conn.close()
    return None if row is None else row[0]


def test_null_request_id_writes_two_different_orders(monkeypatch, tmp_path):
    db_path = _seed_db(tmp_path)
    _use_orders_db(monkeypatch, db_path)
    writes = (
        ("ORD-1", "放驿站", "note-null-1"),
        ("ORD-10086", "另一条备注", "note-null-2"),
    )
    for order_id, note, thread_id in writes:
        model = ToolCallingFakeModel(responses=[
            router_json_message("order_mutation", 0.9),
            select_json_message("update_order_note", {
                "order_id": order_id,
                "note": note,
                "request_id": "null",
            }),
        ])
        graph = build_graph(model, tools=[kb_search], checkpointer=MemorySaver())
        pending = ainvoke_graph(
            graph,
            {
                "messages": [HumanMessage(content=f"给订单 {order_id} 加备注")],
                "user_id": "u1",
                "session_id": thread_id,
                "context": {},
            },
            {"configurable": {"thread_id": thread_id}},
        )
        assert pending.get("__interrupt__")
        out = ainvoke_graph(
            graph,
            Command(resume=_confirm_note(order_id, note)),
            {"configurable": {"thread_id": thread_id}},
        )
        tool_message = next(message for message in out["messages"] if getattr(message, "type", None) == "tool")
        payload = json.loads(tool_message.content)
        assert payload["replayed"] is False
        assert payload["result"]["order_id"] == order_id
        assert payload["result"]["note"] == note
        assert _note_for(db_path, order_id) == note

    conn = sqlite3.connect(db_path)
    try:
        keys = conn.execute(
            "SELECT request_id FROM idempotency WHERE tool_name='update_order_note'"
        ).fetchall()
    finally:
        conn.close()
    assert len(keys) == 2
    assert all(uuid.UUID(row[0]).version == 4 for row in keys)
    assert keys[0][0] != keys[1][0]


def _customer_address(db_path: Path, customer_id: str = "u1") -> str | None:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT address FROM customers WHERE customer_id=?", (customer_id,)
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else row[0]


def test_null_address_and_note_are_missing_fields_not_stored_values():
    import asyncio

    address = _mutation_select_for("update_address", {
        "order_id": "ORD-PAID-1",
        "address": "null",
        "request_id": "null",
    })
    address_out = asyncio.run(address({
        "messages": [HumanMessage(content="改一下收货地址")],
        "user_id": "u1",
        "context": {},
    }))
    address_pending = address_out["context"]["pending_tool"]
    assert address_pending["tool"] == "update_address"
    assert "address" not in address_pending["args"]
    assert uuid.UUID(address_pending["request_id"]).version == 4
    assert "precheck_reason" not in address_out["context"]

    note = _mutation_select_for("update_order_note", {
        "order_id": "ORD-1",
        "note": " NULL ",
        "request_id": "none",
    })
    note_out = asyncio.run(note({
        "messages": [HumanMessage(content="给订单加个备注")],
        "user_id": "u1",
        "context": {},
    }))
    note_pending = note_out["context"]["pending_tool"]
    assert note_pending["tool"] == "update_order_note"
    assert "note" not in note_pending["args"]
    assert uuid.UUID(note_pending["request_id"]).version == 4


def _mutation_select_for(tool_name: str, args: dict):
    model = ToolCallingFakeModel(responses=[select_json_message(tool_name, args)])
    return make_mutation_select_node(
        model,
        DEFAULT_TOOL_REGISTRY.for_risk_level("mutation"),
        DEFAULT_TOOL_REGISTRY,
    )


def test_null_address_and_note_do_not_land_in_the_database(monkeypatch, tmp_path):
    db_path = _seed_db(tmp_path)
    _use_orders_db(monkeypatch, db_path)
    address_before = _customer_address(db_path)
    note_before = _note_for(db_path, "ORD-1")
    cases = (
        (
            "update_address",
            {"order_id": "ORD-PAID-1", "address": "null", "request_id": "null"},
            "addr-null",
            "改一下收货地址",
            "account_profile_update",
        ),
        (
            "update_order_note",
            {"order_id": "ORD-1", "note": "null", "request_id": "null"},
            "note-null",
            "给订单加个备注",
            "order_mutation",
        ),
    )
    for tool_name, args, thread_id, text, route in cases:
        model = ToolCallingFakeModel(responses=[
            router_json_message(route, 0.9),
            select_json_message(tool_name, args),
        ])
        graph = build_graph(model, tools=[kb_search], checkpointer=MemorySaver())
        out = ainvoke_graph(
            graph,
            {
                "messages": [HumanMessage(content=text)],
                "user_id": "u1",
                "session_id": thread_id,
                "context": {},
            },
            {"configurable": {"thread_id": thread_id}},
        )
        assert out.get("__interrupt__")
        assert [message for message in out["messages"] if getattr(message, "type", None) == "tool"] == []

    assert _customer_address(db_path) == address_before
    assert _note_for(db_path, "ORD-1") == note_before
    assert address_before != "null"
    assert note_before != "null"


def test_select_keeps_a_dash_note():
    import asyncio

    node = _mutation_select({
        "order_id": "ORD-1",
        "note": "-",
        "request_id": "11111111-1111-4111-8111-111111111111",
    })
    out = asyncio.run(node({
        "messages": [HumanMessage(content="给订单加备注")],
        "user_id": "u1",
        "context": {},
    }))
    assert out["context"]["pending_tool"]["args"]["note"] == "-"
