import json

from langchain_core.messages import ToolMessage


def read_tool_error(messages, tool_name: str) -> str | None:
    """Return the error string of the latest ``tool_name`` ToolMessage, if any.

    Write graphs run ``execute -> tools`` once per run, so only the last ToolMessage
    with a matching name matters. Content may be a JSON string or an already-decoded
    dict; a non-dict or error-less payload means success.
    """
    for message in reversed(messages or []):
        if not isinstance(message, ToolMessage) or message.name != tool_name:
            continue
        result = message.content
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except json.JSONDecodeError:
                return None
        if isinstance(result, dict) and result.get("error"):
            return result["error"]
        return None
    return None


def message_text(message) -> str:
    """Extract human-readable text from string, content-block, or message input."""
    if message is None:
        return ""
    if isinstance(message, str):
        return message

    blocks = getattr(message, "content", None)
    if blocks is None and isinstance(message, dict):
        blocks = message.get("content", "")

    if isinstance(blocks, str):
        return blocks
    if not isinstance(blocks, (list, tuple)):
        return ""

    texts = []
    for block in blocks:
        if isinstance(block, str):
            text = block
        elif isinstance(block, dict):
            text = block.get("text", "") if block.get("type", "text") == "text" else ""
        else:
            block_type = getattr(block, "type", "")
            text = getattr(block, "text", "") if block_type == "text" else ""
        if isinstance(text, str) and text.strip():
            texts.append(text)
    return "\n".join(texts)
