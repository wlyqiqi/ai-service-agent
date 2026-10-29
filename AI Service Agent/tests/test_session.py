import sqlite3

from ai_service_agent.harness.session import ConversationStatusStore
from ai_service_agent.harness.threads import (
    ThreadLifecycleService,
    ThreadResumeConflict,
)


def _store(tmp_path):
    conn = sqlite3.connect(tmp_path / "test.db")
    return ConversationStatusStore(conn), conn


def test_create_session_writes_ai_mode_without_thread(tmp_path):
    store, conn = _store(tmp_path)
    info = store.create_session("u1")
    assert info["mode"] == "ai"
    assert store.get_mode(info["conversation_id"]) == "ai"
    assert info["thread_id"] is None
    assert store.get_thread_id(info["conversation_id"]) is None
    conn.close()


def test_first_ai_allocation_creates_one_thread_and_reuses_it(tmp_path):
    store, conn = _store(tmp_path)
    info = store.create_session("u1")
    threads = ThreadLifecycleService(store)

    first = threads.ensure_active_thread(info["conversation_id"])
    second = threads.ensure_active_thread(info["conversation_id"])

    assert first.created is True
    assert first.thread_id == second.thread_id
    assert second.created is False
    assert store.get_thread_id(info["conversation_id"]) == first.thread_id
    conn.close()


def test_resume_thread_never_creates_a_replacement(tmp_path):
    import pytest

    store, conn = _store(tmp_path)
    info = store.create_session("u1")
    threads = ThreadLifecycleService(store)

    with pytest.raises(ThreadResumeConflict, match="missing"):
        threads.resolve_resume_thread(info["conversation_id"], expected_thread_id="late-task")
    assert store.get_thread_id(info["conversation_id"]) is None

    current = threads.ensure_active_thread(info["conversation_id"]).thread_id
    with pytest.raises(ThreadResumeConflict, match="mismatch"):
        threads.resolve_resume_thread(info["conversation_id"], expected_thread_id="stale-task")
    assert store.get_thread_id(info["conversation_id"]) == current
    conn.close()


def test_mode_is_two_state(tmp_path):
    store, conn = _store(tmp_path)
    info = store.create_session("u1")
    store.set_mode(info["conversation_id"], "human")
    assert store.get_mode(info["conversation_id"]) == "human"
    conn.close()


def test_active_mode_for_user_follows_latest_message(tmp_path):
    from ai_service_agent.harness.messages import MessageStore

    store, conn = _store(tmp_path)
    MessageStore(conn)
    older = store.create_session("u1")
    store.set_mode(older["conversation_id"], "human")
    newer = store.create_session("u1")
    MessageStore(conn).append(newer["conversation_id"], "user", "还在问助手")
    assert store.active_mode_for_user("u1") == "ai"
    MessageStore(conn).append(older["conversation_id"], "user", "转给坐席后的话")
    assert store.active_mode_for_user("u1") == "human"
    assert store.active_mode_for_user("nobody") == "ai"
    conn.close()


def test_missing_session_fail_open_ai(tmp_path):
    store, conn = _store(tmp_path)
    assert store.get_mode("nope") == "ai"
    conn.close()


def test_legacy_thread_not_null_schema_migrates_without_losing_mapping(tmp_path):
    conn = sqlite3.connect(tmp_path / "legacy.db")
    conn.execute(
        "CREATE TABLE conversation_status ("
        "conversation_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, "
        "mode TEXT NOT NULL DEFAULT 'ai', user_id TEXT, "
        "created_at TEXT DEFAULT (datetime('now')))"
    )
    conn.execute(
        "INSERT INTO conversation_status (conversation_id, thread_id, mode, user_id) "
        "VALUES ('legacy', 'thread-legacy', 'human', 'u1')"
    )
    conn.commit()

    store = ConversationStatusStore(conn)
    columns = {
        row[1]: row for row in conn.execute("PRAGMA table_info(conversation_status)")
    }

    assert columns["thread_id"][3] == 0
    assert store.get_thread_id("legacy") == "thread-legacy"
    assert store.get_mode("legacy") == "human"
    assert store.create_session("u2")["thread_id"] is None
    conn.close()
