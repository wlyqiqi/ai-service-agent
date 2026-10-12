"""Chatwoot AgentBot webhook handler（Transport/harness 层）。"""

import uuid
from typing import Any

from langchain_core.messages import HumanMessage

from ai_service_agent.harness.chatwoot import ChatwootClient, chatwoot_message_id
from ai_service_agent.graph.workflows.copy import FIELD_LABELS
from ai_service_agent.harness.form_events import is_form_interrupt
from ai_service_agent.harness.resume import approval_dispatch, resolve_resume_payload
from ai_service_agent.harness.streaming import event_stream


def _normalize_submitted_values(value: Any) -> dict[str, Any] | None:
    """兼容 Chatwoot form 提交的 object 与 widget/agent-bot 的 [{name, value}] 数组。"""
    if isinstance(value, dict):
        return value
    if not isinstance(value, list):
        return None
    normalized: dict[str, Any] = {}
    for item in value:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("title")
        if name is not None and "value" in item:
            normalized[str(name)] = item["value"]
    return normalized or None


async def handle_agent_bot_message(
    payload: dict[str, Any],
    chatwoot: ChatwootClient,
    graph=None,
    status=None,
    messages_store=None,
    mapping=None,
    profile_loader=None,
    orders_loader=None,
    on_interrupt=None,
    thread_lock_factory=None,
    chatwoot_lock_factory=None,
    callbacks_factory=None,
    on_handoff_created=None,
    on_approval_pending=None,
) -> dict[str, Any] | None:
    """处理 AgentBot incoming 消息，并回写 outgoing 消息。"""
    event = payload.get("event")
    if event not in ("message_created", "message_updated"):
        return None

    message_type = payload.get("message_type")
    is_form_update = (
        event == "message_updated"
        and payload.get("content_type") in ("form", "input_select")
    )
    if message_type not in ("incoming", 0) and not is_form_update:
        return None

    submitted_values = _normalize_submitted_values(
        (payload.get("content_attributes") or {}).get("submitted_values")
    )
    if event == "message_updated" and submitted_values is None:
        return None

    if submitted_values:
        if submitted_values.get("order_id") and submitted_values.get("address"):
            content = (
                f"订单 {submitted_values['order_id']} 的收货地址改成 "
                f"{submitted_values['address']}"
            )
        else:
            content = ", ".join(f"{key}={value}" for key, value in submitted_values.items())
    else:
        content = payload.get("content")
    if not isinstance(content, str) or not content.strip():
        return None

    conversation = payload.get("conversation")
    if not isinstance(conversation, dict):
        return None
    try:
        cw_conv_id = int(conversation.get("id"))
    except (TypeError, ValueError):
        return None

    contact = payload.get("contact") or (conversation.get("meta") or {}).get("sender")
    if not isinstance(contact, dict):
        contact = {}
    contact_id = contact.get("id")

    if chatwoot_lock_factory is not None:
        conversation_lock = await chatwoot_lock_factory(f"chatwoot:{cw_conv_id}")
    else:
        conversation_lock = None

    if conversation_lock is not None:
        async with conversation_lock:
            return await _handle_locked_agent_bot_message(
                payload=payload,
                chatwoot=chatwoot,
                graph=graph,
                status=status,
                messages_store=messages_store,
                mapping=mapping,
                profile_loader=profile_loader,
                orders_loader=orders_loader,
                on_interrupt=on_interrupt,
                thread_lock_factory=thread_lock_factory,
                callbacks_factory=callbacks_factory,
                on_handoff_created=on_handoff_created,
                on_approval_pending=on_approval_pending,
                conversation=conversation,
                cw_conv_id=cw_conv_id,
                contact=contact,
                contact_id=contact_id,
                content=content.strip(),
                submitted_values=submitted_values,
            )
    return await _handle_locked_agent_bot_message(
        payload=payload,
        chatwoot=chatwoot,
        graph=graph,
        status=status,
        messages_store=messages_store,
        mapping=mapping,
        profile_loader=profile_loader,
        orders_loader=orders_loader,
        on_interrupt=on_interrupt,
        thread_lock_factory=thread_lock_factory,
        on_handoff_created=on_handoff_created,
        on_approval_pending=on_approval_pending,
        conversation=conversation,
        cw_conv_id=cw_conv_id,
        contact=contact,
        contact_id=contact_id,
        content=content.strip(),
        submitted_values=submitted_values,
    )


async def _handle_locked_agent_bot_message(
    *,
    payload: dict[str, Any],
    chatwoot: ChatwootClient,
    graph=None,
    status=None,
    messages_store=None,
    mapping=None,
    profile_loader=None,
    orders_loader=None,
    on_interrupt=None,
    thread_lock_factory=None,
    callbacks_factory=None,
    on_handoff_created=None,
    on_approval_pending=None,
    conversation: dict[str, Any],
    cw_conv_id: int,
    contact: dict[str, Any],
    contact_id: Any,
    content: str,
    submitted_values: dict[str, Any] | None,
) -> dict[str, Any] | None:
    rec = mapping.find(cw_conv_id) if mapping is not None else None
    if rec is not None:
        conversation_id = rec["conversation_id"]
        thread_id = status.get_thread_id(conversation_id) if status is not None else conversation_id
    else:
        user_id = contact.get("identifier") or f"chatwoot-{contact_id or contact.get('name') or cw_conv_id}"
        if status is not None:
            session = status.create_session(user_id)
            conversation_id = session["conversation_id"]
            thread_id = session["thread_id"]
        else:
            conversation_id = str(uuid.uuid4())
            thread_id = conversation_id
        if mapping is not None:
            mapping.put(conversation_id, cw_conv_id, contact_id)

    if status is not None and status.get_mode(conversation_id) != "ai":
        return None

    if callbacks_factory is not None:
        on_interrupt, on_handoff_created, on_approval_pending = callbacks_factory(conversation_id, thread_id)

    if messages_store is not None:
        message_id = chatwoot_message_id(payload)
        if message_id and messages_store.get_by_message_id(message_id):
            return None
        messages_store.append(conversation_id, "user", content, message_id=message_id or None)
        history = messages_store.hydrate(conversation_id)
    else:
        history = [HumanMessage(content=content)]

    clarification_data = None
    confirmation_summary = None
    final_message_id = None

    if graph is None:
        final_text = (
            f"[AgentBot] 收到表单：{content}"
            if submitted_values is not None
            else f"[AgentBot] Received: {content}"
        )
    else:
        config = {"configurable": {"thread_id": thread_id}}
        final_text = None
        resume_payload = None
        if getattr(graph, "checkpointer", None):
            state = await graph.aget_state(config)
            if state.interrupts:
                interrupt_value = state.interrupts[0].value
                approval_state = approval_dispatch(interrupt_value)
                if approval_state == "pending":
                    if on_approval_pending is not None:
                        await on_approval_pending(interrupt_value)
                    return None
                if approval_state == "expired":
                    resume_payload = {"result": "expired"}
                elif isinstance(interrupt_value, dict) and (
                    interrupt_value.get("type") == "confirmation_required" or is_form_interrupt(interrupt_value)
                ):
                    action = (submitted_values or {}).get("confirmation") or (submitted_values or {}).get("action")
                    if action == "confirmed":
                        resume_payload = {"action": "confirm"}
                    elif action == "canceled":
                        resume_payload = {"action": "cancel"}
                    else:
                        resume_payload = resolve_resume_payload(interrupt_value, content)
                else:
                    resume_payload = resolve_resume_payload(interrupt_value, content)

        async def run_graph():
            nonlocal final_text, clarification_data, confirmation_summary
            async for event in event_stream(
                graph,
                config,
                history,
                on_interrupt=on_interrupt,
                resume_payload=resume_payload,
                extra_state={"user_id": contact.get("identifier") or (status.get_user_id(conversation_id) if status is not None else "")},
            ):
                if event.get("type") == "done":
                    final_text = event.get("data", {}).get("final")
                    final_message_id = event.get("data", {}).get("message_id")
                elif event.get("type") == "clarification_required":
                    clarification_data = event.get("data") or {}
                elif event.get("type") == "confirmation_required":
                    confirmation_summary = event.get("data", {}).get("summary")
                elif event.get("type") == "handoff_created" and on_handoff_created is not None:
                    await on_handoff_created(event)

        if thread_lock_factory is not None:
            async with await thread_lock_factory(thread_id):
                await run_graph()
        else:
            await run_graph()

    if final_text is not None and (status is None or status.get_mode(conversation_id) == "ai"):
        await chatwoot.send_bot_message(conversation_id, final_text)
        if messages_store is not None:
            messages_store.append(conversation_id, "assistant", final_text, message_id=final_message_id)

    if clarification_data is not None:
        user_id = contact.get("identifier") or (status.get_user_id(conversation_id) if status is not None else None)
        await send_clarify_form(
            chatwoot,
            conversation_id,
            clarification_data.get("missing") or [],
            profile=profile_loader(user_id) if profile_loader else None,
            orders=orders_loader(user_id) if orders_loader else None,
        )
    if confirmation_summary is not None:
        await send_confirmation(chatwoot, conversation_id, confirmation_summary)

    return {
        "conversation_id": conversation_id,
        "chatwoot_conversation_id": cw_conv_id,
        "response": final_text,
    }


async def send_clarify_form(
    chatwoot: ChatwootClient,
    conversation_id: str,
    missing_fields: list[str],
    profile: dict | None = None,
    orders: list[dict] | None = None,
) -> None:
    """把参数澄清事件转换为 Chatwoot 原生表单。"""
    items = []

    if "order_id" in missing_fields:
        options = [
            {"label": f"{item['order_id']} · {item.get('status_display', '')}", "value": item["order_id"]}
            for item in (orders or [])
        ]
        items.append({"name": "order_id", "label": "选择订单", "type": "select", "options": options})

    if "address" in missing_fields:
        old_address = (profile or {}).get("address", "")
        items.append({
            "name": "address",
            "label": f"新收货地址（当前：{old_address}）",
            "type": "text",
            "placeholder": "请输入新地址",
        })

    if "phone" in missing_fields:
        old_phone = (profile or {}).get("phone", "")
        items.append({
            "name": "phone",
            "label": f"新手机号（当前：{old_phone}）",
            "type": "text",
            "placeholder": "请输入新手机号",
        })

    if "address" in missing_fields:
        title = "修改收货地址"
    elif "phone" in missing_fields:
        title = "修改联系电话"
    elif missing_fields:
        title = "请补充：" + "、".join(FIELD_LABELS.get(field, field) for field in missing_fields)
    else:
        title = "请补充信息"

    await chatwoot.send_interactive_message(conversation_id, title, "form", {"items": items})


async def send_confirmation(
    chatwoot: ChatwootClient,
    conversation_id: str,
    summary: str,
) -> None:
    """把确认中断转换为 Chatwoot 原生选择控件。"""
    await chatwoot.send_interactive_message(
        conversation_id,
        summary,
        "input_select",
        {"items": [
            {"title": "确认执行", "value": "confirmed"},
            {"title": "取消操作", "value": "canceled"},
        ]},
    )
