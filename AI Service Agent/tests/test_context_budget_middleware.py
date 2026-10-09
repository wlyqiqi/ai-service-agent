from langchain.agents.middleware import ModelRequest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from ai_service_agent.config import get_settings
from ai_service_agent.graph.context.middleware import ContextBudgetMiddleware
from ai_service_agent.graph.context.policy import make_read_policy


def _make_request(messages):
    return ModelRequest(
        model=FakeMessagesListChatModel(responses=[]),
        messages=messages,
        system_message=SystemMessage(content="system"),
        tools=[],
        state={},
    )


def test_middleware_excludes_prior_tool_chain_but_keeps_current_turn():
    messages = [
        HumanMessage(content="old question"),
        AIMessage(content="old answer"),
        HumanMessage(content="search now"),
        AIMessage(
            content="",
            tool_calls=[{"name": "kb_search", "args": {}, "id": "call-1", "type": "tool_call"}],
        ),
        ToolMessage(content="evidence", tool_call_id="call-1"),
    ]
    request = _make_request(messages)
    middleware = ContextBudgetMiddleware(make_read_policy(get_settings()))

    prepared = middleware._prepare(request)

    assert [message.content for message in prepared.messages] == [
        "old question",
        "old answer",
        "search now",
        "",
        "evidence",
    ]


def test_middleware_counts_system_prompt_against_total_budget():
    messages = [
        HumanMessage(content="one" * 100),
        AIMessage(content="two" * 100),
        HumanMessage(content="three" * 100),
    ]
    request = _make_request(messages)

    class SmallPolicy:
        name = "test"
        max_total_tokens = 10

        def build_messages(self, messages):
            return messages

    middleware = ContextBudgetMiddleware(SmallPolicy())
    prepared = middleware._prepare(request)

    assert len(prepared.messages) < len(messages)
