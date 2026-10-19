"""#72 AgentBot Implicit Cancel — pending Form Interrupt + non-form inbound.

Seam (sole primary, #69/#72): handle_agent_bot_message + Fake Graph/MemorySaver
+ mock Chatwoot. Assert outbound, write, and event_stream resume/invoke call order.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import MemorySaver

from ai_service_agent.domain.orders import SQLiteOrderRepository
from ai_service_agent.graph import builder as builder_mod
from ai_service_agent.graph.workflows.copy import operation_cancelled_text
from ai_service_agent.harness.agent_bot import handle_agent_bot_message
from ai_service_agent.harness.chatwoot import ChatwootMappingStore
from ai_service_agent.harness.graph import make_ai_graph
from ai_service_agent.harness.session import ConversationStatusStore
from ai_service_agent.testing import (
    ToolCallingFakeModel,
    router_json_message,
    select_json_message,
)
from ai_service_agent.tools import orders as orders_mod
from ai_service_agent.tools import write as write_mod


def _contact_first_stores(tmp_path):
    conn = sqlite3.connect(tmp_path / "contact_first.db")
    return ConversationStatusStore(conn), ChatwootMappingStore(conn), conn


def _incoming_text_payload(*, message_id: str, cw_conv_id: int, contact: dict, content: str):
    return {
        "event": "message_created",
        "id": message_id,
        "content": content,
        "message_type": "incoming",
        "content_type": "text",
        "conversation": {"id": cw_conv_id},
        "contact": contact,
    }


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


def _agent_bot_graph(tmp_path, monkeypatch, *, db_name: str, responses: list):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        f"seed_mock_db_ic_{db_name}", root / "scripts" / "seed_mock_db.py"
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
        ToolCallingFakeModel(responses=responses),
        MemorySaver(),
        order_repository=repo,
    )
    return graph, repo


@pytest.mark.asyncio
async def test_pending_form_faq_implicit_cancel_then_new_turn_silent(
    tmp_path, monkeypatch
):
    """UX-05: pending form + FAQ → cancel resume then normal invoke; silent; no old form."""
    import ai_service_agent.harness.agent_bot as agent_bot_mod

    status, mapping, _conn = _contact_first_stores(tmp_path)
    chatwoot = AsyncMock()
    faq_reply = "这是 FAQ 的离线回复（fake model）。"
    graph, repo = _agent_bot_graph(
        tmp_path,
        monkeypatch,
        db_name="ic-faq",
        responses=[
            router_json_message("account_profile_update", 0.9),
            select_json_message("update_address", {}),
            router_json_message("faq", 0.9),
            AIMessage(content=faq_reply),
        ],
    )
    contact = {"id": 72, "identifier": "u1", "name": "郑军"}
    cw_conv_id = 9720

    open_result = await handle_agent_bot_message(
        _incoming_text_payload(
            message_id="ic-faq-open",
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
    chatwoot.send_interactive_message.assert_awaited()
    thread_id = status.get_thread_id(open_result["conversation_id"])
    pending = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    assert pending.interrupts
    before = repo.get_user_profile("u1")["address"]

    stream_calls: list[dict] = []
    real_event_stream = agent_bot_mod.event_stream
    form_sends: list = []
    real_send_mutation_form = agent_bot_mod.send_mutation_form

    async def capturing_event_stream(*args, **kwargs):
        stream_calls.append(
            {
                "resume_payload": kwargs.get("resume_payload"),
                "thread_id": (args[1] if len(args) > 1 else kwargs.get("config", {})).get(
                    "configurable", {}
                ).get("thread_id"),
            }
        )
        async for event in real_event_stream(*args, **kwargs):
            yield event

    async def capturing_send_mutation_form(cw, conversation_id, form_data):
        form_sends.append(form_data)
        await real_send_mutation_form(cw, conversation_id, form_data)

    monkeypatch.setattr(agent_bot_mod, "event_stream", capturing_event_stream)
    monkeypatch.setattr(agent_bot_mod, "send_mutation_form", capturing_send_mutation_form)
    chatwoot.reset_mock()

    result = await handle_agent_bot_message(
        _incoming_text_payload(
            message_id="ic-faq-plain",
            cw_conv_id=cw_conv_id,
            contact=contact,
            content="退货政策是什么",
        ),
        chatwoot,
        graph=graph,
        status=status,
        mapping=mapping,
    )

    assert result is not None
    assert len(stream_calls) >= 2
    assert stream_calls[0]["resume_payload"] == {"action": "cancel"}
    assert stream_calls[1]["resume_payload"] in (None, {})
    assert stream_calls[0]["thread_id"] == thread_id
    assert stream_calls[1]["thread_id"] == thread_id

    assert result["response"] == faq_reply
    bot_texts = [call.args[1] for call in chatwoot.send_bot_message.await_args_list]
    assert faq_reply in bot_texts
    assert "请先完成或取消当前表单" not in bot_texts
    assert operation_cancelled_text() not in bot_texts
    assert all("已按您的要求取消" not in t for t in bot_texts)
    assert form_sends == []
    chatwoot.send_interactive_message.assert_not_awaited()

    assert repo.get_user_profile("u1")["address"] == before
    done = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    assert not done.interrupts
    write_tools = [
        m
        for m in done.values.get("messages", [])
        if getattr(m, "type", "") == "tool" and getattr(m, "name", "") == "update_address"
    ]
    assert write_tools == []


@pytest.mark.asyncio
async def test_pending_form_new_intent_implicit_cancel_no_old_form_repush(
    tmp_path, monkeypatch
):
    """UX-05/06 variant: pending form + new write intent → Implicit Cancel; no old form."""
    import ai_service_agent.harness.agent_bot as agent_bot_mod

    status, mapping, _conn = _contact_first_stores(tmp_path)
    chatwoot = AsyncMock()
    # After cancel, route to phone mutation and open a phone form (or clarify).
    # Minimal: FAQ-style reply is enough to prove new-turn routing without old form.
    new_turn_reply = "好的，请告诉我您的新手机号。"
    graph, repo = _agent_bot_graph(
        tmp_path,
        monkeypatch,
        db_name="ic-new-intent",
        responses=[
            router_json_message("account_profile_update", 0.9),
            select_json_message("update_address", {}),
            router_json_message("account_profile_update", 0.9),
            select_json_message("update_phone", {}),
            AIMessage(content=new_turn_reply),
        ],
    )
    contact = {"id": 73, "identifier": "u1", "name": "郑军"}
    cw_conv_id = 9730

    open_result = await handle_agent_bot_message(
        _incoming_text_payload(
            message_id="ic-intent-open",
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
    address_before = repo.get_user_profile("u1")["address"]
    phone_before = repo.get_user_profile("u1")["phone"]

    stream_calls: list[dict] = []
    real_event_stream = agent_bot_mod.event_stream
    form_sends: list = []
    real_send_mutation_form = agent_bot_mod.send_mutation_form

    async def capturing_event_stream(*args, **kwargs):
        stream_calls.append({"resume_payload": kwargs.get("resume_payload")})
        async for event in real_event_stream(*args, **kwargs):
            yield event

    async def capturing_send_mutation_form(cw, conversation_id, form_data):
        form_sends.append(form_data)
        await real_send_mutation_form(cw, conversation_id, form_data)

    monkeypatch.setattr(agent_bot_mod, "event_stream", capturing_event_stream)
    monkeypatch.setattr(agent_bot_mod, "send_mutation_form", capturing_send_mutation_form)
    chatwoot.reset_mock()

    result = await handle_agent_bot_message(
        _incoming_text_payload(
            message_id="ic-intent-phone",
            cw_conv_id=cw_conv_id,
            contact=contact,
            content="我要改手机号",
        ),
        chatwoot,
        graph=graph,
        status=status,
        mapping=mapping,
    )

    assert result is not None
    assert len(stream_calls) >= 2
    assert stream_calls[0]["resume_payload"] == {"action": "cancel"}
    assert stream_calls[1]["resume_payload"] in (None, {})

    bot_texts = [call.args[1] for call in chatwoot.send_bot_message.await_args_list]
    assert "请先完成或取消当前表单" not in bot_texts
    assert operation_cancelled_text() not in bot_texts
    # Must not re-push the abandoned update_address form.
    assert not any(f.get("tool") == "update_address" for f in form_sends)

    profile = repo.get_user_profile("u1")
    assert profile["address"] == address_before
    assert profile["phone"] == phone_before
    done = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    # New turn may open a phone form interrupt, or clear; either way address interrupt is gone.
    if done.interrupts:
        assert done.interrupts[0].value.get("tool") != "update_address"


@pytest.mark.asyncio
async def test_stale_form_submit_after_implicit_cancel_does_not_dirty_write(
    tmp_path, monkeypatch
):
    """UX-07: after Implicit Cancel cleared interrupt, stale form submit must not write."""
    status, mapping, _conn = _contact_first_stores(tmp_path)
    chatwoot = AsyncMock()
    faq_reply = "这是 FAQ 的离线回复（fake model）。"
    graph, repo = _agent_bot_graph(
        tmp_path,
        monkeypatch,
        db_name="ic-stale",
        responses=[
            router_json_message("account_profile_update", 0.9),
            select_json_message("update_address", {}),
            router_json_message("faq", 0.9),
            AIMessage(content=faq_reply),
            # Stale form submit becomes a normal turn (no interrupt) — spare FAQ reply.
            router_json_message("faq", 0.9),
            AIMessage(content="请重新发起修改地址。"),
        ],
    )
    contact = {"id": 74, "identifier": "u1", "name": "郑军"}
    cw_conv_id = 9740

    open_result = await handle_agent_bot_message(
        _incoming_text_payload(
            message_id="ic-stale-open",
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
    before = repo.get_user_profile("u1")["address"]

    await handle_agent_bot_message(
        _incoming_text_payload(
            message_id="ic-stale-faq",
            cw_conv_id=cw_conv_id,
            contact=contact,
            content="退货政策是什么",
        ),
        chatwoot,
        graph=graph,
        status=status,
        mapping=mapping,
    )
    cleared = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    assert not cleared.interrupts

    chatwoot.reset_mock()
    stale_address = "过期表单脏写地址 99 号"
    await handle_agent_bot_message(
        _form_submit_payload(
            message_id="ic-stale-submit",
            cw_conv_id=cw_conv_id,
            contact=contact,
            submitted_values={
                "action": "confirm",
                "order_id": "ORD-PAID-1",
                "address": stale_address,
            },
        ),
        chatwoot,
        graph=graph,
        status=status,
        mapping=mapping,
    )

    assert repo.get_user_profile("u1")["address"] == before
    assert repo.get_user_profile("u1")["address"] != stale_address
    done = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    write_tools = [
        m
        for m in done.values.get("messages", [])
        if getattr(m, "type", "") == "tool" and getattr(m, "name", "") == "update_address"
    ]
    assert write_tools == []


@pytest.mark.asyncio
async def test_form_rejected_does_not_trigger_implicit_cancel(tmp_path, monkeypatch):
    """UX-04: form_rejected re-push stays same session; no cancel-then-new-turn."""
    import ai_service_agent.harness.agent_bot as agent_bot_mod

    status, mapping, _conn = _contact_first_stores(tmp_path)
    chatwoot = AsyncMock()
    graph, repo = _agent_bot_graph(
        tmp_path,
        monkeypatch,
        db_name="ic-rejected",
        responses=[
            router_json_message("account_profile_update", 0.9),
            select_json_message("update_address", {}),
        ],
    )
    contact = {"id": 75, "identifier": "u1", "name": "郑军"}
    cw_conv_id = 9750

    open_result = await handle_agent_bot_message(
        _incoming_text_payload(
            message_id="ic-rej-open",
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
    before = repo.get_user_profile("u1")["address"]

    stream_calls: list[dict] = []
    real_event_stream = agent_bot_mod.event_stream

    async def capturing_event_stream(*args, **kwargs):
        stream_calls.append({"resume_payload": kwargs.get("resume_payload")})
        async for event in real_event_stream(*args, **kwargs):
            yield event

    monkeypatch.setattr(agent_bot_mod, "event_stream", capturing_event_stream)
    chatwoot.reset_mock()

    await handle_agent_bot_message(
        _form_submit_payload(
            message_id="ic-rej-bad",
            cw_conv_id=cw_conv_id,
            contact=contact,
            submitted_values={
                "action": "confirm",
                "order_id": "ORD-CANCEL-1",
                "address": "不可写的新地址",
            },
        ),
        chatwoot,
        graph=graph,
        status=status,
        mapping=mapping,
    )

    assert len(stream_calls) == 1
    assert stream_calls[0]["resume_payload"]["action"] == "confirm"
    assert repo.get_user_profile("u1")["address"] == before
    still = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    assert still.interrupts
    assert still.interrupts[0].value["form_session_id"] == form_session_id
    chatwoot.send_interactive_message.assert_awaited()
    assert chatwoot.send_interactive_message.await_args.args[2] == "form"
