"""#71: AgentBot update_address open-form layout + empty-state (no empty picker).

Seams (issue acceptance / mutation-form-interrupt-spec):
1. Open-form projection: intro + form IA; no action confirm/cancel select; submit = confirm.
2. Empty state: fixed text only; never send empty dropdown form.
3. Form Interrupt / projection carries current_address; resume write uses new address only.
4. form_rejected still re-pushes same session (no Implicit Cancel change in this ticket).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ai_service_agent.harness.agent_bot import handle_agent_bot_message
from ai_service_agent.harness.mutation_form_projection import (
    mutation_form_items_from_form_data,
    mutation_form_title_from_form_data,
    send_mutation_form,
)
from ai_service_agent.graph.workflows.copy import (
    update_address_empty_state_text,
    update_address_form_footnote_text,
    update_address_form_intro_text,
)


_ROOT = Path(__file__).resolve().parents[1]


def _contact_first_stores(tmp_path):
    import sqlite3

    from ai_service_agent.harness.chatwoot import ChatwootMappingStore
    from ai_service_agent.harness.session import ConversationStatusStore

    db = str(tmp_path / "agentbot-address-71.db")
    conn = sqlite3.connect(db)
    status = ConversationStatusStore(conn)
    mapping = ChatwootMappingStore(conn)
    return status, mapping, conn


def _incoming_text_payload(*, message_id: str, cw_conv_id: int, contact: dict, content: str):
    return {
        "event": "message_created",
        "id": message_id,
        "message_type": "incoming",
        "content_type": "text",
        "content": content,
        "conversation": {"id": cw_conv_id},
        "contact": contact,
    }


def _form_submit_payload(*, message_id: str, cw_conv_id: int, contact: dict, submitted_values: dict):
    return {
        "event": "message_updated",
        "id": message_id,
        "message_type": "incoming",
        "content_type": "form",
        "content_attributes": {
            "submitted_values": [
                {"name": key, "value": value} for key, value in submitted_values.items()
            ]
        },
        "conversation": {"id": cw_conv_id},
        "contact": contact,
    }


def _agent_bot_mutation_form_graph(tmp_path, monkeypatch, *, db_name: str, user_id: str = "u1"):
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

    spec = importlib.util.spec_from_file_location(
        f"seed_mock_db_agent_bot_71_{db_name}", _ROOT / "scripts" / "seed_mock_db.py"
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
    return graph, repo, user_id


# --- Seam 1: open-form layout contract ---------------------------------------


def test_update_address_open_form_items_locked_layout_no_action_select():
    """UX-01 / UX-10: current address → unpaid-selected order → new address → footnote; no action."""
    data = {
        "tool": "update_address",
        "summary": "ignored-for-locked-title",
        "current_address": "123 Chengyuan Avenue #04-01, Singapore",
        "order_candidates": [
            {"order_id": "ORD-PAID-1", "product_summary": "测试商品 x1"},
            {"order_id": "ORD-PAID-2", "product_summary": "另一件 x1"},
        ],
        "prefills": {"order_id": "ORD-PAID-1", "address": "旧预填"},
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

    assert "修改收货地址" in title
    assert "仅已支付且未发货订单可改" in title

    names = [item["name"] for item in items]
    assert names == ["current_address", "order_id", "address", "abandon_footnote"]
    assert all(item["name"] != "action" for item in items)
    assert items[-1]["default"] == update_address_form_footnote_text()

    current = items[0]
    assert current["type"] == "text"
    assert "当前" in current["label"]
    assert current.get("default") == "123 Chengyuan Avenue #04-01, Singapore"

    order_picker = items[1]
    assert order_picker["type"] == "select"
    assert "default" not in order_picker  # never auto-preselect, even with prefill
    assert order_picker["options"] == [
        {"label": "ORD-PAID-1", "value": "ORD-PAID-1"},
        {"label": "ORD-PAID-2", "value": "ORD-PAID-2"},
    ]
    assert "请选择" in (order_picker.get("placeholder") or order_picker.get("label") or "")

    address = items[2]
    assert address["type"] == "text"
    assert "省市区" in (address.get("placeholder") or "")
    assert address.get("default") == "旧预填"


def test_update_address_current_address_empty_shows_placeholder():
    items = mutation_form_items_from_form_data(
        {
            "tool": "update_address",
            "current_address": "",
            "order_candidates": [{"order_id": "ORD-PAID-1", "product_summary": "鞋 x1"}],
            "response_schema": {"properties": {"address": {"type": "string"}}},
        }
    )
    current = next(item for item in items if item["name"] == "current_address")
    assert current.get("default") == "暂无"


@pytest.mark.asyncio
async def test_send_update_address_form_emits_intro_then_form():
    chatwoot = AsyncMock()
    data = {
        "tool": "update_address",
        "current_address": "上海",
        "order_candidates": [{"order_id": "ORD-PAID-1", "product_summary": "鞋 x1"}],
        "response_schema": {
            "properties": {
                "order_id": {"type": "string"},
                "address": {"type": "string"},
            },
        },
    }
    await send_mutation_form(chatwoot, "c1", data)
    assert chatwoot.send_bot_message.await_count == 1
    assert chatwoot.send_bot_message.await_args.args[1] == update_address_form_intro_text()
    chatwoot.send_interactive_message.assert_awaited_once()
    assert chatwoot.send_interactive_message.await_args.args[2] == "form"
    items = chatwoot.send_interactive_message.await_args.args[3]["items"]
    assert all(item["name"] != "action" for item in items)


# --- Seam 2: empty state -----------------------------------------------------


@pytest.mark.asyncio
async def test_send_update_address_empty_candidates_text_only_no_form():
    chatwoot = AsyncMock()
    await send_mutation_form(
        chatwoot,
        "c1",
        {"tool": "update_address", "order_candidates": [], "current_address": ""},
    )
    chatwoot.send_bot_message.assert_awaited_once()
    assert chatwoot.send_bot_message.await_args.args[1] == update_address_empty_state_text()
    chatwoot.send_interactive_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_fake_graph_no_paid_orders_projects_empty_state_not_form(tmp_path, monkeypatch):
    """Acceptance: empty eligible candidates → plain text; no empty picker form."""
    import sqlite3

    status, mapping, _conn = _contact_first_stores(tmp_path)
    chatwoot = AsyncMock()
    graph, repo, _user = _agent_bot_mutation_form_graph(
        tmp_path, monkeypatch, db_name="agentbot-empty-71"
    )
    # Remove paid orders for u1 so Order Picker is empty.
    conn = sqlite3.connect(repo.db_path)
    conn.execute("UPDATE orders SET status = 'shipped' WHERE customer_id = 'u1' AND status = 'paid'")
    conn.commit()
    conn.close()
    assert repo.list_order_picker_candidates("u1", "update_address") == []

    result = await handle_agent_bot_message(
        _incoming_text_payload(
            message_id="empty-open-1",
            cw_conv_id=9710,
            contact={"id": 71, "identifier": "u1", "name": "郑军"},
            content="我要改个地址",
        ),
        chatwoot,
        graph=graph,
        status=status,
        mapping=mapping,
    )

    assert result is not None
    chatwoot.send_interactive_message.assert_not_awaited()
    texts = [call.args[1] for call in chatwoot.send_bot_message.await_args_list]
    assert any(update_address_empty_state_text() in text for text in texts)
    thread_id = status.get_thread_id(result["conversation_id"])
    pending = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    assert not pending.interrupts


# --- Seam 3: current_address on interrupt + resume ignores it ----------------


@pytest.mark.asyncio
async def test_fake_graph_open_form_carries_current_address_and_locked_projection(
    tmp_path, monkeypatch
):
    status, mapping, _conn = _contact_first_stores(tmp_path)
    chatwoot = AsyncMock()
    graph, repo, _user = _agent_bot_mutation_form_graph(
        tmp_path, monkeypatch, db_name="agentbot-open-71"
    )
    expected_address = repo.get_user_profile("u1")["address"]

    result = await handle_agent_bot_message(
        _incoming_text_payload(
            message_id="open-71-1",
            cw_conv_id=9720,
            contact={"id": 72, "identifier": "u1", "name": "郑军"},
            content="我要改个地址",
        ),
        chatwoot,
        graph=graph,
        status=status,
        mapping=mapping,
    )

    assert result is not None
    thread_id = status.get_thread_id(result["conversation_id"])
    pending = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    assert pending.interrupts
    value = pending.interrupts[0].value
    assert value["tool"] == "update_address"
    assert value.get("current_address") == expected_address
    assert value["order_candidates"]

    assert chatwoot.send_bot_message.await_count >= 1
    intro_calls = [
        call.args[1]
        for call in chatwoot.send_bot_message.await_args_list
        if call.args[1] == update_address_form_intro_text()
    ]
    assert intro_calls
    chatwoot.send_interactive_message.assert_awaited_once()
    items = chatwoot.send_interactive_message.await_args.args[3]["items"]
    assert [item["name"] for item in items] == [
        "current_address",
        "order_id",
        "address",
        "abandon_footnote",
    ]
    assert all(item["name"] != "action" for item in items)
    current = next(item for item in items if item["name"] == "current_address")
    assert current.get("default") == expected_address
    order_picker = next(item for item in items if item["name"] == "order_id")
    assert "default" not in order_picker
    assert order_picker["options"]
    assert all(opt["label"] == opt["value"] for opt in order_picker["options"])
    assert items[-1]["default"] == update_address_form_footnote_text()


@pytest.mark.asyncio
async def test_primary_submit_without_action_field_confirms_and_ignores_current_address(
    tmp_path, monkeypatch
):
    """Submit semantics = confirm; resume write fields exclude read-only current_address."""
    import ai_service_agent.harness.agent_bot as agent_bot_mod

    status, mapping, _conn = _contact_first_stores(tmp_path)
    chatwoot = AsyncMock()
    graph, repo, _user = _agent_bot_mutation_form_graph(
        tmp_path, monkeypatch, db_name="agentbot-submit-71"
    )
    contact = {"id": 73, "identifier": "u1", "name": "郑军"}
    cw_conv_id = 9730
    new_address = "山东省青岛市市南区示例路 88 号"

    open_result = await handle_agent_bot_message(
        _incoming_text_payload(
            message_id="submit-71-open",
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
    before = repo.get_user_profile("u1")["address"]

    captured: dict = {}
    real_event_stream = agent_bot_mod.event_stream

    async def capturing_event_stream(*args, **kwargs):
        captured["resume_payload"] = kwargs.get("resume_payload")
        async for event in real_event_stream(*args, **kwargs):
            yield event

    monkeypatch.setattr(agent_bot_mod, "event_stream", capturing_event_stream)

    confirm_result = await handle_agent_bot_message(
        _form_submit_payload(
            message_id="submit-71-confirm",
            cw_conv_id=cw_conv_id,
            contact=contact,
            submitted_values={
                "current_address": before,  # read-only echo — must not become write arg
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
    assert "current_address" not in captured["resume_payload"]
    assert repo.get_user_profile("u1")["address"] == new_address


# --- Seam 4: form_rejected same session (no Implicit Cancel) -----------------


def test_update_address_form_rejected_keeps_layout_and_paints_errors():
    data = {
        "tool": "update_address",
        "current_address": "旧址",
        "order_candidates": [{"order_id": "ORD-PAID-1", "product_summary": "鞋 x1"}],
        "prefills": {"order_id": "ORD-PAID-1", "address": "太短"},
        "errors": {
            "address": "请填写有效地址",
            "order_id": "该订单已不可改",
        },
        "response_schema": {
            "properties": {
                "order_id": {"type": "string"},
                "address": {"type": "string"},
            },
        },
    }
    title = mutation_form_title_from_form_data(data)
    items = mutation_form_items_from_form_data(data)
    assert "修改收货地址" in title
    assert all(item["name"] != "action" for item in items)
    # form_rejected may keep draft via prefills on writable fields
    address = next(item for item in items if item["name"] == "address")
    assert address.get("default") == "太短"
    assert address.get("placeholder") == "请填写有效地址"
    order_picker = next(item for item in items if item["name"] == "order_id")
    assert "该订单已不可改" in order_picker["label"]
    assert order_picker["options"] == [{"label": "ORD-PAID-1", "value": "ORD-PAID-1"}]


@pytest.mark.asyncio
async def test_form_rejected_event_repushes_form_not_empty_state(monkeypatch, tmp_path):
    status, mapping, _conn = _contact_first_stores(tmp_path)
    chatwoot = AsyncMock()
    form_data = {
        "tool": "update_address",
        "current_address": "旧址",
        "order_candidates": [{"order_id": "ORD-PAID-1", "product_summary": "鞋 x1"}],
        "form_session_id": "fs-rejected-71",
        "errors": {"address": "请填写有效地址"},
        "prefills": {"address": "x"},
        "response_schema": {
            "properties": {
                "order_id": {"type": "string"},
                "address": {"type": "string"},
            },
        },
    }

    async def fake_event_stream(*_args, **_kwargs):
        yield {"type": "form_rejected", "data": form_data}
        yield {"type": "done", "data": {"final": None, "message_id": None}}

    monkeypatch.setattr(
        "ai_service_agent.harness.agent_bot.event_stream", fake_event_stream
    )

    result = await handle_agent_bot_message(
        _incoming_text_payload(
            message_id="rejected-71",
            cw_conv_id=9740,
            contact={"id": 74, "identifier": "u1", "name": "郑军"},
            content="我要改个地址",
        ),
        chatwoot,
        graph=SimpleNamespace(checkpointer=None),
        status=status,
        mapping=mapping,
        profile_loader=MagicMock(side_effect=AssertionError("no legacy")),
        orders_loader=MagicMock(side_effect=AssertionError("no legacy")),
    )

    assert result is not None
    chatwoot.send_interactive_message.assert_awaited_once()
    assert chatwoot.send_interactive_message.await_args.args[2] == "form"
    # Empty-state text must not replace form_rejected projection.
    empty = update_address_empty_state_text()
    for call in chatwoot.send_bot_message.await_args_list:
        assert call.args[1] != empty
