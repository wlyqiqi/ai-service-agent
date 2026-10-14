import asyncio
import hashlib
import hmac
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from fastapi.staticfiles import StaticFiles

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from ai_service_agent.config import get_settings
from ai_service_agent.domain.orders import OrderRepositoryUnavailable, SQLiteOrderRepository
from ai_service_agent.graph.runtime_context import GraphContext

from ai_service_agent.graph.handoff_rules import detect_handoff_intent
from ai_service_agent.graph.workflows.copy import form_plain_text_refuse_text
from ai_service_agent.harness.approvals import (
    APPROVER_USER_HEADER,
    ApprovalAlreadyProcessed,
    ApprovalExpired,
    ApprovalNotFound,
    ApprovalSeparationOfDuties,
    ApprovalService,
)
from ai_service_agent.harness.auth import UserStore, create_access_token, verify_bearer
from ai_service_agent.harness.agent_bot import handle_agent_bot_message
from ai_service_agent.harness.chatwoot import (
    ChatwootClient,
    ChatwootMappingStore,
    chatwoot_message_id,
    is_agent_message,
    verify_chatwoot_signature,
)
from ai_service_agent.harness.form_events import is_form_interrupt, project_form_interrupt
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


class NoCacheStaticFiles(StaticFiles):
    """Serve frontend updates immediately in local development."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-store"
        return response


class CreateSessionBody(BaseModel):
    user_id: str


class ChatBody(BaseModel):
    session_id: str
    message: str


class ConfirmBody(BaseModel):
    conversation_id: str
    action: str
    order_id: str | None = None
    fields: dict[str, Any] | None = None


class LoginBody(BaseModel):
    phone: str | None = None
    account: str | None = None
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


def _agent_bot_callbacks_factory(handoff, approvals, chatwoot, messages_store):
    def make_callbacks(conv_id, thread_id):
        cb = _combined_interrupt_callback(
            handoff.make_interrupt_callback(conv_id), approvals.make_interrupt_callback(conv_id, thread_id)
        )

        async def send_handoff_notice(event: dict) -> None:
            messages_store.append(conv_id, "assistant", HANDOFF_NOTICE)
            await chatwoot.send_bot_message(conv_id, HANDOFF_NOTICE)
            await chatwoot.set_conversation_status(conv_id, "open")

        async def send_approval_pending(value: dict) -> None:
            content = "高风险操作 · 人工审批中：" + (value.get("summary") or "")
            await chatwoot.send_bot_message(conv_id, content)

        return cb, send_handoff_notice, send_approval_pending

    return make_callbacks


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
    chatwoot_hmac_secret: str | None = None,
    chatwoot_agent_bot_secret: str | None = None,
    auth_enabled: bool | None = None,
    jwt_secret: str | None = None,
    profile_db_path: str | None = None,
    order_repository=None,
):
    """FastAPI 只做 Transport；业务编排留在 LangGraph（AGENTS.md §4）。

    入口层例外：conversation_mode 轻量判断（handoff-spec.md §2）与 Message Store
    持久化 / 水合（state.messages 工作视图，handoff-spec.md §5.2）。
    """
    settings = get_settings()
    resolved_profile_db_path = profile_db_path or settings.mock_ecommerce_db_path
    resolved_order_repository = order_repository or SQLiteOrderRepository(resolved_profile_db_path)
    required_auth = settings.auth_enabled if auth_enabled is None else auth_enabled
    secret = jwt_secret or settings.jwt_secret
    if required_auth and not secret:
        raise ValueError("JWT_SECRET is required when authentication is enabled")
    sim = settings.simulate_approval if simulate_approval is None else simulate_approval
    conn = sqlite3.connect(db_path, check_same_thread=False)
    users = UserStore(conn)
    status = ConversationStatusStore(conn)
    handoff = HandoffService(conn, status)
    messages_store = MessageStore(conn)
    mapping = ChatwootMappingStore(conn)
    settings_chatwoot = settings
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
    widget_hmac_secret = chatwoot_hmac_secret or settings_chatwoot.chatwoot_hmac_secret
    agent_bot_secret = chatwoot_agent_bot_secret or settings_chatwoot.chatwoot_agent_bot_secret
    approvals = ApprovalService(conn, simulate_approval=sim)

    def _runtime_context(user_id: str = "") -> GraphContext:
        return GraphContext(
            order_repository=resolved_order_repository,
            approval_service=approvals,
            user_id=user_id or "",
        )

    app = FastAPI()

    def _profile_for_agent_bot(user_id: str | None):
        if not user_id:
            return None
        try:
            return resolved_order_repository.get_user_profile(user_id)
        except OrderRepositoryUnavailable:
            return None

    def _address_editable_orders_for_agent_bot(user_id: str | None):
        if not user_id:
            return []
        try:
            orders = resolved_order_repository.list_user_orders(
                user_id, address_editable=True
            )
        except OrderRepositoryUnavailable:
            return []
        if orders is None:
            return []
        return [
            {"order_id": row["order_id"], "status_display": row["status_display"]}
            for row in orders
        ]

    def _auth_user(request: Request):
        if not required_auth:
            return None
        return verify_bearer(request.headers, secret)

    def _require_auth_user(request: Request):
        user_id = _auth_user(request)
        if not user_id:
            raise HTTPException(status_code=401, detail="authentication required")
        return user_id

    def _ensure_conversation_owner(auth_user: str | None, conversation_id: str):
        if auth_user is not None and status.get_user_id(conversation_id) != auth_user:
            raise HTTPException(status_code=403, detail="conversation forbidden")

    def _ensure_approval_owner(auth_user: str | None, record: dict):
        """Read path only: the requester may view their own approval.

        Approve/reject must not use this check. Separation of duties requires a
        different actor (see ``_decision_actor``).
        """
        if auth_user is not None and record["requester_user_id"] != auth_user:
            raise HTTPException(status_code=403, detail="approval forbidden")

    def _decision_actor(request: Request) -> str:
        """Identity recorded as decided_by on approve/reject.

        Auth on: JWT subject only. ``X-Approver-User-Id`` is ignored so the
        requester cannot impersonate someone else.
        Auth off (explicit local/test): that header is the actor. A missing
        header is anonymous and fails separation of duties.
        """
        if required_auth:
            return _require_auth_user(request)
        return (request.headers.get(APPROVER_USER_HEADER) or "").strip()

    def _chat_ownership(request: Request, body: ChatBody):
        _ensure_conversation_owner(_auth_user(request), body.session_id)

    def _confirm_ownership(request: Request, body: ConfirmBody):
        _ensure_conversation_owner(_auth_user(request), body.conversation_id)

    def _conversation_ownership(request: Request, conversation_id: str):
        _ensure_conversation_owner(_auth_user(request), conversation_id)

    def _handoff_ownership(request: Request, handoff_id: str):
        record = handoff.get(handoff_id)
        if record is not None:
            _ensure_conversation_owner(_auth_user(request), record["conversation_id"])

    def _approval_ownership(request: Request, approval_id: str):
        record = approvals.get(approval_id)
        if record is not None:
            _ensure_approval_owner(_auth_user(request), record)

    async def _emit_handoff(conv_id: str, ev: dict):
        """统一 handoff side-effect path（P0 D3）：落 NOTICE + ensure Chatwoot（失败只记日志）+ 事件流。

        图内动态 handoff（interrupt 建 record 后）与入口显式 handoff 共用；
        事件顺序 token(HANDOFF_NOTICE) -> handoff_created，与 event-spec / 页面 contract 一致。
        """
        messages_store.append(conv_id, "assistant", HANDOFF_NOTICE)
        if chatwoot is not None:
            try:
                await chatwoot.ensure_conversation(conv_id, HANDOFF_NOTICE)
            except Exception:
                logger.exception("chatwoot ensure failed: conv=%s", conv_id)
        yield {"type": "token", "data": {"text": HANDOFF_NOTICE}}
        yield ev

    async def _sync_chatwoot_agent_messages(conv_id: str) -> None:
        """Resolve 前兜底同步；webhook 延迟或丢失时仍能补齐人工上下文。"""
        if chatwoot is None:
            return
        try:
            for message in await chatwoot.list_messages(conv_id):
                if not is_agent_message(message):
                    continue
                try:
                    raw_message_id = int(message["id"])
                    message_id = chatwoot_message_id(message)
                except (KeyError, TypeError, ValueError, OverflowError):
                    continue
                if mapping.is_runtime_message(raw_message_id):
                    continue
                messages_store.append_if_absent(message_id, conv_id, "assistant", message["content"])
        except Exception:
            logger.exception("chatwoot resolve sync failed: conv=%s", conv_id)

    @app.post("/auth/login")
    def auth_login(body: LoginBody):
        phone = body.phone or body.account
        if not phone:
            raise HTTPException(status_code=422, detail="phone is required")
        if not secret:
            raise HTTPException(status_code=503, detail="JWT signing is unavailable")
        user = users.authenticate(phone, body.password)
        if user is None:
            raise HTTPException(status_code=401, detail="invalid credentials")
        token = create_access_token(user["user_id"], secret, settings.jwt_expires_minutes)
        return {"token": token, "user": {"id": user["user_id"], "name": user["name"]}}

    @app.get("/config")
    def frontend_config():
        """用户页 Widget 所需的非敏感运行时配置。"""
        return {
            "chatwoot_base_url": settings_chatwoot.chatwoot_base_url,
            "chatwoot_website_token": settings_chatwoot.chatwoot_website_token,
        }

    @app.get("/me/chatwoot_identity")
    def chatwoot_identity(request: Request):
        """认证用户的 Widget identity；避免公开 website token 可伪造 user_id。"""
        if not widget_hmac_secret:
            raise HTTPException(status_code=503, detail="Chatwoot identity signing is unavailable")
        user_id = _require_auth_user(request)
        digest = hmac.new(widget_hmac_secret.encode(), user_id.encode(), hashlib.sha256).hexdigest()
        return {"identifier": user_id, "identifier_hash": digest}

    @app.get("/me/profile")
    def me_profile(request: Request):
        """Read-only presentation data for frontend form old-info display."""
        user_id = _require_auth_user(request)
        try:
            profile = resolved_order_repository.get_user_profile(user_id)
        except OrderRepositoryUnavailable as exc:
            raise HTTPException(status_code=503, detail="profile data source unavailable") from exc
        if profile is None:
            raise HTTPException(status_code=404, detail="profile not found")
        return profile

    @app.get("/me/orders")
    def me_orders(request: Request, address_editable: bool = False):
        """Read-only order options for frontend forms."""
        user_id = _require_auth_user(request)
        try:
            orders = resolved_order_repository.list_user_orders(
                user_id, address_editable=address_editable
            )
        except OrderRepositoryUnavailable as exc:
            raise HTTPException(status_code=503, detail="profile data source unavailable") from exc
        if orders is None:
            raise HTTPException(status_code=404, detail="profile not found")
        return {
            "user_id": user_id,
            "address_editable": address_editable,
            "orders": orders,
            "count": len(orders),
        }

    @app.post("/sessions")
    def create_session(body: CreateSessionBody, request: Request):
        auth_user = _auth_user(request)
        if auth_user is not None and body.user_id != auth_user:
            raise HTTPException(status_code=403, detail="user mismatch")
        info = status.create_session(body.user_id)
        return {"session_id": info["conversation_id"], "mode": info["mode"]}

    @app.post("/chat")
    async def chat(body: ChatBody, _: None = Depends(_chat_ownership)):
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
            if chatwoot is not None:
                try:
                    await chatwoot.send_as_contact(conv_id, body.message)
                except Exception:
                    logger.exception("chatwoot send failed: conv=%s", conv_id)

            def human_stream():
                yield {"type": "done", "data": {"final": "已转人工，坐席将尽快回复。", "message_id": None}}

            return _sse_response(human_stream())

        if detect_handoff_intent(body.message):
            # P0 D1：显式转人工 -> 不进 Router/Guard/Graph，直接走统一 handoff side-effect path。
            # 先落 user 消息（Message Store 唯一真相源，handoff-spec §5.2），再 request_handoff（幂等）。
            messages_store.append(conv_id, "user", body.message)
            ev = handoff.request_handoff(conv_id, "用户显式请求转人工", body.message[:80])

            async def explicit_handoff_stream():
                async for out in _emit_handoff(conv_id, ev):
                    yield out
                yield {"type": "done", "data": {"final": HANDOFF_NOTICE, "message_id": None}}

            return _sse_response(explicit_handoff_stream())

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
                    graph = make_ai_graph(_resolve_model(model), saver, order_repository=resolved_order_repository)
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
                        elif is_form_interrupt(it):
                            # 纯文本不是 Form Resume。挂起仍以 checkpoint interrupt 为准；
                            # 重放 form_* 投影，并给出 Form-Only Write 友好引导。
                            tip = form_plain_text_refuse_text()
                            yield {"type": "token", "data": {"text": tip}}
                            projected = project_form_interrupt(st.interrupts[0])
                            if projected:
                                yield projected
                            messages_store.append(conv_id, "assistant", tip)
                            yield {"type": "done", "data": {"final": tip, "message_id": None}}
                            return
                        else:
                            resume_payload = resolve_resume_payload(it, body.message)
                    user_id = status.get_user_id(conv_id) or ""
                    async for ev in event_stream(
                        graph, cfg, history, on_interrupt=cb,
                        resume_payload=resume_payload,
                        extra_state={"user_id": user_id},
                        context=_runtime_context(user_id),
                    ):
                        if ev["type"] == "handoff_created":
                            # 外层文案（D3）：复用统一 side-effect path（落 NOTICE + Chatwoot ensure + 事件流）
                            async for out in _emit_handoff(conv_id, ev):
                                yield out
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

    @app.post("/actions/confirm")
    async def action_confirm(body: ConfirmBody, _: None = Depends(_confirm_ownership)):
        """页面确认按钮的 Transport 入口；Graph 编排与 checkpoint resume 仍在图内。"""
        conv_id = body.conversation_id
        thread_id = status.get_thread_id(conv_id)
        if thread_id is None:
            raise HTTPException(status_code=404, detail="conversation not found")
        if body.action not in ("confirm", "confirmed", "cancel", "canceled"):
            raise HTTPException(status_code=422, detail="invalid action")
        normalized = "confirm" if body.action in ("confirm", "confirmed") else "cancel"
        if status.get_mode(conv_id) != "ai":
            raise HTTPException(status_code=409, detail="conversation is not in ai mode")

        lock = await _thread_lock(thread_id)
        async with lock:
            async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
                graph = make_ai_graph(_resolve_model(model), saver, order_repository=resolved_order_repository)
                cfg = {"configurable": {"thread_id": thread_id}}
                state = await graph.aget_state(cfg)
                pending = state.interrupts[0].value if state.interrupts else None
                if not isinstance(pending, dict) or (
                    pending.get("type") != "confirmation_required" and not is_form_interrupt(pending)
                ):
                    raise HTTPException(status_code=409, detail="no pending confirmation")

                events: list[dict] = []
                final: str | None = None
                message_id: str | None = None
                failed = False
                resume_payload: dict[str, Any] = {"action": normalized}
                if normalized == "confirm":
                    for key, value in (body.fields or {}).items():
                        if key not in ("action", "request_id"):
                            resume_payload[key] = value
                    if body.order_id is not None:
                        resume_payload["order_id"] = body.order_id
                async for event in event_stream(
                    graph,
                    cfg,
                    [],
                    resume_payload=resume_payload,
                    context=_runtime_context(status.get_user_id(conv_id) or ""),
                ):
                    if event.get("type") == "done":
                        final = event.get("data", {}).get("final")
                        message_id = event.get("data", {}).get("message_id")
                    elif event.get("type") == "error":
                        failed = True
                    events.append(event)
                if final is not None:
                    messages_store.append(conv_id, "assistant", final, message_id=message_id)
        if failed:
            raise HTTPException(status_code=502, detail="resume execution failed")
        return {"ok": True, "action": normalized, "final": final, "events": events}

    @app.get("/conversations/{conversation_id}/messages")
    def get_messages(conversation_id: str, _: None = Depends(_conversation_ownership)):
        if status.get_thread_id(conversation_id) is None:
            raise HTTPException(status_code=404, detail="conversation not found")
        return {"items": messages_store.list(conversation_id), "mode": status.get_mode(conversation_id)}

    @app.post("/handoffs/{handoff_id}/resolve")
    async def resolve(handoff_id: str, _: None = Depends(_handoff_ownership)):
        pending_handoff = handoff.get(handoff_id)
        if pending_handoff is None:
            raise HTTPException(status_code=404, detail="handoff not found")

        conv_id = pending_handoff["conversation_id"]
        if pending_handoff["status"] != "active":
            return {"handoff_id": handoff_id, "mode": "ai"}
        if status.get_mode(conv_id) == "human":
            await _sync_chatwoot_agent_messages(conv_id)

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
        if chatwoot is not None:
            try:
                await chatwoot.set_conversation_status(conv_id, "pending")
            except Exception:
                logger.exception("chatwoot restore pending failed: conv=%s", conv_id)
        lock = await _thread_lock(thread_id)
        async with lock:
            async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
                graph = make_ai_graph(_resolve_model(model), saver, order_repository=resolved_order_repository)
                cfg = {"configurable": {"thread_id": thread_id}}
                st = await graph.aget_state(cfg)
                if st.interrupts:
                    history = messages_store.hydrate(conv_id)
                    try:
                        await graph.ainvoke(
                            Command(resume={"action": HANDOFF_RESUME_ACTION}, update={"messages": history}),
                            config=cfg,
                            context=_runtime_context(status.get_user_id(conv_id) or ""),
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

    async def _approval_resume(approval_id: str, resume_payload: dict, decided: str, decided_by: str) -> dict:
        first = approvals.get(approval_id)
        if first is None:
            raise HTTPException(status_code=404, detail="approval not found")
        thread_id = first["thread_id"]
        conv_id = first["conversation_id"]
        lock = await _thread_lock(thread_id)
        async with lock:
            rec = _precheck_approval(approval_id)  # 锁内权威预检：并发双 approve 只有一个通过（D7 幂等）
            # SoD after precheck so expired/processed stay 410/409. Empty or
            # self identity never reaches the graph or a terminal status.
            try:
                approvals.mark_decided(approval_id, decided, decided_by=decided_by)
            except ApprovalSeparationOfDuties:
                raise HTTPException(status_code=403, detail="approval separation of duties")
            # mark_decided 在图执行前（T6 裁定）：即使执行中崩溃，审批状态已终态，不会重试重复执行
            async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
                graph = make_ai_graph(_resolve_model(model), saver, order_repository=resolved_order_repository)
                cfg = {"configurable": {"thread_id": thread_id}}
                before_ids = await snapshot_assistant_ids(graph, cfg)  # D7：resume 前快照
                await graph.ainvoke(
                    Command(resume=resume_payload),
                    cfg,
                    context=_runtime_context(status.get_user_id(conv_id) or ""),
                )
                # D7：diff 取本轮新增 assistant 文本 -> Message Store（唯一真相源）
                final, final_message_id = await new_assistant_text_and_id(graph, cfg, before_ids)
                if final is not None:
                    messages_store.append(conv_id, "assistant", final, message_id=final_message_id)
                    if chatwoot is not None:
                        await chatwoot.send_bot_message(conv_id, final)
        return {"approval_id": approval_id, "status": decided}

    @app.post("/approvals/{approval_id}/approve")
    async def approve_approval(approval_id: str, request: Request):
        return await _approval_resume(
            approval_id, {"action": "approve"}, "approved", _decision_actor(request)
        )

    @app.post("/approvals/{approval_id}/reject")
    async def reject_approval(approval_id: str, request: Request, body: RejectBody | None = None):
        resume = {"action": "reject"}
        if body is not None and body.note:
            resume["note"] = body.note
        return await _approval_resume(approval_id, resume, "rejected", _decision_actor(request))

    @app.get("/approvals/{approval_id}")
    def get_approval(approval_id: str, _: None = Depends(_approval_ownership)):
        rec = approvals.get(approval_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="approval not found")
        return rec

    @app.get("/approvals")
    def list_approvals(request: Request, status: str | None = None):
        auth_user = _auth_user(request)
        items = approvals.list_pending() if status == "pending" else approvals.all()
        if auth_user is not None:
            items = [item for item in items if item["requester_user_id"] == auth_user]
        return {"items": items}

    @app.get("/handoffs")
    def list_handoffs(conversation_id: str, _: None = Depends(_conversation_ownership)):
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
        except (UnicodeDecodeError, ValueError):
            return {"ok": True}
        if not isinstance(data, dict) or data.get("event") != "message_created" or not is_agent_message(data):
            return {"ok": True}

        conversation = data.get("conversation")
        if not isinstance(conversation, dict):
            return {"ok": True}
        try:
            cw_id = int(conversation.get("id"))
        except (TypeError, ValueError):
            return {"ok": True}

        rec = mapping.find(cw_id)
        if rec is None:
            return {"ok": True}
        conv_id = rec["conversation_id"]
        if status.get_mode(conv_id) != "human":
            return {"ok": True}

        try:
            msg_id = int(data.get("id"))
        except (TypeError, ValueError):
            return {"ok": True}
        if mapping.is_runtime_message(msg_id):
            return {"ok": True}

        messages_store.append_if_absent(
            chatwoot_message_id(data),
            conv_id,
            "assistant",
            data["content"],
        )
        return {"ok": True}

    _web_dir = Path(__file__).resolve().parents[3] / "web"
    @app.post("/webhooks/agent_bot")
    async def agent_bot_webhook(request: Request):
        payload = await request.body()
        signature = request.headers.get("X-Chatwoot-Signature")
        timestamp = request.headers.get("X-Chatwoot-Timestamp")
        if not agent_bot_secret or not verify_chatwoot_signature(payload, signature, agent_bot_secret, timestamp):
            raise HTTPException(status_code=401, detail="invalid signature")
        try:
            data = json.loads(payload)
        except (UnicodeDecodeError, ValueError):
            return {"ok": True}
        if chatwoot is not None:
            if not isinstance(data, dict):
                return {"ok": True}
            conversation = data.get("conversation")
            if not isinstance(conversation, dict):
                return {"ok": True}
            try:
                int(conversation.get("id"))
            except (TypeError, ValueError):
                return {"ok": True}

            async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
                graph = make_ai_graph(_resolve_model(model), saver, order_repository=resolved_order_repository)
                await handle_agent_bot_message(
                    data,
                    chatwoot,
                    graph=graph,
                    status=status,
                    messages_store=messages_store,
                    mapping=mapping,
                    profile_loader=_profile_for_agent_bot,
                    orders_loader=_address_editable_orders_for_agent_bot,
                    thread_lock_factory=_thread_lock,
                    chatwoot_lock_factory=_thread_lock,
                    callbacks_factory=_agent_bot_callbacks_factory(handoff, approvals, chatwoot, messages_store),
                    context=_runtime_context(),
                )
        return {"ok": True}
    if _web_dir.exists():
        # 同源托管 web/ 静态资源（如 landing.html）；自建 SSE 用户页 index.html 已下线。
        # API 路由已在挂载前注册，优先级不变。
        app.mount("/", NoCacheStaticFiles(directory=_web_dir, html=True), name="web")

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
