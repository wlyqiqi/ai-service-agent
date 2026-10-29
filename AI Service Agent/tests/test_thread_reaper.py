import asyncio
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from ai_service_agent.harness.approvals import ApprovalService
from ai_service_agent.harness.chatwoot import ChatwootMappingStore
from ai_service_agent.harness.graph import make_ai_graph
from ai_service_agent.harness.messages import MessageStore
from ai_service_agent.harness.session import ConversationStatusStore
from ai_service_agent.harness.thread_reaper import ThreadReaper
from ai_service_agent.testing import make_fake_model

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)


def _checkpoints(db: str, thread_id: str) -> int:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM checkpoints WHERE thread_id = ?", (thread_id,)
        ).fetchone()[0]
    finally:
        conn.close()


def _pointer(db: str, conversation_id: str) -> str | None:
    conn = sqlite3.connect(db)
    try:
        return ConversationStatusStore(conn).get_thread_id(conversation_id)
    finally:
        conn.close()


async def _write_checkpoint(db: str, thread_id: str) -> None:
    async with AsyncSqliteSaver.from_conn_string(db) as saver:
        await saver.setup()
        graph = make_ai_graph(make_fake_model(), saver)
        await graph.aupdate_state(
            {"configurable": {"thread_id": thread_id}},
            {"messages": []},
            as_node="complaint_graph",
        )


def _seed_rows(db: str, *, age_hours: float = 48, mode: str = "ai") -> tuple[str, str]:
    """同步建业务行（不写 checkpoint）。"""
    conn = sqlite3.connect(db)
    status = ConversationStatusStore(conn)
    conversation_id = status.create_session("u1")["conversation_id"]
    thread_id, _ = status.ensure_thread(conversation_id, at=NOW - timedelta(hours=age_hours))
    status.mark_ai_execution(conversation_id, thread_id, at=NOW - timedelta(hours=age_hours))
    MessageStore(conn).append(conversation_id, "user", "历史消息")
    if mode != "ai":
        status.set_mode(conversation_id, mode)
    conn.close()
    return conversation_id, thread_id


async def _seed(db: str, *, age_hours: float = 48, mode: str = "ai") -> tuple[str, str]:
    """异步测试用：建行 + 真 checkpoint。"""
    conversation_id, thread_id = _seed_rows(db, age_hours=age_hours, mode=mode)
    await _write_checkpoint(db, thread_id)
    return conversation_id, thread_id


def _seed_sync(db: str, *, age_hours: float = 48, mode: str = "ai") -> tuple[str, str]:
    """同步测试用：自己开 loop 写 checkpoint（不能在 async 测试里调用）。"""
    conversation_id, thread_id = _seed_rows(db, age_hours=age_hours, mode=mode)
    asyncio.run(_write_checkpoint(db, thread_id))
    return conversation_id, thread_id


def _reaper(
    db: str,
    *,
    status=None,
    lock_factory=None,
    interrupt_loader=None,
    delete_thread=None,
) -> ThreadReaper:
    if status is None:
        status = ConversationStatusStore(sqlite3.connect(db))
    return ThreadReaper(
        status=status,
        db_path=db,
        lock_factory=lock_factory,
        interrupt_loader=interrupt_loader or (lambda _tid: []),
        delete_thread=delete_thread or _make_delete(db),
    )


def _make_delete(db: str):
    async def delete(thread_id: str) -> None:
        async with AsyncSqliteSaver.from_conn_string(db) as saver:
            await saver.setup()
            await saver.adelete_thread(thread_id)

    return delete


@pytest.mark.asyncio
async def test_cleanup_once_deletes_idle_thread_and_clears_pointer(tmp_path):
    db = str(tmp_path / "reap.db")
    conversation_id, thread_id = await _seed(db)

    report = await _reaper(db).cleanup_once(now=NOW)

    assert report.deleted == [thread_id]
    assert report.failed == []
    assert _checkpoints(db, thread_id) == 0
    assert _pointer(db, conversation_id) is None


@pytest.mark.asyncio
async def test_cleanup_once_takes_conversation_then_thread_lock(tmp_path):
    db = str(tmp_path / "locks.db")
    conversation_id, thread_id = await _seed(db)
    keys: list[str] = []

    async def factory(key):
        keys.append(key)
        return asyncio.Lock()

    await _reaper(db, lock_factory=factory).cleanup_once(now=NOW)

    # 顺序必须是会话锁 -> 裸 thread_id，否则与 resume 路径互斥不上。
    assert keys == [f"conversation:{conversation_id}", thread_id]


@pytest.mark.asyncio
async def test_cleanup_once_rechecks_blockers_after_locks(tmp_path):
    """扫描时无挂起；拿到锁之前出现未过期审批 -> 必须放弃删除。"""
    db = str(tmp_path / "recheck.db")
    conversation_id, thread_id = await _seed(db)
    injected = False

    async def factory(key):
        nonlocal injected
        if key.startswith("conversation:") and not injected:
            injected = True
            conn = sqlite3.connect(db)
            # 审批过期判定走真实墙钟（ApprovalService 的惰性过期），
            # 所以 expires_at 必须相对 now() 而不是注入的 NOW，否则会被判成 expired。
            ApprovalService(conn).insert_if_absent(
                "ap-late",
                conversation_id,
                thread_id,
                {
                    "summary": "退款",
                    "expires_at": (
                        datetime.now(timezone.utc) + timedelta(minutes=20)
                    ).isoformat(),
                },
            )
            conn.commit()
            conn.close()
        return asyncio.Lock()

    report = await _reaper(db, lock_factory=factory).cleanup_once(now=NOW)

    assert report.deleted == []
    assert report.candidates[0].status == "protected"
    assert "approval_pending" in report.candidates[0].protected_by
    assert _checkpoints(db, thread_id) == 1


@pytest.mark.asyncio
async def test_cleanup_once_protects_when_conversation_became_active(tmp_path):
    """扫描时已空闲 48h；拿到锁之前又跑了一轮 -> 空闲谓词必须重验，不能删。"""
    db = str(tmp_path / "active-again.db")
    conversation_id, thread_id = await _seed(db)
    injected = False

    async def factory(key):
        nonlocal injected
        if key.startswith("conversation:") and not injected:
            injected = True
            conn = sqlite3.connect(db)
            # 模拟 /chat 刚跑完一轮：last_ai_execution_at 被刷新到 NOW。
            ConversationStatusStore(conn).mark_ai_execution(conversation_id, thread_id, at=NOW)
            conn.close()
        return asyncio.Lock()

    report = await _reaper(db, lock_factory=factory).cleanup_once(now=NOW)

    assert report.deleted == []
    assert report.candidates[0].status == "protected"
    assert "no_longer_idle" in report.candidates[0].protected_by
    assert _checkpoints(db, thread_id) == 1
    assert _pointer(db, conversation_id) == thread_id


@pytest.mark.asyncio
async def test_cleanup_once_deletes_orphan_when_pointer_rotated_before_lock(tmp_path):
    """扫描到拿锁之间指针被轮换走：旧 thread 已是孤儿，必须照常删除。"""
    db = str(tmp_path / "rotated.db")
    conversation_id, thread_id = await _seed(db)
    rotated = False

    async def factory(key):
        nonlocal rotated
        if key.startswith("conversation:") and not rotated:
            rotated = True
            conn = sqlite3.connect(db)
            ConversationStatusStore(conn).rotate_thread(
                conversation_id, expected_thread_id=thread_id, reason="turn_limit", at=NOW
            )
            conn.close()
        return asyncio.Lock()

    report = await _reaper(db, lock_factory=factory).cleanup_once(now=NOW)

    assert report.deleted == [thread_id]
    assert _checkpoints(db, thread_id) == 0
    # 新指针不受影响，仍是轮换后的 thread。
    assert _pointer(db, conversation_id) not in (None, thread_id)


def test_scan_reserves_batch_quota_for_queue_rows(tmp_path):
    """被永久保护的 idle 会话占满 batch 时，队列行不能饿死。"""
    db = str(tmp_path / "quota.db")
    for _ in range(5):
        _seed_sync(db)

    # 另造一条「已摘指针、只在队列里」的 thread（轮换失败/删除失败的样子）。
    conn = sqlite3.connect(db)
    status = ConversationStatusStore(conn)
    queued_conversation = status.create_session("u2")["conversation_id"]
    queued_thread, _ = status.ensure_thread(queued_conversation, at=NOW - timedelta(hours=48))
    status.detach_thread_and_enqueue(queued_conversation, queued_thread, "rotation", at=NOW)
    conn.close()

    candidates = _reaper(db)._scan(NOW, 2)

    assert len(candidates) <= 2
    assert any(c["thread_id"] == queued_thread and c["queued"] for c in candidates)


@pytest.mark.asyncio
async def test_delete_failure_keeps_queue_row_with_retry_metadata(tmp_path):
    db = str(tmp_path / "fail.db")
    conversation_id, thread_id = await _seed(db)

    async def boom(_thread_id):
        raise RuntimeError("disk busy")

    report = await _reaper(db, delete_thread=boom).cleanup_once(now=NOW)

    assert report.failed == [thread_id]
    assert _pointer(db, conversation_id) is None  # 指针先摘，不留悬空引用
    conn = sqlite3.connect(db)
    row = ConversationStatusStore(conn).list_cleanup_queue()[0]
    conn.close()
    assert row["thread_id"] == thread_id
    assert row["retry_count"] == 1
    assert "disk busy" in row["last_error"]
    assert row["next_retry_at"] is not None


@pytest.mark.asyncio
async def test_cleanup_once_skips_rows_waiting_for_retry(tmp_path):
    db = str(tmp_path / "wait.db")
    conversation_id, thread_id = await _seed(db)

    async def boom(_thread_id):
        raise RuntimeError("disk busy")

    await _reaper(db, delete_thread=boom).cleanup_once(now=NOW)
    # 同一时刻再跑一轮：next_retry_at 还没到，不该重撞。
    report = await _reaper(db, delete_thread=boom).cleanup_once(now=NOW)

    assert report.candidates == []
    assert report.failed == []


@pytest.mark.asyncio
async def test_cleanup_once_is_idempotent_for_missing_checkpoint(tmp_path):
    db = str(tmp_path / "idem.db")
    conversation_id, thread_id = await _seed(db)
    await _reaper(db).cleanup_once(now=NOW)

    # 指针已空、队列已清；再跑一轮什么都不做，且不报错。
    report = await _reaper(db).cleanup_once(now=NOW)

    assert report.deleted == []
    assert report.failed == []
    assert report.candidates == []


@pytest.mark.asyncio
async def test_cleanup_once_keeps_messages_and_business_rows(tmp_path):
    db = str(tmp_path / "keep.db")
    conversation_id, thread_id = await _seed(db)
    conn = sqlite3.connect(db)
    ChatwootMappingStore(conn).put(conversation_id, 777, contact_id=5)
    conn.close()

    await _reaper(db).cleanup_once(now=NOW)

    conn = sqlite3.connect(db)
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM conversation_message WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()[0]
        == 1
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM chatwoot_conversation WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()[0]
        == 1
    )
    conn.close()


@pytest.mark.asyncio
async def test_cleanup_once_skips_human_mode_conversation(tmp_path):
    db = str(tmp_path / "human-skip.db")
    conversation_id, thread_id = await _seed(db, mode="human")

    report = await _reaper(db).cleanup_once(now=NOW)

    assert report.candidates == []
    assert _checkpoints(db, thread_id) == 1


@pytest.mark.asyncio
async def test_dry_run_reports_without_mutating(tmp_path):
    db = str(tmp_path / "dry.db")
    conversation_id, thread_id = await _seed(db)

    report = await _reaper(db).cleanup_once(now=NOW, apply=False)

    assert report.mode == "dry-run"
    assert report.candidates[0].status == "deletable"
    assert _checkpoints(db, thread_id) == 1
    assert _pointer(db, conversation_id) == thread_id


def test_next_ai_message_after_cleanup_creates_new_thread(tmp_path):
    from fastapi.testclient import TestClient

    from ai_service_agent.harness.server import create_app

    db = str(tmp_path / "after-cleanup.db")
    conversation_id, old_thread = _seed_sync(db)
    asyncio.run(_reaper(db).cleanup_once(now=NOW))

    client = TestClient(create_app(db, model="fake"))
    with client.stream(
        "POST", "/chat", json={"session_id": conversation_id, "message": "我回来了"}
    ) as response:
        body = "".join(response.iter_text())
    assert '"done"' in body

    conn = sqlite3.connect(db)
    new_thread = ConversationStatusStore(conn).get_thread_id(conversation_id)
    messages = conn.execute(
        "SELECT COUNT(*) FROM conversation_message WHERE conversation_id = ?",
        (conversation_id,),
    ).fetchone()[0]
    conn.close()

    assert new_thread is not None and new_thread != old_thread
    assert messages >= 2  # 旧历史 + 本轮，不随 thread 回收丢失


# --- lifespan 接线 ----------------------------------------------------------


def test_lifespan_does_not_start_reaper_by_default(tmp_path):
    from fastapi.testclient import TestClient

    from ai_service_agent.harness.server import create_app

    db = str(tmp_path / "no-reaper.db")
    conversation_id, thread_id = _seed_sync(db)  # 先 seed 再启动 app，避免两个事件循环同时写库
    app = create_app(db, model="fake")
    with TestClient(app):
        time.sleep(0.05)
        # 默认 interval=0：没有后台任务，空闲会话不会被删。
        assert _checkpoints(db, thread_id) == 1


@pytest.mark.asyncio
async def test_lifespan_cancels_reaper_cleanly(tmp_path):
    from ai_service_agent.harness.server import _run_thread_reaper

    db = str(tmp_path / "cancel.db")
    conn = sqlite3.connect(db)
    status = ConversationStatusStore(conn)
    reaper = ThreadReaper(
        status=status,
        db_path=db,
        interrupt_loader=lambda _tid: [],
        delete_thread=_make_delete(db),
    )
    task = asyncio.create_task(
        _run_thread_reaper(reaper, interval_seconds=3600, clock=lambda: NOW)
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    conn.close()


def test_lifespan_wires_server_thread_lock(tmp_path):
    """reaper 必须拿到 server 那把锁；换成别的 factory 会让保护归零而测试全绿。"""
    from fastapi.testclient import TestClient

    import ai_service_agent.harness.server as server

    captured: dict = {}
    real = server.ThreadReaper

    class Spy(real):
        def __init__(self, **kwargs):
            captured.update(kwargs)
            super().__init__(**kwargs)

    db = str(tmp_path / "wiring.db")
    _seed_sync(db)
    original = server.ThreadReaper
    server.ThreadReaper = Spy
    try:
        app = server.create_app(db, model="fake", thread_reaper_interval_seconds=1)
        with TestClient(app):
            pass
    finally:
        server.ThreadReaper = original

    assert captured["lock_factory"] is server._thread_lock
    assert captured["status"] is not None
    assert callable(captured["delete_thread"])
    assert callable(captured["interrupt_loader"])


@pytest.mark.asyncio
async def test_reaper_loop_survives_a_failed_pass():
    """单轮抛异常不能打死循环，否则一次瞬时错误就永久停摆。"""
    from types import SimpleNamespace

    from ai_service_agent.harness.server import _run_thread_reaper

    calls: list[int] = []

    class Flaky:
        async def cleanup_once(self, **_kwargs):
            calls.append(len(calls))
            if len(calls) == 1:
                raise RuntimeError("transient")
            return SimpleNamespace(summary=lambda: "ok")

    task = asyncio.create_task(_run_thread_reaper(Flaky(), interval_seconds=0.01))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if len(calls) >= 2:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(calls) >= 2
