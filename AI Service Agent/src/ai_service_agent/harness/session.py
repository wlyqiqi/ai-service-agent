import sqlite3
import uuid
from datetime import datetime, timezone


SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation_status (
    conversation_id TEXT PRIMARY KEY,
    thread_id TEXT,
    mode TEXT NOT NULL DEFAULT 'ai',
    user_id TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    last_ai_execution_at TEXT
);

CREATE TABLE IF NOT EXISTS thread_cleanup_queue (
    thread_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    queued_at TEXT NOT NULL,
    retry_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
"""


def _utc_now_iso(at: datetime | None = None) -> str:
    value = at or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def parse_utc_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class ConversationNotFound(LookupError):
    pass


class ConversationStatusStore:
    """轻量 conversation_status：会话模式与当前 Graph thread 指针。

    conversation_id 长期稳定；thread_id 只在第一次真正进入 AI Graph 前分配。
    handoff-spec.md §2：模式判断必须轻量，不触 checkpointer。
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._migrate()
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def _migrate(self) -> None:
        row = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='conversation_status'"
        ).fetchone()
        if row is None:
            return
        columns = {
            item[1]: item
            for item in self.conn.execute("PRAGMA table_info(conversation_status)").fetchall()
        }
        thread_column = columns.get("thread_id")
        if thread_column is not None and bool(thread_column[3]):
            # SQLite 无法直接删除 NOT NULL；重建表以保留既有会话与 thread 映射。
            self.conn.execute("ALTER TABLE conversation_status RENAME TO conversation_status_not_null_old")
            self.conn.execute(
                """
                CREATE TABLE conversation_status (
                    conversation_id TEXT PRIMARY KEY,
                    thread_id TEXT,
                    mode TEXT NOT NULL DEFAULT 'ai',
                    user_id TEXT,
                    created_at TEXT DEFAULT (datetime('now')),
                    last_ai_execution_at TEXT
                )
                """
            )
            self.conn.execute(
                "INSERT INTO conversation_status "
                "(conversation_id, thread_id, mode, user_id, created_at) "
                "SELECT conversation_id, thread_id, mode, user_id, created_at "
                "FROM conversation_status_not_null_old"
            )
            self.conn.execute("DROP TABLE conversation_status_not_null_old")
            columns = {
                item[1]: item
                for item in self.conn.execute("PRAGMA table_info(conversation_status)").fetchall()
            }
        if "last_ai_execution_at" not in columns:
            self.conn.execute("ALTER TABLE conversation_status ADD COLUMN last_ai_execution_at TEXT")

    def create_session(self, user_id: str) -> dict:
        conversation_id = str(uuid.uuid4())
        # 显式写 'ai'；thread 留空，直到第一次 AI Graph 执行前才分配。
        self.conn.execute(
            "INSERT INTO conversation_status (conversation_id, thread_id, mode, user_id) "
            "VALUES (?, NULL, 'ai', ?)",
            (conversation_id, user_id),
        )
        self.conn.commit()
        return {"conversation_id": conversation_id, "thread_id": None, "mode": "ai"}

    def conversation_exists(self, conversation_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM conversation_status WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        return row is not None

    def get_mode(self, conversation_id: str) -> str:
        row = self.conn.execute(
            "SELECT mode FROM conversation_status WHERE conversation_id = ?", (conversation_id,)
        ).fetchone()
        # 两态判断，fail-open：缺失/意外值按 ai 放行（handoff-spec.md §1.1）
        return "human" if row and row[0] == "human" else "ai"

    def set_mode(self, conversation_id: str, mode: str, commit: bool = True) -> None:
        self.conn.execute(
            "UPDATE conversation_status SET mode = ? WHERE conversation_id = ?", (mode, conversation_id)
        )
        if commit:
            self.conn.commit()

    def get_thread_id(self, conversation_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT thread_id FROM conversation_status WHERE conversation_id = ?", (conversation_id,)
        ).fetchone()
        return row[0] if row else None

    def ensure_thread(
        self, conversation_id: str, *, at: datetime | None = None
    ) -> tuple[str, bool]:
        """原子分配当前 thread；已存在时直接复用。

        指针在任何 Graph 调用之前提交。并发请求只有一个 CREATE 分支成功，其余读取
        同一个已提交 thread_id。
        """
        candidate = str(uuid.uuid4())
        timestamp = _utc_now_iso(at)
        with self.conn:
            cursor = self.conn.execute(
                "UPDATE conversation_status "
                "SET thread_id = ?, last_ai_execution_at = ? "
                "WHERE conversation_id = ? AND thread_id IS NULL",
                (candidate, timestamp, conversation_id),
            )
        row = self.conn.execute(
            "SELECT thread_id FROM conversation_status WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        if row is None:
            raise ConversationNotFound(conversation_id)
        if row[0] is None:
            raise RuntimeError(f"failed to allocate thread for conversation {conversation_id}")
        return row[0], cursor.rowcount == 1 and row[0] == candidate

    def mark_ai_execution(
        self,
        conversation_id: str,
        thread_id: str,
        *,
        at: datetime | None = None,
    ) -> None:
        self.conn.execute(
            "UPDATE conversation_status SET last_ai_execution_at = ? "
            "WHERE conversation_id = ? AND thread_id = ?",
            (_utc_now_iso(at), conversation_id, thread_id),
        )
        self.conn.commit()

    def clear_thread_pointer(
        self,
        conversation_id: str,
        *,
        expected_thread_id: str,
        commit: bool = True,
    ) -> bool:
        cursor = self.conn.execute(
            "UPDATE conversation_status SET thread_id = NULL "
            "WHERE conversation_id = ? AND thread_id = ?",
            (conversation_id, expected_thread_id),
        )
        if commit:
            self.conn.commit()
        return cursor.rowcount == 1

    def detach_thread_and_enqueue(
        self,
        conversation_id: str,
        thread_id: str,
        reason: str,
        *,
        at: datetime | None = None,
    ) -> bool:
        """同一事务清空当前指针并入队；失败时两者都不变。"""
        timestamp = _utc_now_iso(at)
        with self.conn:
            cursor = self.conn.execute(
                "UPDATE conversation_status SET thread_id = NULL "
                "WHERE conversation_id = ? AND thread_id = ?",
                (conversation_id, thread_id),
            )
            if cursor.rowcount != 1:
                return False
            self.conn.execute(
                "INSERT INTO thread_cleanup_queue "
                "(thread_id, conversation_id, reason, queued_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(thread_id) DO NOTHING",
                (thread_id, conversation_id, reason, timestamp),
            )
        return True

    def enqueue_thread(
        self,
        thread_id: str,
        conversation_id: str,
        reason: str,
        *,
        at: datetime | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO thread_cleanup_queue "
            "(thread_id, conversation_id, reason, queued_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(thread_id) DO NOTHING",
            (thread_id, conversation_id, reason, _utc_now_iso(at)),
        )
        self.conn.commit()

    def list_cleanup_queue(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT thread_id, conversation_id, reason, queued_at, retry_count, last_error "
            "FROM thread_cleanup_queue ORDER BY queued_at, thread_id"
        ).fetchall()
        return [
            {
                "thread_id": row[0],
                "conversation_id": row[1],
                "reason": row[2],
                "queued_at": row[3],
                "retry_count": row[4],
                "last_error": row[5],
            }
            for row in rows
        ]

    def remove_cleanup_item(self, thread_id: str) -> None:
        self.conn.execute("DELETE FROM thread_cleanup_queue WHERE thread_id = ?", (thread_id,))
        self.conn.commit()

    def record_cleanup_failure(self, thread_id: str, error: str) -> None:
        self.conn.execute(
            "UPDATE thread_cleanup_queue "
            "SET retry_count = retry_count + 1, last_error = ? WHERE thread_id = ?",
            (error[:2000], thread_id),
        )
        self.conn.commit()

    def idle_conversations(
        self,
        *,
        now: datetime,
        idle_after_seconds: float,
    ) -> list[dict]:
        rows = self.conn.execute(
            "SELECT conversation_id, thread_id, last_ai_execution_at, created_at "
            "FROM conversation_status WHERE thread_id IS NOT NULL "
            "ORDER BY COALESCE(last_ai_execution_at, created_at), conversation_id"
        ).fetchall()
        result: list[dict] = []
        for row in rows:
            last_seen = parse_utc_timestamp(row[2]) or parse_utc_timestamp(row[3])
            if last_seen is None:
                continue
            if (now - last_seen).total_seconds() >= idle_after_seconds:
                result.append(
                    {
                        "conversation_id": row[0],
                        "thread_id": row[1],
                        "last_ai_execution_at": row[2],
                        "created_at": row[3],
                    }
                )
        return result

    def active_mode_for_user(self, user_id: str) -> str:
        """该用户最近有消息的会话 mode；没有消息时用最新会话。没有会话则 ai。"""
        try:
            row = self.conn.execute(
                """
                SELECT cs.mode
                FROM conversation_status cs
                LEFT JOIN (
                    SELECT conversation_id, MAX(id) AS last_id
                    FROM conversation_message
                    GROUP BY conversation_id
                ) m ON m.conversation_id = cs.conversation_id
                WHERE cs.user_id = ?
                ORDER BY m.last_id DESC, cs.created_at DESC
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            row = self.conn.execute(
                "SELECT mode FROM conversation_status WHERE user_id = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (user_id,),
            ).fetchone()
        return "human" if row and row[0] == "human" else "ai"

    def get_user_id(self, conversation_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT user_id FROM conversation_status WHERE conversation_id = ?", (conversation_id,)
        ).fetchone()
        return row[0] if row else None
