import asyncio
import hashlib
import hmac
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from fastapi.staticfiles import StaticFiles

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from ai_service_agent.config import get_settings
from ai_service_agent.domain.orders import OrderRepositoryUnavailable, SQLiteOrderRepository
from ai_service_agent.graph.runtime_context import GraphContext

from ai_service_agent.graph.workflows.copy import (
    handoff_cancel_confirm_text,
    handoff_form_cancelled_customer_text,
    handoff_form_cancelled_seat_note_text,
    handoff_request_private_note_text,
    handoff_returned_to_bot_text,
    human_service_header_text,
)
from ai_service_agent.harness.approvals import ApprovalService
from ai_service_agent.harness.auth import UserStore, create_access_token, verify_bearer
from ai_service_agent.harness.agent_bot import handle_agent_bot_message
from ai_service_agent.harness.approval_side_channel import (
    ApprovalExpiredError,
    ApprovalMissing,
    ApprovalProcessed,
    ApprovalSideChannel,
    SeatAction,
    SeatAuthError,
)
from ai_service_agent.harness.outbound_gate import TurnOrigin, decide_delivery
from ai_service_agent.harness.chatwoot import (
    ChatwootClient,
    ChatwootMappingStore,
    chatwoot_message_id,
    is_agent_message,
    verify_chatwoot_signature,
)
from ai_service_agent.harness.form_events import pending_form_value
from ai_service_agent.harness.form_gate import (
    FormDecisionKind,
    FormInboundKind,
    decide_form_inbound,
)
from ai_service_agent.harness.graph import make_ai_graph
from ai_service_agent.harness.handoff import (
    HANDOFF_JOINED_NOTICE,
    STATUS_ACTIVE,
    STATUS_AWAITING,
    HandoffService,
    handoff_request_tip,
)
from ai_service_agent.harness.handoff_entry import EntryDecision, decide_entry
from ai_service_agent.harness.handoff_signal import human_takeover_signal, is_bot_error_signal
from ai_service_agent.harness.messages import MessageStore
from ai_service_agent.harness.resume import (
    HANDOFF_RESUME_ACTION,
    approval_dispatch,
    resolve_resume_payload,
)
from ai_service_agent.harness.session import ConversationStatusStore
from ai_service_agent.harness.streaming import event_stream
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


def _agent_bot_callbacks_factory(handoff, seat, chatwoot, messages_store):
    def make_callbacks(conv_id, thread_id):
        cb = _combined_interrupt_callback(
            handoff.make_interrupt_callback(conv_id),
            seat.interrupt_callback(conv_id, thread_id),
        )

        async def on_handoff_requested(event: dict) -> None:
            # created 是唯一判据：挂起期间重复申请不再打扰坐席（handoff-spec §2）。
            created = bool(event.get("created", True))
            tip = handoff_request_tip(created=created)
            data = event.get("data") or {}
            messages_store.append(conv_id, "assistant", tip)
            await chatwoot.send_bot_message(conv_id, tip)
            try:
                await chatwoot.ensure_conversation(conv_id, tip)
                if created:
                    await chatwoot.prompt_handoff_request(
                        conv_id,
                        handoff_request_private_note_text(data.get("summary") or ""),
                    )
            except Exception:
                logging.getLogger("ai_service_agent").exception(
                    "chatwoot handoff-request prompt failed: conv=%s", conv_id
                )

        async def send_approval_pending(value: dict) -> None:
            # Seat-facing private form (#64); do not push Approve/Reject to Widget.
            await seat.project(conv_id, value)

        return cb, on_handoff_requested, send_approval_pending

    return make_callbacks


async def _approval_pending_events(it: dict):
    """OQ-4 hold：审批挂起提示事件（event-spec §2 approval_required data 形状）。"""
    yield {"type": "approval_required", "data": {
        "approval_id": it.get("approval_id", ""),
        "summary": it.get("summary", ""),
        "expires_at": it.get("expires_at", ""),
    }}
    yield {"type": "done", "data": {"final": None, "message_id": None}}


# Seat HTTP badge headers（approval-spec §3.2.1）：口令 + 坐席 actor id，缺一不可。
_SEAT_CREDENTIAL_HEADER = "X-Seat-Approval-Credential"
_SEAT_ACTOR_HEADER = "X-Seat-Actor-Id"


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
    seat_approval_secret: str | None = None,
    profile_db_path: str | None = None,
    order_repository=None,
    chatwoot_revert_bot_error_open: bool | None = None,
    chatwoot_bot_error_open_grace_seconds: float | None = None,
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
    seat_secret = (
        settings.seat_approval_secret if seat_approval_secret is None else seat_approval_secret
    ) or None
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
    revert_bot_error_open = (
        settings_chatwoot.chatwoot_revert_bot_error_open
        if chatwoot_revert_bot_error_open is None
        else chatwoot_revert_bot_error_open
    )
    bot_error_open_grace_seconds = (
        settings_chatwoot.chatwoot_bot_error_open_grace_seconds
        if chatwoot_bot_error_open_grace_seconds is None
        else chatwoot_bot_error_open_grace_seconds
    )
    # Generation token so a late grace-revert cannot undo a real Take over.
    _open_watch: dict[str, int] = {}
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
        if auth_user is not None and record["requester_user_id"] != auth_user:
            raise HTTPException(status_code=403, detail="approval forbidden")

    def _seat_http(exc: Exception) -> HTTPException:
        """侧信道错误 → HTTP（模块不依赖 FastAPI）。"""
        if isinstance(exc, SeatAuthError):
            return HTTPException(status_code=403, detail="seat approval forbidden")
        if isinstance(exc, ApprovalMissing):
            return HTTPException(status_code=404, detail="approval not found")
        if isinstance(exc, ApprovalProcessed):
            return HTTPException(status_code=409, detail="approval already processed")
        if isinstance(exc, ApprovalExpiredError):
            return HTTPException(status_code=410, detail="approval expired")
        return HTTPException(status_code=500, detail="approval failed")

    def _seat_actor(request: Request):
        """Seat HTTP badge from request headers（ADR-0002 / approval-spec §3.2.1）。"""
        try:
            return seat.authenticate(
                request.headers.get(_SEAT_CREDENTIAL_HEADER) or "",
                request.headers.get(_SEAT_ACTOR_HEADER) or "",
            )
        except SeatAuthError as exc:
            raise _seat_http(exc) from exc
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

    def _approval_read_access(request: Request, approval_id: str):
        """客户只读 GET：鉴权开启时按 Requester 过滤；鉴权关闭时禁止倾倒（403）。"""
        if not required_auth:
            raise HTTPException(status_code=403, detail="approval forbidden")
        auth_user = _auth_user(request)
        record = approvals.get(approval_id)
        if record is not None:
            _ensure_approval_owner(auth_user, record)

    async def _emit_handoff_requested(conv_id: str, ev: dict):
        """Handoff Request 挂起 side-effect：tip 与坐席提示只看 ``ev["created"]``。

        ``created`` 是 Runtime 内部控制位（handoff-spec §2）：新建挂起才发强确认 +
        坐席 private note；挂起期间重复申请只发幂等短 tip。对外 SSE 事件只保留
        ``{type, data}``，控制位在此剥除。
        """
        created = bool(ev.get("created", True))
        tip = handoff_request_tip(created=created)
        data = ev.get("data") or {}
        messages_store.append(conv_id, "assistant", tip)
        if chatwoot is not None:
            try:
                await chatwoot.ensure_conversation(conv_id, tip)
                if created:
                    await chatwoot.prompt_handoff_request(
                        conv_id,
                        handoff_request_private_note_text(data.get("summary") or ""),
                    )
            except Exception:
                logger.exception("chatwoot handoff-request prompt failed: conv=%s", conv_id)
        yield {"type": "token", "data": {"text": tip}}
        yield {"type": ev.get("type", "handoff_requested"), "data": data}

    async def _resume_runner(thread_id: str, *, resume_payload, on_interrupt, user_id):
        """审批 resume 的图执行（Transport 侧）：模块只拥有顺序与门控。"""
        async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
            graph = make_ai_graph(
                _resolve_model(model), saver, order_repository=resolved_order_repository
            )
            cfg = {"configurable": {"thread_id": thread_id}}
            async for event in event_stream(
                graph,
                cfg,
                [],
                on_interrupt=on_interrupt,
                resume_payload=resume_payload,
                context=_runtime_context(user_id),
            ):
                yield event

    async def _handoff_events(conv_id: str, ev: dict) -> None:
        async for _ in _emit_handoff_requested(conv_id, ev):
            pass

    seat = ApprovalSideChannel(
        store=approvals,
        status=status,
        messages=messages_store,
        chatwoot=chatwoot,
        resume_runner=_resume_runner,
        lock_factory=_thread_lock,
        interrupt_callbacks=lambda conv_id, thread_id: _combined_interrupt_callback(
            handoff.make_interrupt_callback(conv_id),
            seat.interrupt_callback(conv_id, thread_id),
        ),
        on_handoff_requested=_handoff_events,
        seat_secret=seat_secret,
        simulate_approval=bool(sim),
    )

    async def _emit_joined_notice(conv_id: str) -> None:
        notice = HANDOFF_JOINED_NOTICE
        messages_store.append(conv_id, "assistant", notice)
        if chatwoot is not None:
            try:
                await chatwoot.ensure_conversation(conv_id, notice)
                await chatwoot.send_bot_message(conv_id, notice)
            except Exception:
                logger.exception("chatwoot joined notice failed: conv=%s", conv_id)

    async def _emit_returned_to_bot(conv_id: str) -> None:
        """交回 Bot：客户与坐席都可见的短提示（公开消息，非 private note）。"""
        tip = handoff_returned_to_bot_text()
        messages_store.append(conv_id, "assistant", tip)
        if chatwoot is None:
            return
        try:
            await chatwoot.ensure_conversation(conv_id, tip)
            await chatwoot.send_bot_message(conv_id, tip)
        except Exception:
            logger.exception("chatwoot returned-to-bot tip failed: conv=%s", conv_id)

    async def _emit_form_cancelled_on_handoff(conv_id: str) -> None:
        """进入 human 且清掉未提交表单后：客户提示 + 可选坐席 private note。"""
        tip = handoff_form_cancelled_customer_text()
        messages_store.append(conv_id, "assistant", tip)
        if chatwoot is None:
            return
        try:
            await chatwoot.send_bot_message(conv_id, tip)
            await chatwoot.send_bot_message(
                conv_id, handoff_form_cancelled_seat_note_text(), private=True
            )
        except Exception:
            logger.exception("chatwoot form-cancelled-on-handoff tip failed: conv=%s", conv_id)

    async def _cancel_pending_form_interrupt(conv_id: str) -> bool:
        """Resume a pending Mutation Form with cancel. Caller holds the thread lock.

        Hang source is the checkpointer interrupt (``pending_form_value``), not
        parent execution status. Approval and handoff interrupts are left alone.
        Raises if a form interrupt is still pending after cancel, so Takeover
        does not flip mode first.
        """
        thread_id = status.get_thread_id(conv_id)
        if thread_id is None:
            return False
        async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
            graph = make_ai_graph(
                _resolve_model(model), saver, order_repository=resolved_order_repository
            )
            cfg = {"configurable": {"thread_id": thread_id}}
            st = await graph.aget_state(cfg)
            if pending_form_value(st.interrupts) is None:
                return False
            await graph.ainvoke(
                Command(resume={"action": "cancel"}),
                config=cfg,
                context=_runtime_context(status.get_user_id(conv_id) or ""),
            )
            st_after = await graph.aget_state(cfg)
            if pending_form_value(st_after.interrupts) is not None:
                raise RuntimeError(f"form interrupt still pending after cancel: {conv_id}")
        return True

    async def _complete_handoff_resolve(handoff_id: str, *, restore_pending: bool = True) -> dict:
        """HTTP resolve、webhook resolved，以及 webhook pending（已是 pending）共用完成路径。

        restore_pending：Chatwoot 尚未 pending 时 toggle_status(pending)。
        官方交回是 status 已回到 pending，此时不得再 toggle（防环）。
        """
        pending_handoff = handoff.get(handoff_id)
        if pending_handoff is None:
            raise KeyError(f"handoff not found: {handoff_id}")
        conv_id = pending_handoff["conversation_id"]
        if pending_handoff["status"] in ("resolved", "cancelled"):
            return {
                "handoff_id": handoff_id,
                "mode": "ai",
                "conversation_id": conv_id,
                "event": {"type": "handoff_resolved", "data": {"handoff_id": handoff_id, "conversation_id": conv_id}},
            }
        was_human = status.get_mode(conv_id) == "human"
        if was_human:
            await _sync_chatwoot_agent_messages(conv_id)
        result = handoff.resolve_handoff(handoff_id)
        conv_id = result["conversation_id"]
        pending = approvals.pending_for_conversation(conv_id)
        if pending:
            logger.info(
                "resolve handoff %s: conversation %s has %d pending approval(s)",
                handoff_id,
                conv_id,
                len(pending),
            )
        thread_id = status.get_thread_id(conv_id)
        if restore_pending and chatwoot is not None:
            try:
                await chatwoot.set_conversation_status(conv_id, "pending")
            except Exception:
                logger.exception("chatwoot restore pending failed: conv=%s", conv_id)
        if thread_id is not None:
            lock = await _thread_lock(thread_id)
            async with lock:
                async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
                    graph = make_ai_graph(
                        _resolve_model(model), saver, order_repository=resolved_order_repository
                    )
                    cfg = {"configurable": {"thread_id": thread_id}}
                    st = await graph.aget_state(cfg)
                    if st.interrupts:
                        history = messages_store.hydrate(conv_id)
                        try:
                            await graph.ainvoke(
                                Command(
                                    resume={"action": HANDOFF_RESUME_ACTION},
                                    update={"messages": history},
                                ),
                                config=cfg,
                                context=_runtime_context(status.get_user_id(conv_id) or ""),
                            )
                        except Exception:
                            pass
        if was_human:
            await _emit_returned_to_bot(conv_id)
        event = {
            "type": "handoff_resolved",
            "data": {"handoff_id": handoff_id, "conversation_id": conv_id},
        }
        logger.info("event handoff_resolved conv=%s handoff=%s", conv_id, handoff_id)
        return {
            "handoff_id": handoff_id,
            "mode": "ai",
            "conversation_id": conv_id,
            "event": event,
        }

    async def _seat_takeover_for_conversation(conv_id: str) -> dict:
        """Account Webhook open → cancel pending form, then Seat Takeover + Joined Notice.

        Already-human is idempotent (no second notice, no second cancel).
        Form cancel runs before mode flips and under the thread lock, so a late
        Form Resume waiting on that lock cannot write.
        """
        if status.get_mode(conv_id) == "human":
            rec = handoff.get_active(conv_id) or {}
            return {"type": "handoff_created", "data": rec}
        thread_id = status.get_thread_id(conv_id)
        cancelled = False
        if thread_id is None:
            rec = handoff.seat_takeover(conv_id)
        else:
            lock = await _thread_lock(thread_id)
            async with lock:
                cancelled = await _cancel_pending_form_interrupt(conv_id)
                rec = handoff.seat_takeover(conv_id)
        await _emit_joined_notice(conv_id)
        if cancelled:
            await _emit_form_cancelled_on_handoff(conv_id)
        return {"type": "handoff_created", "data": rec}

    def _cancel_open_watch(conv_id: str) -> int:
        token = _open_watch.get(conv_id, 0) + 1
        _open_watch[conv_id] = token
        return token

    async def _revert_unassigned_open(conv_id: str, token: int) -> None:
        """Pull a system auto-open back to pending. Never called for a real Take over."""
        if _open_watch.get(conv_id) != token:
            return
        if status.get_mode(conv_id) == "human":
            return
        if not revert_bot_error_open or chatwoot is None:
            return
        try:
            await chatwoot.set_conversation_status(conv_id, "pending")
        except Exception:
            logger.exception("chatwoot revert bot-error open failed: conv=%s", conv_id)

    async def _arm_bot_error_revert(conv_id: str, *, immediate: bool) -> None:
        token = _cancel_open_watch(conv_id)
        grace = 0.0 if immediate else float(bot_error_open_grace_seconds)
        if grace <= 0:
            await _revert_unassigned_open(conv_id, token)
            return

        async def _later() -> None:
            try:
                await asyncio.sleep(grace)
                await _revert_unassigned_open(conv_id, token)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("bot-error open revert failed: conv=%s", conv_id)

        asyncio.create_task(_later())

    async def _enter_human(conv_id: str) -> dict:
        _cancel_open_watch(conv_id)
        created = await _seat_takeover_for_conversation(conv_id)
        logger.info(
            "event handoff_created conv=%s handoff=%s",
            conv_id,
            (created.get("data") or {}).get("handoff_id"),
        )
        return {"ok": True, "event": created}

    def _webhook_conversation_status(data: dict) -> tuple[int | None, str | None]:
        """Parse conversation_status_changed → (chatwoot_conversation_id, status)."""
        status_val = data.get("status")
        conversation = data.get("conversation") if isinstance(data.get("conversation"), dict) else None
        if status_val is None and conversation is not None:
            status_val = conversation.get("status")
        cw_id = data.get("id")
        if conversation is not None and conversation.get("id") is not None:
            cw_id = conversation.get("id")
        try:
            cw_int = int(cw_id) if cw_id is not None else None
        except (TypeError, ValueError):
            cw_int = None
        if status_val is not None:
            status_val = str(status_val)
        return cw_int, status_val

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

        # 入口策略单点在 harness/handoff_entry.py（与 AgentBot 共用）。
        decision = decide_entry(
            mode=status.get_mode(conv_id),
            awaiting=handoff.get_awaiting(conv_id) is not None,
            text=body.message,
        )

        if decision is EntryDecision.HUMAN_LANE:
            # 人工车道：只写 Message Store。不发 bot 气泡，也不把客户话再 create_message 一次。
            messages_store.append(conv_id, "user", body.message)
            clear = getattr(chatwoot, "clear_typing", None) if chatwoot is not None else None
            if clear is not None:
                try:
                    await clear(conv_id)
                except Exception:
                    logger.exception("chatwoot clear typing failed: conv=%s", conv_id)

            def human_stream():
                yield {"type": "done", "data": {"final": None, "message_id": None}}

            return _sse_response(human_stream())

        if decision is EntryDecision.CANCEL:
            # Handoff Cancel：清挂起，不切 mode，不进 Graph。
            messages_store.append(conv_id, "user", body.message)
            handoff.cancel_handoff_request(conv_id)
            tip = handoff_cancel_confirm_text()
            messages_store.append(conv_id, "assistant", tip)

            async def cancel_stream():
                yield {"type": "token", "data": {"text": tip}}
                yield {"type": "done", "data": {"final": tip, "message_id": None}}

            return _sse_response(cancel_stream())

        if decision is EntryDecision.REQUEST:
            # Seat-gated：显式转人工 → Handoff Request 挂起（mode 仍 ai），本 turn 只回确认。
            messages_store.append(conv_id, "user", body.message)
            ev = handoff.request_handoff(conv_id, "用户显式请求转人工", body.message[:80])
            tip = handoff_request_tip(created=bool(ev.get("created", True)))

            async def explicit_handoff_stream():
                async for out in _emit_handoff_requested(conv_id, ev):
                    yield out
                yield {"type": "done", "data": {"final": tip, "message_id": None}}

            return _sse_response(explicit_handoff_stream())

        async def ai_stream():
            # 先持久化用户消息（即便本 turn 失败也留痕），再水合历史作为 state.messages 输入
            messages_store.append(conv_id, "user", body.message)
            history = messages_store.hydrate(conv_id)
            cb = _combined_interrupt_callback(
                handoff.make_interrupt_callback(conv_id),
                seat.interrupt_callback(conv_id, thread_id),
            )
            lock = await _thread_lock(thread_id)
            async with lock:
                if status.get_mode(conv_id) != "ai":
                    # Takeover won the lock: do not start a graph turn or send a bot bubble.
                    yield {"type": "done", "data": {"final": None, "message_id": None}}
                    return
                async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
                    graph = make_ai_graph(_resolve_model(model), saver, order_repository=resolved_order_repository)
                    cfg = {"configurable": {"thread_id": thread_id}}
                    st = await graph.aget_state(cfg)
                    user_id = status.get_user_id(conv_id) or ""
                    resume_payload = None
                    implicit_cancel = False
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
                        elif pending_form_value(st.interrupts) is not None:
                            # 与 AgentBot 同一决策点（harness/form_gate.py）：纯文本永不
                            # 构成 Form Resume，唯一策略是 Implicit Cancel（CONTEXT.md）。
                            decision = decide_form_inbound(it, kind=FormInboundKind.TEXT)
                            implicit_cancel = (
                                decision.kind is FormDecisionKind.IMPLICIT_CANCEL
                            )
                            if not implicit_cancel:
                                resume_payload = decision.resume_payload
                        else:
                            resume_payload = resolve_resume_payload(it, body.message)
                    if implicit_cancel:
                        # Phase 1：官方 cancel resume 清挂起；静默排空，不投影取消提示、
                        # 不重推旧表单（#72 Implicit Cancel）。
                        async for _ev in event_stream(
                            graph, cfg, [], on_interrupt=cb,
                            resume_payload={"action": "cancel"},
                            extra_state={"user_id": user_id},
                            context=_runtime_context(user_id),
                        ):
                            pass
                        resume_payload = None
                    async for ev in event_stream(
                        graph, cfg, history, on_interrupt=cb,
                        resume_payload=resume_payload,
                        extra_state={"user_id": user_id},
                        context=_runtime_context(user_id),
                    ):
                        if ev["type"] == "handoff_requested":
                            async for out in _emit_handoff_requested(conv_id, ev):
                                yield out
                            continue
                        if ev["type"] == "approval_required":
                            # Approval Side-Channel: project private seat form (#64).
                            try:
                                await seat.project(conv_id, ev.get("data") or {})
                            except Exception:
                                logger.exception("chatwoot approval form project failed: conv=%s", conv_id)
                        if ev["type"] == "done":
                            # 发送前实时复检：已被切走则整轮作废（handoff-spec §1.4）
                            delivery = decide_delivery(
                                mode=status.get_mode(conv_id),
                                origin=TurnOrigin.AI_TURN,
                                final=ev["data"].get("final"),
                            )
                            if delivery.notice is not None:
                                yield {"type": "error", "data": {"code": delivery.notice, "message": "AI 回复已丢弃（会话已转人工）"}}
                                yield {"type": "done", "data": {"final": None, "message_id": None}}
                                return
                            if delivery.persist is not None:
                                messages_store.append(conv_id, "assistant", delivery.persist, message_id=ev["data"].get("message_id"))
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
            if status.get_mode(conv_id) != "ai":
                raise HTTPException(status_code=409, detail="conversation is not in ai mode")
            async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
                graph = make_ai_graph(_resolve_model(model), saver, order_repository=resolved_order_repository)
                cfg = {"configurable": {"thread_id": thread_id}}
                state = await graph.aget_state(cfg)
                pending = state.interrupts[0].value if state.interrupts else None
                form_value = pending_form_value(state.interrupts)
                legacy_confirm = (
                    isinstance(pending, dict) and pending.get("type") == "confirmation_required"
                )
                if form_value is None and not legacy_confirm:
                    raise HTTPException(status_code=409, detail="no pending confirmation")

                events: list[dict] = []
                final: str | None = None
                message_id: str | None = None
                failed = False
                fields = dict(body.fields or {})
                fields["action"] = normalized
                if body.order_id is not None:
                    fields["order_id"] = body.order_id
                decision = decide_form_inbound(
                    form_value if form_value is not None else pending,
                    kind=FormInboundKind.EXPLICIT,
                    fields=fields,
                )
                resume_payload = decision.resume_payload
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

    @app.get("/me/conversation_mode")
    def me_conversation_mode(request: Request):
        """Widget 顶栏：该用户当前会话为 human 时返回「人工服务中」。"""
        user_id = _require_auth_user(request)
        mode = status.active_mode_for_user(user_id)
        return {
            "mode": mode,
            "header": human_service_header_text() if mode == "human" else None,
        }

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
        if pending_handoff["status"] not in (STATUS_ACTIVE, STATUS_AWAITING):
            return {"handoff_id": handoff_id, "mode": "ai"}
        try:
            result = await _complete_handoff_resolve(handoff_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="handoff not found")
        return {
            "handoff_id": result["handoff_id"],
            "mode": result["mode"],
            "event": result.get("event"),
        }

    @app.post("/approvals/{approval_id}/approve")
    async def approve_approval(approval_id: str, request: Request):
        actor = _seat_actor(request)
        try:
            return await seat.decide(approval_id, SeatAction.APPROVE, actor=actor)
        except (ApprovalMissing, ApprovalProcessed, ApprovalExpiredError, SeatAuthError) as exc:
            raise _seat_http(exc) from exc

    @app.post("/approvals/{approval_id}/reject")
    async def reject_approval(
        approval_id: str, request: Request, body: RejectBody | None = None
    ):
        actor = _seat_actor(request)
        note = body.note if body is not None else None
        try:
            return await seat.decide(approval_id, SeatAction.REJECT, actor=actor, note=note)
        except (ApprovalMissing, ApprovalProcessed, ApprovalExpiredError, SeatAuthError) as exc:
            raise _seat_http(exc) from exc

    @app.get("/approvals/{approval_id}")
    def get_approval(approval_id: str, _: None = Depends(_approval_read_access)):
        rec = approvals.get(approval_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="approval not found")
        return rec

    @app.get("/approvals")
    def list_approvals(request: Request, status: str | None = None):
        # Fail-closed：客户鉴权关闭时不得全站倾倒（approval-spec §3.2.1）
        if not required_auth:
            return {"items": []}
        auth_user = _auth_user(request)
        items = approvals.list_pending() if status == "pending" else approvals.all()
        items = [item for item in items if item["requester_user_id"] == auth_user]
        return {"items": items}

    @app.get("/handoffs")
    def list_handoffs(conversation_id: str, _: None = Depends(_conversation_ownership)):
        rec = handoff.get_by_conversation(conversation_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="handoff not found")
        return rec

    async def _seat_decide_from_chatwoot(data: dict) -> bool:
        """Verified Chatwoot seat form submit → 侧信道工牌决策入口（#64）。

        验签后由适配器把配置口令 + ``sender.id`` 交给侧信道；这里只提供
        Chatwoot 会话 id → Runtime 会话 id 的映射（Transport 职责）。
        """
        return await seat.decide_from_seat_form(
            data, conversation_for=_mapped_conversation_id
        )

    def _mapped_conversation_id(cw_id: int) -> str | None:
        record = mapping.find(cw_id)
        return record["conversation_id"] if record is not None else None

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
        if not isinstance(data, dict):
            return {"ok": True}

        # Approval Side-Channel: seat form submit (private) before human-lane message sync.
        try:
            if await _seat_decide_from_chatwoot(data):
                return {"ok": True}
        except (ApprovalMissing, ApprovalProcessed, ApprovalExpiredError):
            # 预检冲突 / 归属不符 → ack（避免 Chatwoot 重试风暴）。
            return {"ok": True}
        except SeatAuthError as exc:
            # 鉴权必须 fail closed：工牌不对不能静默吞掉。
            raise _seat_http(exc) from exc

        event_name = data.get("event")
        if event_name == "conversation_status_changed":
            cw_id, new_status = _webhook_conversation_status(data)
            if cw_id is None or new_status is None:
                return {"ok": True}
            rec = mapping.find(cw_id)
            if rec is None:
                return {"ok": True}
            conv_id = rec["conversation_id"]
            if new_status == "open":
                # User assignee = real Take over. A bare open is the same shape
                # as Chatwoot's Agent Bot timeout fallback (activity arrives later).
                if human_takeover_signal(data):
                    return await _enter_human(conv_id)
                immediate = is_bot_error_signal(data) or float(bot_error_open_grace_seconds) <= 0
                await _arm_bot_error_revert(conv_id, immediate=immediate)
                logger.info(
                    "ignore unassigned open conv=%s immediate=%s",
                    conv_id,
                    immediate,
                )
                return {"ok": True}
            if new_status == "pending":
                # Official return-to-bot. Already-ai is a no-op (anti-loop after
                # our own toggle_status(pending) or a pending baseline).
                if status.get_mode(conv_id) != "human":
                    return {"ok": True}
                active = handoff.get_active(conv_id) or handoff.get_awaiting(conv_id)
                if active is None:
                    status.set_mode(conv_id, "ai")
                    await _emit_returned_to_bot(conv_id)
                    return {"ok": True}
                try:
                    result = await _complete_handoff_resolve(
                        active["handoff_id"], restore_pending=False
                    )
                    return {"ok": True, "event": result.get("event")}
                except KeyError:
                    return {"ok": True}
            if new_status == "resolved":
                # Compatible close: Resolve also returns the lane, then Runtime
                # toggle_status(pending). The following pending webhook is a no-op.
                active = handoff.get_active(conv_id) or handoff.get_awaiting(conv_id)
                if active is not None:
                    try:
                        result = await _complete_handoff_resolve(active["handoff_id"])
                        return {"ok": True, "event": result.get("event")}
                    except KeyError:
                        pass
            return {"ok": True}

        if event_name == "conversation_updated":
            cw_id, conv_status = _webhook_conversation_status(data)
            updated = mapping.find(cw_id) if cw_id is not None else None
            if (
                updated is not None
                and conv_status == "open"
                and human_takeover_signal(data)
            ):
                return await _enter_human(updated["conversation_id"])
            return {"ok": True}

        if event_name == "message_created":
            cw_id, conv_status = _webhook_conversation_status(data)
            activity_rec = mapping.find(cw_id) if cw_id is not None else None
            if activity_rec is not None:
                activity_conv = activity_rec["conversation_id"]
                # System activity after Net::ReadTimeout. Do not toast, do not
                # enter human. A seat who already took over is left in place.
                if is_bot_error_signal(data) and not human_takeover_signal(data):
                    if status.get_mode(activity_conv) == "human":
                        _cancel_open_watch(activity_conv)
                    else:
                        await _arm_bot_error_revert(activity_conv, immediate=True)
                        logger.info("ignore bot-error auto-open conv=%s", activity_conv)
                    return {"ok": True}
                if conv_status == "open" and human_takeover_signal(data) and not is_agent_message(data):
                    return await _enter_human(activity_conv)

        if event_name != "message_created" or not is_agent_message(data):
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
    async def _process_agent_bot(data: dict) -> None:
        """LLM / Graph / outbound Chatwoot. Runs after the HTTP 200 is sent."""
        try:
            async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
                graph = make_ai_graph(
                    _resolve_model(model), saver, order_repository=resolved_order_repository
                )
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
                    callbacks_factory=_agent_bot_callbacks_factory(
                        handoff, seat, chatwoot, messages_store
                    ),
                    handoff=handoff,
                    context=_runtime_context(),
                )
        except Exception:
            logger.exception("agent bot webhook processing failed")

    @app.post("/webhooks/agent_bot")
    async def agent_bot_webhook(request: Request, background_tasks: BackgroundTasks):
        payload = await request.body()
        signature = request.headers.get("X-Chatwoot-Signature")
        timestamp = request.headers.get("X-Chatwoot-Timestamp")
        if not agent_bot_secret or not verify_chatwoot_signature(payload, signature, agent_bot_secret, timestamp):
            raise HTTPException(status_code=401, detail="invalid signature")
        try:
            data = json.loads(payload)
        except (UnicodeDecodeError, ValueError):
            return {"ok": True}
        if chatwoot is None or not isinstance(data, dict):
            return {"ok": True}
        conversation = data.get("conversation")
        if not isinstance(conversation, dict):
            return {"ok": True}
        try:
            int(conversation.get("id"))
        except (TypeError, ValueError):
            return {"ok": True}
        # Chatwoot's default webhook read timeout is 5s. ACK before the graph
        # so a slow turn cannot trip Net::ReadTimeout → system auto-open.
        background_tasks.add_task(_process_agent_bot, data)
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
