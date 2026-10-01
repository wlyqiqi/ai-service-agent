"""Hybrid real-model verification of async timeout recovery.

These tests make a real HTTP request for the first matching boundary call,
then delay that response only at the test HTTP transport layer so LangGraph
times out. The second matching boundary call uses a deterministic recovery
payload because a real API cannot reliably respond within the tiny timeout.
For the router test, non-boundary downstream calls are also deterministic so
the assertion remains focused on router timeout behavior rather than network
latency.

Offline run:
    $env:E2E_MODEL="fake"; uv run --no-sync pytest tests/test_async_timeout_real.py -q

Real-model run:
    $env:E2E_MODEL="real"; uv run --no-sync pytest tests/test_async_timeout_real.py -vv
"""

import asyncio
import os
import time
import uuid

import httpx
import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_openai import ChatOpenAI
from pydantic import PrivateAttr

from ai_service_agent.config import get_settings
from ai_service_agent.graph.builder import build_graph
from ai_service_agent.graph.workflows.mutation_agent import make_mutation_graph
from ai_service_agent.tools import DEFAULT_TOOL_REGISTRY
from ai_service_agent.tools.kb import kb_search


pytestmark = pytest.mark.skipif(
    os.environ.get("E2E_MODEL") != "real",
    reason="real-model timeout verification runs only with E2E_MODEL=real",
)


class SlowRealModel(ChatOpenAI):
    """Real ARK model with deterministic recovery on its second boundary call."""

    _matching_calls: int = PrivateAttr(default=0)
    _boundary: str = PrivateAttr()
    _recovery_result: ChatResult | None = PrivateAttr(default=None)
    _downstream_result: ChatResult | None = PrivateAttr(default=None)

    def __init__(
        self,
        *,
        boundary: str,
        recovery_result: ChatResult | None = None,
        downstream_result: ChatResult | None = None,
        **kwargs,
    ):
        settings = get_settings()
        super().__init__(
            model=settings.model_name,
            api_key=settings.model_api_key,
            base_url=settings.model_endpoint,
            max_retries=0,
            **kwargs,
        )
        self._boundary = boundary
        self._recovery_result = recovery_result
        self._downstream_result = downstream_result

    def _matches_boundary(self, messages: list) -> bool:
        system_text = "\n".join(
            message.content if isinstance(message.content, str) else str(message.content)
            for message in messages
            if isinstance(message, BaseMessage) and getattr(message, "type", "") == "system"
        )
        return self._boundary in system_text

    async def _agenerate_with_cache(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        if self._matches_boundary(messages):
            self._matching_calls += 1
            if self._matching_calls >= 2 and self._recovery_result is not None:
                return self._recovery_result
        elif self._matching_calls >= 2 and self._downstream_result is not None:
            return self._downstream_result
        return await super()._agenerate_with_cache(
            messages, stop=stop, run_manager=run_manager, **kwargs
        )

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        if self._matches_boundary(messages):
            self._matching_calls += 1
        async for chunk in super()._astream(
            messages, stop=stop, run_manager=run_manager, **kwargs
        ):
            yield chunk


class FirstBoundaryDelayTransport(httpx.AsyncHTTPTransport):
    """Delay the response after forwarding the first real boundary request."""

    def __init__(self, *, boundary: str, delay_seconds: float):
        super().__init__()
        self._boundary = boundary
        self._delay_seconds = delay_seconds
        self.delayed_requests = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        request_body = request.content.decode("utf-8", errors="replace")
        should_delay = self._boundary in request_body
        if should_delay:
            self.delayed_requests += 1
        response = await super().handle_async_request(request)
        if should_delay:
            await asyncio.sleep(self._delay_seconds)
        return response


def _configure_model_environment(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setenv("FAKE_MODEL", "false")
    monkeypatch.setenv("JEV_ENABLED", "true")
    monkeypatch.setenv("TYPESAFE_API_KEY", settings.jev_api_key or "test-key")
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_real_router_timeout_retries_and_recovers(monkeypatch):
    from ai_service_agent.graph import router as router_mod

    def unavailable_jev(*args, **kwargs):
        raise httpx.ConnectError("JEV unavailable")

    class immediate_http_client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(router_mod.httpx, "Client", immediate_http_client)

    router_boundary = "router-timeout-boundary"
    route_prompt = router_boundary + "\n\n" + router_mod.ROUTE_INTENT_SYSTEM
    monkeypatch.setattr(router_mod, "_classify_route_intent_with_jev", unavailable_jev)
    monkeypatch.setattr(router_mod, "ROUTE_INTENT_SYSTEM", route_prompt)
    _configure_model_environment(monkeypatch)
    monkeypatch.setenv("ROUTER_NODE_TIMEOUT_SECONDS", "0.05")
    get_settings.cache_clear()
    thread_id = f"real-router-timeout-{uuid.uuid4()}"
    model = None
    transport = None

    try:
        recovery_result = ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content='{"route_intent":"faq","confidence":0.95}'
                    )
                )
            ]
        )
        downstream_result = ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="产品使用方法已整理完成。（来源：router-timeout-fixture）"
                    )
                )
            ]
        )
        transport = FirstBoundaryDelayTransport(
            boundary=router_boundary,
            delay_seconds=0.10,
        )
        model = SlowRealModel(
            boundary=router_boundary,
            recovery_result=recovery_result,
            downstream_result=downstream_result,
            http_async_client=httpx.AsyncClient(transport=transport),
        )
        graph = build_graph(
            model,
            tools=[kb_search],
            registry=DEFAULT_TOOL_REGISTRY,
        )
        started = time.perf_counter()
        output = await graph.ainvoke(
            {
                "messages": [HumanMessage(content="商品怎么使用？")],
                "user_id": "u1",
                "session_id": thread_id,
                "context": {},
            },
            {"configurable": {"thread_id": thread_id}},
        )
        elapsed = time.perf_counter() - started
    finally:
        if model is not None:
            await model.http_async_client.aclose()
        get_settings.cache_clear()

    assert transport.delayed_requests == 1
    assert model._matching_calls == 2
    assert output["execution_status"] == "completed"
    assert elapsed < 5.0


@pytest.mark.asyncio
async def test_real_select_timeout_retries_and_recovers(monkeypatch):
    _configure_model_environment(monkeypatch)
    monkeypatch.setenv("LLM_NODE_TIMEOUT_SECONDS", "0.05")
    get_settings.cache_clear()
    thread_id = f"real-select-timeout-{uuid.uuid4()}"
    model = None
    transport = None

    try:
        recovery_result = ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "update_address",
                                "args": {
                                    "order_id": "ORD-10086",
                                    "address": "北京市朝阳区",
                                },
                                "id": "select-recovery",
                                "type": "tool_call",
                            }
                        ],
                    )
                )
            ]
        )
        transport = FirstBoundaryDelayTransport(
            boundary="客服操作选择器",
            delay_seconds=0.10,
        )
        model = SlowRealModel(
            boundary="客服操作选择器",
            recovery_result=recovery_result,
            http_async_client=httpx.AsyncClient(transport=transport),
        )
        graph = make_mutation_graph(
            model,
            DEFAULT_TOOL_REGISTRY.for_risk_level("mutation"),
            DEFAULT_TOOL_REGISTRY,
        )
        output = await graph.ainvoke(
            {
                "messages": [
                    HumanMessage(content="订单10086的收货地址改成北京市朝阳区")
                ],
                "user_id": "u1",
                "session_id": thread_id,
                "context": {},
            },
            {"configurable": {"thread_id": thread_id}},
        )
    finally:
        if model is not None:
            await model.http_async_client.aclose()
        get_settings.cache_clear()

    assert transport.delayed_requests == 1
    assert model._matching_calls == 2
    assert output["context"]["pending_tool"]["tool"] == "update_address"
