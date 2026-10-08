"""Chatwoot AgentBot webhook handler（Transport/harness 层）。"""

import uuid
from typing import Any

from langchain_core.messages import HumanMessage

from ai_service_agent.harness.chatwoot import ChatwootClient
from ai_service_agent.harness.streaming import event_stream


async def handle_agent_bot_message(
    payload: dict[str, Any],
    chatwoot: ChatwootClient,
    graph=None,
    status=None,
    messages_store=None,
    mapping=None,
) -> dict[str, Any] | None:
    """处理 AgentBot incoming 消息，并回写 outgoing 消息。"""
    if payload.get("event") != "message_created":
        return None

    message_type = payload.get("message_type")
    if message_type not in ("incoming", 0):
        return None

    content = payload.get("content")
    if not isinstance(content, str) or not content.strip():
        return None

    conversation = payload.get("conversation") or {}
    cw_conv_id = conversation.get("id")
    if not cw_conv_id:
        return None

    contact = payload.get("contact") or {}
    contact_id = contact.get("id")
    rec = mapping.find(int(cw_conv_id)) if mapping is not None else None
    if rec is not None:
        conversation_id = rec["conversation_id"]
        thread_id = status.get_thread_id(conversation_id) if status is not None else conversation_id
    else:
        user_id = f"chatwoot-{contact_id or contact.get('name') or cw_conv_id}"
        if status is not None:
            session = status.create_session(user_id)
            conversation_id = session["conversation_id"]
            thread_id = session["thread_id"]
        else:
            conversation_id = str(uuid.uuid4())
            thread_id = conversation_id
        if mapping is not None:
            mapping.put(conversation_id, int(cw_conv_id), contact_id)

    if status is not None and status.get_mode(conversation_id) != "ai":
        return None

    if messages_store is not None:
        messages_store.append(conversation_id, "user", content.strip())
        history = messages_store.hydrate(conversation_id)
    else:
        history = [HumanMessage(content=content.strip())]

    if graph is None:
        final_text = f"[AgentBot] Received: {content.strip()}"
    else:
        config = {"configurable": {"thread_id": thread_id}}
        final_text = None
        async for event in event_stream(
            graph,
            config,
            history,
            extra_state={"user_id": status.get_user_id(conversation_id) if status is not None else ""},
        ):
            if event.get("type") == "done":
                final_text = event.get("data", {}).get("final")

    if final_text is not None:
        await chatwoot.send_bot_message(conversation_id, final_text)
        if messages_store is not None:
            messages_store.append(conversation_id, "assistant", final_text)
    return {
        "conversation_id": conversation_id,
        "chatwoot_conversation_id": cw_conv_id,
        "response": final_text,
    }
