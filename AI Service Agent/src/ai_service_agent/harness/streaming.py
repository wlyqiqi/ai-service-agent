from typing import AsyncGenerator, Callable

from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, ToolMessage
from langgraph.types import Command

from ai_service_agent.harness.errors import normalize_error


async def snapshot_assistant_ids(graph, config: dict) -> set:
    """D7：resume run 前快照 checkpoint 中已有 assistant message id 集合（供 pre/post diff）。"""
    try:
        state = await _aget_state(graph, config)
        return {m.id for m in state.values.get("messages", []) if isinstance(m, AIMessage) and m.id}
    except Exception:
        return set()


async def new_assistant_text_and_id(graph, config: dict, before_ids: set) -> tuple:
    """D7：run 后取末条 assistant 文本，仅当其 id 不在 before 快照（本轮新增）时返回 (text, id)；
    否则 (None, None)——不回落历史文本（P1 原则）。"""
    try:
        state = await _aget_state(graph, config)
        for m in reversed(state.values.get("messages", [])):
            if isinstance(m, AIMessage) and m.content:
                if m.id not in before_ids:
                    return (m.content if isinstance(m.content, str) else str(m.content)), m.id
                return None, None
    except Exception:
        pass
    return None, None


async def _aget_state(graph, config: dict):
    return await graph.aget_state(config)


async def event_stream(
    graph,
    config: dict,
    messages: list[BaseMessage],
    on_interrupt: Callable | None = None,
    resume_payload: dict | None = None,
    extra_state: dict | None = None,
) -> AsyncGenerator[dict, None]:
    """graph.astream -> SSE 归一化事件（event-spec.md）。

    token / tool_call / tool_result / error / done + custom 事件；done 必有且最后。
    subgraphs=True（2026-09-15 实测必要）：custom 事件只出现在子图 namespace，必须用
    3-tuple (namespace, mode, payload) 读取；__interrupt__ 同时出现在顶层与子图 namespace，
    只在顶层消费一次避免双重 on_interrupt；工具事件只从顶层 updates 的节点聚合 dict 派生
    （嵌套子图消息以聚合形式出现在顶层 read_graph/... 节点 update，避免与子图 namespace
    重复——实测同一 tool_call/ToolMessage 会在 3 层 namespace 各出现一次）。
    中断以 updates 顶层 {"__interrupt__": (Interrupt,)} 呈现，由 on_interrupt 接管。
    resume_payload 非 None 时以 Command(resume=..., update={messages}) 恢复挂起中断
    （入口层 resume 调度，graph-spec.md §3）；done.final 只取本轮新增（token 流 / diff），interrupt 或纯历史轮为 None，
    不回落历史文本（避免重复落库）。
    done 的 message_id = 本 turn 最后一条 assistant 消息的 langchain id，
    供 Message Store 对账持久化（handoff-spec.md §5.2）。
    """
    # 非 resume 首轮：messages 之外按需补业务初始 state（如 user_id，供安全预检消费）；
    # resume 轮 user_id 已随 checkpoint 恢复，仅注入 messages。
    if resume_payload:
        invocation = Command(resume=resume_payload, update={"messages": messages})
    else:
        invocation = {"messages": messages}
        if extra_state:
            invocation = {**invocation, **extra_state}
    final_text: list[str] = []
    final_message_id: str | None = None
    before_ids: set = await snapshot_assistant_ids(graph, config)
    try:
        async for _ns, mode, payload in graph.astream(
            invocation, config=config, stream_mode=["messages", "updates", "custom"], subgraphs=True
        ):
            if mode == "custom":
                if isinstance(payload, dict) and "type" in payload:
                    yield payload
            elif mode == "messages":
                chunk, _meta = payload
                # 只收顶层直接写入（safety_final 等，ns=()）与嵌套子图回复（read_graph/fallback 等，len>=2）；
                # 跳过 len==1 顶层节点内部 agent 流——subgraphs=True 时 router 的 create_agent
                # 分类 JSON 会以 ('router:<uuid>',) 形式透传，那不是回复文本（2026-09-15 实测）。
                if _ns and len(_ns) < 2:
                    continue
                # fake model 产出完整 AIMessage，真实模型产出 AIMessageChunk
                if isinstance(chunk, (AIMessage, AIMessageChunk)) and chunk.content:
                    text = chunk.content if isinstance(chunk.content, str) else str(chunk.content)
                    final_text.append(text)
                    final_message_id = chunk.id or final_message_id
                    yield {"type": "token", "data": {"text": text}}
            elif mode == "updates":
                if not _ns:  # 顶层：__interrupt__ 权威出现位置（子图 namespace 重复项忽略）
                    interrupts = payload.get("__interrupt__")
                    if interrupts and on_interrupt is not None:
                        for it in interrupts:
                            extra = await on_interrupt(it.value)
                            if extra:
                                yield extra
                    for _node_name, node_update in payload.items():
                        if _node_name == "__interrupt__" or not isinstance(node_update, dict):
                            continue
                        for msg in node_update.get("messages", []):
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
        if not final:
            # 统一 diff 语义（D7）：只取本轮新增 assistant，不回落历史文本（防重复落库）
            text, msg_id = await new_assistant_text_and_id(graph, config, before_ids)
            final = text
            final_message_id = final_message_id or msg_id
        yield {"type": "done", "data": {"final": final, "message_id": final_message_id}}
    except Exception as exc:
        yield normalize_error(exc)
        yield {"type": "done", "data": {"final": None, "message_id": None}}

