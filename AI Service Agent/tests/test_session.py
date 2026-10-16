import sqlite3

from ai_service_agent.harness.session import ConversationStatusStore


def _store(tmp_path):
    conn = sqlite3.connect(tmp_path / "test.db")
    return ConversationStatusStore(conn), conn


def test_create_session_writes_ai_mode(tmp_path):
    store, conn = _store(tmp_path)
    info = store.create_session("u1")
    assert info["mode"] == "ai"
    assert store.get_mode(info["conversation_id"]) == "ai"
    assert store.get_thread_id(info["conversation_id"]) == info["thread_id"]
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
