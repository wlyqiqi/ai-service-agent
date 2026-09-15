import asyncio

from langchain_core.messages import AIMessage, HumanMessage

from ai_service_agent.graph.builder import build_graph
from ai_service_agent.harness.streaming import event_stream
from ai_service_agent.testing import ToolCallingFakeModel
from ai_service_agent.tools.kb import kb_search


def _collect(gen):
    return asyncio.run(_acollect(gen))


async def _acollect(gen):
    return [e async for e in gen]


def test_stream_emits_token_and_done():
    model = ToolCallingFakeModel(
        responses=[
            AIMessage(content='{"capability":"faq","risk_level":"read","confidence":0.9}'),
            AIMessage(content="FAQ 回答。"),
        ]
    )
    graph = build_graph(model, tools=[kb_search])
    events = _collect(event_stream(graph, config={"thread_id": "t1"}, messages=[HumanMessage(content="hi")]))
    types = [e["type"] for e in events]
    assert types[-1] == "done"
    assert "token" in types
    assert events[-1]["data"]["final"] == "FAQ 回答。"


def test_stream_emits_error_then_done():
    class Boom(Exception):
        pass

    def bad_model(*args, **kwargs):
        raise Boom("boom")

    graph = bad_model
    events = _collect(event_stream(graph, config={"thread_id": "t1"}, messages=[HumanMessage(content="hi")]))
    types = [e["type"] for e in events]
    assert types[-1] == "done"
    assert "error" in types
    assert events[-1]["data"]["final"] is None

from langgraph.checkpoint.memory import MemorySaver

from ai_service_agent.graph.builder import build_graph
from ai_service_agent.tools.kb import kb_search


def test_done_carries_message_id():
    model = ToolCallingFakeModel(
        responses=[
            AIMessage(content='{"capability":"faq","risk_level":"read","confidence":0.9}'),
            AIMessage(content="带 id 的回复。"),
        ]
    )
    graph = build_graph(model, tools=[kb_search], checkpointer=MemorySaver())
    events = _collect(event_stream(graph, {"configurable": {"thread_id": "t1"}}, [HumanMessage(content="hi")]))
    assert events[-1]["type"] == "done"
    assert events[-1]["data"]["final"] == "带 id 的回复。"
    assert events[-1]["data"]["message_id"]  # 非空 uuid
