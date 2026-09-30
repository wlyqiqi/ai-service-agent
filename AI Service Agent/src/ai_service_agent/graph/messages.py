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
