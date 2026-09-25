import asyncio
import os
import time
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from ai_service_agent.config import get_settings
from ai_service_agent.graph.builder import build_graph
from ai_service_agent.testing import ToolCallingFakeModel, router_json_message
from ai_service_agent.tools.kb import kb_search


class ConcurrentFakeModel(ToolCallingFakeModel):
    """并发离线模型：路由请求返回合法 JSON，其余调用返回固定回复。"""

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        router_prompt = any("客服意图路由器" in str(message.content) for message in messages)
        message = (
            router_json_message("faq", "read", 0.95)
            if router_prompt
            else AIMessage(content="离线并发回复")
        )
        return ChatResult(generations=[ChatGeneration(message=message)])


@pytest.fixture(autouse=True)
def offline_settings(monkeypatch):
    monkeypatch.setenv("JEV_ROUTE_INTENT_ENABLED", "false")
    monkeypatch.setenv("JEV_ENABLED", "false")
    get_settings.cache_clear()
    try:
        get_settings()
        yield
    finally:
        get_settings.cache_clear()


def _concurrency_users() -> int:
    return int(os.getenv("ASYNC_CONCURRENCY_USERS", "100"))


def _invoke_payload(user_id: str, session_id: str) -> dict:
    return {
        "messages": [HumanMessage(content="这是一个离线并发测试问题")],
        "user_id": user_id,
        "session_id": session_id,
        "context": {},
    }


@pytest.mark.asyncio
async def test_root_graph_supports_100_concurrent_users():
    user_count = _concurrency_users()
    graph = build_graph(ConcurrentFakeModel(responses=[]), tools=[kb_search])

    async def invoke(index: int):
        session_id = f"async-concurrency-{uuid4()}-{index}"
        thread_id = f"thread-{session_id}"
        return await graph.ainvoke(
            _invoke_payload(f"user-{session_id}", session_id),
            {"configurable": {"thread_id": thread_id}},
        )

    started = time.perf_counter()
    results = await asyncio.gather(*(invoke(index) for index in range(user_count)))
    elapsed = time.perf_counter() - started

    assert len(results) == user_count
    assert all(result["execution_status"] == "completed" for result in results)
    assert elapsed < 10.0


@pytest.mark.asyncio
async def test_async_timeout_does_not_block_event_loop():
    invocation_count = 20
    graph = build_graph(ConcurrentFakeModel(responses=[]), tools=[kb_search])
    loop_lags = []

    async def heartbeat():
        for _ in range(10):
            started = time.perf_counter()
            await asyncio.sleep(0.01)
            loop_lags.append(time.perf_counter() - started - 0.01)

    heartbeat_task = asyncio.create_task(heartbeat())

    async def invoke(index: int):
        session_id = f"async-heartbeat-{uuid4()}-{index}"
        thread_id = f"thread-{session_id}"
        return await graph.ainvoke(
            _invoke_payload(f"user-{session_id}", session_id),
            {"configurable": {"thread_id": thread_id}},
        )

    results = await asyncio.gather(*(invoke(index) for index in range(invocation_count)))
    await heartbeat_task

    assert len(loop_lags) == 10
    assert len(results) == invocation_count
    assert all(result["execution_status"] == "completed" for result in results)
    assert max(loop_lags) < 0.5
