from dataclasses import dataclass
from datetime import datetime

from langchain_core.messages import BaseMessage
from langchain_core.messages.utils import count_tokens_approximately, trim_messages

from ai_service_agent.config import get_settings
from ai_service_agent.harness.messages import MessageStore
from ai_service_agent.harness.session import ConversationStatusStore


DEFAULT_NEW_THREAD_RECENT_TURNS = 2


class ThreadResumeConflict(RuntimeError):
    """恢复引用缺失或过期；调用方必须显式报错，不得另建 thread。"""


@dataclass(frozen=True)
class ThreadAllocation:
    thread_id: str
    created: bool


@dataclass(frozen=True)
class ThreadTurn:
    thread_id: str
    created: bool
    history: list[BaseMessage]


class ThreadLifecycleService:
    """Demo 切片的 thread 解析 seam：新建轮与 resume 轮分开。"""

    def __init__(self, status: ConversationStatusStore):
        self.status = status

    def ensure_active_thread(
        self, conversation_id: str, *, at: datetime | None = None
    ) -> ThreadAllocation:
        thread_id, created = self.status.ensure_thread(conversation_id, at=at)
        return ThreadAllocation(thread_id=thread_id, created=created)

    def prepare_ai_turn(
        self,
        conversation_id: str,
        messages_store: MessageStore,
        *,
        at: datetime | None = None,
    ) -> ThreadTurn:
        """分配/复用 thread，并给出本轮进入 Graph 的有界消息视图。"""
        allocation = self.ensure_active_thread(conversation_id, at=at)
        self.touch_ai_execution(conversation_id, allocation.thread_id, at=at)
        if allocation.created:
            history = messages_store.hydrate_recent_turns(
                conversation_id,
                previous_completed_turns=DEFAULT_NEW_THREAD_RECENT_TURNS,
            )
            history = trim_messages(
                history,
                strategy="last",
                max_tokens=get_settings().read_total_input_max_tokens,
                token_counter=count_tokens_approximately,
                start_on="human",
                end_on=("human", "tool"),
                allow_partial=False,
            )
        else:
            history = messages_store.hydrate(conversation_id)
        return ThreadTurn(
            thread_id=allocation.thread_id,
            created=allocation.created,
            history=history,
        )

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
