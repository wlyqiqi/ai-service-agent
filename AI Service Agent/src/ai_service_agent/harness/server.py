import asyncio
import json
import sqlite3

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from ai_service_agent.harness.graph import make_ai_graph
from ai_service_agent.harness.handoff import HANDOFF_NOTICE, HandoffService
from ai_service_agent.harness.messages import MessageStore
from ai_service_agent.harness.resume import HANDOFF_RESUME_ACTION, resolve_resume_payload
from ai_service_agent.harness.session import ConversationStatusStore
from ai_service_agent.harness.streaming import event_stream
from ai_service_agent.testing import make_fake_model

_locks: dict[str, asyncio.Lock] = {}
_locks_guard = asyncio.Lock()


class CreateSessionBody(BaseModel):
    user_id: str


class ChatBody(BaseModel):
    session_id: str
    message: str


def _resolve_model(model):
    if model == "fake" or model is None:
        return make_fake_model()
    return model


async def _thread_lock(thread_id: str) -> asyncio.Lock:
    """同一 thread_id 的图调用串行化，杜绝双重 resume（用户并发要求）。"""
    async with _locks_guard:
        if thread_id not in _locks:
            _locks[thread_id] = asyncio.Lock()
        return _locks[thread_id]


def create_app(db_path: str, model=None):
    """FastAPI 只做 Transport；业务编排留在 LangGraph（AGENTS.md §4）。

    入口层例外：conversation_mode 轻量判断（handoff-spec.md §2）与 Message Store
    持久化 / 水合（state.messages 工作视图，handoff-spec.md §5.2）。
    """
    conn = sqlite3.connect(db_path, check_same_thread=False)
    status = ConversationStatusStore(conn)
    handoff = HandoffService(conn, status)
    messages_store = MessageStore(conn)

    app = FastAPI()

    @app.post("/sessions")
    def create_session(body: CreateSessionBody):
        info = status.create_session(body.user_id)
        return {"session_id": info["conversation_id"], "mode": info["mode"]}

    @app.post("/chat")
    def chat(body: ChatBody):
        conv_id = body.session_id
        thread_id = status.get_thread_id(conv_id)
        if thread_id is None:
            # 无效会话 -> 归一化 error + done（mvp-spec 验收 #3）
            def unknown_stream():
                yield {"type": "error", "data": {"code": "SessionNotFound", "message": "session not found"}}
                yield {"type": "done", "data": {"final": None, "message_id": None}}

            return _sse_response(unknown_stream())

        if status.get_mode(conv_id) == "human":
            # 人工车道：只写 Message Store（唯一真相源），不触图（handoff-spec.md §1.1）
            messages_store.append(conv_id, "user", body.message)
            messages_store.append(conv_id, "assistant", "已转人工，坐席将尽快回复。")

            def human_stream():
                yield {"type": "done", "data": {"final": "已转人工，坐席将尽快回复。", "message_id": None}}

            return _sse_response(human_stream())

        async def ai_stream():
            # 先持久化用户消息（即便本 turn 失败也留痕），再水合历史作为 state.messages 输入
            messages_store.append(conv_id, "user", body.message)
            history = messages_store.hydrate(conv_id)
            cb = handoff.make_interrupt_callback(conv_id)
            lock = await _thread_lock(thread_id)
            async with lock:
                async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
                    graph = make_ai_graph(_resolve_model(model), saver)
                    cfg = {"configurable": {"thread_id": thread_id}}
                    st = await graph.aget_state(cfg)
                    resume_payload = resolve_resume_payload(st.interrupts[0].value, body.message) if st.interrupts else None
                    async for ev in event_stream(graph, cfg, history, on_interrupt=cb, resume_payload=resume_payload):
                        if ev["type"] == "handoff_created":
                            # 外层文案（D3）：落库 + 发 token，图内不生成
                            messages_store.append(conv_id, "assistant", HANDOFF_NOTICE)
                            yield {"type": "token", "data": {"text": HANDOFF_NOTICE}}
                            yield ev
                            continue
                        if ev["type"] == "done":
                            mode_now = status.get_mode(conv_id)
                            if mode_now != "ai":
                                # 发送前实时复检：已被切走则丢弃且不持久化（handoff-spec.md §1.4）
                                yield {"type": "error", "data": {"code": "ModeChanged", "message": "AI 回复已丢弃（会话已转人工）"}}
                                yield {"type": "done", "data": {"final": None, "message_id": None}}
                                return
                            final = ev["data"].get("final")
                            if final is not None:
                                messages_store.append(conv_id, "assistant", final, message_id=ev["data"].get("message_id"))
                            yield ev
                            return
                        yield ev

        return _sse_response(ai_stream())

    @app.post("/handoffs/{handoff_id}/resolve")
    async def resolve(handoff_id: str):
        try:
            result = handoff.resolve_handoff(handoff_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="handoff not found")
        conv_id = result["conversation_id"]
        thread_id = status.get_thread_id(conv_id)
        lock = await _thread_lock(thread_id)
        async with lock:
            async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
                graph = make_ai_graph(_resolve_model(model), saver)
                cfg = {"configurable": {"thread_id": thread_id}}
                st = await graph.aget_state(cfg)
                if st.interrupts:
                    history = messages_store.hydrate(conv_id)
                    try:
                        await graph.ainvoke(
                            Command(resume={"action": HANDOFF_RESUME_ACTION}, update={"messages": history}),
                            config=cfg,
                        )
                    except Exception:
                        # 降级（D2）：中断保留，下一用户消息经入口 resume 调度兜底
                        pass
        return {"handoff_id": handoff_id, "mode": "ai"}

    return app


def _sse_response(events):
    return StreamingResponse(
        _sse(events),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse(events):
    async def gen():
        async for ev in _wrap(events):
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"

    return gen()


async def _wrap(events):
    if hasattr(events, "__aiter__"):
        async for e in events:
            yield e
    else:
        for e in events:
            yield e

