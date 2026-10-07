"""Chatwoot AgentBot webhook handler（Transport/harness 层）。"""

from typing import Any

from ai_service_agent.harness.chatwoot import ChatwootClient


async def handle_agent_bot_message(
    payload: dict[str, Any],
    chatwoot: ChatwootClient,
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

    response_text = f"[AgentBot] Received: {content.strip()}"
    await chatwoot.send_bot_message(str(cw_conv_id), response_text)
    return {"chatwoot_conversation_id": cw_conv_id, "response": response_text}
