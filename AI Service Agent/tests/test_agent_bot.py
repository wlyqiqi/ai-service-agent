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


@pytest.mark.asyncio
async def test_send_clarify_form_address():
    from ai_service_agent.harness.agent_bot import send_clarify_form

    chatwoot = AsyncMock()

    await send_clarify_form(
        chatwoot,
        "c1",
        ["order_id", "address"],
        profile={"address": "No.119 langGraph street", "phone": "+65-89136375"},
        orders=[
            {"order_id": "ORD-1", "status_display": "已付款"},
            {"order_id": "ORD-2", "status_display": "已付款"},
        ],
    )

    chatwoot.send_interactive_message.assert_awaited_once()
    args = chatwoot.send_interactive_message.call_args
    assert args.args[1] == "修改收货地址"
    assert args.args[2] == "form"
    attrs = args.args[3]
    assert len(attrs["items"]) >= 2
    select_field = next(item for item in attrs["items"] if item.get("type") == "select")
    assert len(select_field["options"]) == 2


@pytest.mark.asyncio
async def test_send_confirmation_uses_input_select():
    from ai_service_agent.harness.agent_bot import send_confirmation

    chatwoot = AsyncMock()
    summary = "您即将进行「收货地址修改」操作"

    await send_confirmation(chatwoot, "c1", summary)

    chatwoot.send_interactive_message.assert_awaited_once()
    args = chatwoot.send_interactive_message.call_args
    assert args.args[1] == summary
    assert args.args[2] == "input_select"
    assert args.args[3]["items"] == [
        {"title": "确认执行", "value": "confirmed"},
        {"title": "取消操作", "value": "canceled"},
    ]
