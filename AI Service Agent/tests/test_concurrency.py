import asyncio
import sqlite3

import httpx

from fastapi.testclient import TestClient

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from ai_service_agent.harness.graph import make_ai_graph
from ai_service_agent.harness.messages import MessageStore
from ai_service_agent.harness.server import create_app
from ai_service_agent.harness.session import ConversationStatusStore
from ai_service_agent.testing import ApprovalFlowFakeModel, ToolCallingFakeModel


class RaceKeywordModel(ToolCallingFakeModel):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        user = next((m.content for m in reversed(messages) if getattr(m, "type", "") == "human"), "")
        payload = '{"capability": "unknown", "risk_level": "read", "confidence": 0.2}' if "转人工" in user else '{"capability": "faq", "risk_level": "read", "confidence": 0.9}'
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=payload))])


def test_resolve_and_chat_race_no_double_processing(tmp_path):
    """P0：入口层显式转人工（无图内中断）；resolve 与 /chat 并发命中同一 thread 不破坏状态。

    原「双 resume」语义随 P0 入口层拦截失效（显式转人工不再产生图内 checkpoint 中断）；
    本测试改为验证并发下 resolve 切 ai 与 /chat 消息处理不互相破坏：mode 收敛 ai、
    无挂起中断、外层文案只落一次、后续消息正常完成。
    """
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

    # 最终一致性：mode=ai、无挂起图内中断（入口层转人工本就不产生图内中断）
    conn = sqlite3.connect(db)
    assert ConversationStatusStore(conn).get_mode(session_id) == "ai"
    conn.close()

    async def final_no_interrupt():
        async with AsyncSqliteSaver.from_conn_string(db) as saver:
            graph = make_ai_graph(RaceKeywordModel(responses=[]), saver)
            st = await graph.aget_state({"configurable": {"thread_id": thread_id}})
            return st.interrupts
    assert not asyncio.run(final_no_interrupt())

    # 外层文案只落一次（不因并发双重持久化）
    conn = sqlite3.connect(db)
    rows = MessageStore(conn).list(session_id)
    conn.close()
    notices = [r for r in rows if r["role"] == "assistant" and "转接人工" in r["content"]]
    assert len(notices) == 1

    # resolve 后正常消息继续走 AI 车道完成（execution_status=completed）
    with client.stream("POST", "/chat", json={"session_id": session_id, "message": "再问一下"}) as r:
        assert '"done"' in "".join(r.iter_text())

    async def final_completed():
        async with AsyncSqliteSaver.from_conn_string(db) as saver:
            graph = make_ai_graph(RaceKeywordModel(responses=[]), saver)
            st = await graph.aget_state({"configurable": {"thread_id": thread_id}})
            return st.values.get("execution_status")
    assert asyncio.run(final_completed()) == "completed"


def test_approval_double_resume_no_double_execution(tmp_path):
    """同一 approval_id 双 approve 并发：thread 锁 + 锁内权威 precheck -> 一个 200 一个 409，只执行一次。"""
    db = str(tmp_path / "ar.db")
    app = create_app(db, model=ApprovalFlowFakeModel(responses=[]))
    client = TestClient(app)
    session_id = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    with client.stream("POST", "/chat", json={"session_id": session_id, "message": "申请退款"}) as r:
        assert "approval_required" in "".join(r.iter_text())
    conn = sqlite3.connect(db)
    approval_id = conn.execute("SELECT approval_id FROM approval_record LIMIT 1").fetchone()[0]
    conn.close()

    async def race():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r1, r2 = await asyncio.gather(
                c.post(f"/approvals/{approval_id}/approve"),
                c.post(f"/approvals/{approval_id}/approve"),
            )
            return sorted([r1.status_code, r2.status_code])

    assert asyncio.run(race()) == [200, 409]
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT status FROM approval_record WHERE approval_id=?", (approval_id,)).fetchone()[0] == "approved"
    conn.close()
