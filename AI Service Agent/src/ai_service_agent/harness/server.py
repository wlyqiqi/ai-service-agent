import json
import sqlite3

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from langchain_core.messages import HumanMessage
from pydantic import BaseModel

from ai_service_agent.graph.builder import build_graph
from ai_service_agent.harness.handoff import HandoffService
from ai_service_agent.harness.session import ConversationStatusStore
from ai_service_agent.harness.streaming import event_stream
from ai_service_agent.testing import make_fake_model


MESSAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation_message (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);
"""


class CreateSessionBody(BaseModel):
    user_id: str


class ChatBody(BaseModel):
    session_id: str
    message: str


def _resolve_model(model):
    if model == "fake" or model is None:
        return make_fake_model()
    return model


def create_app(db_path: str, model=None):
    """FastAPI 只做 Transport；业务编排留在 LangGraph（AGENTS.md §4）。"""
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute(MESSAGE_SCHEMA)
    conn.commit()
    status = ConversationStatusStore(conn)
    handoff = HandoffService(conn, status)
    graph = build_graph(_resolve_model(model), tools=[])

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
                yield {"type": "done", "data": {"final": None}}

            return _sse_response(unknown_stream())

        if status.get_mode(conv_id) == "human":
            # 人工车道：写展示记录，不触图（handoff-spec.md §1.1；真相源未决，仅展示用途）
            with conn:
                conn.execute(
                    "INSERT INTO conversation_message (conversation_id, role, content) VALUES (?, 'user', ?)",
                    (conv_id, body.message),
                )
                conn.execute(
                    "INSERT INTO conversation_message (conversation_id, role, content) VALUES (?, 'assistant', ?)",
                    (conv_id, "已转人工，坐席将尽快回复。"),
                )

            def human_stream():
                yield {"type": "done", "data": {"final": "已转人工，坐席将尽快回复。"}}

            return _sse_response(human_stream())

        async def ai_stream():
            cb = handoff.make_interrupt_callback(conv_id)
            async for ev in event_stream(
                graph, {"thread_id": thread_id}, HumanMessage(content=body.message), on_interrupt=cb
            ):
                if ev["type"] == "done" and status.get_mode(conv_id) != "ai":
                    # 发送前实时复检：已被切走则丢弃（handoff-spec.md §1.4）
                    yield {"type": "error", "data": {"code": "ModeChanged", "message": "AI 回复已丢弃（会话已转人工）"}}
                    yield {"type": "done", "data": {"final": None}}
                    return
                yield ev

        return _sse_response(ai_stream())

    @app.post("/handoffs/{handoff_id}/resolve")
    def resolve(handoff_id: str):
        try:
            return handoff.resolve_handoff(handoff_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="handoff not found")

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
