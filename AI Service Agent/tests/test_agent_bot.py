import pytest
from pathlib import Path
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


@pytest.mark.asyncio
async def test_form_required_projects_chatwoot_native_form_from_interrupt(monkeypatch, tmp_path):
    """#44: Form Interrupt → Chatwoot native form from interrupt payload (not legacy clarify-form).

    Seam: handle_agent_bot_message + Fake Graph events + mock Chatwoot.
    """
    status, mapping, _conn = _contact_first_stores(tmp_path)
    chatwoot = AsyncMock()
    form_data = {
        "tool": "update_address",
        "order_candidates": [
            {
                "order_id": "ORD-PAID-1",
                "product_summary": "测试商品 x1",
            },
            {
                "order_id": "ORD-PAID-2",
                "product_summary": "另一件 x1",
            },
        ],
        "form_session_id": "fs-agentbot-44",
        "summary": "您即将进行「收货地址修改」操作",
        "prefills": {"address": "北京市朝阳区预填路 1 号"},
        "response_schema": {
            "type": "object",
            "properties": {
                "action": {"enum": ["confirm", "cancel"]},
                "order_id": {"type": "string"},
                "address": {"type": "string"},
            },
        },
    }

    async def fake_event_stream(*_args, **_kwargs):
        yield {"type": "form_required", "data": form_data}
        yield {"type": "done", "data": {"final": None, "message_id": None}}

    monkeypatch.setattr(
        "ai_service_agent.harness.agent_bot.event_stream", fake_event_stream
    )
    profile_loader = MagicMock(side_effect=AssertionError("legacy profile_loader must not build Mutation Form"))
    orders_loader = MagicMock(side_effect=AssertionError("legacy orders_loader must not build Mutation Form"))

    result = await handle_agent_bot_message(
        _incoming_text_payload(
            message_id="form-open-1",
            cw_conv_id=9400,
            contact={"id": 20, "identifier": "u1", "name": "郑军"},
            content="我要改个地址",
        ),
        chatwoot,
        graph=SimpleNamespace(checkpointer=None),
        status=status,
        mapping=mapping,
        profile_loader=profile_loader,
        orders_loader=orders_loader,
    )

    assert result is not None
    chatwoot.send_interactive_message.assert_awaited_once()
    args = chatwoot.send_interactive_message.await_args.args
    assert args[1] == form_data["summary"]
    assert args[2] == "form"
    items = args[3]["items"]
    order_picker = next(item for item in items if item["name"] == "order_id")
    assert order_picker["type"] == "select"
    assert order_picker["options"] == [
        {"label": "ORD-PAID-1 · 测试商品 x1", "value": "ORD-PAID-1"},
        {"label": "ORD-PAID-2 · 另一件 x1", "value": "ORD-PAID-2"},
    ]
    address_field = next(item for item in items if item["name"] == "address")
    assert address_field["type"] == "text"
    assert address_field["default"] == "北京市朝阳区预填路 1 号"
    assert all(item["name"] != "request_id" for item in items)
    action_field = next(item for item in items if item["name"] == "action")
    assert action_field["type"] == "select"
    assert {"label": "确认", "value": "confirm"} in action_field["options"]
    assert {"label": "取消", "value": "cancel"} in action_field["options"]
    profile_loader.assert_not_called()
    orders_loader.assert_not_called()


def test_mutation_form_items_prefer_explicit_fields_over_response_schema():
    """#44 AC: Chatwoot items can be derived from interrupt ``fields`` + prefills."""
    from ai_service_agent.harness.agent_bot import mutation_form_items_from_form_data

    items = mutation_form_items_from_form_data(
        {
            "order_candidates": [{"order_id": "ORD-1", "product_summary": "鞋 x1"}],
            "prefills": {"note": "工作日配送"},
            "fields": [
                {"name": "note", "label": "订单备注", "type": "text"},
                {"name": "action", "type": "select", "options": [{"label": "确认", "value": "confirm"}]},
            ],
            "response_schema": {
                "properties": {
                    "address": {"type": "string"},
                    "action": {"enum": ["confirm", "cancel"]},
                },
            },
        }
    )

    assert [item["name"] for item in items] == ["order_id", "note", "action"]
    assert items[0]["options"] == [{"label": "ORD-1 · 鞋 x1", "value": "ORD-1"}]
    assert items[1]["default"] == "工作日配送"
    assert items[1]["label"] == "订单备注"
    assert items[2]["options"] == [
        {"label": "确认", "value": "confirm"},
        {"label": "取消", "value": "cancel"},
    ]


def test_mutation_form_projects_form_rejected_errors_into_chatwoot_copy():
    """#47: form_rejected errors appear in Chatwoot title/items (no native error field)."""
    from ai_service_agent.harness.agent_bot import (
        mutation_form_items_from_form_data,
        mutation_form_title_from_form_data,
    )

    data = {
        "summary": "您即将进行「收货地址修改」操作",
        "order_candidates": [{"order_id": "ORD-PAID-1", "product_summary": "鞋 x1"}],
        "errors": {
            "order_id": "抱歉，该订单已取消，无需修改地址。",
            "address": "请填写有效地址",
        },
        "response_schema": {
            "properties": {
                "order_id": {"type": "string"},
                "address": {"type": "string"},
                "action": {"enum": ["confirm", "cancel"]},
            },
        },
    }
    title = mutation_form_title_from_form_data(data)
    items = mutation_form_items_from_form_data(data)
    assert "抱歉，该订单已取消，无需修改地址。" in title
    order_picker = next(item for item in items if item["name"] == "order_id")
    assert "抱歉，该订单已取消，无需修改地址。" in order_picker["label"]
    address = next(item for item in items if item["name"] == "address")
    assert address["placeholder"] == "请填写有效地址"


@pytest.mark.asyncio
async def test_operation_clarify_stays_chat_text_without_mutation_form(monkeypatch, tmp_path):
    """#44: Operation Clarify is chat text only — must not open a Mutation Form."""
    status, mapping, _conn = _contact_first_stores(tmp_path)
    chatwoot = AsyncMock()

    async def fake_event_stream(*_args, **_kwargs):
        yield {
            "type": "clarification_required",
            "data": {"missing": [], "reason": "operation_unclear"},
        }
        yield {
            "type": "done",
            "data": {
                "final": "好的，您是想修改订单相关信息。请问具体要办理哪一项呢？",
                "message_id": "m-op",
            },
        }

    monkeypatch.setattr(
        "ai_service_agent.harness.agent_bot.event_stream", fake_event_stream
    )

    result = await handle_agent_bot_message(
        _incoming_text_payload(
            message_id="op-clarify-1",
            cw_conv_id=9410,
            contact={"id": 21, "identifier": "u1", "name": "郑军"},
            content="我要改一下订单",
        ),
        chatwoot,
        graph=SimpleNamespace(checkpointer=None),
        status=status,
        mapping=mapping,
    )

    assert result is not None
    assert "修改订单相关信息" in result["response"]
    chatwoot.send_bot_message.assert_awaited_once()
    chatwoot.send_interactive_message.assert_not_awaited()


def _form_submit_payload(
    *,
    message_id: str,
    cw_conv_id: int,
    contact: dict,
    submitted_values: dict,
):
    return {
        "event": "message_updated",
        "id": message_id,
        "content": "",
        "message_type": "incoming",
        "content_type": "form",
        "content_attributes": {"submitted_values": submitted_values},
        "conversation": {"id": cw_conv_id},
        "contact": contact,
    }


def _agent_bot_mutation_form_graph(tmp_path, monkeypatch, *, db_name: str):
    """Fake Graph + MemorySaver + seeded mock DB for AgentBot Mutation Form tests."""
    import importlib.util

    from langgraph.checkpoint.memory import MemorySaver

    from ai_service_agent.domain.orders import SQLiteOrderRepository
    from ai_service_agent.graph import builder as builder_mod
    from ai_service_agent.harness.graph import make_ai_graph
    from ai_service_agent.testing import (
        ToolCallingFakeModel,
        router_json_message,
        select_json_message,
    )
    from ai_service_agent.tools import orders as orders_mod
    from ai_service_agent.tools import write as write_mod

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        f"seed_mock_db_agent_bot_{db_name}", root / "scripts" / "seed_mock_db.py"
    )
    seed_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seed_module)
    business_db = tmp_path / f"{db_name}.db"
    seed_module.build_database(str(business_db))

    class _WriteSettings:
        mock_ecommerce_db_path = str(business_db)

    monkeypatch.setattr(write_mod, "get_settings", lambda: _WriteSettings())
    monkeypatch.setattr(orders_mod, "get_settings", lambda: _WriteSettings())
    monkeypatch.setattr(
        builder_mod,
        "SQLiteOrderRepository",
        lambda path: SQLiteOrderRepository(str(business_db)),
    )

    repo = SQLiteOrderRepository(str(business_db))
    graph = make_ai_graph(
        ToolCallingFakeModel(
            responses=[
                router_json_message("account_profile_update", 0.9),
                select_json_message("update_address", {}),
            ]
        ),
        MemorySaver(),
        order_repository=repo,
    )
    return graph, repo


@pytest.mark.asyncio
async def test_fake_graph_form_interrupt_projects_chatwoot_form(tmp_path, monkeypatch):
    """#44: Fake Graph Form Interrupt → outbound Chatwoot form (AgentBot seam)."""
    status, mapping, _conn = _contact_first_stores(tmp_path)
    chatwoot = AsyncMock()
    graph, _repo = _agent_bot_mutation_form_graph(
        tmp_path, monkeypatch, db_name="agentbot-form-mock"
    )
    profile_loader = MagicMock(side_effect=AssertionError("legacy clarify-form path"))
    orders_loader = MagicMock(side_effect=AssertionError("legacy clarify-form path"))

    result = await handle_agent_bot_message(
        _incoming_text_payload(
            message_id="form-graph-1",
            cw_conv_id=9420,
            contact={"id": 22, "identifier": "u1", "name": "郑军"},
            content="我要改个地址",
        ),
        chatwoot,
        graph=graph,
        status=status,
        mapping=mapping,
        profile_loader=profile_loader,
        orders_loader=orders_loader,
    )

    assert result is not None
    chatwoot.send_interactive_message.assert_awaited_once()
    args = chatwoot.send_interactive_message.await_args.args
    assert args[2] == "form"
    items = args[3]["items"]
    order_picker = next(item for item in items if item["name"] == "order_id")
    assert order_picker["type"] == "select"
    assert any(opt["value"] == "ORD-PAID-1" for opt in order_picker["options"])
    assert any(item["name"] == "address" and item["type"] == "text" for item in items)
    action_field = next(item for item in items if item["name"] == "action")
    assert {"label": "取消", "value": "cancel"} in action_field["options"]
    profile_loader.assert_not_called()
    orders_loader.assert_not_called()


@pytest.mark.asyncio
async def test_pending_form_confirm_resumes_structured_values_and_writes_once(
    tmp_path, monkeypatch
):
    """#45: Confirm → Form Resume with structured fields; single write; not NL→Router."""
    import ai_service_agent.harness.agent_bot as agent_bot_mod

    status, mapping, _conn = _contact_first_stores(tmp_path)
    chatwoot = AsyncMock()
    graph, repo = _agent_bot_mutation_form_graph(
        tmp_path, monkeypatch, db_name="agentbot-form-confirm"
    )
    contact = {"id": 23, "identifier": "u1", "name": "郑军"}
    cw_conv_id = 9430
    new_address = "上海市浦东新区确认路 45 号"

    open_result = await handle_agent_bot_message(
        _incoming_text_payload(
            message_id="form-confirm-open",
            cw_conv_id=cw_conv_id,
            contact=contact,
            content="我要改个地址",
        ),
        chatwoot,
        graph=graph,
        status=status,
        mapping=mapping,
    )
    assert open_result is not None
    chatwoot.send_interactive_message.assert_awaited_once()
    thread_id = status.get_thread_id(open_result["conversation_id"])
    pending = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    assert pending.interrupts

    before = repo.get_user_profile("u1")["address"]
    assert before != new_address

    captured: dict = {}
    real_event_stream = agent_bot_mod.event_stream

    async def capturing_event_stream(*args, **kwargs):
        captured["resume_payload"] = kwargs.get("resume_payload")
        async for event in real_event_stream(*args, **kwargs):
            yield event

    monkeypatch.setattr(agent_bot_mod, "event_stream", capturing_event_stream)

    confirm_result = await handle_agent_bot_message(
        _form_submit_payload(
            message_id="form-confirm-submit",
            cw_conv_id=cw_conv_id,
            contact=contact,
            submitted_values={
                "action": "confirm",
                "order_id": "ORD-PAID-1",
                "address": new_address,
            },
        ),
        chatwoot,
        graph=graph,
        status=status,
        mapping=mapping,
    )

    assert confirm_result is not None
    assert captured["resume_payload"] == {
        "action": "confirm",
        "order_id": "ORD-PAID-1",
        "address": new_address,
    }
    assert "收货地址改成" not in str(captured["resume_payload"])
    after = repo.get_user_profile("u1")["address"]
    assert after == new_address
    done = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    assert not done.interrupts
    write_tools = [
        m
        for m in done.values.get("messages", [])
        if getattr(m, "type", "") == "tool" and getattr(m, "name", "") == "update_address"
    ]
    assert len(write_tools) == 1
    assert confirm_result["response"]
    assert "已更新完成" in confirm_result["response"] or "收货地址" in confirm_result["response"]


@pytest.mark.asyncio
async def test_pending_form_cancel_resumes_without_write_and_clears_interrupt(
    tmp_path, monkeypatch
):
    """#45: Cancel button → resume cancel; no tool write; interrupt cleared."""
    import ai_service_agent.harness.agent_bot as agent_bot_mod

    status, mapping, _conn = _contact_first_stores(tmp_path)
    chatwoot = AsyncMock()
    graph, repo = _agent_bot_mutation_form_graph(
        tmp_path, monkeypatch, db_name="agentbot-form-cancel"
    )
    contact = {"id": 24, "identifier": "u1", "name": "郑军"}
    cw_conv_id = 9440

    open_result = await handle_agent_bot_message(
        _incoming_text_payload(
            message_id="form-cancel-open",
            cw_conv_id=cw_conv_id,
            contact=contact,
            content="我要改个地址",
        ),
        chatwoot,
        graph=graph,
        status=status,
        mapping=mapping,
    )
    assert open_result is not None
    thread_id = status.get_thread_id(open_result["conversation_id"])
    pending = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    assert pending.interrupts
    before = repo.get_user_profile("u1")["address"]

    captured: dict = {}
    real_event_stream = agent_bot_mod.event_stream

    async def capturing_event_stream(*args, **kwargs):
        captured["resume_payload"] = kwargs.get("resume_payload")
        async for event in real_event_stream(*args, **kwargs):
            yield event

    monkeypatch.setattr(agent_bot_mod, "event_stream", capturing_event_stream)

    cancel_result = await handle_agent_bot_message(
        _form_submit_payload(
            message_id="form-cancel-submit",
            cw_conv_id=cw_conv_id,
            contact=contact,
            submitted_values={"action": "cancel"},
        ),
        chatwoot,
        graph=graph,
        status=status,
        mapping=mapping,
    )

    assert cancel_result is not None
    assert captured["resume_payload"] == {"action": "cancel"}
    assert repo.get_user_profile("u1")["address"] == before
    done = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    assert not done.interrupts
    write_tools = [
        m
        for m in done.values.get("messages", [])
        if getattr(m, "type", "") == "tool" and getattr(m, "name", "") == "update_address"
    ]
    assert write_tools == []
    assert cancel_result["response"]
    assert "取消" in cancel_result["response"]


@pytest.mark.asyncio
async def test_pending_form_plain_text_refuses_and_repushes_same_form_session(
    tmp_path, monkeypatch
):
    """#46: Plain text while Form Interrupt → refuse copy + re-push same form_session_id.

    Seam: handle_agent_bot_message + Fake Graph/MemorySaver + mock Chatwoot.
    Must not write and must not Form Resume as confirm.
    """
    import ai_service_agent.harness.agent_bot as agent_bot_mod
    from ai_service_agent.graph.workflows.copy import form_plain_text_refuse_text

    status, mapping, _conn = _contact_first_stores(tmp_path)
    chatwoot = AsyncMock()
    graph, repo = _agent_bot_mutation_form_graph(
        tmp_path, monkeypatch, db_name="agentbot-form-refuse"
    )
    contact = {"id": 25, "identifier": "u1", "name": "郑军"}
    cw_conv_id = 9450

    open_result = await handle_agent_bot_message(
        _incoming_text_payload(
            message_id="form-refuse-open",
            cw_conv_id=cw_conv_id,
            contact=contact,
            content="我要改个地址",
        ),
        chatwoot,
        graph=graph,
        status=status,
        mapping=mapping,
    )
    assert open_result is not None
    chatwoot.send_interactive_message.assert_awaited_once()
    thread_id = status.get_thread_id(open_result["conversation_id"])
    pending = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    assert pending.interrupts
    form_session_id = pending.interrupts[0].value["form_session_id"]
    before = repo.get_user_profile("u1")["address"]

    captured: dict = {}
    real_event_stream = agent_bot_mod.event_stream
    real_send_mutation_form = agent_bot_mod.send_mutation_form

    async def capturing_event_stream(*args, **kwargs):
        captured["event_stream_called"] = True
        captured["resume_payload"] = kwargs.get("resume_payload")
        async for event in real_event_stream(*args, **kwargs):
            yield event

    async def capturing_send_mutation_form(cw, conversation_id, form_data):
        captured["repush_form_data"] = form_data
        await real_send_mutation_form(cw, conversation_id, form_data)

    monkeypatch.setattr(agent_bot_mod, "event_stream", capturing_event_stream)
    monkeypatch.setattr(agent_bot_mod, "send_mutation_form", capturing_send_mutation_form)
    chatwoot.reset_mock()

    refuse_result = await handle_agent_bot_message(
        _incoming_text_payload(
            message_id="form-refuse-plain",
            cw_conv_id=cw_conv_id,
            contact=contact,
            content="把地址改成上海市浦东新区随便写的",
        ),
        chatwoot,
        graph=graph,
        status=status,
        mapping=mapping,
    )

    assert refuse_result is not None
    assert refuse_result["response"] == form_plain_text_refuse_text()
    chatwoot.send_bot_message.assert_awaited_once_with(
        open_result["conversation_id"], form_plain_text_refuse_text()
    )
    assert captured.get("event_stream_called") is not True
    assert "repush_form_data" in captured
    assert captured["repush_form_data"]["form_session_id"] == form_session_id
    assert captured["repush_form_data"]["tool"] == "update_address"
    chatwoot.send_interactive_message.assert_awaited_once()
    assert chatwoot.send_interactive_message.await_args.args[2] == "form"
    items = chatwoot.send_interactive_message.await_args.args[3]["items"]
    assert any(item["name"] == "address" for item in items)
    assert repo.get_user_profile("u1")["address"] == before
    still = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    assert still.interrupts
    assert still.interrupts[0].value["form_session_id"] == form_session_id


@pytest.mark.asyncio
async def test_pending_form_single_hang_tips_without_second_mutation_form(
    tmp_path, monkeypatch
):
    """#46: Concurrent write intent while Form Interrupt → tip; no second Mutation Form."""
    import ai_service_agent.harness.agent_bot as agent_bot_mod
    from ai_service_agent.graph.workflows.copy import form_plain_text_refuse_text

    status, mapping, _conn = _contact_first_stores(tmp_path)
    chatwoot = AsyncMock()
    graph, repo = _agent_bot_mutation_form_graph(
        tmp_path, monkeypatch, db_name="agentbot-form-single-hang"
    )
    contact = {"id": 26, "identifier": "u1", "name": "郑军"}
    cw_conv_id = 9460

    open_result = await handle_agent_bot_message(
        _incoming_text_payload(
            message_id="form-hang-open",
            cw_conv_id=cw_conv_id,
            contact=contact,
            content="我要改个地址",
        ),
        chatwoot,
        graph=graph,
        status=status,
        mapping=mapping,
    )
    assert open_result is not None
    thread_id = status.get_thread_id(open_result["conversation_id"])
    pending = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    form_session_id = pending.interrupts[0].value["form_session_id"]
    address_before = repo.get_user_profile("u1")["address"]
    phone_before = repo.get_user_profile("u1")["phone"]

    captured: dict = {}
    real_send_mutation_form = agent_bot_mod.send_mutation_form

    async def capturing_send_mutation_form(cw, conversation_id, form_data):
        captured.setdefault("forms", []).append(form_data)
        await real_send_mutation_form(cw, conversation_id, form_data)

    monkeypatch.setattr(agent_bot_mod, "send_mutation_form", capturing_send_mutation_form)
    chatwoot.reset_mock()

    tip_result = await handle_agent_bot_message(
        _incoming_text_payload(
            message_id="form-hang-new-write",
            cw_conv_id=cw_conv_id,
            contact=contact,
            content="我要改手机号",
        ),
        chatwoot,
        graph=graph,
        status=status,
        mapping=mapping,
    )

    assert tip_result is not None
    tip = tip_result["response"]
    assert tip == form_plain_text_refuse_text()
    assert "表单" in tip and "完成" in tip and "取消" in tip
    chatwoot.send_bot_message.assert_awaited_once()
    assert len(captured.get("forms", [])) == 1
    assert captured["forms"][0]["form_session_id"] == form_session_id
    assert captured["forms"][0]["tool"] == "update_address"
    chatwoot.send_interactive_message.assert_awaited_once()
    items = chatwoot.send_interactive_message.await_args.args[3]["items"]
    assert any(item["name"] == "address" for item in items)
    assert not any(item["name"] == "phone" for item in items)
    profile = repo.get_user_profile("u1")
    assert profile["address"] == address_before
    assert profile["phone"] == phone_before
    still = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    assert len(still.interrupts) == 1
    assert still.interrupts[0].value["form_session_id"] == form_session_id
    assert still.interrupts[0].value["tool"] == "update_address"


@pytest.mark.asyncio
async def test_form_rejected_repushes_same_session_with_errors_then_fix_writes_once(
    tmp_path, monkeypatch
):
    """#47: Failed Confirm → form_rejected same form_session_id with errors; fix writes once.

    Seam: handle_agent_bot_message + Fake Graph/MemorySaver + mock Chatwoot.
    Assert outbound Chatwoot form (errors visible in items/copy) and single write.
    """
    import ai_service_agent.harness.agent_bot as agent_bot_mod

    status, mapping, _conn = _contact_first_stores(tmp_path)
    chatwoot = AsyncMock()
    graph, repo = _agent_bot_mutation_form_graph(
        tmp_path, monkeypatch, db_name="agentbot-form-rejected"
    )
    contact = {"id": 27, "identifier": "u1", "name": "郑军"}
    cw_conv_id = 9470

    open_result = await handle_agent_bot_message(
        _incoming_text_payload(
            message_id="form-reject-open",
            cw_conv_id=cw_conv_id,
            contact=contact,
            content="我要改个地址",
        ),
        chatwoot,
        graph=graph,
        status=status,
        mapping=mapping,
    )
    assert open_result is not None
    chatwoot.send_interactive_message.assert_awaited_once()
    thread_id = status.get_thread_id(open_result["conversation_id"])
    pending = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    assert pending.interrupts
    form_session_id = pending.interrupts[0].value["form_session_id"]
    before = repo.get_user_profile("u1")["address"]

    captured: dict = {}
    real_send_mutation_form = agent_bot_mod.send_mutation_form

    async def capturing_send_mutation_form(cw, conversation_id, form_data):
        captured.setdefault("forms", []).append(form_data)
        await real_send_mutation_form(cw, conversation_id, form_data)

    monkeypatch.setattr(agent_bot_mod, "send_mutation_form", capturing_send_mutation_form)
    chatwoot.reset_mock()

    bad_address = "不可写的新地址"
    reject_result = await handle_agent_bot_message(
        _form_submit_payload(
            message_id="form-reject-confirm-bad",
            cw_conv_id=cw_conv_id,
            contact=contact,
            submitted_values={
                "action": "confirm",
                "order_id": "ORD-CANCEL-1",
                "address": bad_address,
            },
        ),
        chatwoot,
        graph=graph,
        status=status,
        mapping=mapping,
    )

    assert reject_result is not None
    assert repo.get_user_profile("u1")["address"] == before
    assert len(captured.get("forms", [])) == 1
    rejected_form = captured["forms"][0]
    assert rejected_form["form_session_id"] == form_session_id
    assert rejected_form["tool"] == "update_address"
    assert rejected_form.get("errors")
    chatwoot.send_interactive_message.assert_awaited_once()
    reject_args = chatwoot.send_interactive_message.await_args.args
    assert reject_args[2] == "form"
    reject_title = reject_args[1]
    reject_items = reject_args[3]["items"]
    error_blob = " ".join(
        [
            reject_title,
            *(str(item.get("label", "")) for item in reject_items),
            *(str(item.get("placeholder", "")) for item in reject_items),
        ]
    )
    assert any(str(msg) in error_blob for msg in rejected_form["errors"].values())
    still = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    assert still.interrupts
    assert still.interrupts[0].value["form_session_id"] == form_session_id
    assert still.interrupts[0].value.get("errors")

    chatwoot.reset_mock()
    good_address = "修正后的表单地址 66 号"
    fix_result = await handle_agent_bot_message(
        _form_submit_payload(
            message_id="form-reject-confirm-fix",
            cw_conv_id=cw_conv_id,
            contact=contact,
            submitted_values={
                "action": "confirm",
                "order_id": "ORD-PAID-1",
                "address": good_address,
            },
        ),
        chatwoot,
        graph=graph,
        status=status,
        mapping=mapping,
    )

    assert fix_result is not None
    assert repo.get_user_profile("u1")["address"] == good_address
    done = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    assert not done.interrupts
    write_tools = [
        m
        for m in done.values.get("messages", [])
        if getattr(m, "type", "") == "tool" and getattr(m, "name", "") == "update_address"
    ]
    assert len(write_tools) == 1
    assert fix_result["response"]
    assert "已更新完成" in fix_result["response"] or "收货地址" in fix_result["response"]
    # Reject re-push already counted; fix path must not open another rejected form.
    assert len(captured["forms"]) == 1
