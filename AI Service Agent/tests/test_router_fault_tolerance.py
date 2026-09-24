import time

import httpx
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from ai_service_agent.config import get_settings
from ai_service_agent.graph import router as router_mod
from ai_service_agent.graph.builder import build_graph
from ai_service_agent.testing import ToolCallingFakeModel, route_intent_json_message
from ai_service_agent.tools.kb import kb_search


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


def test_jev_error_retries_only_deepseek(monkeypatch):
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
        output = graph.invoke(
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
