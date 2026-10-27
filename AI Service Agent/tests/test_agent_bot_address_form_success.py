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
from langchain_core.messages import AIMessage

from ai_service_agent.harness.agent_bot import handle_agent_bot_message
from ai_service_agent.testing import router_json_message
from ai_service_agent.harness.mutation_projection import (
    send_mutation_success_card,
    update_address_success_card_items,
)
from ai_service_agent.graph.workflows.copy import (
    build_mutation_success_card,
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


def _agent_bot_mutation_form_graph(
    tmp_path,
    monkeypatch,
    *,
    db_name: str,
    user_id: str = "u1",
    tool: str = "update_address",
    select_args: dict | None = None,
    extra_responses: list | None = None,
):
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
                select_json_message(tool, {} if select_args is None else select_args),
                *(extra_responses or []),
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
    assert description.startswith(f"• 状态：{update_address_success_status_text()}")
    assert "\n\n• " in description
    assert "ORD-PAID-1" in description
    assert "山东省青岛市市南区示例路 88 号" in description
    assert f"账户地址：{update_address_success_account_updated_text()}" in description
    assert card["actions"][0]["text"] == "知道了"
    assert card["actions"][0]["payload"] == "update_address_success_ack"


@pytest.mark.asyncio
async def test_send_mutation_success_card_emits_cards_then_optional_followup():
    chatwoot = AsyncMock()
    spec = build_mutation_success_card(
        "update_address",
        {"order_id": "ORD-PAID-1", "address": "北京市朝阳区望京 SOHO"},
    )
    assert spec is not None
    await send_mutation_success_card(chatwoot, "c1", spec)
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
    assert card["description"].startswith(f"• 状态：{update_address_success_status_text()}")
    assert "\n\n" in card["description"]

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


@pytest.mark.asyncio
async def test_phone_confirm_success_sends_shared_card_not_plain_bubble_only(
    tmp_path, monkeypatch
):
    """update_phone confirm uses the same cards + follow-up, not only a text bubble."""
    status, mapping, _conn = _contact_first_stores(tmp_path)
    chatwoot = AsyncMock()
    graph, repo, _user = _agent_bot_mutation_form_graph(
        tmp_path, monkeypatch, db_name="agentbot-phone-card", tool="update_phone"
    )
    contact = {"id": 83, "identifier": "u1", "name": "郑军"}
    new_phone = "18600001111"

    await handle_agent_bot_message(
        _incoming_text_payload(
            message_id="phone-card-open",
            cw_conv_id=9831,
            contact=contact,
            content="我要改手机号",
        ),
        chatwoot,
        graph=graph,
        status=status,
        mapping=mapping,
    )
    chatwoot.reset_mock()
    confirm_result = await handle_agent_bot_message(
        _form_submit_payload(
            message_id="phone-card-confirm",
            cw_conv_id=9831,
            contact=contact,
            submitted_values={"phone": new_phone, "action": "confirm"},
        ),
        chatwoot,
        graph=graph,
        status=status,
        mapping=mapping,
    )
    assert confirm_result is not None
    assert repo.get_user_profile("u1")["phone"] == new_phone
    card_calls = [
        call
        for call in chatwoot.send_interactive_message.await_args_list
        if call.args[2] == "cards"
    ]
    assert len(card_calls) == 1
    card = card_calls[0].args[3]["items"][0]
    assert card["title"] == "手机号已修改成功"
    assert card["description"].startswith("• 状态：已完成")
    assert "\n\n• 新手机号：18600001111" in card["description"]
    assert "• 通知号码：已切换" in card["description"]
    assert "订单号" not in card["description"]
    assert card["actions"][0]["text"] == "知道了"
    texts = [call.args[1] for call in chatwoot.send_bot_message.await_args_list]
    assert texts == ["好的，您的联系电话已更新，后续通知将发送到新号码。"]
    assert confirm_result["response"] == texts[0]


@pytest.mark.asyncio
async def test_previous_address_success_is_not_replayed_on_next_normal_turn(
    tmp_path, monkeypatch
):
    """Regression: one completed write must not leak historical tool events into later turns."""
    import ai_service_agent.harness.agent_bot as agent_bot_mod

    status, mapping, _conn = _contact_first_stores(tmp_path)
    chatwoot = AsyncMock()
    graph, repo, _user = _agent_bot_mutation_form_graph(
        tmp_path,
        monkeypatch,
        db_name="agentbot-success-no-replay",
        extra_responses=[
            router_json_message("faq", 0.9),
            AIMessage(content="这是 FAQ 的新回答。"),
        ],
    )
    contact = {"id": 84, "identifier": "u1", "name": "郑军"}
    cw_conv_id = 9832
    new_address = "上海市浦东新区不会重放路 74 号"

    await handle_agent_bot_message(
        _incoming_text_payload(
            message_id="no-replay-open",
            cw_conv_id=cw_conv_id,
            contact=contact,
            content="我要改个地址",
        ),
        chatwoot,
        graph=graph,
        status=status,
        mapping=mapping,
    )
    await handle_agent_bot_message(
        _form_submit_payload(
            message_id="no-replay-confirm",
            cw_conv_id=cw_conv_id,
            contact=contact,
            submitted_values={"order_id": "ORD-PAID-1", "address": new_address},
        ),
        chatwoot,
        graph=graph,
        status=status,
        mapping=mapping,
    )
    assert repo.get_user_profile("u1")["address"] == new_address

    captured_events: list[dict] = []
    real_event_stream = agent_bot_mod.event_stream

    async def capturing_event_stream(*args, **kwargs):
        async for event in real_event_stream(*args, **kwargs):
            captured_events.append(event)
            yield event

    monkeypatch.setattr(agent_bot_mod, "event_stream", capturing_event_stream)
    chatwoot.reset_mock()

    result = await handle_agent_bot_message(
        _incoming_text_payload(
            message_id="no-replay-next",
            cw_conv_id=cw_conv_id,
            contact=contact,
            content="你好",
        ),
        chatwoot,
        graph=graph,
        status=status,
        mapping=mapping,
    )

    assert result is not None
    assert result["response"] == "这是 FAQ 的新回答。"
    assert not any(
        event.get("type") in ("tool_call", "tool_result")
        and (event.get("data") or {}).get("name") == "update_address"
        for event in captured_events
    )
    assert not any(
        call.args[2] == "cards"
        for call in chatwoot.send_interactive_message.await_args_list
    )
    texts = [call.args[1] for call in chatwoot.send_bot_message.await_args_list]
    assert texts == ["这是 FAQ 的新回答。"]

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
    # 失败路径只发失败卡：标题不是成功文案，按钮只有「重新填写」。
    assert len(card_calls) == 1
    card = card_calls[0].args[3]["items"][0]
    assert card["title"] == "地址未能修改"
    assert [action["text"] for action in card["actions"]] == ["重新填写"]
    assert card["actions"][0]["payload"] == "update_address_failure_retry"
    # 失败卡 + 重推 form，顺序固定。
    assert chatwoot.send_interactive_message.await_count == 2
    assert chatwoot.send_interactive_message.await_args.args[2] == "form"
    # 长 done.final（假成功文案）不得出站。
    for call in chatwoot.send_bot_message.await_args_list:
        assert "已修改成功" not in str(call.args[1])
