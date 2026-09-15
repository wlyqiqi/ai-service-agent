import asyncio
import sqlite3

import httpx

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from ai_service_agent.harness.graph import make_ai_graph
from ai_service_agent.harness.messages import MessageStore
from ai_service_agent.harness.server import create_app
from ai_service_agent.harness.session import ConversationStatusStore
from ai_service_agent.testing import ToolCallingFakeModel


class RaceKeywordModel(ToolCallingFakeModel):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        user = next((m.content for m in reversed(messages) if getattr(m, "type", "") == "human"), "")
        payload = '{"capability": "unknown", "risk_level": "read", "confidence": 0.2}' if "转人工" in user else '{"capability": "faq", "risk_level": "read", "confidence": 0.9}'
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=payload))])


def test_resolve_and_chat_race_no_double_resume(tmp_path):
    db = str(tmp_path / "race.db")
    app = create_app(db, model=RaceKeywordModel(responses=[]))

    # 先造一个真实挂起中断（/chat 转人工）
    from fastapi.testclient import TestClient
    client = TestClient(app)
    session_id = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    with client.stream("POST", "/chat", json={"session_id": session_id, "message": "我要转人工"}) as r:
        assert "handoff_created" in "".join(r.iter_text())

    conn = sqlite3.connect(db)
    handoff_id = conn.execute("SELECT handoff_id FROM handoff_record WHERE conversation_id=?", (session_id,)).fetchone()[0]
    thread_id = ConversationStatusStore(conn).get_thread_id(session_id)
    conn.close()

    async def race():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            # 同时发起：resolve 主动 resume vs /chat 入口兜底 resume
            resolve_task = c.post(f"/handoffs/{handoff_id}/resolve")
            chat_ctx = c.stream("POST", "/chat", json={"session_id": session_id, "message": "在吗"})
            chat_resp = await chat_ctx.__aenter__()
            resolve_resp = await resolve_task
            chat_text = "".join([chunk async for chunk in chat_resp.aiter_text()])
            await chat_ctx.__aexit__(None, None, None)
            return resolve_resp, chat_text

    resolve_resp, chat_text = asyncio.run(race())
    assert resolve_resp.status_code == 200
    assert resolve_resp.json()["mode"] == "ai"
    assert '"done"' in chat_text          # /chat 正常结束
    assert '"error"' not in chat_text     # 无异常事件

    # 最终一致性：无挂起中断、execution_status=completed、mode=ai
    conn = sqlite3.connect(db)
    assert ConversationStatusStore(conn).get_mode(session_id) == "ai"
    conn.close()

    async def final_state():
        async with AsyncSqliteSaver.from_conn_string(db) as saver:
            graph = make_ai_graph(RaceKeywordModel(responses=[]), saver)
            st = await graph.aget_state({"configurable": {"thread_id": thread_id}})
            return st.interrupts, st.values.get("execution_status")

    interrupts, status = asyncio.run(final_state())
    assert not interrupts
    assert status == "completed"

    # 外层文案只落一次（不因并发双重持久化）
    conn = sqlite3.connect(db)
    rows = MessageStore(conn).list(session_id)
    conn.close()
    notices = [r for r in rows if r["role"] == "assistant" and "转接人工" in r["content"]]
    assert len(notices) == 1
