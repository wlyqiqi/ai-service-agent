"""Approval Side-Channel（ADR-0002）：坐席侧投影、入站解析与决策执行链。

坐席侧产物住在客户侧适配器（``agent_bot``）里会让 ``server`` 反向依赖它；
决策 / 工牌鉴权 / resume 编排原本散在 ``server.create_app`` 闭包，无法脱离
FastAPI 单测。本模块是这条链的唯一归属：

    投影 → 解析 → 工牌/SoD → mark_decided → resume → Message Store / 门控出站

**不做什么**：批准后执行什么、拒绝怎么收尾仍在 ``HighRiskGraph``
（``approval-spec §3.1``）；存储仍是 ``ApprovalService`` 的浅 CRUD；本模块
不建图——resume 通过注入的 ``resume_runner`` 执行（``AGENTS.md §4``）。

客户 JWT 的只读归属过滤（``GET /approvals*``）留在 Transport：那是客户身份
鉴权，不是坐席侧信道。
"""

import hmac
from dataclasses import dataclass
from enum import Enum
from typing import Any, AsyncIterator, Awaitable, Callable

from ai_service_agent.harness.approvals import (
    ApprovalAlreadyProcessed,
    ApprovalExpired,
    ApprovalNotFound,
    ApprovalService,
)
from ai_service_agent.harness.chatwoot import ChatwootClient
from ai_service_agent.harness.form_gate import normalize_submitted_values
from ai_service_agent.harness.mutation_projection import (
    WriteSuccessCapture,
    send_mutation_success_card,
)
from ai_service_agent.harness.outbound_gate import TurnOrigin, decide_delivery

# Seat Approver Approval Side-Channel form fields (ADR-0002 / #64).
# Customer Widget must never treat these as Mutation Form Resume.
_APPROVAL_FORM_ID_FIELD = "approval_id"
_APPROVAL_FORM_DECISION_FIELD = "decision"
_APPROVAL_FORM_NOTE_FIELD = "note"


# --- 错误：模块不依赖 FastAPI，由 Transport 映射状态码 -----------------------


class SeatAuthError(Exception):
    """工牌缺失 / 口令不符 / 缺坐席 id → 403。"""


class ApprovalMissing(Exception):
    """审批不存在或不属于该会话 → 404。"""


class ApprovalProcessed(Exception):
    """已决策（幂等）→ 409。"""


class ApprovalExpiredError(Exception):
    """已过期 → 410。"""


@dataclass(frozen=True)
class SeatActor:
    """工牌校验通过的坐席身份；``decided_by`` 唯一命名空间（ADR-0002）。"""

    actor_id: str

    @property
    def decided_by(self) -> str:
        return f"seat:{self.actor_id}"


class SeatAction(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"


# --- 投影 / 解析（纯函数） ---------------------------------------------------


def _approval_form_decision_item() -> dict[str, Any]:
    """Fresh item per call: a shallow ``dict(...)`` copy would share ``options``."""
    return {
        "name": _APPROVAL_FORM_DECISION_FIELD,
        "label": "审批决定",
        "type": "select",
        "options": [
            {"label": "批准", "value": "approve"},
            {"label": "拒绝", "value": "reject"},
        ],
    }


def approval_form_title_from_payload(data: dict[str, Any]) -> str:
    """Seat-facing Chatwoot form title: enough summary to decide (#64)."""
    summary = (data.get("summary") or "").strip() or "高风险操作待审批"
    approval_id = (data.get("approval_id") or "").strip()
    expires_at = (data.get("expires_at") or "").strip()
    parts = [f"高风险审批 · {summary}"]
    if approval_id:
        parts.append(f"单号 {approval_id}")
    if expires_at:
        parts.append(f"截止 {expires_at}")
    return "\n".join(parts)


def approval_form_items_from_payload(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Build private Chatwoot ``form`` items for Seat Approver approve/reject."""
    approval_id = str(data.get("approval_id") or "")
    items: list[dict[str, Any]] = [
        {
            "name": _APPROVAL_FORM_ID_FIELD,
            "label": "审批单号",
            "type": "text",
            "default": approval_id,
        },
        _approval_form_decision_item(),
        {
            "name": _APPROVAL_FORM_NOTE_FIELD,
            "label": "拒绝备注（可选）",
            "type": "text",
            "placeholder": "拒绝时填写原因",
        },
    ]
    return items


def parse_approval_form_submission(
    submitted: dict[str, Any] | list | None,
) -> dict[str, str] | None:
    """Parse seat approval form submit → ``{approval_id, action, note?}`` or None."""
    values = normalize_submitted_values(submitted) if not isinstance(submitted, dict) else submitted
    if not values:
        return None
    approval_id = str(values.get(_APPROVAL_FORM_ID_FIELD) or "").strip()
    action = str(values.get(_APPROVAL_FORM_DECISION_FIELD) or "").strip()
    if not approval_id or action not in ("approve", "reject"):
        return None
    parsed: dict[str, str] = {"approval_id": approval_id, "action": action}
    note = values.get(_APPROVAL_FORM_NOTE_FIELD)
    if isinstance(note, str) and note.strip():
        parsed["note"] = note.strip()
    return parsed


async def send_approval_form(
    chatwoot: ChatwootClient,
    conversation_id: str,
    data: dict[str, Any],
) -> None:
    """Project ``approval_required`` to a private seat-facing Chatwoot form (#64).

    ``private=True`` keeps Approve/Reject off the customer Widget (ADR-0001).
    """
    title = approval_form_title_from_payload(data)
    items = approval_form_items_from_payload(data)
    await chatwoot.send_interactive_message(
        conversation_id,
        title,
        "form",
        {"items": items},
        private=True,
    )


def make_approval_interrupt_callback(
    store: ApprovalService,
    conversation_id: str,
    thread_id: str,
    *,
    simulate_approval: bool | None = None,
):
    """``approval_required`` interrupt → 幂等落库（+ simulate 标记 approved）→ 事件。

    「interrupt → 落库 + simulate」是侧信道语义，不是存储语义，故不在
    ``ApprovalService`` 上（``approval-spec §3.1`` 存储不承担决策）。
    """
    sim = store.simulate_approval if simulate_approval is None else simulate_approval

    async def on_approval_interrupt(value: dict) -> dict | None:
        if not isinstance(value, dict) or value.get("type") != "approval_required":
            return None
        approval_id = value.get("approval_id")
        if not approval_id:
            return None
        rec = store.insert_if_absent(approval_id, conversation_id, thread_id, value)
        if sim:
            store.mark_decided(approval_id, "approved", decided_by="simulate")
        return {"type": "approval_required", "data": {
            "approval_id": approval_id,
            "summary": rec["summary"],
            "expires_at": rec["expires_at"],
        }}

    return on_approval_interrupt


# --- 执行链 ------------------------------------------------------------------

#: ``(thread_id, *, resume_payload, on_interrupt, user_id) -> AsyncIterator[event]``
ResumeRunner = Callable[..., AsyncIterator[dict]]


class ApprovalSideChannel:
    """侧信道执行链：工牌 → 预检 → 标记 → resume → 落库 / 门控出站。"""

    def __init__(
        self,
        *,
        store: ApprovalService,
        status,
        messages,
        resume_runner: ResumeRunner,
        lock_factory: Callable[[str], Awaitable[Any]],
        interrupt_callbacks: Callable[[str, str], Callable],
        chatwoot: ChatwootClient | None = None,
        on_handoff_requested: Callable[[str, dict], Awaitable[None]] | None = None,
        seat_secret: str | None = None,
        simulate_approval: bool = False,
    ) -> None:
        self._store = store
        self._status = status
        self._messages = messages
        self._chatwoot = chatwoot
        self._runner = resume_runner
        self._lock_factory = lock_factory
        self._interrupt_callbacks = interrupt_callbacks
        self._on_handoff_requested = on_handoff_requested
        self._seat_secret = seat_secret
        self._simulate_approval = simulate_approval

    # --- 鉴权 ---

    def authenticate(self, credential: str, actor_id: str) -> SeatActor:
        """Seat badge（``approval-spec §3.2.1``）：内部口令 + 坐席 id，缺一不可。"""
        if not self._seat_secret:
            raise SeatAuthError("seat approval forbidden")
        actor = (actor_id or "").strip()
        try:
            credential_ok = bool(credential) and hmac.compare_digest(credential, self._seat_secret)
        except (TypeError, ValueError):
            credential_ok = False
        if not credential_ok or not actor:
            raise SeatAuthError("seat approval forbidden")
        return SeatActor(actor)

    # --- 投影 ---

    async def project(self, conversation_id: str, interrupt_value: dict) -> None:
        """把 ``approval_required`` 投影为坐席私有表单（无 Chatwoot 时静默跳过）。"""
        if self._chatwoot is None:
            return
        await send_approval_form(self._chatwoot, conversation_id, interrupt_value)

    def interrupt_callback(self, conversation_id: str, thread_id: str):
        return make_approval_interrupt_callback(
            self._store,
            conversation_id,
            thread_id,
            simulate_approval=self._simulate_approval,
        )

    # --- 决策 ---

    async def decide(
        self,
        approval_id: str,
        action: SeatAction,
        *,
        actor: SeatActor,
        note: str | None = None,
    ) -> dict:
        """坐席决策 → resume → 落库 / 门控出站。并发双批只有一个通过（D7 幂等）。"""
        record = self._store.get(approval_id)
        if record is None:
            raise ApprovalMissing(approval_id)
        thread_id = record["thread_id"]
        conversation_id = record["conversation_id"]
        decided = "approved" if action is SeatAction.APPROVE else "rejected"

        resume_payload: dict[str, Any] = {"action": action.value}
        if action is SeatAction.REJECT and note:
            resume_payload["note"] = note

        lock = await self._lock_factory(thread_id)
        async with lock:
            self._precheck(approval_id)  # 锁内权威预检：并发双 approve 只过一个
            # mark_decided 在图执行前（T6 裁定）：执行中崩溃也不会重试重复执行。
            self._store.mark_decided(approval_id, decided, decided_by=actor.decided_by)
            spec, final, message_id = await self._run(
                thread_id, conversation_id, resume_payload
            )
            await self._deliver(conversation_id, spec, final, message_id)
        return {"approval_id": approval_id, "status": decided}

    async def decide_from_seat_form(
        self,
        payload: dict,
        *,
        conversation_for: Callable[[int], str | None],
    ) -> bool:
        """验签后的 Chatwoot 私有表单提交 → 同一工牌决策入口。

        ``conversation_for`` 把 Chatwoot 会话 id 映射为 Runtime 会话 id（Transport
        职责）。返回 True 表示该载荷是审批表单提交（已处理）。
        """
        if payload.get("event") != "message_updated":
            return False
        if payload.get("private") is not True:
            return False
        if payload.get("content_type") not in ("form", "input_select"):
            return False
        sender = payload.get("sender") or {}
        if sender.get("type") != "user":
            return False
        decision = parse_approval_form_submission(
            (payload.get("content_attributes") or {}).get("submitted_values")
        )
        if decision is None:
            return False
        conversation = payload.get("conversation")
        if not isinstance(conversation, dict):
            return False
        try:
            cw_id = int(conversation.get("id"))
        except (TypeError, ValueError):
            return False

        conversation_id = conversation_for(cw_id)
        if conversation_id is None:
            raise ApprovalMissing(f"chatwoot conversation {cw_id}")
        record = self._store.get(decision["approval_id"])
        if record is None or record["conversation_id"] != conversation_id:
            raise ApprovalMissing(decision["approval_id"])

        actor = self.authenticate(self._seat_secret or "", str(sender.get("id") or ""))
        await self.decide(
            decision["approval_id"],
            SeatAction(decision["action"]),
            actor=actor,
            note=decision.get("note"),
        )
        return True

    # --- 内部 ---

    def _precheck(self, approval_id: str) -> dict:
        try:
            return self._store.precheck(approval_id)
        except ApprovalNotFound as exc:
            raise ApprovalMissing(approval_id) from exc
        except ApprovalAlreadyProcessed as exc:
            raise ApprovalProcessed(approval_id) from exc
        except ApprovalExpired as exc:
            raise ApprovalExpiredError(approval_id) from exc

    async def _run(
        self, thread_id: str, conversation_id: str, resume_payload: dict
    ) -> tuple[Any, str | None, str | None]:
        """resume 一次图执行；follow-on 中断按同一路径投影 / 建 handoff。"""
        write_success = WriteSuccessCapture()
        final: str | None = None
        message_id: str | None = None
        async for event in self._runner(
            thread_id,
            resume_payload=resume_payload,
            on_interrupt=self._interrupt_callbacks(conversation_id, thread_id),
            user_id=self._status.get_user_id(conversation_id) or "",
        ):
            write_success.observe(event)
            if event["type"] == "done":
                final = event["data"].get("final")
                message_id = event["data"].get("message_id")
            elif event["type"] == "approval_required":
                # follow-on 审批同样要投影坐席表单（approval-spec §3.2.2：不得静默）。
                await self.project(conversation_id, event.get("data") or {})
            elif event["type"] == "handoff_requested" and self._on_handoff_requested is not None:
                await self._on_handoff_requested(conversation_id, event)
        return write_success.spec(), final, message_id

    async def _deliver(
        self, conversation_id: str, spec: Any, final: str | None, message_id: str | None
    ) -> None:
        """落库与出站同一门控（``handoff-spec`` §1.4 / ADR-0002）。"""
        delivery = decide_delivery(
            mode=self._status.get_mode(conversation_id),
            origin=TurnOrigin.SEAT_DECISION,
            success_spec=spec,
            final=final,
            can_send=self._chatwoot is not None,
        )
        if delivery.card and delivery.outbound:
            bubble = await send_mutation_success_card(
                self._chatwoot, conversation_id, spec
            )
            if bubble:
                self._messages.append(
                    conversation_id, "assistant", bubble, message_id=message_id
                )
        elif delivery.persist is not None:
            self._messages.append(
                conversation_id, "assistant", delivery.persist, message_id=message_id
            )
            if delivery.outbound:
                await self._chatwoot.send_bot_message(conversation_id, delivery.persist)
