import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import sqlite3

from ai_service_agent.harness.agent_bot import handle_agent_bot_message
from ai_service_agent.harness.messages import MessageStore


@pytest.mark.asyncio
async def test_handle_agent_bot_message_incoming_text():
    payload = {
        "event": "message_created",
        "id": "100",
        "content": "你好",
        "message_type": "incoming",
        "content_type": "text",
        "conversation": {"id": 42},
        "contact": {"id": 1, "identifier": "u-test", "name": "test-user"},
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
        "contact": {"id": 2, "identifier": "u-graph", "name": "test-user"},
    }
    chatwoot = AsyncMock()
    graph = make_ai_graph(make_fake_model(), None)

    result = await handle_agent_bot_message(payload, chatwoot, graph=graph)

    assert result is not None
    assert result["response"] is not None
    chatwoot.send_bot_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_agent_bot_forwards_runtime_context(monkeypatch):
    """GraphContext must reach event_stream (PR #33 wiring must not stop at the public API)."""
    captured: dict = {}

    async def fake_event_stream(*_args, **kwargs):
        captured["context"] = kwargs.get("context")
        yield {"type": "done", "data": {"final": "ctx-ok", "message_id": "m-ctx"}}

    monkeypatch.setattr(
        "ai_service_agent.harness.agent_bot.event_stream", fake_event_stream
    )
    payload = {
        "event": "message_created",
        "id": "102-ctx",
        "content": "你好",
        "message_type": "incoming",
        "content_type": "text",
        "conversation": {"id": 44},
        "contact": {"id": 3, "identifier": "u-ctx", "name": "ctx-user"},
    }
    chatwoot = AsyncMock()
    graph = SimpleNamespace(checkpointer=None)
    sentinel = MagicMock(name="GraphContext")

    result = await handle_agent_bot_message(
        payload, chatwoot, graph=graph, context=sentinel
    )

    assert result is not None
    assert result["response"] == "ctx-ok"
    assert captured["context"] is sentinel


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


@pytest.mark.asyncio
async def test_message_updated_submitted_values_runs_one_turn():
    payload = {
        "event": "message_updated",
        "id": "103",
        "content": "ORD-1\n新地址",
        "message_type": "incoming",
        "content_type": "form",
        "content_attributes": {
            "submitted_values": {"order_id": "ORD-1", "address": "新地址"}
        },
        "conversation": {"id": 44},
        "contact": {"id": 3, "identifier": "u0100", "name": "郑军"},
    }
    chatwoot = AsyncMock()

    result = await handle_agent_bot_message(payload, chatwoot)

    assert result is not None
    assert result["response"] == "[AgentBot] 收到表单：订单 ORD-1 的收货地址改成 新地址"
    chatwoot.send_bot_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_message_updated_widget_array_values_formats_address_turn():
    payload = {
        "event": "message_updated",
        "id": "105",
        "content": "ORD-1 新地址",
        "message_type": "incoming",
        "content_type": "form",
        "content_attributes": {
            "submitted_values": [
                {"name": "order_id", "value": "ORD-1"},
                {"name": "address", "value": "新地址"},
            ]
        },
        "conversation": {"id": 46},
        "contact": {"id": 3, "identifier": "u0100", "name": "郑军"},
    }
    chatwoot = AsyncMock()

    result = await handle_agent_bot_message(payload, chatwoot)

    assert result is not None
    assert result["response"] == "[AgentBot] 收到表单：订单 ORD-1 的收货地址改成 新地址"
    chatwoot.send_bot_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_message_updated_outgoing_form_array_runs_one_turn():
    payload = {
        "event": "message_updated",
        "id": "106",
        "content": "ORD-1 新地址",
        "message_type": 1,
        "content_type": "form",
        "content_attributes": {
            "submitted_values": [
                {"name": "order_id", "value": "ORD-1"},
                {"name": "address", "value": "新地址"},
            ]
        },
        "conversation": {"id": 47},
        "contact": {"id": 3, "identifier": "u0100", "name": "郑军"},
    }
    chatwoot = AsyncMock()

    result = await handle_agent_bot_message(payload, chatwoot)

    assert result is not None
    assert result["response"] == "[AgentBot] 收到表单：订单 ORD-1 的收货地址改成 新地址"
    chatwoot.send_bot_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_agent_bot_deduplicates_chatwoot_message_id(tmp_path):
    payload = {
        "event": "message_created",
        "id": "900",
        "content": "你好",
        "message_type": "incoming",
        "content_type": "text",
        "conversation": {"id": 48},
        "contact": {"id": 4, "identifier": "u-repeat", "name": "repeat-user"},
    }
    chatwoot = AsyncMock()
    store = MessageStore(sqlite3.connect(tmp_path / "messages.db"))

    first = await handle_agent_bot_message(payload, chatwoot, messages_store=store)
    second = await handle_agent_bot_message(payload, chatwoot, messages_store=store)

    assert first is not None
    assert second is None
    chatwoot.send_bot_message.assert_awaited_once()
    rows = store.list(first["conversation_id"])
    assert [row["role"] for row in rows] == ["user", "assistant"]
    assert rows[0]["message_id"] == "chatwoot-900"


@pytest.mark.asyncio
async def test_agent_bot_serializes_same_chatwoot_conversation(tmp_path):
    import asyncio

    from ai_service_agent.harness.chatwoot import ChatwootMappingStore
    from ai_service_agent.harness.session import ConversationStatusStore

    db = str(tmp_path / "race.db")
    conn = sqlite3.connect(db, check_same_thread=False)
    status = ConversationStatusStore(conn)
    mapping = ChatwootMappingStore(conn)
    messages = MessageStore(conn)
    chatwoot = AsyncMock()
    locks = {}

    async def lock_factory(key):
        return locks.setdefault(key, asyncio.Lock())

    async def make_payload(message_id):
        return {
            "event": "message_created",
            "id": message_id,
            "content": f"message-{message_id}",
            "message_type": "incoming",
            "content_type": "text",
            "conversation": {"id": 777},
            "contact": {"id": 8, "identifier": "u1"},
        }

    results = await asyncio.gather(
        handle_agent_bot_message(
            await make_payload("901"), chatwoot, status=status, mapping=mapping,
            messages_store=messages, chatwoot_lock_factory=lock_factory, thread_lock_factory=lock_factory,
        ),
        handle_agent_bot_message(
            await make_payload("902"), chatwoot, status=status, mapping=mapping,
            messages_store=messages, chatwoot_lock_factory=lock_factory, thread_lock_factory=lock_factory,
        ),
    )

    assert all(result is not None for result in results)
    assert results[0]["conversation_id"] == results[1]["conversation_id"]
    session_count = conn.execute(
        "SELECT COUNT(*) FROM conversation_status WHERE user_id=?", ("u1",)
    ).fetchone()[0]
    assert session_count == 1
    assert len(messages.list(results[0]["conversation_id"])) == 4


@pytest.mark.asyncio
async def test_message_updated_submitted_values_formats_address_turn():
    payload = {
        "event": "message_updated",
        "id": "104",
        "content": "ORD-1\n新地址",
        "message_type": "incoming",
        "content_type": "form",
        "content_attributes": {
            "submitted_values": {"order_id": "ORD-1", "address": "新地址"}
        },
        "conversation": {"id": 45},
        "contact": {"id": 3, "identifier": "u0100", "name": "郑军"},
    }
    chatwoot = AsyncMock()

    result = await handle_agent_bot_message(payload, chatwoot)

    assert result is not None
    assert result["response"] == "[AgentBot] 收到表单：订单 ORD-1 的收货地址改成 新地址"


def _contact_first_stores(tmp_path):
    from ai_service_agent.harness.chatwoot import ChatwootMappingStore
    from ai_service_agent.harness.session import ConversationStatusStore

    conn = sqlite3.connect(tmp_path / "contact_first.db")
    return ConversationStatusStore(conn), ChatwootMappingStore(conn), conn


def _incoming_text_payload(*, message_id: str, cw_conv_id: int, contact: dict, content: str = "你好"):
    return {
        "event": "message_created",
        "id": message_id,
        "content": content,
        "message_type": "incoming",
        "content_type": "text",
        "conversation": {"id": cw_conv_id},
        "contact": contact,
    }


@pytest.mark.asyncio
async def test_contact_first_rejects_missing_identifier(tmp_path):
    """v1 Contact-first：无 identifier 不得匿名建 Runtime 会话 / Chatwoot mapping。"""
    status, mapping, conn = _contact_first_stores(tmp_path)
    chatwoot = AsyncMock()
    payload = _incoming_text_payload(
        message_id="cf-missing",
        cw_conv_id=9001,
        contact={"id": 1, "name": "anon-widget"},
    )

    result = await handle_agent_bot_message(
        payload, chatwoot, status=status, mapping=mapping
    )

    assert result is None
    assert mapping.find(9001) is None
    chatwoot.send_bot_message.assert_not_awaited()
    assert conn.execute("SELECT COUNT(*) FROM conversation_status").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_contact_first_rejects_blank_identifier(tmp_path):
    status, mapping, conn = _contact_first_stores(tmp_path)
    chatwoot = AsyncMock()
    payload = _incoming_text_payload(
        message_id="cf-blank",
        cw_conv_id=9002,
        contact={"id": 2, "identifier": "   ", "name": "blank"},
    )

    result = await handle_agent_bot_message(
        payload, chatwoot, status=status, mapping=mapping
    )

    assert result is None
    assert mapping.find(9002) is None
    assert conn.execute("SELECT COUNT(*) FROM conversation_status").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_contact_first_binding_establishes_mapping_on_first_message(tmp_path):
    """首条带 validated identifier 的 AgentBot 消息懒建 conversation_id ↔ Chatwoot mapping。"""
    status, mapping, _conn = _contact_first_stores(tmp_path)
    chatwoot = AsyncMock()
    payload = _incoming_text_payload(
        message_id="cf-first",
        cw_conv_id=9100,
        contact={"id": 10, "identifier": "u0100", "name": "郑军"},
    )

    result = await handle_agent_bot_message(
        payload, chatwoot, status=status, mapping=mapping
    )

    assert result is not None
    assert result["chatwoot_conversation_id"] == 9100
    rec = mapping.find(9100)
    assert rec is not None
    assert rec["conversation_id"] == result["conversation_id"]
    assert status.get_user_id(result["conversation_id"]) == "u0100"
    assert status.get_thread_id(result["conversation_id"]) is not None
    chatwoot.send_bot_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_contact_first_binding_reuses_runtime_conversation(tmp_path):
    status, mapping, conn = _contact_first_stores(tmp_path)
    chatwoot = AsyncMock()
    contact = {"id": 10, "identifier": "u0100", "name": "郑军"}

    first = await handle_agent_bot_message(
        _incoming_text_payload(message_id="cf-r1", cw_conv_id=9200, contact=contact),
        chatwoot,
        status=status,
        mapping=mapping,
    )
    second = await handle_agent_bot_message(
        _incoming_text_payload(
            message_id="cf-r2",
            cw_conv_id=9200,
            contact=contact,
            content="第二句",
        ),
        chatwoot,
        status=status,
        mapping=mapping,
    )

    assert first is not None and second is not None
    assert first["conversation_id"] == second["conversation_id"]
    thread_id = status.get_thread_id(first["conversation_id"])
    assert thread_id is not None
    assert status.get_thread_id(second["conversation_id"]) == thread_id
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM conversation_status WHERE user_id=?", ("u0100",)
        ).fetchone()[0]
        == 1
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM chatwoot_conversation WHERE chatwoot_conversation_id=?",
            (9200,),
        ).fetchone()[0]
        == 1
    )


@pytest.mark.asyncio
async def test_contact_first_ai_mode_plain_text_returns_bot_text(tmp_path):
    """AI 模式普通文本 turn：Fake Graph + MemorySaver → mock Chatwoot 出站 bot 文本。"""
    from langgraph.checkpoint.memory import MemorySaver

    from ai_service_agent.harness.graph import make_ai_graph
    from ai_service_agent.testing import make_fake_model

    status, mapping, _conn = _contact_first_stores(tmp_path)
    chatwoot = AsyncMock()
    graph = make_ai_graph(make_fake_model(), MemorySaver())
    payload = _incoming_text_payload(
        message_id="cf-ai",
        cw_conv_id=9300,
        contact={"id": 11, "identifier": "u0200", "name": "reader"},
        content="退货政策是什么",
    )

    result = await handle_agent_bot_message(
        payload, chatwoot, graph=graph, status=status, mapping=mapping
    )

    assert result is not None
    assert result["response"] == "这是 FAQ 的离线回复（fake model）。"
    assert mapping.find(9300)["conversation_id"] == result["conversation_id"]
    chatwoot.send_bot_message.assert_awaited_once()
    assert chatwoot.send_bot_message.await_args.args[1] == result["response"]
