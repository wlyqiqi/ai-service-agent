"""#73: AgentBot success card after update_address confirm.

Seams (issue acceptance / mutation-form-interrupt-spec):
1. Success projection: cards with locked copy + optional short bubble; no long done duplicate.
2. Confirm success path: write once; no leftover Form Interrupt; no new form outbound.
3. form_rejected / non-success: never send success cards.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ai_service_agent.harness.agent_bot import (
    handle_agent_bot_message,
    send_mutation_success_card,
    update_address_success_card_items,
)
from ai_service_agent.graph.workflows.copy import (
    update_address_success_account_updated_text,
    update_address_success_card_title_text,
    update_address_success_followup_text,
    update_address_success_status_text,
)


_ROOT = Path(__file__).resolve().parents[1]


def _contact_first_stores(tmp_path):
    import sqlite3

    from ai_service_agent.harness.chatwoot import ChatwootMappingStore
    from ai_service_agent.harness.session import ConversationStatusStore

    db = str(tmp_path / "agentbot-address-73.db")
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
        f"seed_mock_db_agent_bot_73_{db_name}", _ROOT / "scripts" / "seed_mock_db.py"
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


# --- Seam 1: success card payload contract -----------------------------------


def test_update_address_success_card_items_locked_copy():
    items = update_address_success_card_items(
        order_id="ORD-PAID-1",
        address="山东省青岛市市南区示例路 88 号",
    )
    assert len(items) == 1
    card = items[0]
    assert card["title"] == update_address_success_card_title_text()
    description = card["description"]
    assert update_address_success_status_text() in description
    assert "ORD-PAID-1" in description
    assert "山东省青岛市市南区示例路 88 号" in description
    assert update_address_success_account_updated_text() in description
    assert card["actions"]  # Chatwoot cards require actions


@pytest.mark.asyncio
async def test_send_mutation_success_card_emits_cards_then_optional_followup():
    chatwoot = AsyncMock()
    await send_mutation_success_card(
        chatwoot,
        "c1",
        order_id="ORD-PAID-1",
        address="北京市朝阳区望京 SOHO",
    )
    chatwoot.send_interactive_message.assert_awaited_once()
    args = chatwoot.send_interactive_message.await_args.args
    assert args[2] == "cards"
    items = args[3]["items"]
    assert items[0]["title"] == update_address_success_card_title_text()
    assert update_address_success_account_updated_text() in items[0]["description"]
    chatwoot.send_bot_message.assert_awaited_once()
    assert chatwoot.send_bot_message.await_args.args[1] == update_address_success_followup_text()


# --- Seam 2: confirm success → cards; write once; no leftover interrupt ------


@pytest.mark.asyncio
async def test_confirm_success_sends_cards_with_account_updated_not_long_done(
    tmp_path, monkeypatch
):
    """UX-03 / UX-09: prefer cards + short bubble; suppress long done duplicate."""
    status, mapping, _conn = _contact_first_stores(tmp_path)
    chatwoot = AsyncMock()
    graph, repo, _user = _agent_bot_mutation_form_graph(
        tmp_path, monkeypatch, db_name="agentbot-success-73"
    )
    contact = {"id": 83, "identifier": "u1", "name": "郑军"}
    cw_conv_id = 9830
    new_address = "上海市浦东新区确认路 73 号"

    open_result = await handle_agent_bot_message(
        _incoming_text_payload(
            message_id="success-73-open",
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
    assert before != new_address

    chatwoot.reset_mock()

    confirm_result = await handle_agent_bot_message(
        _form_submit_payload(
            message_id="success-73-confirm",
            cw_conv_id=cw_conv_id,
            contact=contact,
            submitted_values={
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
    assert repo.get_user_profile("u1")["address"] == new_address
    done = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    assert not done.interrupts
    write_tools = [
        m
        for m in done.values.get("messages", [])
        if getattr(m, "type", "") == "tool" and getattr(m, "name", "") == "update_address"
    ]
    assert len(write_tools) == 1

    # Success → cards (not form).
    card_calls = [
        call
        for call in chatwoot.send_interactive_message.await_args_list
        if call.args[2] == "cards"
    ]
    assert len(card_calls) == 1
    card = card_calls[0].args[3]["items"][0]
    assert card["title"] == update_address_success_card_title_text()
    assert update_address_success_account_updated_text() in card["description"]
    assert "ORD-PAID-1" in card["description"]
    assert new_address in card["description"]
    assert update_address_success_status_text() in card["description"]

    form_calls = [
        call
        for call in chatwoot.send_interactive_message.await_args_list
        if call.args[2] == "form"
    ]
    assert form_calls == []

    texts = [call.args[1] for call in chatwoot.send_bot_message.await_args_list]
    assert update_address_success_followup_text() in texts
    # Long graph done must not be duplicated outbound.
    assert not any("收货地址已修改成功" in text for text in texts)
    assert confirm_result["response"] == update_address_success_followup_text()


# --- Seam 3: form_rejected must not emit success cards -----------------------


@pytest.mark.asyncio
async def test_form_rejected_does_not_send_success_card(monkeypatch, tmp_path):
    status, mapping, _conn = _contact_first_stores(tmp_path)
    chatwoot = AsyncMock()
    form_data = {
        "tool": "update_address",
        "current_address": "旧址",
        "order_candidates": [{"order_id": "ORD-PAID-1", "product_summary": "鞋 x1"}],
        "form_session_id": "fs-rejected-73",
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
        yield {
            "type": "done",
            "data": {
                "final": "订单 ORD-PAID-1 的收货地址已修改成功：假成功。",
                "message_id": None,
            },
        }

    monkeypatch.setattr(
        "ai_service_agent.harness.agent_bot.event_stream", fake_event_stream
    )

    result = await handle_agent_bot_message(
        _incoming_text_payload(
            message_id="rejected-73",
            cw_conv_id=9840,
            contact={"id": 84, "identifier": "u1", "name": "郑军"},
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
    card_calls = [
        call
        for call in chatwoot.send_interactive_message.await_args_list
        if len(call.args) > 2 and call.args[2] == "cards"
    ]
    assert card_calls == []
    chatwoot.send_interactive_message.assert_awaited_once()
    assert chatwoot.send_interactive_message.await_args.args[2] == "form"
