from typing import AsyncGenerator, Callable

from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, ToolMessage
from langgraph.types import Command

from ai_service_agent.harness.errors import normalize_error


async def event_stream(
    graph,
    config: dict,
    messages: list[BaseMessage],
    on_interrupt: Callable | None = None,
    resume_payload: dict | None = None,
) -> AsyncGenerator[dict, None]:
    """graph.astream -> SSE 归一化事件（event-spec.md）。

    token / tool_call / tool_result / error / done；done 必有且最后；
    中断以 updates 顶层 {"__interrupt__": (Interrupt,)} 呈现，由 on_interrupt 接管。
    resume_payload 非 None 时以 Command(resume=..., update={messages}) 恢复挂起中断
    （入口层 resume 调度，graph-spec.md §3）；resume 的 final 只取本轮流式增量，
    为空则 None（不回落历史文本，避免重复落库）。
    done 的 message_id = 本 turn 最后一条 assistant 消息的 langchain id，
    供 Message Store 对账持久化（handoff-spec.md §5.2）。
    """
    invocation = Command(resume=resume_payload, update={"messages": messages}) if resume_payload else {"messages": messages}
    final_text: list[str] = []
    final_message_id: str | None = None
    try:
        async for mode, payload in graph.astream(invocation, config=config, stream_mode=["messages", "updates"]):
            if mode == "messages":
                chunk, _meta = payload
                # fake model 产出完整 AIMessage，真实模型产出 AIMessageChunk
                if isinstance(chunk, (AIMessage, AIMessageChunk)) and chunk.content:
                    text = chunk.content if isinstance(chunk.content, str) else str(chunk.content)
                    final_text.append(text)
                    final_message_id = chunk.id or final_message_id
                    yield {"type": "token", "data": {"text": text}}
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
        final = "".join(final_text) or None
        if not final and not resume_payload:
            text, msg_id = await _last_assistant_text_and_id(graph, config)
            final = text
            final_message_id = final_message_id or msg_id
        yield {"type": "done", "data": {"final": final, "message_id": final_message_id}}
    except Exception as exc:
        yield normalize_error(exc)
        yield {"type": "done", "data": {"final": None, "message_id": None}}


async def _last_assistant_text_and_id(graph, config: dict) -> tuple[str | None, str | None]:
    try:
        state = await graph.aget_state(config)
        for m in reversed(state.values.get("messages", [])):
            if isinstance(m, AIMessage) and m.content:
                return (m.content if isinstance(m.content, str) else str(m.content)), m.id
    except Exception:
        pass
    return None, None
