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
    events = _collect(event_stream(graph, config={"thread_id": "t1"}, message=HumanMessage(content="hi")))
    types = [e["type"] for e in events]
    assert types[-1] == "done"
    assert "token" in types


def test_stream_emits_error_then_done():
    class Boom(Exception):
        pass

    def bad_model(*args, **kwargs):
        raise Boom("boom")

    graph = bad_model
    events = _collect(event_stream(graph, config={"thread_id": "t1"}, message=HumanMessage(content="hi")))
    types = [e["type"] for e in events]
    assert types[-1] == "done"
    assert "error" in types
