import asyncio
import json
import logging
import sqlite3

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pathlib import Path

from pydantic import BaseModel
from fastapi.staticfiles import StaticFiles

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from ai_service_agent.config import get_settings

from ai_service_agent.harness.approvals import (
    ApprovalAlreadyProcessed,
    ApprovalExpired,
    ApprovalNotFound,
    ApprovalService,
)
from ai_service_agent.harness.chatwoot import (
    ChatwootClient,
    ChatwootMappingStore,
    verify_chatwoot_signature,
)
from ai_service_agent.harness.graph import make_ai_graph
from ai_service_agent.harness.handoff import HANDOFF_NOTICE, HandoffService
from ai_service_agent.harness.messages import MessageStore
from ai_service_agent.harness.resume import (
    HANDOFF_RESUME_ACTION,
    approval_dispatch,
    resolve_resume_payload,
)
from ai_service_agent.harness.session import ConversationStatusStore
from ai_service_agent.harness.streaming import event_stream, new_assistant_text_and_id, snapshot_assistant_ids
from ai_service_agent.testing import make_fake_model

logger = logging.getLogger("ai_service_agent")

_locks: dict[str, asyncio.Lock] = {}
_locks_guard = asyncio.Lock()


class CreateSessionBody(BaseModel):
    user_id: str


class ChatBody(BaseModel):
    session_id: str
    message: str


class LoginBody(BaseModel):
    account: str
    password: str


class RejectBody(BaseModel):
    note: str | None = None


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


def _combined_interrupt_callback(handoff_cb, approval_cb):
    """handoff / approval 两类 interrupt 的统一 on_interrupt 分发（approval-spec §3.1）。"""

    async def on_interrupt(value):
        if isinstance(value, dict) and value.get("type") == "approval_required":
            return await approval_cb(value)
        return await handoff_cb(value)

    return on_interrupt


async def _approval_pending_events(it: dict):
    """OQ-4 hold：审批挂起提示事件（event-spec §2 approval_required data 形状）。"""
    yield {"type": "approval_required", "data": {
        "approval_id": it.get("approval_id", ""),
        "summary": it.get("summary", ""),
        "expires_at": it.get("expires_at", ""),
    }}
    yield {"type": "done", "data": {"final": None, "message_id": None}}


def create_app(
    db_path: str,
    model=None,
    simulate_approval: bool | None = None,
    chatwoot: object | None = None,
    chatwoot_webhook_secret: str | None = None,
):
    """FastAPI 只做 Transport；业务编排留在 LangGraph（AGENTS.md §4）。

    入口层例外：conversation_mode 轻量判断（handoff-spec.md §2）与 Message Store
    持久化 / 水合（state.messages 工作视图，handoff-spec.md §5.2）。
    """
    sim = get_settings().simulate_approval if simulate_approval is None else simulate_approval
    conn = sqlite3.connect(db_path, check_same_thread=False)
    status = ConversationStatusStore(conn)
    handoff = HandoffService(conn, status)
    messages_store = MessageStore(conn)
    mapping = ChatwootMappingStore(conn)
    settings_chatwoot = get_settings()
    if chatwoot is None and settings_chatwoot.chatwoot_enabled and settings_chatwoot.chatwoot_base_url \
            and settings_chatwoot.chatwoot_account_id and settings_chatwoot.chatwoot_inbox_id \
            and settings_chatwoot.chatwoot_api_token:
        chatwoot = ChatwootClient(
            base_url=settings_chatwoot.chatwoot_base_url,
            account_id=settings_chatwoot.chatwoot_account_id,
            inbox_id=settings_chatwoot.chatwoot_inbox_id,
            api_token=settings_chatwoot.chatwoot_api_token,
            mapping=mapping,
        )
    webhook_secret = chatwoot_webhook_secret or settings_chatwoot.chatwoot_webhook_secret
    approvals = ApprovalService(conn, simulate_approval=sim)

    app = FastAPI()

    @app.post("/auth/login")
    def auth_login(body: LoginBody):
        # 占位认证（user-page-spec.md §10）：待真实认证方案定案；返回 dev token，页面据此携带 Bearer
        return {
            "token": "dev-" + body.account,
            "user": {"id": body.account, "name": body.account},
        }

    @app.post("/sessions")
    def create_session(body: CreateSessionBody):
        info = status.create_session(body.user_id)
        return {"session_id": info["conversation_id"], "mode": info["mode"]}

    @app.post("/chat")
    async def chat(body: ChatBody):
        conv_id = body.session_id
        thread_id = status.get_thread_id(conv_id)
        if thread_id is None:
            # 无效会话 -> 归一化 error + done（mvp-spec 验收 #3）
            def unknown_stream():
                yield {"type": "error", "data": {"code": "SessionNotFound", "message": "session not found"}}
                yield {"type": "done", "data": {"final": None, "message_id": None}}

            return _sse_response(unknown_stream())

        if status.get_mode(conv_id) == "human":
            # 人工车道：写 Message Store（唯一真相源）+ 同步推送 Chatwoot（失败只记日志）
            messages_store.append(conv_id, "user", body.message)
            messages_store.append(conv_id, "assistant", "已转人工，坐席将尽快回复。")
            if chatwoot is not None:
                try:
                    await chatwoot.send_as_contact(conv_id, body.message)
                except Exception:
                    logger.exception("chatwoot send failed: conv=%s", conv_id)

            def human_stream():
                yield {"type": "done", "data": {"final": "已转人工，坐席将尽快回复。", "message_id": None}}

            return _sse_response(human_stream())

        async def ai_stream():
            # 先持久化用户消息（即便本 turn 失败也留痕），再水合历史作为 state.messages 输入
            messages_store.append(conv_id, "user", body.message)
            history = messages_store.hydrate(conv_id)
            cb = _combined_interrupt_callback(
                handoff.make_interrupt_callback(conv_id),
                approvals.make_interrupt_callback(conv_id, thread_id),
            )
            lock = await _thread_lock(thread_id)
            async with lock:
                async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
                    graph = make_ai_graph(_resolve_model(model), saver)
                    cfg = {"configurable": {"thread_id": thread_id}}
                    st = await graph.aget_state(cfg)
                    resume_payload = None
                    if st.interrupts:
                        it = st.interrupts[0].value
                        approval_state = approval_dispatch(it)
                        if approval_state == "pending":
                            rec = approvals.get(it.get("approval_id", "")) if it.get("approval_id") else None
                            if sim and rec is not None and rec["status"] == "approved":
                                # simulate_approval（approval-spec §5）：自动构造 approve 动作，走同一 resume 路径
                                resume_payload = {"action": "approve"}
                            else:
                                # OQ-4：审批挂起 -> 不 resume、不新起 run（用户消息已落 Message Store），保持挂起
                                async for ev in _approval_pending_events(it):
                                    yield ev
                                return
                        elif approval_state == "expired":
                            resume_payload = {"result": "expired"}   # HighRiskGraph 过期收尾
                        else:
                            resume_payload = resolve_resume_payload(it, body.message)
                    async for ev in event_stream(
                        graph, cfg, history, on_interrupt=cb,
                        resume_payload=resume_payload,
                        extra_state={"user_id": status.get_user_id(conv_id) or ""},
                    ):
                        if ev["type"] == "handoff_created":
                            # 外层文案（D3）：落库 + 发 token，图内不生成
                            messages_store.append(conv_id, "assistant", HANDOFF_NOTICE)
                            if chatwoot is not None:
                                try:
                                    await chatwoot.ensure_conversation(conv_id, HANDOFF_NOTICE)
                                except Exception:
                                    logger.exception("chatwoot ensure failed: conv=%s", conv_id)
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

    @app.get("/conversations/{conversation_id}/messages")
    def get_messages(conversation_id: str):
        if status.get_thread_id(conversation_id) is None:
            raise HTTPException(status_code=404, detail="conversation not found")
        return {"items": messages_store.list(conversation_id), "mode": status.get_mode(conversation_id)}

    @app.post("/handoffs/{handoff_id}/resolve")
    async def resolve(handoff_id: str):
        try:
            result = handoff.resolve_handoff(handoff_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="handoff not found")
        conv_id = result["conversation_id"]
        pending = approvals.pending_for_conversation(conv_id)
        if pending:
            # D9 裁定：只读信息项，仅日志/坐席展示，不影响 resolve 分支（Handoff 与 Approval 完全独立状态机）
            logger.info("resolve handoff %s: conversation %s has %d pending approval(s)", handoff_id, conv_id, len(pending))
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

    def _precheck_approval(approval_id: str) -> dict:
        try:
            return approvals.precheck(approval_id)
        except ApprovalNotFound:
            raise HTTPException(status_code=404, detail="approval not found")
        except ApprovalAlreadyProcessed:
            raise HTTPException(status_code=409, detail="approval already processed")
        except ApprovalExpired:
            raise HTTPException(status_code=410, detail="approval expired")

    async def _approval_resume(approval_id: str, resume_payload: dict, decided: str) -> dict:
        first = approvals.get(approval_id)
        if first is None:
            raise HTTPException(status_code=404, detail="approval not found")
        thread_id = first["thread_id"]
        conv_id = first["conversation_id"]
        lock = await _thread_lock(thread_id)
        async with lock:
            rec = _precheck_approval(approval_id)  # 锁内权威预检：并发双 approve 只有一个通过（D7 幂等）
            async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
                graph = make_ai_graph(_resolve_model(model), saver)
                cfg = {"configurable": {"thread_id": thread_id}}
                before_ids = await snapshot_assistant_ids(graph, cfg)  # D7：resume 前快照
                await graph.ainvoke(Command(resume=resume_payload), cfg)
                # D7：diff 取本轮新增 assistant 文本 -> Message Store（唯一真相源）
                final, final_message_id = await new_assistant_text_and_id(graph, cfg, before_ids)
                if final is not None:
                    messages_store.append(conv_id, "assistant", final, message_id=final_message_id)
            # mark_decided 在锁内（T6 裁定）：锁释放前落库，第二个并发请求的锁内 precheck 才能看到非 pending
            approvals.mark_decided(approval_id, decided, decided_by="approver")
        return {"approval_id": approval_id, "status": decided}

    @app.post("/approvals/{approval_id}/approve")
    async def approve_approval(approval_id: str):
        return await _approval_resume(approval_id, {"action": "approve"}, "approved")

    @app.post("/approvals/{approval_id}/reject")
    async def reject_approval(approval_id: str, body: RejectBody | None = None):
        resume = {"action": "reject"}
        if body is not None and body.note:
            resume["note"] = body.note
        return await _approval_resume(approval_id, resume, "rejected")

    @app.get("/approvals/{approval_id}")
    def get_approval(approval_id: str):
        rec = approvals.get(approval_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="approval not found")
        return rec

    @app.get("/approvals")
    def list_approvals(status: str | None = None):
        return {"items": approvals.list_pending() if status == "pending" else approvals.all()}

    @app.get("/handoffs")
    def list_handoffs(conversation_id: str):
        rec = handoff.get_by_conversation(conversation_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="handoff not found")
        return rec

    @app.post("/webhooks/chatwoot")
    async def chatwoot_webhook(request: Request):
        payload = await request.body()
        signature = request.headers.get("X-Chatwoot-Signature")
        timestamp = request.headers.get("X-Chatwoot-Timestamp")
        if not webhook_secret or not verify_chatwoot_signature(payload, signature, webhook_secret, timestamp):
            raise HTTPException(status_code=401, detail="invalid signature")
        try:
            data = json.loads(payload)
        except ValueError:
            return {"ok": True}
        if data.get("event") != "message_created":
            return {"ok": True}
        if data.get("message_type") != "outgoing" or data.get("private"):
            return {"ok": True}
        sender = data.get("sender") or {}
        # v4.17.1 契约：坐席(User).webhook_data.type = "user"；Contact 无 type 字段
        if sender.get("type") != "user":
            return {"ok": True}
        cw_id = (data.get("conversation") or {}).get("id")
        content = data.get("content")
        if not cw_id or not content:
            return {"ok": True}
        rec = mapping.find(int(cw_id))
        if rec is None:
            return {"ok": True}
        conv_id = rec["conversation_id"]
        if status.get_mode(conv_id) != "human":
            # 坐席回复只接受人工车道（AI 车道由图负责）
            return {"ok": True}
        msg_id = data.get("id")
        if msg_id is not None and mapping.is_runtime_message(int(msg_id)):
            # Runtime 自己创建的 Chatwoot 消息（如转人工提示首条）回显，跳过防重复落库
            return {"ok": True}
        messages_store.append(conv_id, "assistant", content)
        return {"ok": True}

    _web_dir = Path(__file__).resolve().parents[3] / "web"
    if _web_dir.exists():
        # 同源托管用户页面（user-page-spec §10）：baseUrl='' 相对路径 + file:// 强制 mock，
        # 真实模式需 Runtime 同源提供页面；API 路由已在挂载前注册，优先级不变。
        app.mount("/", StaticFiles(directory=_web_dir, html=True), name="web")

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

