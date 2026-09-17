import sqlite3

from ai_service_agent.harness.chatwoot import ChatwootMappingStore


def test_mapping_put_get(tmp_path):
    conn = sqlite3.connect(tmp_path / "m.db")
    m = ChatwootMappingStore(conn)
    m.put("c1", 101, contact_id=7)
    rec = m.get("c1")
    assert rec is not None
    assert rec["chatwoot_conversation_id"] == 101
    assert rec["contact_id"] == 7
    assert m.get("nope") is None


def test_mapping_find_reverse(tmp_path):
    conn = sqlite3.connect(tmp_path / "m.db")
    m = ChatwootMappingStore(conn)
    m.put("c1", 101)
    assert m.find(101)["conversation_id"] == "c1"
    assert m.find(999) is None
