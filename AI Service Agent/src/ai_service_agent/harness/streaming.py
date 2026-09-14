from typing import AsyncGenerator, Callable

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage

from ai_service_agent.harness.errors import normalize_error


async def event_stream(
    graph, config: dict, message: HumanMessage, on_interrupt: Callable | None = None
) -> AsyncGenerator[dict, None]:
    """graph.astream -> SSE 归一化事件（event-spec.md）。

    token / tool_call / tool_result / error / done；done 必有且最后；
    中断以 updates 顶层 {"__interrupt__": (Interrupt,)} 呈现，由 on_interrupt 接管。
    """
    try:
        async for mode, payload in graph.astream(
            {"messages": [message]}, config=config, stream_mode=["messages", "updates"]
        ):
            if mode == "messages":
                chunk, _meta = payload
                # fake model 产出完整 AIMessage，真实模型产出 AIMessageChunk
                if isinstance(chunk, (AIMessage, AIMessageChunk)) and chunk.content:
                    yield {"type": "token", "data": {"text": chunk.content}}
            elif mode == "updates":
                interrupts = payload.get("__interrupt__")
                if interrupts and on_interrupt is not None:
                    for it in interrupts:
                        extra = await on_interrupt(it.value)
                        if extra:
                            yield extra
                for node_name, update in payload.items():
                    if node_name == "__interrupt__":
                        continue
                    for msg in update.get("messages", []):
                        if isinstance(msg, AIMessage) and msg.tool_calls:
                            for tc in msg.tool_calls:
                                yield {
                                    "type": "tool_call",
                                    "data": {"name": tc["name"], "args": tc["args"]},
                                }
                        elif isinstance(msg, ToolMessage):
                            yield {
                                "type": "tool_result",
                                "data": {"name": msg.name or "tool", "ok": True, "summary": str(msg.content)[:80]},
                            }
        final = _last_assistant_text(graph, config)
        yield {"type": "done", "data": {"final": final}}
    except Exception as exc:
        yield normalize_error(exc)
        yield {"type": "done", "data": {"final": None}}


def _last_assistant_text(graph, config: dict) -> str | None:
    try:
        state = graph.get_state(config)
        for m in reversed(state.values.get("messages", [])):
            if isinstance(m, AIMessage) and m.content:
                return m.content if isinstance(m.content, str) else str(m.content)
    except Exception:
        return None
    return None
