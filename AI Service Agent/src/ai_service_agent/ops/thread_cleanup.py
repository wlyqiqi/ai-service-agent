"""Offline Demo checkpoint cleanup.

Dry-run is the default. Apply mode clears a conversation's current pointer before
calling LangGraph's official ``AsyncSqliteSaver.adelete_thread()``. The utility
never deletes business tables and never invokes checkpoint pruning.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from ai_service_agent.harness.graph import make_ai_graph
from ai_service_agent.harness.approvals import APPROVAL_EXECUTION_TERMINAL_STATUSES
from ai_service_agent.harness.session import ConversationStatusStore, parse_utc_timestamp
from ai_service_agent.testing import make_fake_model


@dataclass
class CleanupItem:
    thread_id: str
    conversation_id: str
    reason: str
    status: str = "candidate"
    protected_by: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class CleanupReport:
    mode: str
    candidates: list[CleanupItem]
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


async def _default_interrupt_loader(thread_id: str, db_path: str) -> list:
    async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
        await saver.setup()
        graph = make_ai_graph(make_fake_model(), saver)
        state = await graph.aget_state({"configurable": {"thread_id": thread_id}})
        return [interrupt.value for interrupt in state.interrupts]


async def _default_delete_thread(thread_id: str, db_path: str) -> None:
    async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
        await saver.setup()
        await saver.adelete_thread(thread_id)


class ThreadCleanupService:
    """One testable cleanup pass over a SQLite database copy or offline database."""

    def __init__(
        self,
        db_path: str,
        *,
        idle_after: timedelta = timedelta(hours=24),
        interrupt_loader: Callable[[str], Awaitable[list]] | None = None,
        delete_thread: Callable[[str], Awaitable[None]] | None = None,
    ):
        self.db_path = str(db_path)
        self.idle_after = idle_after
        self._interrupt_loader = interrupt_loader or (
            lambda thread_id: _default_interrupt_loader(thread_id, self.db_path)
        )
        self._delete_thread = delete_thread or (
            lambda thread_id: _default_delete_thread(thread_id, self.db_path)
        )

    async def run(
        self,
        *,
        apply: bool = False,
        vacuum: bool = False,
        now: datetime | None = None,
        batch_size: int = 1000,
    ) -> CleanupReport:
        if vacuum and not apply:
            raise ValueError("VACUUM requires explicit apply mode")
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        db_size_before = Path(self.db_path).stat().st_size
        conn = sqlite3.connect(self.db_path)
        status = ConversationStatusStore(conn)
        queue = status.list_cleanup_queue()
        idle = status.idle_conversations(
            now=now,
            idle_after_seconds=self.idle_after.total_seconds(),
        )

        candidate_map: dict[str, dict] = {}
        for row in idle:
            candidate_map[row["thread_id"]] = {
                **row,
                "reason": "idle_timeout",
                "queued": False,
            }
        for row in queue:
            candidate_map[row["thread_id"]] = {
                **row,
                "queued": True,
            }
        candidates = list(candidate_map.values())[:batch_size]
        items: list[CleanupItem] = []
        report = CleanupReport(mode="apply" if apply else "dry-run", candidates=items)

        for candidate in candidates:
            thread_id = candidate["thread_id"]
            conversation_id = candidate["conversation_id"]
            item = CleanupItem(
                thread_id=thread_id,
                conversation_id=conversation_id,
                reason=candidate["reason"],
            )
            pointer = status.get_thread_id(conversation_id)
            protections = await self._pending_protections(
                conn, thread_id, conversation_id, now
            )
            if candidate["queued"] and pointer == thread_id:
                protections.append("current_thread_pointer")
            if protections:
                item.status = "protected"
                item.protected_by = sorted(set(protections))
                items.append(item)
                continue
            if not apply:
                item.status = "deletable"
                items.append(item)
                continue

            if pointer == thread_id:
                detached = status.detach_thread_and_enqueue(
                    conversation_id,
                    thread_id,
                    candidate["reason"],
                    at=now,
                )
                if not detached:
                    item.status = "protected"
                    item.protected_by = ["current_thread_pointer_changed"]
                    items.append(item)
                    continue
            elif candidate["queued"] is False:
                status.enqueue_thread(
                    thread_id,
                    conversation_id,
                    candidate["reason"],
                    at=now,
                )

            try:
                await self._delete_thread(thread_id)
            except Exception as exc:
                item.status = "failed"
                item.error = str(exc)
                status.record_cleanup_failure(thread_id, str(exc))
                report.failed.append(thread_id)
            else:
                item.status = "deleted"
                status.remove_cleanup_item(thread_id)
                report.deleted.append(thread_id)
            items.append(item)

        conn.close()
        if vacuum:
            await asyncio.to_thread(self._vacuum)
            report.vacuumed = True
        report.db_size_before = db_size_before
        report.db_size_after = Path(self.db_path).stat().st_size
        return report

    async def _pending_protections(
        self,
        conn: sqlite3.Connection,
        thread_id: str,
        conversation_id: str,
        now: datetime,
    ) -> list[str]:
        protections: list[str] = []
        if self._has_active_handoff(conn, conversation_id):
            protections.append("handoff_awaiting_or_active")
        approvals = self._approval_records(conn, thread_id, conversation_id)
        for record in approvals:
            reason = self._approval_protection(record, now)
            if reason:
                protections.append(reason)

        try:
            loaded = self._interrupt_loader(thread_id)
            interrupts = await loaded if inspect.isawaitable(loaded) else loaded
        except Exception:
            protections.append("checkpoint_inspection_failed")
        else:
            for value in interrupts:
                reason = self._interrupt_protection(value, approvals, now)
                if reason:
                    protections.append(reason)
        return protections

    @staticmethod
    def _has_active_handoff(conn: sqlite3.Connection, conversation_id: str) -> bool:
        try:
            row = conn.execute(
                "SELECT 1 FROM handoff_record WHERE conversation_id = ? "
                "AND status IN ('awaiting_seat', 'active') LIMIT 1",
                (conversation_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            return False
        return row is not None

    @staticmethod
    def _approval_records(
        conn: sqlite3.Connection, thread_id: str, conversation_id: str
    ) -> list[dict]:
        try:
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(approval_record)")
            }
            execution_expr = (
                "execution_status" if "execution_status" in columns else "'not_started'"
            )
            rows = conn.execute(
                f"SELECT approval_id, status, expires_at, {execution_expr} "
                "FROM approval_record WHERE thread_id = ? AND conversation_id = ?",
                (thread_id, conversation_id),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [
            {
                "approval_id": row[0],
                "status": row[1],
                "expires_at": row[2],
                "execution_status": row[3],
            }
            for row in rows
        ]

    @staticmethod
    def _approval_protection(record: dict, now: datetime) -> str | None:
        status = record.get("status")
        if status == "pending":
            expires = parse_utc_timestamp(record.get("expires_at"))
            if expires is None:
                return "approval_expiry_invalid"
            if now < expires:
                return "approval_pending"
        if (
            status == "approved"
            and record.get("execution_status")
            not in APPROVAL_EXECUTION_TERMINAL_STATUSES
        ):
            return "approval_execution_incomplete"
        return None

    def _interrupt_protection(
        self, value: object, approvals: list[dict], now: datetime
    ) -> str | None:
        if not isinstance(value, dict):
            return "checkpoint_interrupt"
        if value.get("type") != "approval_required":
            return "checkpoint_interrupt"
        approval_id = str(value.get("approval_id") or "")
        record = next(
            (row for row in approvals if row.get("approval_id") == approval_id),
            None,
        )
        if record is not None:
            return self._approval_protection(record, now)
        expires = parse_utc_timestamp(value.get("expires_at"))
        if expires is None:
            return "approval_interrupt_unexpired_or_unknown"
        return "approval_pending" if now < expires else None

    def _vacuum(self) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("VACUUM")
        finally:
            conn.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="offline thread checkpoint cleanup")
    parser.add_argument("--db", default="data/ai_service.db", help="SQLite runtime database")
    parser.add_argument("--apply", action="store_true", help="execute deletions; default is dry-run")
    parser.add_argument("--vacuum", action="store_true", help="run offline VACUUM after apply")
    parser.add_argument("--idle-hours", type=float, default=24.0)
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    return parser


async def _run_cli(args: argparse.Namespace) -> CleanupReport:
    service = ThreadCleanupService(
        args.db,
        idle_after=timedelta(hours=args.idle_hours),
    )
    return await service.run(
        apply=args.apply,
        vacuum=args.vacuum,
        batch_size=args.batch_size,
    )


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.vacuum and not args.apply:
        parser.error("--vacuum requires --apply and the service must be stopped")
    report = asyncio.run(_run_cli(args))
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(
            f"mode={report.mode} candidates={len(report.candidates)} "
            f"deleted={len(report.deleted)} protected={len(report.protected)} "
            f"failed={len(report.failed)} vacuumed={report.vacuumed} "
            f"db_size={report.db_size_before}->{report.db_size_after}"
        )
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
