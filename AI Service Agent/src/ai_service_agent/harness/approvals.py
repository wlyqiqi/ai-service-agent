"""Approval Service（D8）：SQLite 轻量基础设施状态存储。

只持久化 pending/approved/rejected/expired 与 expires_at，供 UI 查询与 HTTP 端点前置校验；
不承担任何业务决策（approval-spec §3.1）。"超期"是纯时间比较的机械结论，读取时惰性落地。
迁移触发条件（登记）：出现多进程/容器化部署需求时迁移独立存储（如 Postgres），并同步评估
线程锁与 checkpointer 迁移（类比 checkpointer SQLite→Postgres）。
"""

import sqlite3
from datetime import datetime, timezone
from typing import Callable

APPROVAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS approval_record (
    approval_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'high_risk',
    summary TEXT NOT NULL,
    requester_user_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    decision TEXT,
    decided_by TEXT,
    created_at TEXT,
    expires_at TEXT NOT NULL,
    decided_at TEXT
);
"""


class ApprovalNotFound(Exception):
    pass


class ApprovalAlreadyProcessed(Exception):
    pass


class ApprovalExpired(Exception):
    pass


class ApprovalService:
    def __init__(self, conn: sqlite3.Connection, simulate_approval: bool = False):
        self.conn = conn
        self.simulate_approval = simulate_approval
        self.conn.execute(APPROVAL_SCHEMA)
        self.conn.commit()

    @staticmethod
    def _is_expired(expires_at: str) -> bool:
        try:
            expires = datetime.fromisoformat(expires_at)
        except ValueError:
            return False
        return datetime.now(timezone.utc) >= expires

    def insert_if_absent(self, approval_id: str, conversation_id: str, thread_id: str, payload: dict) -> dict:
        """幂等落库（重复 interrupt 不重复插入）。"""
        existing = self.get(approval_id)
        if existing is not None:
            return existing
        with self.conn:
            self.conn.execute(
                "INSERT INTO approval_record (approval_id, conversation_id, thread_id, type, summary, requester_user_id, created_at, expires_at) "
                "VALUES (?, ?, ?, 'high_risk', ?, ?, ?, ?)",
                (approval_id, conversation_id, thread_id,
                 payload.get("summary", ""), payload.get("requester_user_id", ""),
                 payload.get("created_at", ""), payload.get("expires_at", "")),
            )
        return self.get(approval_id)

    def get(self, approval_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT approval_id, conversation_id, thread_id, type, summary, requester_user_id, status, decision, decided_by, created_at, expires_at, decided_at "
            "FROM approval_record WHERE approval_id = ?",
            (approval_id,),
        ).fetchone()
        if row is None:
            return None
        rec = self._row_to_dict(row)
        if rec["status"] == "pending" and self._is_expired(rec["expires_at"]):
            self._set_status(approval_id, "expired", decided_by="system")
            rec = self.get(approval_id)  # 惰性过期（approval-spec §3.1，机械落库）
        return rec

    def _set_status(self, approval_id: str, status: str, decided_by: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE approval_record SET status = ?, decision = ?, decided_by = ?, decided_at = datetime('now') "
                "WHERE approval_id = ? AND status = 'pending'",
                (status, status, decided_by, approval_id),
            )

    def mark_decided(self, approval_id: str, status: str, decided_by: str) -> None:
        """幂等：仅 pending 可写（approval-spec §2，重复 decision 不覆盖）。"""
        self._set_status(approval_id, status, decided_by)

    def precheck(self, approval_id: str) -> dict:
        """前置有效性检查（approval-spec §3.2，只读 + 惰性过期）：存在 / pending / 未过期。

        过期判定先于已处理判定：get() 的惰性过期会把超期记录落为 expired，
        若先查 status 会误判为 AlreadyProcessed（2026-09-15 计划自身测试锚定 ApprovalExpired）。
        """
        rec = self.get(approval_id)
        if rec is None:
            raise ApprovalNotFound(approval_id)
        if rec["status"] == "expired" or self._is_expired(rec["expires_at"]):
            raise ApprovalExpired(approval_id)
        if rec["status"] != "pending":
            raise ApprovalAlreadyProcessed(approval_id)
        return rec

    def pending_for_conversation(self, conversation_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT approval_id, conversation_id, thread_id, type, summary, requester_user_id, status, decision, decided_by, created_at, expires_at, decided_at "
            "FROM approval_record WHERE conversation_id = ? AND status = 'pending' ORDER BY created_at",
            (conversation_id,),
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def list_pending(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT approval_id, conversation_id, thread_id, type, summary, requester_user_id, status, decision, decided_by, created_at, expires_at, decided_at "
            "FROM approval_record WHERE status = 'pending' ORDER BY created_at",
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def all(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT approval_id, conversation_id, thread_id, type, summary, requester_user_id, status, decision, decided_by, created_at, expires_at, decided_at "
            "FROM approval_record ORDER BY created_at",
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    @staticmethod
    def _row_to_dict(row) -> dict:
        return {
            "approval_id": row[0], "conversation_id": row[1], "thread_id": row[2], "type": row[3],
            "summary": row[4], "requester_user_id": row[5], "status": row[6], "decision": row[7],
            "decided_by": row[8], "created_at": row[9], "expires_at": row[10], "decided_at": row[11],
        }

    def make_interrupt_callback(self, conversation_id: str, thread_id: str) -> Callable:
        """on_interrupt 包装：approval_required -> 幂等落库（+ simulate_approval 标记 approved）
        -> 返回 approval_required 事件。"""

        async def on_approval_interrupt(value: dict) -> dict | None:
            if not isinstance(value, dict) or value.get("type") != "approval_required":
                return None
            approval_id = value.get("approval_id")
            if not approval_id:
                return None
            rec = self.insert_if_absent(approval_id, conversation_id, thread_id, value)
            if self.simulate_approval:
                self.mark_decided(approval_id, "approved", decided_by="simulate")
            return {"type": "approval_required", "data": {
                "approval_id": approval_id,
                "summary": rec["summary"],
                "expires_at": rec["expires_at"],
            }}

        return on_approval_interrupt
