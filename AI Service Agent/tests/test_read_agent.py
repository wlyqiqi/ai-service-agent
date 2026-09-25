import inspect

from unittest.mock import MagicMock
from unittest.mock import AsyncMock

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.types import TimeoutPolicy

from ai_service_agent.graph.agents import read_agent as read_agent_mod
from ai_service_agent.graph.agents.read_agent import make_read_graph
from ai_service_agent.graph.workflows.copy import RUNTIME_ERROR_TEXT
from ai_service_agent.config import get_settings
from ai_service_agent.testing import ToolCallingFakeModel
from ai_service_agent.tools.kb import kb_search


class LoopingToolModel(ToolCallingFakeModel):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        message = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "kb_search",
                    "args": {"text": "warranty", "request_id": "loop"},
                    "id": f"call-{len(messages)}",
                    "type": "tool_call",
                }
            ],
        )
        return ChatResult(generations=[ChatGeneration(message=message)])


def test_read_graph_async_node_and_timeout_policy():
    model = ToolCallingFakeModel(responses=[AIMessage(content="查询结果：订单已发货。")])
    graph = make_read_graph(model, tools=[kb_search])
    node_spec = graph.nodes["agent_llm"]
    assert inspect.iscoroutinefunction(node_spec.bound.afunc)
    assert node_spec.timeout == TimeoutPolicy(run_timeout=get_settings().llm_node_timeout_seconds)


def test_read_graph_answers_and_completes():
    from conftest import ainvoke_graph

    model = ToolCallingFakeModel(responses=[AIMessage(content="查询结果：订单已发货。")])
    graph = make_read_graph(model, tools=[kb_search])
    out = ainvoke_graph(graph, {"messages": [], "user_id": "u1", "session_id": "s1", "context": {}})
    assert out["execution_status"] == "completed"
    assert "发货" in out["messages"][-1].content


def test_read_graph_failure_uses_local_finalize_failed(monkeypatch):
    failing_agent = MagicMock()
    failing_agent.ainvoke = AsyncMock(side_effect=RuntimeError("model unavailable"))
    monkeypatch.setattr(read_agent_mod, "create_agent", lambda *args, **kwargs: failing_agent)

    graph = make_read_graph(ToolCallingFakeModel(responses=[]), [kb_search])
    from conftest import ainvoke_graph

    output = ainvoke_graph(
        graph,
        {
            "messages": [HumanMessage(content="查订单 ORD-1")],
            "user_id": "u1",
            "session_id": "read-failure-1",
            "context": {},
        },
        {"configurable": {"thread_id": "read-failure-1"}},
    )

    assert output["execution_status"] == "failed"
    assert output["messages"][-1].content == RUNTIME_ERROR_TEXT


def test_read_graph_limits_model_calls_to_three():
    from ai_service_agent.graph.agents.read_agent import READ_MODEL_CALL_LIMIT

    graph = make_read_graph(LoopingToolModel(responses=[]), tools=[kb_search])
    from conftest import ainvoke_graph

    out = ainvoke_graph(
        graph,
        {"messages": [], "user_id": "u1", "session_id": "s-loop", "context": {}},
        config={"configurable": {"thread_id": "s-loop"}},
    )
    tool_calls = [
        message
        for message in out["messages"]
        if getattr(message, "tool_calls", None)
    ]
    assert len(tool_calls) == READ_MODEL_CALL_LIMIT
    assert out["execution_status"] == "completed"
    assert "Model call limits exceeded" in out["messages"][-1].content


def test_read_system_prompt_requires_source_citation():
    from ai_service_agent.prompts import READ_SYSTEM_PROMPT
    assert "来源" in READ_SYSTEM_PROMPT
    assert "（来源：" in READ_SYSTEM_PROMPT
    assert "未查询到" in READ_SYSTEM_PROMPT


def test_read_system_prompt_friendly_no_result_tone():
    """知识缺口回复要求专业客服口吻：如实告知 + 表达歉意/引导替代帮助，而非机械复述。"""
    from ai_service_agent.prompts import READ_SYSTEM_PROMPT
    assert "如实告知" in READ_SYSTEM_PROMPT
    assert "友好" in READ_SYSTEM_PROMPT
    assert "人工客服" in READ_SYSTEM_PROMPT
    assert "引导" in READ_SYSTEM_PROMPT
    assert "换个说法" in READ_SYSTEM_PROMPT
