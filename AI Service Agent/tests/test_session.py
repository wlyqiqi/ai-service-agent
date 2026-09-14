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


def test_missing_session_fail_open_ai(tmp_path):
    store, conn = _store(tmp_path)
    assert store.get_mode("nope") == "ai"
    conn.close()
