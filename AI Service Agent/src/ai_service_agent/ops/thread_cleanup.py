"""离线 Demo checkpoint 清理 CLI。

引擎在 ``harness/thread_reaper.py``（与运行时 reaper 共用）。本模块只保留离线语义：
dry-run 默认、``--vacuum``、JSON 报告。**不要**把这里的任何默认值搬进 lifespan。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Awaitable, Callable

from ai_service_agent.harness.session import ConversationStatusStore
from ai_service_agent.harness.thread_probes import (
    delete_thread as delete_thread_probe,
    load_thread_interrupts,
)
from ai_service_agent.harness.thread_reaper import ReapReport, ThreadReaper

#: 兼容旧导入名（runbook / 外部脚本用过 CleanupReport）。
CleanupReport = ReapReport


class ThreadCleanupService:
    """离线清理一轮：无锁（要求服务已停止），可 dry-run，可选 VACUUM。"""

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
            lambda thread_id: load_thread_interrupts(thread_id, self.db_path)
        )
        self._delete_thread = delete_thread or (
            lambda thread_id: delete_thread_probe(thread_id, self.db_path)
        )

    async def run(
        self,
        *,
        apply: bool = False,
        vacuum: bool = False,
        now: datetime | None = None,
        batch_size: int = 1000,
    ) -> ReapReport:
        if vacuum and not apply:
            raise ValueError("VACUUM requires explicit apply mode")
        db_size_before = Path(self.db_path).stat().st_size
        conn = sqlite3.connect(self.db_path)
        try:
            reaper = ThreadReaper(
                status=ConversationStatusStore(conn),
                db_path=self.db_path,
                idle_after=self.idle_after,
                interrupt_loader=self._interrupt_loader,
                delete_thread=self._delete_thread,
            )
            # 离线是人工重试：不尊重退避，否则刚修完问题还得干等 30 分钟。
            report = await reaper.cleanup_once(
                now=now, apply=apply, batch_size=batch_size, respect_backoff=False
            )
        finally:
            conn.close()
        if vacuum:
            await asyncio.to_thread(self._vacuum)
            report.vacuumed = True
        report.db_size_before = db_size_before
        report.db_size_after = Path(self.db_path).stat().st_size
        return report

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


async def _run_cli(args: argparse.Namespace) -> ReapReport:
    service = ThreadCleanupService(args.db, idle_after=timedelta(hours=args.idle_hours))
    return await service.run(apply=args.apply, vacuum=args.vacuum, batch_size=args.batch_size)


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
