import inspect

from langchain_core.messages import AIMessage, HumanMessage
from unittest.mock import AsyncMock, MagicMock
from langgraph.types import Command
from langgraph.types import TimeoutPolicy

from ai_service_agent.graph.workflows.fallback import check_handoff, make_fallback_graph
from ai_service_agent.config import get_settings
from ai_service_agent.testing import ToolCallingFakeModel


def test_check_handoff_ignores_normal_input():
    out = check_handoff({"messages": [], "context": {}, "user_id": "u1", "session_id": "s1"})
    assert out == {"handoff": False, "execution_status": "completed", "context": {"fallback_count": 1}}


def test_check_handoff_detects_keyword():
    out = check_handoff(
        {"messages": [HumanMessage(content="我要转人工")], "context": {}, "user_id": "u1", "session_id": "s1"}
    )
    assert isinstance(out, Command)
    assert out.update["handoff"] is True
    assert out.update["execution_status"] == "waiting_handoff"
    assert out.goto == "pause"


def test_fallback_graph_interrupts_on_handoff():
    from conftest import ainvoke_graph

    model = ToolCallingFakeModel(responses=[])
    graph = make_fallback_graph(model)
    out = ainvoke_graph(
        graph,
        {"messages": [HumanMessage(content="我要转人工")], "user_id": "u1", "session_id": "s1", "context": {}, "handoff": False}
    )
    assert out["execution_status"] == "waiting_handoff"
    assert out["handoff"] is True
    interrupt_values = [it.value for it in out.get("__interrupt__", [])]
    assert any(v.get("type") == "handoff_request" for v in interrupt_values)


def test_fallback_explain_llm_receives_policy_context(monkeypatch):
    from ai_service_agent.testing import ToolCallingFakeModel

    captured = {}

    class CaptureModel(ToolCallingFakeModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            captured["messages"] = list(messages)
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    model = CaptureModel(responses=[AIMessage(content="兜底回复")])

    from ai_service_agent.graph.workflows.fallback import make_fallback_graph

    graph = make_fallback_graph(model)
    state = {
        "messages": [
            HumanMessage(content="第零轮"),
            AIMessage(content="第零轮回复"),
            HumanMessage(content="第一轮"),
            AIMessage(content="第一轮回复"),
            HumanMessage(content="第二轮"),
            AIMessage(content="第二轮回复"),
            HumanMessage(content="随便说说"),
        ],
        "context": {},
        "user_id": "u1",
        "session_id": "s1",
        "handoff": False,
    }
    import asyncio
    asyncio.run(graph.ainvoke(state))

    human_texts = [message.content for message in captured["messages"] if message.type == "human"]
    assert human_texts[-1] == "随便说说"
    assert len(human_texts) <= 3


def test_fallback_non_handoff_routes_to_explain_llm():
    """fallback explain 由 LLM 生成（FALLBACK_SYSTEM_PROMPT），不再是确定性固定文案。"""
    from conftest import ainvoke_graph

    graph = make_fallback_graph(ToolCallingFakeModel(
        responses=[AIMessage(content="抱歉，暂时无法直接处理，您可以换个说法，或让我为您转接人工客服。")]
    ))
    node_spec = graph.nodes["explain_llm"]
    assert inspect.iscoroutinefunction(node_spec.bound.afunc)
    assert node_spec.timeout == TimeoutPolicy(run_timeout=get_settings().llm_node_timeout_seconds)
    out = ainvoke_graph(graph, {"messages": [HumanMessage(content="随便说说")], "context": {}, "user_id": "u1", "session_id": "s1", "handoff": False})
    assert out["execution_status"] == "completed"
    assert out["messages"][-1].content == "抱歉，暂时无法直接处理，您可以换个说法，或让我为您转接人工客服。"


def test_fallback_allows_five_turns_then_triggers_handoff():
    """同一 thread 允许 5 次 fallback 回复；第 6 次 fallback 必须确定性转人工。"""
    from conftest import ainvoke_graph

    graph = make_fallback_graph(ToolCallingFakeModel(
        responses=[AIMessage(content=f"兜底回复 {index}") for index in range(1, 6)]
    ))
    context = {}
    for index in range(1, 6):
        out = ainvoke_graph(
                graph,
                {"messages": [HumanMessage(content=f"第 {index} 次")], "context": context, "handoff": False}
        )
        assert out["execution_status"] == "completed"
        assert out["context"]["fallback_count"] == index
        context = out["context"]

    sixth = ainvoke_graph(
        graph,
        {"messages": [HumanMessage(content="第 6 次")], "context": context, "handoff": False}
    )
    interrupt_values = [item.value for item in sixth.get("__interrupt__", [])]
    assert sixth["execution_status"] == "waiting_handoff"
    assert sixth["handoff"] is True
    assert any(item.get("reason") == "fallback limit exceeded" for item in interrupt_values)
    assert sixth["context"]["fallback_count"] == 0


def test_fallback_handoff_path_generates_no_in_graph_text():
    from conftest import ainvoke_graph

    graph = make_fallback_graph(ToolCallingFakeModel(responses=[]))
    out = ainvoke_graph(graph, {"messages": [HumanMessage(content="我要转人工")], "context": {}, "user_id": "u1", "session_id": "s1", "handoff": False})
    assert out["execution_status"] == "waiting_handoff"
    assert [m for m in out.get("messages", []) if getattr(m, "type", "") == "ai"] == []


def test_fallback_failure_uses_local_fallback_failed(monkeypatch):
    from conftest import ainvoke_graph
    from unittest.mock import AsyncMock

    from ai_service_agent.graph.workflows.copy import RUNTIME_ERROR_TEXT

    model = ToolCallingFakeModel(responses=[])
    failing_agent = MagicMock()
    failing_agent.ainvoke = AsyncMock(side_effect=RuntimeError("model unavailable"))
    monkeypatch.setattr(
        "ai_service_agent.graph.workflows.fallback.create_agent",
        lambda *args, **kwargs: failing_agent,
    )
    graph = make_fallback_graph(model)
    out = ainvoke_graph(
        graph,
        {"messages": [HumanMessage(content="随便说说")], "context": {}, "user_id": "u1", "session_id": "s1", "handoff": False},
    )
    assert out["execution_status"] == "failed"
    assert out["messages"][-1].content == RUNTIME_ERROR_TEXT


def test_check_handoff_reuses_shared_detector():
    out = check_handoff(
        {"messages": [HumanMessage(content="叫个真人来")], "context": {}, "user_id": "u1", "session_id": "s1"}
    )
    assert isinstance(out, Command)
    assert out.update["handoff"] is True
    assert out.goto == "pause"


def test_check_handoff_no_false_positive_on_topic():
    out = check_handoff(
        {"messages": [HumanMessage(content="人工客服培训需要多久")], "context": {}, "user_id": "u1", "session_id": "s1"}
    )
    assert out == {
        "handoff": False,
        "execution_status": "completed",
        "context": {"fallback_count": 1},
    }
