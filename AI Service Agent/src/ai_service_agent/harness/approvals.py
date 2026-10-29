"""Approval Service（D8）：SQLite 轻量基础设施状态存储。

只持久化 pending/approved/rejected/expired 与 expires_at，供 UI 查询与 HTTP 端点前置校验；
不承担任何业务决策（approval-spec §3.1）。"超期"是纯时间比较的机械结论，读取时惰性落地。
侧信道语义（interrupt 落库、工牌、resume）在 `approval_side_channel.py`。
迁移触发条件（登记）：出现多进程/容器化部署需求时迁移独立存储（如 Postgres），并同步评估
线程锁与 checkpointer 迁移（类比 checkpointer SQLite→Postgres）。
"""

import json
import sqlite3
import threading
from datetime import datetime, timezone

APPROVAL_EXECUTION_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "uncertain"})

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
    decided_at TEXT,
    execution_status TEXT NOT NULL DEFAULT 'not_started'
);
"""

#: 后续追加的列。``CREATE TABLE IF NOT EXISTS`` 不会给已存在的表补列，
#: 这里按 ``PRAGMA table_info`` 幂等补齐（可空，回滚代码后旧逻辑照常工作）。
APPROVAL_ADDED_COLUMNS = {
    "payload_json": "TEXT",   # 原始 interrupt 载荷：投影卡片、确认页都要用
    "projected_at": "TEXT",   # 坐席侧已投影时间：同一审批只发一次
    "execution_status": "TEXT NOT NULL DEFAULT 'not_started'",
}

#: 显式列名（不写 ``SELECT *``：加列不会打乱索引，回滚也不会读错位）。
_COLUMNS = (
    "approval_id, conversation_id, thread_id, type, summary, requester_user_id, "
    "status, decision, decided_by, created_at, expires_at, decided_at, "
    "payload_json, projected_at, execution_status"
)


def _migrate(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(approval_record)")}
    for name, decl in APPROVAL_ADDED_COLUMNS.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE approval_record ADD COLUMN {name} {decl}")


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
        self.lock = threading.RLock()
        self.conn.execute(APPROVAL_SCHEMA)
        _migrate(self.conn)
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
        with self.lock:
            existing = self.get(approval_id)
            if existing is not None:
                return existing
            with self.conn:
                self.conn.execute(
                    "INSERT INTO approval_record (approval_id, conversation_id, thread_id, type, summary, requester_user_id, created_at, expires_at, payload_json) "
                    "VALUES (?, ?, ?, 'high_risk', ?, ?, ?, ?, ?)",
                    (approval_id, conversation_id, thread_id,
                     payload.get("summary", ""), payload.get("requester_user_id", ""),
                     payload.get("created_at", ""), payload.get("expires_at", ""),
                     json.dumps(payload, ensure_ascii=False, default=str)),
                )
            return self.get(approval_id)

    def get(self, approval_id: str) -> dict | None:
        with self.lock:
            row = self.conn.execute(
                f"SELECT {_COLUMNS} FROM approval_record WHERE approval_id = ?",
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
        with self.lock, self.conn:
            self.conn.execute(
                "UPDATE approval_record SET status = ?, decision = ?, decided_by = ?, decided_at = datetime('now') "
                "WHERE approval_id = ? AND status = 'pending'",
                (status, status, decided_by, approval_id),
            )

    def mark_decided(self, approval_id: str, status: str, decided_by: str) -> None:
        """幂等：仅 pending 可写（approval-spec §2，重复 decision 不覆盖）。"""
        self._set_status(approval_id, status, decided_by)
        if status != "approved":
            with self.lock, self.conn:
                self.conn.execute(
                    "UPDATE approval_record SET execution_status = 'succeeded' "
                    "WHERE approval_id = ? AND status = ?",
                    (approval_id, status),
                )

    def mark_execution_started(self, approval_id: str) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "UPDATE approval_record SET execution_status = 'running' "
                "WHERE approval_id = ? AND status = 'approved' "
                "AND execution_status IN ('not_started', 'failed', 'uncertain')",
                (approval_id,),
            )

    def mark_execution_finished(self, approval_id: str, status: str) -> None:
        if status not in APPROVAL_EXECUTION_TERMINAL_STATUSES:
            raise ValueError(f"invalid approval execution status: {status}")
        with self.lock, self.conn:
            self.conn.execute(
                "UPDATE approval_record SET execution_status = ? "
                "WHERE approval_id = ? AND status = 'approved'",
                (status, approval_id),
            )

    def mark_projected(self, approval_id: str) -> None:
        """记录坐席侧已投影。只有第一次写入生效（重复投影不再刷新时间）。"""
        with self.lock, self.conn:
            self.conn.execute(
                "UPDATE approval_record SET projected_at = datetime('now') "
                "WHERE approval_id = ? AND projected_at IS NULL",
                (approval_id,),
            )

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
        with self.lock:
            rows = self.conn.execute(
                f"SELECT {_COLUMNS} FROM approval_record WHERE conversation_id = ? AND status = 'pending' ORDER BY created_at",
                (conversation_id,),
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]

    def list_pending(self) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                f"SELECT {_COLUMNS} FROM approval_record WHERE status = 'pending' ORDER BY created_at",
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]

    def all(self) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                f"SELECT {_COLUMNS} FROM approval_record ORDER BY created_at",
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]

    @staticmethod
    def _row_to_dict(row) -> dict:
        return {
            "approval_id": row[0], "conversation_id": row[1], "thread_id": row[2], "type": row[3],
            "summary": row[4], "requester_user_id": row[5], "status": row[6], "decision": row[7],
            "decided_by": row[8], "created_at": row[9], "expires_at": row[10], "decided_at": row[11],
            "payload_json": row[12], "projected_at": row[13], "execution_status": row[14],
        }
