"""运行时 thread 空闲回收（#151）。

离线 CLI（``ops/thread_cleanup.py``）与 FastAPI lifespan 共用本引擎；
本模块不含 argparse / vacuum / JSON 报告等离线语义。

安全边界：
- 每条候选先取 ``conversation:{conversation_id}`` 锁、再取**裸** ``thread_id`` 锁
  （``server._thread_lock`` 的既有 key 约定），与 ``/chat``、AgentBot、
  ``/actions/confirm``、handoff resolve、seat takeover、坐席 ``decide()`` 全部互斥。
- 两把锁都拿到后**重读指针并重跑** ``collect_blockers``；加锁前的快照一律不用于最终判断。
- 摘指针与入队在同一个 SQLite 事务；删除只调用官方 ``adelete_thread()``。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from ai_service_agent.harness.session import ConversationStatusStore
from ai_service_agent.harness.thread_guard import collect_blockers

logger = logging.getLogger(__name__)

DEFAULT_IDLE_AFTER = timedelta(hours=24)
DEFAULT_REAP_BATCH_SIZE = 50


class _NullLock:
    """CLI 单进程离线场景：没有并发，但保留同一条 ``async with`` 代码路径。"""

    async def __aenter__(self) -> "_NullLock":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


@dataclass
class ReapItem:
    thread_id: str
    conversation_id: str
    reason: str
    status: str = "candidate"
    protected_by: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class ReapReport:
    mode: str
    candidates: list[ReapItem]
    deleted: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    vacuumed: bool = False
    db_size_before: int = 0
    db_size_after: int = 0

    @property
    def protected(self) -> list[str]:
        return [item.thread_id for item in self.candidates if item.status == "protected"]

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "candidates": [asdict(item) for item in self.candidates],
            "deleted": self.deleted,
            "protected": self.protected,
            "failed": self.failed,
            "vacuumed": self.vacuumed,
            "db_size_before": self.db_size_before,
            "db_size_after": self.db_size_after,
        }

    def summary(self) -> str:
        """一行日志，给 lifespan 用。"""
        return (
            f"mode={self.mode} candidates={len(self.candidates)} "
            f"deleted={len(self.deleted)} protected={len(self.protected)} "
            f"failed={len(self.failed)}"
        )


class ThreadReaper:
    """一轮 ``cleanup_once`` = 扫描 -> 双锁 -> 锁后重查 -> 摘指针 -> 官方删除。"""

    def __init__(
        self,
        *,
        status: ConversationStatusStore,
        db_path: str,
        idle_after: timedelta = DEFAULT_IDLE_AFTER,
        lock_factory: Callable[[str], Awaitable[Any]] | None = None,
        interrupt_loader: Callable[[str], Awaitable[list]] | None = None,
        delete_thread: Callable[[str], Awaitable[None]] | None = None,
    ):
        self.status = status
        self.db_path = str(db_path)
        self.idle_after = idle_after
        self._lock_factory = lock_factory
        self._interrupt_loader = interrupt_loader
        self._delete_thread = delete_thread

    async def _lock(self, key: str) -> Any:
        if self._lock_factory is None:
            return _NullLock()
        return await self._lock_factory(key)

    def _scan(self, now: datetime, batch_size: int) -> list[dict]:
        """同步 sqlite 扫描：只读、不加锁（候选随时可能失效，锁后要重查）。"""
        idle = self.status.idle_conversations(
            now=now,
            idle_after_seconds=self.idle_after.total_seconds(),
            exclude_human=True,
        )
        queue = self.status.list_cleanup_queue(now=now)
        merged: dict[str, dict] = {}
        for row in idle:
            merged[row["thread_id"]] = {**row, "reason": "idle_timeout", "queued": False}
        for row in queue:
            merged[row["thread_id"]] = {**row, "queued": True}
        return list(merged.values())[:batch_size]

    async def _blockers(self, thread_id: str, conversation_id: str, now: datetime) -> list[str]:
        return await collect_blockers(
            conn=self.status.conn,
            thread_id=thread_id,
            conversation_id=conversation_id,
            now=now,
            interrupt_loader=self._interrupt_loader,
            # 清理侧：已过期的审批视为可回收（与轮换侧相反，见 thread_guard 文档）。
            block_expired_approvals=False,
        )

    async def cleanup_once(
        self,
        *,
        now: datetime | None = None,
        apply: bool = True,
        batch_size: int = DEFAULT_REAP_BATCH_SIZE,
    ) -> ReapReport:
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        candidates = self._scan(moment, batch_size)
        report = ReapReport(mode="apply" if apply else "dry-run", candidates=[])
        for candidate in candidates:
            item = await self._process(candidate, now=moment, apply=apply)
            report.candidates.append(item)
            if item.status == "deleted":
                report.deleted.append(item.thread_id)
            elif item.status == "failed":
                report.failed.append(item.thread_id)
            # 同步 sqlite 是毫秒级；让出事件循环，避免长批次霸占 loop。
            await asyncio.sleep(0)
        return report

    async def _process(self, candidate: dict, *, now: datetime, apply: bool) -> ReapItem:
        thread_id = candidate["thread_id"]
        conversation_id = candidate["conversation_id"]
        item = ReapItem(
            thread_id=thread_id,
            conversation_id=conversation_id,
            reason=candidate["reason"],
        )

        if not apply:
            blockers = await self._blockers(thread_id, conversation_id, now)
            if candidate["queued"] and self.status.get_thread_id(conversation_id) == thread_id:
                blockers.append("current_thread_pointer")
            item.status = "protected" if blockers else "deletable"
            item.protected_by = sorted(set(blockers))
            return item

        conversation_lock = await self._lock(f"conversation:{conversation_id}")
        async with conversation_lock:
            thread_lock = await self._lock(thread_id)
            async with thread_lock:
                return await self._reap_locked(candidate, item, now)

    async def _reap_locked(self, candidate: dict, item: ReapItem, now: datetime) -> ReapItem:
        thread_id = candidate["thread_id"]
        conversation_id = candidate["conversation_id"]

        # 锁后重查：指针与保护判定都不复用加锁前的快照。
        pointer = self.status.get_thread_id(conversation_id)
        blockers = await self._blockers(thread_id, conversation_id, now)
        if candidate["queued"] and pointer == thread_id:
            blockers.append("current_thread_pointer")
        if blockers:
            item.status = "protected"
            item.protected_by = sorted(set(blockers))
            logger.info(
                "thread reap blocked: conversation=%s thread=%s blockers=%s",
                conversation_id,
                thread_id,
                item.protected_by,
            )
            return item

        if pointer == thread_id:
            detached = self.status.detach_thread_and_enqueue(
                conversation_id, thread_id, candidate["reason"], at=now
            )
            if not detached:
                item.status = "protected"
                item.protected_by = ["current_thread_pointer_changed"]
                return item
        elif not candidate["queued"]:
            self.status.enqueue_thread(thread_id, conversation_id, candidate["reason"], at=now)

        if self._delete_thread is None:
            item.status = "failed"
            item.error = "delete_thread not configured"
            return item

        try:
            await self._delete_thread(thread_id)
        except Exception as exc:
            item.status = "failed"
            item.error = str(exc)
            logger.warning(
                "thread reap delete failed (kept in queue): thread=%s error=%s", thread_id, exc
            )
            try:
                self.status.record_cleanup_failure(thread_id, str(exc), at=now)
            except Exception:
                logger.exception("record cleanup failure failed: thread=%s", thread_id)
        else:
            item.status = "deleted"
            try:
                self.status.remove_cleanup_item(thread_id)
            except Exception:
                logger.exception("remove cleanup item failed: thread=%s", thread_id)
        return item
