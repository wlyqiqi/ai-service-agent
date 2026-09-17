import sqlite3

from fastapi.testclient import TestClient

from ai_service_agent.harness.messages import MessageStore
from ai_service_agent.harness.server import create_app
from ai_service_agent.harness.session import ConversationStatusStore


class FakeChatwoot:
    """离线 fake：记录 ensure/send 调用；可配置抛错验证降级。"""

    def __init__(self, fail: bool = False):
        self.calls: list[tuple[str, str, str]] = []  # (method, conversation_id, content)
        self.fail = fail

    async def ensure_conversation(self, conversation_id: str, summary: str):
        if self.fail:
            raise RuntimeError("chatwoot down")
        self.calls.append(("ensure", conversation_id, summary))
        return 1

    async def send_as_contact(self, conversation_id: str, content: str):
        if self.fail:
            raise RuntimeError("chatwoot down")
        self.calls.append(("send", conversation_id, content))


def test_human_lane_pushes_to_chatwoot(tmp_path):
    conn = sqlite3.connect(tmp_path / "s.db")
    status = ConversationStatusStore(conn)
    info = status.create_session("u1")
    status.set_mode(info["conversation_id"], "human")
    conn.close()

    cw = FakeChatwoot()
    client = TestClient(create_app(str(tmp_path / "s.db"), model="fake", chatwoot=cw))
    with client.stream("POST", "/chat", json={"session_id": info["conversation_id"], "message": "在吗"}) as r:
        text = "".join(r.iter_text())
    assert '"done"' in text
    assert ("send", info["conversation_id"], "在吗") in cw.calls
    rows = MessageStore(sqlite3.connect(tmp_path / "s.db")).list(info["conversation_id"])
    assert rows[0]["role"] == "user" and rows[0]["content"] == "在吗"


def test_chatwoot_failure_degrades_human_lane(tmp_path):
    conn = sqlite3.connect(tmp_path / "s.db")
    status = ConversationStatusStore(conn)
    info = status.create_session("u1")
    status.set_mode(info["conversation_id"], "human")
    conn.close()

    cw = FakeChatwoot(fail=True)
    client = TestClient(create_app(str(tmp_path / "s.db"), model="fake", chatwoot=cw))
    with client.stream("POST", "/chat", json={"session_id": info["conversation_id"], "message": "在吗"}) as r:
        text = "".join(r.iter_text())
    assert r.status_code == 200
    assert '"done"' in text  # Chatwoot 故障不影响人工车道主链路


def test_get_messages_returns_store_and_404(tmp_path):
    conn = sqlite3.connect(tmp_path / "s.db")
    status = ConversationStatusStore(conn)
    info = status.create_session("u1")
    MessageStore(conn).append(info["conversation_id"], "user", "你好")
    conn.close()

    client = TestClient(create_app(str(tmp_path / "s.db"), model="fake"))
    r = client.get(f"/conversations/{info['conversation_id']}/messages")
    assert r.status_code == 200
    assert r.json()["items"][0]["content"] == "你好"
    r2 = client.get("/conversations/nope/messages")
    assert r2.status_code == 404
