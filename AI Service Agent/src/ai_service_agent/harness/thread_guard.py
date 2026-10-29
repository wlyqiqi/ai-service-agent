"""Thread 保护判定单点。

轮换（#150）与离线清理（#149）必须用同一套判定，否则两边会各自漂移：
一处认为「可以删」，另一处认为「还挂着」。

只认**有效**挂起：approval 必须未过期；`approved` 必须有终态执行结果。
"""

from __future__ import annotations

import inspect
import sqlite3
from datetime import datetime
from typing import Awaitable, Callable

from ai_service_agent.harness.approvals import APPROVAL_EXECUTION_TERMINAL_STATUSES
from ai_service_agent.harness.form_events import is_form_interrupt
from ai_service_agent.harness.session import parse_utc_timestamp

#: checkpoint 检查能力缺失/抛错时的保护标记（fail closed）。
INSPECTION_FAILED = "checkpoint_inspection_failed"


def has_active_handoff(conn: sqlite3.Connection, conversation_id: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM handoff_record WHERE conversation_id = ? "
            "AND status IN ('awaiting_seat', 'active') LIMIT 1",
            (conversation_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    return row is not None


def approval_records(
    conn: sqlite3.Connection, thread_id: str, conversation_id: str
) -> list[dict]:
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(approval_record)")}
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


def approval_protection(record: dict, now: datetime) -> str | None:
    status = record.get("status")
    if status == "pending":
        expires = parse_utc_timestamp(record.get("expires_at"))
        if expires is None:
            return "approval_expiry_invalid"
        if now < expires:
            return "approval_pending"
    if (
        status == "approved"
        and record.get("execution_status") not in APPROVAL_EXECUTION_TERMINAL_STATUSES
    ):
        return "approval_execution_incomplete"
    return None


def interrupt_protection(
    value: object,
    approvals: list[dict],
    now: datetime,
    *,
    block_expired_approvals: bool = False,
) -> str | None:
    """checkpoint 里仍有中断时的保护原因。

    ``block_expired_approvals``：轮换用 True——checkpoint 上挂着 ``approval_required``
    就说明「审批已过期」的收尾回复还没发出去，此时删 thread 会让该路径永远不可达
    （spec §4.1）。清理用 False——已过期的审批视为可回收。
    """
    if not isinstance(value, dict):
        return "checkpoint_interrupt"
    if value.get("type") == "approval_required":
        approval_id = str(value.get("approval_id") or "")
        record = next(
            (row for row in approvals if row.get("approval_id") == approval_id), None
        )
        if record is not None:
            reason = approval_protection(record, now)
            if (
                reason is None
                and block_expired_approvals
                and record.get("status") == "pending"
            ):
                return "approval_pending_expired"
            return reason
        expires = parse_utc_timestamp(value.get("expires_at"))
        if expires is None:
            return "approval_interrupt_unexpired_or_unknown"
        if now < expires:
            return "approval_pending"
        return "approval_pending_expired" if block_expired_approvals else None
    if is_form_interrupt(value):
        # Mutation / HighRisk 的 Form Interrupt：确认表单还没提交。
        # 这类挂起目前**永不自动过期**（24h TTL 尚未实现），所以会一直钉住 thread。
        return "blocked_by_form"
    return "checkpoint_interrupt"


async def collect_blockers(
    *,
    conn: sqlite3.Connection,
    thread_id: str,
    conversation_id: str,
    now: datetime,
    interrupt_loader: Callable[[str], Awaitable[list]] | None,
    block_expired_approvals: bool = False,
) -> list[str]:
    """返回阻止「轮换 / 删除」的保护原因；空列表表示安全。

    ``interrupt_loader`` 为 ``None`` 时无法证明安全 → 返回 ``INSPECTION_FAILED``。
    """
    blockers: list[str] = []
    if has_active_handoff(conn, conversation_id):
        blockers.append("handoff_awaiting_or_active")
    approvals = approval_records(conn, thread_id, conversation_id)
    for record in approvals:
        reason = approval_protection(record, now)
        if reason:
            blockers.append(reason)

    if interrupt_loader is None:
        blockers.append(INSPECTION_FAILED)
        return blockers
    try:
        loaded = interrupt_loader(thread_id)
        interrupts = await loaded if inspect.isawaitable(loaded) else loaded
    except Exception:
        blockers.append(INSPECTION_FAILED)
        return blockers
    for value in interrupts:
        reason = interrupt_protection(
            value, approvals, now, block_expired_approvals=block_expired_approvals
        )
        if reason:
            blockers.append(reason)
    return blockers
