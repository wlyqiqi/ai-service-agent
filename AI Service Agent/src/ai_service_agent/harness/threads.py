import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from langchain_core.messages import BaseMessage
from langchain_core.messages.utils import count_tokens_approximately, trim_messages

from ai_service_agent.config import get_settings
from ai_service_agent.harness.messages import MessageStore
from ai_service_agent.harness.session import (
    ConversationStatusStore,
    parse_utc_timestamp,
)
from ai_service_agent.harness.thread_guard import collect_blockers

logger = logging.getLogger(__name__)


DEFAULT_NEW_THREAD_RECENT_TURNS = 2
DEFAULT_ROTATION_MAX_TURNS = 30
DEFAULT_ROTATION_IDLE_HOURS = 24.0

#: 轮换时必须跟着走进新 thread 的会话级状态（其余 context 键一律不带）。
PRESERVED_CONTEXT_KEYS = (
    "fallback_count",
    "clarify_count",
    "clarify_route_intent",
    "clarify_target_graph",
    "complaint_offer_pending",
    "complaint_summary",
    "complaint_offer_reconfirmed",
    "complaint_handoff_accept",
)


class ThreadResumeConflict(RuntimeError):
    """恢复引用缺失或过期；调用方必须显式报错，不得另建 thread。"""


@dataclass(frozen=True)
class RotationPolicy:
    """Demo 轮换阈值：两个条件取 OR，达到即轮换。"""

    max_completed_turns: int = DEFAULT_ROTATION_MAX_TURNS
    idle_after: timedelta = timedelta(hours=DEFAULT_ROTATION_IDLE_HOURS)


def decide_rotation(
    *,
    completed_turns: int,
    last_ai_execution_at: datetime | None,
    now: datetime,
    policy: RotationPolicy,
) -> str | None:
    """返回轮换原因（``turn_limit`` / ``idle_timeout``），不轮换返回 ``None``。"""
    if completed_turns >= policy.max_completed_turns:
        return "turn_limit"
    if last_ai_execution_at is not None and (now - last_ai_execution_at) >= policy.idle_after:
        return "idle_timeout"
    return None


@dataclass(frozen=True)
class ThreadAllocation:
    thread_id: str
    created: bool


@dataclass(frozen=True)
class ThreadTurn:
    thread_id: str
    created: bool
    history: list[BaseMessage]
    rotated_from: str | None = None
    rotation_reason: str | None = None
    #: 轮换时从旧 thread 带过来的会话级状态；调用方注入新 thread 初始 state。
    context_seed: dict | None = None


def _preserved_context(context: object) -> dict | None:
    if not isinstance(context, dict):
        return None
    preserved = {key: context[key] for key in PRESERVED_CONTEXT_KEYS if key in context}
    return preserved or None


class ThreadLifecycleService:
    """Demo 切片的 thread 解析 seam：新建轮 / 复用轮 / 轮换 / resume 分开。

    ``interrupt_loader`` / ``context_reader`` / ``delete_thread`` 由 Transport 注入
    （需要 checkpointer 与 db_path）。未注入 ``interrupt_loader`` 时无法证明「无挂起」，
    轮换一律跳过（fail closed）——宁可 checkpoint 涨，也不切断待恢复任务。
    """

    def __init__(
        self,
        status: ConversationStatusStore,
        *,
        policy: RotationPolicy | None = None,
        interrupt_loader: Callable[[str], Awaitable[list]] | None = None,
        context_reader: Callable[[str], Awaitable[dict | None]] | None = None,
        delete_thread: Callable[[str], Awaitable[None]] | None = None,
    ):
        self.status = status
        self.policy = policy or RotationPolicy()
        self._interrupt_loader = interrupt_loader
        self._context_reader = context_reader
        self._delete_thread = delete_thread

    def ensure_active_thread(
        self, conversation_id: str, *, at: datetime | None = None
    ) -> ThreadAllocation:
        thread_id, created = self.status.ensure_thread(conversation_id, at=at)
        return ThreadAllocation(thread_id=thread_id, created=created)

    async def prepare_ai_turn(
        self,
        conversation_id: str,
        messages_store: MessageStore,
        *,
        at: datetime | None = None,
    ) -> ThreadTurn:
        """分配 / 复用 / 轮换 thread，并给出本轮进入 Graph 的有界消息视图。"""
        now = at or datetime.now(timezone.utc)
        allocation = self.ensure_active_thread(conversation_id, at=at)
        thread_id = allocation.thread_id
        rotated_from: str | None = None
        rotation_reason: str | None = None
        context_seed: dict | None = None
        use_bounded = allocation.created

        if not allocation.created:
            reason = await self._rotation_reason(conversation_id, now)
            if reason is not None:
                old_context = await self._read_context(thread_id)
                rotated = self.status.rotate_thread(
                    conversation_id,
                    expected_thread_id=thread_id,
                    reason=reason,
                    at=now,
                )
                if rotated is None:
                    # 指针被并发改动（另一个 turn 已轮换 / 离线清理已摘指针）。
                    # 绝不在可能已被删除的旧 thread 上继续跑图。
                    current = self.status.get_thread_id(conversation_id)
                    logger.info(
                        "thread rotation skipped: pointer moved conversation=%s thread=%s current=%s",
                        conversation_id,
                        thread_id,
                        current,
                    )
                    if current is None:
                        thread_id = self.ensure_active_thread(
                            conversation_id, at=now
                        ).thread_id
                        use_bounded = True
                    else:
                        thread_id = current
                else:
                    rotated_from = thread_id
                    rotation_reason = reason
                    thread_id = rotated
                    context_seed = _preserved_context(old_context)
                    use_bounded = True
                    logger.info(
                        "thread rotated: conversation=%s from=%s to=%s reason=%s",
                        conversation_id,
                        rotated_from,
                        thread_id,
                        reason,
                    )
                    await self._delete_old_thread(rotated_from)

        self.touch_ai_execution(conversation_id, thread_id, at=now)
        if use_bounded:
            history = self._bounded_history(conversation_id, messages_store)
        else:
            history = messages_store.hydrate(conversation_id)
        return ThreadTurn(
            thread_id=thread_id,
            created=allocation.created,
            history=history,
            rotated_from=rotated_from,
            rotation_reason=rotation_reason,
            context_seed=context_seed,
        )

    async def _rotation_reason(self, conversation_id: str, now: datetime) -> str | None:
        state = self.status.turn_state(conversation_id)
        reason = decide_rotation(
            completed_turns=state["completed_turns"],
            last_ai_execution_at=parse_utc_timestamp(state["last_ai_execution_at"]),
            now=now,
            policy=self.policy,
        )
        if reason is None:
            return None
        thread_id = self.status.get_thread_id(conversation_id)
        if thread_id is None:
            return None
        blockers = await collect_blockers(
            conn=self.status.conn,
            thread_id=thread_id,
            conversation_id=conversation_id,
            now=now,
            interrupt_loader=self._interrupt_loader,
        )
        if blockers:
            logger.info(
                "thread rotation blocked: conversation=%s reason=%s blockers=%s",
                conversation_id,
                reason,
                sorted(set(blockers)),
            )
            return None
        return reason

    async def _read_context(self, thread_id: str) -> dict | None:
        if self._context_reader is None:
            return None
        try:
            return await self._context_reader(thread_id)
        except Exception:
            logger.exception("read generation context failed: thread=%s", thread_id)
            return None

    async def _delete_old_thread(self, thread_id: str) -> None:
        """best-effort：失败只记账，绝不影响本轮用户请求。"""
        if self._delete_thread is None:
            return
        try:
            await self._delete_thread(thread_id)
        except Exception as exc:
            logger.warning(
                "retired thread delete failed (kept in queue): thread=%s error=%s",
                thread_id,
                exc,
            )
            self.status.record_cleanup_failure(thread_id, str(exc))
        else:
            self.status.remove_cleanup_item(thread_id)

    @staticmethod
    def _bounded_history(
        conversation_id: str, messages_store: MessageStore
    ) -> list[BaseMessage]:
        history = messages_store.hydrate_recent_turns(
            conversation_id,
            previous_completed_turns=DEFAULT_NEW_THREAD_RECENT_TURNS,
        )
        return trim_messages(
            history,
            strategy="last",
            max_tokens=get_settings().read_total_input_max_tokens,
            token_counter=count_tokens_approximately,
            start_on="human",
            end_on=("human", "tool"),
            allow_partial=False,
        )

    def complete_turn(self, conversation_id: str, turn_id: str) -> bool:
        """完成轮次计数；重复投递同一 turn_id 不重复计数。"""
        return self.status.complete_turn(conversation_id, turn_id)

    def touch_ai_execution(
        self,
        conversation_id: str,
        thread_id: str,
        *,
        at: datetime | None = None,
    ) -> None:
        self.status.mark_ai_execution(conversation_id, thread_id, at=at)

    def resolve_resume_thread(
        self,
        conversation_id: str,
        *,
        expected_thread_id: str | None,
    ) -> str:
        if not self.status.conversation_exists(conversation_id):
            raise ThreadResumeConflict("conversation missing")
        current = self.status.get_thread_id(conversation_id)
        if current is None:
            raise ThreadResumeConflict("thread missing or expired")
        if expected_thread_id and expected_thread_id != current:
            raise ThreadResumeConflict("thread mismatch")
        return current
