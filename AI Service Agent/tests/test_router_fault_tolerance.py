import asyncio
import time

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.errors import NodeTimeoutError

from ai_service_agent.config import get_settings
from ai_service_agent.graph import router as router_mod
from ai_service_agent.graph.builder import build_graph
from ai_service_agent.prompts import ROUTE_INTENT_SYSTEM
from ai_service_agent.testing import ToolCallingFakeModel, route_intent_json_message
from ai_service_agent.tools.kb import kb_search
from ai_service_agent.graph.workflows.copy import RUNTIME_ERROR_TEXT


class FlakyDeepSeekModel(ToolCallingFakeModel):
    router_calls: int = 0

    def __init__(self):
        super().__init__(responses=[])

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        system = next(
            (m.content for m in messages if getattr(m, "type", "") == "system"),
            "",
        )
        if "Route Intent" in system or "路由" in system:
            self.router_calls += 1
            if self.router_calls == 1:
                raise ConnectionError("deepseek transient failure")
            return ChatResult(generations=[ChatGeneration(message=route_intent_json_message("faq", 0.9))])
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="这是 FAQ 的离线回复。"))])


class OnceRouteIntentModel(ToolCallingFakeModel):
    router_calls: int = 0

    def __init__(self):
        super().__init__(responses=[])
        self.router_calls = 0

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        system = next(
            (m.content for m in messages if getattr(m, "type", "") == "system"),
            "",
        )
        if system == ROUTE_INTENT_SYSTEM:
            self.router_calls += 1
            return ChatResult(
                generations=[ChatGeneration(message=route_intent_json_message("faq", 0.9))]
            )
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="这是 FAQ 的离线回复。"))]
        )


class SlowDeepSeekModel(ToolCallingFakeModel):
    router_calls: int = 0
    delay: float = 0.0

    def __init__(self, *, delay: float):
        super().__init__(responses=[])
        self.delay = delay

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        system = next(
            (m.content for m in messages if getattr(m, "type", "") == "system"),
            "",
        )
        is_router_call = system == ROUTE_INTENT_SYSTEM
        if is_router_call:
            self.router_calls += 1
        await asyncio.sleep(self.delay)
        if is_router_call:
            return ChatResult(
                generations=[ChatGeneration(message=route_intent_json_message("faq", 0.9))]
            )
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="这是 FAQ 的离线回复。"))]
        )


async def stream_timeout_graph(graph, input_data, thread_id):
    updates = []
    events = []
    errors = []
    started = time.monotonic()
    try:
        async for mode, payload in graph.astream(
            input_data,
            {"configurable": {"thread_id": thread_id}},
            stream_mode=["updates", "custom"],
        ):
            if mode == "updates":
                updates.append(payload)
            elif mode == "custom":
                events.append(payload)
    except NodeTimeoutError as error:
        errors.append(error)
    return updates, events, errors, time.monotonic() - started


def updates_by_node(updates):
    return {name: update for chunk in updates for name, update in chunk.items()}


@pytest.mark.asyncio
async def test_jev_error_retries_only_deepseek(monkeypatch):
    jev_calls = []

    def failing_jev(client, state, settings):
        jev_calls.append("jev")
        raise httpx.ConnectError("jev unavailable")

    monkeypatch.setattr(router_mod, "_classify_route_intent_with_jev", failing_jev)
    monkeypatch.setenv("JEV_ROUTE_INTENT_ENABLED", "true")
    monkeypatch.setenv("JEV_ENABLED", "true")
    monkeypatch.setenv("FAKE_MODEL", "false")
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setenv("JEV_TIMEOUT_SECONDS", "0.1")
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    get_settings.cache_clear()

    try:
        model = FlakyDeepSeekModel()
        graph = build_graph(model, tools=[kb_search])
        output = await graph.ainvoke(
            {
                "messages": [HumanMessage(content="商品怎么使用？")],
                "user_id": "u1",
                "session_id": "router-fault-1",
                "context": {},
            },
            {"configurable": {"thread_id": "router-fault-1"}},
        )
    finally:
        get_settings.cache_clear()

    assert jev_calls == ["jev"]
    assert model.router_calls == 2
    assert output["execution_status"] == "completed"
    assert "FAQ" in output["messages"][-1].content


@pytest.mark.asyncio
async def test_jev_node_timeout_falls_back_without_chain_retry(monkeypatch):
    jev_calls = []

    def slow_failing_jev(client, state, settings):
        jev_calls.append("jev")
        time.sleep(1.0)
        raise httpx.ConnectError("jev unavailable")

    monkeypatch.setattr(router_mod, "_classify_route_intent_with_jev", slow_failing_jev)
    monkeypatch.setenv("JEV_ROUTE_INTENT_ENABLED", "true")
    monkeypatch.setenv("JEV_ENABLED", "true")
    monkeypatch.setenv("FAKE_MODEL", "false")
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setenv("JEV_NODE_TIMEOUT_SECONDS", "0.50")
    monkeypatch.setenv("JEV_TIMEOUT_SECONDS", "1.20")
    monkeypatch.setenv("ROUTER_NODE_TIMEOUT_SECONDS", "2.0")
    get_settings.cache_clear()

    try:
        model = SlowDeepSeekModel(delay=0.10)
        graph = build_graph(model, tools=[kb_search])
        elapsed = 0.0
        for invocation in range(2):
            updates, events, errors, elapsed = await stream_timeout_graph(
                graph,
                {
                    "messages": [HumanMessage(content="商品怎么使用？")],
                    "user_id": "u1",
                    "session_id": f"router-jev-timeout-{invocation}",
                    "context": {},
                },
                f"router-jev-timeout-{invocation}",
            )
            nodes = updates_by_node(updates)
            assert nodes["__error_handler__router_jev"] == {
                "execution_status": "routing",
                "routing_backend": "jev",
                "routing_fallback": True,
                "route_control": "deepseek_fallback",
            }
            assert nodes["router_deepseek"]["routing_fallback"] is True
            assert nodes["final"]["execution_status"] == "completed"
            assert len(errors) == 1
            assert errors[0].node == "router_jev"
            assert events == []
    finally:
        get_settings.cache_clear()

    assert jev_calls == ["jev", "jev"]
    assert model.router_calls == 2
    assert elapsed < 5.0


@pytest.mark.asyncio
async def test_deepseek_node_timeout_retries_only_router(monkeypatch):
    monkeypatch.setenv("JEV_ROUTE_INTENT_ENABLED", "true")
    monkeypatch.setenv("JEV_ENABLED", "false")
    monkeypatch.setenv("ROUTER_NODE_TIMEOUT_SECONDS", "0.05")
    get_settings.cache_clear()

    try:
        model = SlowDeepSeekModel(delay=0.10)
        graph = build_graph(model, tools=[kb_search])
        elapsed = 0.0
        for invocation in range(2):
            updates, events, errors, elapsed = await stream_timeout_graph(
                graph,
                {
                    "messages": [HumanMessage(content="商品怎么使用？")],
                    "user_id": "u1",
                    "session_id": f"router-deepseek-timeout-{invocation}",
                    "context": {},
                },
                f"router-deepseek-timeout-{invocation}",
            )
            nodes = updates_by_node(updates)
            assert nodes["__error_handler__router_deepseek"] == {
                "execution_status": "failed"
            }
            assert events == [{
                "type": "error",
                "data": {"code": "runtime_error", "message": RUNTIME_ERROR_TEXT},
            }]
            assert len(errors) == 1
            assert errors[0].node == "router_deepseek"
    finally:
        get_settings.cache_clear()

    assert model.router_calls == 4
    assert elapsed < 1.0


@pytest.mark.asyncio
async def test_non_jev_route_intent_invokes_deepseek_once(monkeypatch):
    monkeypatch.setenv("JEV_ROUTE_INTENT_ENABLED", "true")
    monkeypatch.setenv("JEV_ENABLED", "false")
    get_settings.cache_clear()
    try:
        model = OnceRouteIntentModel()
        graph = build_graph(model, tools=[kb_search])
        output = await graph.ainvoke(
            {
                "messages": [HumanMessage(content="商品怎么使用？")],
                "user_id": "u1",
                "session_id": "route-intent-once-1",
                "context": {},
            },
            {"configurable": {"thread_id": "route-intent-once-1"}},
        )
    finally:
        get_settings.cache_clear()

    assert model.router_calls == 1
    assert output["route_control"] == "policy"
    assert output["target_graph"] == "read_graph"
    assert output["execution_class"] == "read_only"
    assert output["execution_status"] == "completed"
