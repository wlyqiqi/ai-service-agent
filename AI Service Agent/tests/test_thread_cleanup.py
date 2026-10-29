import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ai_service_agent.harness.approvals import ApprovalService
from ai_service_agent.harness.auth import UserStore
from ai_service_agent.harness.chatwoot import ChatwootMappingStore
from ai_service_agent.harness.handoff import HandoffService
from ai_service_agent.harness.messages import MessageStore
from ai_service_agent.harness.session import ConversationStatusStore, retry_backoff
from ai_service_agent.ops.thread_cleanup import ThreadCleanupService


def _seed_thread(db: str, user_id: str = "u1", *, age_hours: float = 48):
    conn = sqlite3.connect(db)
    status = ConversationStatusStore(conn)
    conversation_id = status.create_session(user_id)["conversation_id"]
    thread_id, _ = status.ensure_thread(conversation_id)
    old = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    status.mark_ai_execution(conversation_id, thread_id, at=old)
    MessageStore(conn).append(
        conversation_id,
        "user",
        f"message-for-{conversation_id}",
        message_id=f"m-{conversation_id}",
    )
    conn.commit()
    conn.close()
    return conversation_id, thread_id


def _put_checkpoint(db: str, thread_id: str, size: int = 1024) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS checkpoints ("
        "thread_id TEXT NOT NULL, checkpoint_ns TEXT NOT NULL DEFAULT '', "
        "checkpoint_id TEXT NOT NULL, parent_checkpoint_id TEXT, type TEXT, "
        "checkpoint BLOB, metadata BLOB, "
        "PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS writes ("
        "thread_id TEXT NOT NULL, checkpoint_ns TEXT NOT NULL DEFAULT '', "
        "checkpoint_id TEXT NOT NULL, task_id TEXT NOT NULL, task_path TEXT NOT NULL DEFAULT '', "
        "idx INTEGER NOT NULL, channel TEXT NOT NULL, type TEXT, value BLOB, "
        "PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, task_path, idx))"
    )
    conn.execute(
        "INSERT OR REPLACE INTO checkpoints "
        "(thread_id, checkpoint_ns, checkpoint_id, type, checkpoint, metadata) "
        "VALUES (?, '', ?, 'test', ?, '{}')",
        (thread_id, f"cp-{time.time_ns()}", b"x" * size),
    )
    conn.commit()
    conn.close()


@pytest.mark.asyncio
async def test_cleanup_dry_run_is_default_noop_and_apply_deletes_only_checkpoints(tmp_path):
    db = str(tmp_path / "cleanup.db")
    conversation_id, thread_id = _seed_thread(db)
    _put_checkpoint(db, thread_id, size=4096)
    conn = sqlite3.connect(db)
    status = ConversationStatusStore(conn)
    approvals = ApprovalService(conn)
    mapping = ChatwootMappingStore(conn)
    UserStore(conn).upsert("u1", "+65-81234567", "张伟", "123456")
    mapping.put(conversation_id, 123, contact_id=7)
    handoffs = HandoffService(conn, status)
    completed_handoff = handoffs.create_handoff_request(conversation_id, "manual", "done")
    handoffs.resolve_handoff(completed_handoff["handoff_id"])
    approvals.insert_if_absent(
        "A-TERMINAL",
        conversation_id,
        thread_id,
        {
            "approval_id": "A-TERMINAL",
            "created_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
            "expires_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        },
    )
    conn.close()
    service = ThreadCleanupService(db, interrupt_loader=lambda _thread_id: [])
    now = datetime.now(timezone.utc)

    dry = await service.run(apply=False, now=now)

    assert dry.candidates[0].thread_id == thread_id
    assert _count(db, "checkpoints", "thread_id = ?", (thread_id,)) == 1
    assert _pointer(db, conversation_id) == thread_id
    assert _queue(db, thread_id) == 0

    applied = await service.run(apply=True, now=now)

    assert applied.deleted == [thread_id]
    assert _count(db, "checkpoints", "thread_id = ?", (thread_id,)) == 0
    assert _pointer(db, conversation_id) is None
    assert _queue(db, thread_id) == 0
    assert _count(db, "conversation_message", "conversation_id = ?", (conversation_id,)) == 1
    assert _count(db, "approval_record", "approval_id = ?", ("A-TERMINAL",)) == 1
    assert _count(db, "handoff_record", "handoff_id = ?", (completed_handoff["handoff_id"],)) == 1
    assert _count(db, "chatwoot_conversation", "conversation_id = ?", (conversation_id,)) == 1
    assert _count(db, "users", "user_id = ?", ("u1",)) == 1


@pytest.mark.asyncio
async def test_cleanup_protects_pending_interrupts_handoffs_and_unexpired_approvals(tmp_path):
    db = str(tmp_path / "protected.db")
    cases = []
    for index in range(3):
        cases.append(_seed_thread(db, user_id=f"u{index}"))
        _put_checkpoint(db, cases[-1][1])

    conn = sqlite3.connect(db)
    status = ConversationStatusStore(conn)
    handoffs = HandoffService(conn, status)
    approvals = ApprovalService(conn)
    handoffs.create_handoff_request(cases[1][0], "manual", "waiting")
    approvals.insert_if_absent(
        "A-PENDING",
        cases[2][0],
        cases[2][1],
        {
            "approval_id": "A-PENDING",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
        },
    )
    conn.close()

    async def interrupts(thread_id: str):
        if thread_id == cases[0][1]:
            return [{"type": "handoff_request", "reason": "pending"}]
        return []

    service = ThreadCleanupService(db, interrupt_loader=interrupts)
    report = await service.run(apply=True)

    assert set(report.protected) == {thread_id for _conversation, thread_id in cases}
    for _conversation_id, thread_id in cases:
        assert _count(db, "checkpoints", "thread_id = ?", (thread_id,)) == 1


@pytest.mark.asyncio
async def test_expired_approval_does_not_pin_but_approved_not_terminal_does(tmp_path):
    db = str(tmp_path / "approval-expiry.db")
    expired_conversation, expired_thread = _seed_thread(db, user_id="expired")
    approved_conversation, approved_thread = _seed_thread(db, user_id="approved")
    now = datetime.now(timezone.utc)
    conn = sqlite3.connect(db)
    approvals = ApprovalService(conn)
    approvals.insert_if_absent(
        "A-EXPIRED",
        expired_conversation,
        expired_thread,
        {
            "approval_id": "A-EXPIRED",
            "created_at": (now - timedelta(hours=2)).isoformat(),
            "expires_at": (now - timedelta(hours=1)).isoformat(),
        },
    )
    approvals.insert_if_absent(
        "A-NOT-TERMINAL",
        approved_conversation,
        approved_thread,
        {
            "approval_id": "A-NOT-TERMINAL",
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=30)).isoformat(),
        },
    )
    approvals.mark_decided("A-NOT-TERMINAL", "approved", decided_by="seat:test")
    conn.close()

    report = await ThreadCleanupService(
        db, interrupt_loader=lambda _thread_id: []
    ).run(apply=True, now=now)

    assert expired_thread in report.deleted, report.to_dict()
    assert approved_thread in report.protected
    assert _pointer(db, expired_conversation) is None
    assert _pointer(db, approved_conversation) == approved_thread


@pytest.mark.asyncio
async def test_cleanup_clears_pointer_before_delete_and_failure_is_retryable(tmp_path):
    db = str(tmp_path / "retry.db")
    conversation_id, thread_id = _seed_thread(db)
    _put_checkpoint(db, thread_id)
    observed_pointer = []

    async def failing_delete(candidate_thread: str) -> None:
        conn = sqlite3.connect(db)
        observed_pointer.append(
            ConversationStatusStore(conn).get_thread_id(conversation_id)
        )
        conn.close()
        raise RuntimeError("simulated delete failure")

    first = await ThreadCleanupService(
        db, interrupt_loader=lambda _thread_id: [], delete_thread=failing_delete
    ).run(apply=True)

    assert first.failed == [thread_id]
    assert observed_pointer == [None]
    assert _pointer(db, conversation_id) is None
    assert _queue(db, thread_id) == 1
    assert _count(db, "checkpoints", "thread_id = ?", (thread_id,)) == 1

    second = await ThreadCleanupService(
        db, interrupt_loader=lambda _thread_id: []
    ).run(apply=True)

    assert second.deleted == [thread_id]
    assert _queue(db, thread_id) == 0
    assert _count(db, "checkpoints", "thread_id = ?", (thread_id,)) == 0


@pytest.mark.asyncio
async def test_cleanup_never_deletes_a_queued_thread_still_referenced(tmp_path):
    db = str(tmp_path / "referenced.db")
    conversation_id, thread_id = _seed_thread(db)
    _put_checkpoint(db, thread_id)
    conn = sqlite3.connect(db)
    ConversationStatusStore(conn).enqueue_thread(thread_id, conversation_id, "manual")
    conn.close()

    report = await ThreadCleanupService(
        db, interrupt_loader=lambda _thread_id: []
    ).run(apply=True)

    assert thread_id in report.protected
    assert _pointer(db, conversation_id) == thread_id
    assert _count(db, "checkpoints", "thread_id = ?", (thread_id,)) == 1


@pytest.mark.asyncio
async def test_cleanup_is_idempotent_when_checkpoints_were_already_deleted(tmp_path):
    db = str(tmp_path / "already-deleted.db")
    conversation_id, thread_id = _seed_thread(db)
    conn = sqlite3.connect(db)
    status = ConversationStatusStore(conn)
    status.clear_thread_pointer(conversation_id, expected_thread_id=thread_id)
    status.enqueue_thread(thread_id, conversation_id, "manual")
    conn.close()

    report = await ThreadCleanupService(
        db, interrupt_loader=lambda _thread_id: []
    ).run(apply=True)

    assert report.deleted == [thread_id]
    assert report.failed == []
    assert _queue(db, thread_id) == 0


@pytest.mark.asyncio
async def test_vacuum_rehearsal_reduces_copy_and_preserves_protected_thread(tmp_path):
    db = str(tmp_path / "rehearsal.db")
    idle_conversation, idle_thread = _seed_thread(db, user_id="idle")
    protected_conversation, protected_thread = _seed_thread(db, user_id="protected")
    for index in range(20):
        _put_checkpoint(db, idle_thread, size=100_000)
        _put_checkpoint(db, protected_thread, size=1_000)
    conn = sqlite3.connect(db)
    HandoffService(conn, ConversationStatusStore(conn)).create_handoff_request(
        protected_conversation, "manual", "keep"
    )
    conn.close()
    before_size = Path(db).stat().st_size

    report = await ThreadCleanupService(
        db, interrupt_loader=lambda _thread_id: []
    ).run(apply=True, vacuum=True)
    after_size = Path(db).stat().st_size

    assert report.vacuumed is True
    assert idle_thread in report.deleted
    assert protected_thread in report.protected
    assert _count(db, "checkpoints", "thread_id = ?", (protected_thread,)) == 20
    assert after_size < before_size


# --- #151：队列退避 ---------------------------------------------------------


def test_retry_backoff_doubles_then_caps():
    assert retry_backoff(1) == timedelta(minutes=30)
    assert retry_backoff(2) == timedelta(hours=1)
    assert retry_backoff(3) == timedelta(hours=2)
    assert retry_backoff(4) == timedelta(hours=4)
    assert retry_backoff(9) == timedelta(hours=4)


def test_record_cleanup_failure_sets_next_retry_at(tmp_path):
    db = str(tmp_path / "backoff.db")
    conn = sqlite3.connect(db)
    status = ConversationStatusStore(conn)
    now = datetime.now(timezone.utc)
    status.enqueue_thread("t1", "c1", "idle_timeout", at=now)

    status.record_cleanup_failure("t1", "disk busy", at=now)

    row = status.list_cleanup_queue()[0]
    assert row["retry_count"] == 1
    assert row["last_error"] == "disk busy"
    assert row["next_retry_at"] == (now + timedelta(minutes=30)).isoformat()

    status.record_cleanup_failure("t1", "disk busy", at=now)
    row = status.list_cleanup_queue()[0]
    assert row["retry_count"] == 2
    assert row["next_retry_at"] == (now + timedelta(hours=1)).isoformat()
    conn.close()


def test_list_cleanup_queue_respects_next_retry_at(tmp_path):
    db = str(tmp_path / "backoff-filter.db")
    conn = sqlite3.connect(db)
    status = ConversationStatusStore(conn)
    now = datetime.now(timezone.utc)
    status.enqueue_thread("t-due", "c1", "idle_timeout", at=now - timedelta(hours=2))
    status.enqueue_thread("t-wait", "c2", "idle_timeout", at=now)
    status.record_cleanup_failure("t-wait", "boom", at=now)  # 30 分钟后才该重试

    due = [row["thread_id"] for row in status.list_cleanup_queue(now=now)]
    everything = [row["thread_id"] for row in status.list_cleanup_queue()]

    assert due == ["t-due"]
    assert everything == ["t-due", "t-wait"]  # 不给 now 时保持旧行为（CLI 报告要看全部）
    conn.close()


def _count(db: str, table: str, where: str, params: tuple) -> int:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params).fetchone()[0]
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


def _pointer(db: str, conversation_id: str) -> str | None:
    conn = sqlite3.connect(db)
    try:
        return ConversationStatusStore(conn).get_thread_id(conversation_id)
    finally:
        conn.close()


def _queue(db: str, thread_id: str) -> int:
    return _count(db, "thread_cleanup_queue", "thread_id = ?", (thread_id,))
