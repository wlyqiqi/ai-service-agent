import pytest

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
    chatwoot = pytest.importorskip("unittest.mock").AsyncMock()
    chatwoot.send_bot_message = pytest.importorskip("unittest.mock").AsyncMock()

    result = await handle_agent_bot_message(payload, chatwoot)

    assert result is not None
    chatwoot.send_bot_message.assert_awaited_once_with("42", "[AgentBot] Received: 你好")


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
    chatwoot = pytest.importorskip("unittest.mock").AsyncMock()

    result = await handle_agent_bot_message(payload, chatwoot)

    assert result is None
    chatwoot.send_bot_message.assert_not_awaited()
