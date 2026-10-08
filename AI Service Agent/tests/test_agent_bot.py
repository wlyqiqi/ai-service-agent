import pytest
from unittest.mock import AsyncMock

from ai_service_agent.harness.agent_bot import handle_agent_bot_message


@pytest.mark.asyncio
async def test_handle_agent_bot_message_incoming_text():
    payload = {
        "event": "message_created",
        "id": "100",
        "content": "你好",
        "message_type": "incoming",
        "content_type": "text",
        "conversation": {"id": 42},
        "contact": {"id": 1, "name": "test-user"},
    }
    chatwoot = AsyncMock()

    result = await handle_agent_bot_message(payload, chatwoot)

    assert result is not None
    assert result["chatwoot_conversation_id"] == 42
    assert result["response"] == "[AgentBot] Received: 你好"
    chatwoot.send_bot_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_agent_bot_message_ignores_outgoing():
    payload = {
        "event": "message_created",
        "id": "101",
        "content": "AI reply",
        "message_type": "outgoing",
        "content_type": "text",
        "conversation": {"id": 42},
        "contact": {"id": 1, "name": "test-user"},
    }
    chatwoot = AsyncMock()

    result = await handle_agent_bot_message(payload, chatwoot)

    assert result is None
    chatwoot.send_bot_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_agent_bot_invokes_graph():
    from ai_service_agent.harness.graph import make_ai_graph
    from ai_service_agent.testing import make_fake_model

    payload = {
        "event": "message_created",
        "id": "102",
        "content": "你好",
        "message_type": "incoming",
        "content_type": "text",
        "conversation": {"id": 43},
        "contact": {"id": 2, "name": "test-user"},
    }
    chatwoot = AsyncMock()
    graph = make_ai_graph(make_fake_model(), None)

    result = await handle_agent_bot_message(payload, chatwoot, graph=graph)

    assert result is not None
    assert result["response"] is not None
    chatwoot.send_bot_message.assert_awaited_once()
