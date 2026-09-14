import sqlite3

from ai_service_agent.harness.messages import MessageStore


def _store(tmp_path):
    conn = sqlite3.connect(tmp_path / "m.db")
    return MessageStore(conn), conn


def test_append_and_list_roundtrip(tmp_path):
    store, conn = _store(tmp_path)
    store.append("c1", "user", "你好")
    store.append("c1", "assistant", "你好，有什么可以帮你？")
    rows = store.list("c1")
    assert [r["role"] for r in rows] == ["user", "assistant"]
    assert rows[0]["content"] == "你好"
    conn.close()


def test_hydrate_returns_langchain_messages_in_order(tmp_path):
    store, conn = _store(tmp_path)
    store.append("c1", "user", "u1")
    store.append("c1", "assistant", "a1")
    store.append("c1", "user", "u2")
    msgs = store.hydrate("c1")
    assert [type(m).__name__ for m in msgs] == ["HumanMessage", "AIMessage", "HumanMessage"]
    assert msgs[1].content == "a1"
    conn.close()


def test_hydrate_empty_conversation(tmp_path):
    store, conn = _store(tmp_path)
    assert store.hydrate("nope") == []
    conn.close()
