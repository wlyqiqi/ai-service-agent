import pytest
from langchain_core.messages import AIMessage
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver

from ai_service_agent.config import get_settings
from ai_service_agent.graph.builder import build_graph
from conftest import ainvoke_graph
from ai_service_agent.testing import ToolCallingFakeModel, route_intent_json_message, router_json_message, select_json_message
from ai_service_agent.tools.kb import kb_search
from ai_service_agent.tools.orders import my_orders, order_lookup, shipment_query


@pytest.fixture(autouse=True)
def isolated_business_db(monkeypatch, tmp_path):
    import importlib.util
    from pathlib import Path
    from ai_service_agent.graph import builder as builder_mod

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("seed_mock_db_route", root / "scripts" / "seed_mock_db.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    db_path = tmp_path / "mock_ecommerce.db"
    module.build_database(str(db_path))
    class _Settings:
        mock_ecommerce_db_path = str(db_path)
        def __init__(self, real):
            self.__dict__.update(real.__dict__)
            self.mock_ecommerce_db_path = str(db_path)
    real_settings = builder_mod.get_settings()
    monkeypatch.setattr(builder_mod, "get_settings", lambda: _Settings(real_settings))


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("JEV_ENABLED", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def disabled(monkeypatch):
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_router_always_outputs_route_intent(disabled):
    model = ToolCallingFakeModel(
        responses=[
            router_json_message("order_query", 0.94),
            AIMessage(content="ORD-1 是已发货订单，金额 99 元。"),
        ]
    )
    graph = build_graph(
        model,
        tools=[kb_search, order_lookup, my_orders, shipment_query],
        checkpointer=MemorySaver(),
    )
    out = ainvoke_graph(graph,
        {
            "messages": [HumanMessage(content="帮我查订单 ORD-1")],
            "user_id": "u1",
            "session_id": "route-legacy-order-1",
            "context": {},
        },
        {"configurable": {"thread_id": "route-legacy-order-1"}},
    )
    assert out["route_intent"] == "order_query"
    assert out["execution_status"] == "completed"
    assert out["target_graph"] == "read_graph"
    assert out["execution_class"] == "read_only"
    assert out["allowed_tools"] == ["order_lookup"]


def test_route_intent_graph_routes_order_query(enabled):
    model = ToolCallingFakeModel(
        responses=[
            route_intent_json_message("order_query", 0.95),
            AIMessage(content="ORD-1 是已发货订单，金额 99 元。"),
        ]
    )
    graph = build_graph(
        model,
        tools=[kb_search, order_lookup, my_orders, shipment_query],
        checkpointer=MemorySaver(),
    )
    out = ainvoke_graph(graph,
        {
            "messages": [HumanMessage(content="帮我查订单 ORD-1")],
            "user_id": "u1",
            "session_id": "route-order-1",
            "context": {},
        },
        {"configurable": {"thread_id": "route-order-1"}},
    )
    assert out["route_intent"] == "order_query"
    assert out["target_graph"] == "read_graph"
    assert out["execution_class"] == "read_only"
    assert out["allowed_tools"] == ["order_lookup"]
    assert out["requires_approval"] is False
    assert out["execution_status"] == "completed"
    assert "capability" not in out
    assert "risk_level" not in out


def test_route_intent_graph_routes_unsupported_to_fallback(enabled):
    model = ToolCallingFakeModel(
        responses=[
            route_intent_json_message("unsupported_action", 0.93),
            AIMessage(content="当前系统暂不支持这个操作。"),
        ]
    )
    graph = build_graph(
        model,
        tools=[kb_search, order_lookup, my_orders, shipment_query],
        checkpointer=MemorySaver(),
    )
    out = ainvoke_graph(graph,
        {
            "messages": [HumanMessage(content="清空我的订单历史")],
            "user_id": "u1",
            "session_id": "route-unsupported-1",
            "context": {},
        },
        {"configurable": {"thread_id": "route-unsupported-1"}},
    )
    assert out["route_intent"] == "unsupported_action"
    assert out["target_graph"] == "fallback"
    assert out["execution_class"] == "read_only"
    assert out["allowed_tools"] == []
    assert out["execution_status"] == "completed"


def test_route_intent_graph_routes_high_risk_mutation_to_approval(enabled):
    model = ToolCallingFakeModel(
        responses=[
            route_intent_json_message("refund_apply_execute", 0.95),
            select_json_message("refund_apply", {"order_id": "ORD-PAID-1"}),
        ]
    )
    graph = build_graph(
        model,
        tools=[kb_search, order_lookup, my_orders, shipment_query],
        checkpointer=MemorySaver(),
    )
    out = ainvoke_graph(graph,
        {
            "messages": [HumanMessage(content="给 ORD-PAID-1 申请退款")],
            "user_id": "u1",
            "session_id": "route-high-risk-mutation-1",
            "context": {},
        },
        {"configurable": {"thread_id": "route-high-risk-mutation-1"}},
    )
    assert out["route_intent"] == "refund_apply_execute"
    assert out["target_graph"] == "high_risk_graph"
    assert out["execution_class"] == "high_risk"
    assert out["allowed_tools"] == ["refund_apply"]
    assert out["requires_approval"] is True
    assert out["__interrupt__"]
