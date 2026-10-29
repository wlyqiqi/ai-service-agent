import asyncio
import json
import sqlite3

from fastapi.testclient import TestClient
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from ai_service_agent.harness.graph import make_ai_graph
from ai_service_agent.harness.approvals import ApprovalService
from ai_service_agent.harness.messages import MessageStore
from ai_service_agent.harness.server import create_app
from ai_service_agent.harness.session import ConversationStatusStore
from ai_service_agent.testing import make_fake_model


def _sse_types(text: str) -> list[str]:
    events = []
    for line in text.splitlines():
        if not line.startswith("data: "):
            continue
        events.append(json.loads(line[len("data: "):])["type"])
    return events


def _status_row(db: str, conversation_id: str) -> dict:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM conversation_status WHERE conversation_id = ?",
        (conversation_id,),
    ).fetchone()
    conn.close()
    assert row is not None
    return dict(row)


def _table_count(db: str, table: str, where: str = "", params: tuple = ()) -> int:
    conn = sqlite3.connect(db)
    try:
        clause = f" WHERE {where}" if where else ""
        return conn.execute(f"SELECT COUNT(*) FROM {table}{clause}", params).fetchone()[0]
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


def _read_checkpoint_messages(db: str, thread_id: str):
    async def read():
        async with AsyncSqliteSaver.from_conn_string(db) as saver:
            graph = make_ai_graph(make_fake_model(), saver)
            state = await graph.aget_state({"configurable": {"thread_id": thread_id}})
            return list(state.values.get("messages", []))

    return asyncio.run(read())


def test_post_sessions_creates_no_thread_or_checkpoint(tmp_path):
    db = str(tmp_path / "sessions.db")
    client = TestClient(create_app(db, model="fake"))

    response = client.post("/sessions", json={"user_id": "u1"})

    assert response.status_code == 200
    conversation_id = response.json()["session_id"]
    assert _status_row(db, conversation_id)["thread_id"] is None
    assert _table_count(db, "checkpoints") == 0
    assert _table_count(db, "writes") == 0


def test_first_ai_turn_creates_one_thread_and_follow_up_reuses_it(tmp_path):
    db = str(tmp_path / "reuse.db")
    client = TestClient(create_app(db, model="fake"))
    conversation_id = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]

    with client.stream("POST", "/chat", json={"session_id": conversation_id, "message": "你好"}) as response:
        first_text = "".join(response.iter_text())
    first_thread = _status_row(db, conversation_id)["thread_id"]
    with client.stream("POST", "/chat", json={"session_id": conversation_id, "message": "继续"}) as response:
        second_text = "".join(response.iter_text())
    second_thread = _status_row(db, conversation_id)["thread_id"]

    assert first_thread
    assert second_thread == first_thread
    assert "done" in _sse_types(first_text)
    assert "done" in _sse_types(second_text)
    assert _table_count(db, "checkpoints", "thread_id = ?", (first_thread,)) > 0
    assert _table_count(db, "checkpoints") == _table_count(
        db, "checkpoints", "thread_id = ?", (first_thread,)
    )


def test_new_thread_hydrates_only_current_and_bounded_recent_messages(tmp_path):
    db = str(tmp_path / "bounded.db")
    client = TestClient(create_app(db, model="fake"))
    conversation_id = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]

    conn = sqlite3.connect(db)
    store = MessageStore(conn)
    for index in range(20):
        store.append(conversation_id, "user", f"old-user-{index}")
        store.append(conversation_id, "assistant", f"old-assistant-{index}")
    conn.close()

    with client.stream(
        "POST", "/chat", json={"session_id": conversation_id, "message": "CURRENT-MESSAGE"}
    ) as response:
        text = "".join(response.iter_text())

    assert "done" in _sse_types(text)
    thread_id = _status_row(db, conversation_id)["thread_id"]
    messages = _read_checkpoint_messages(db, thread_id)
    serialized = " ".join(str(message.content) for message in messages)
    assert "CURRENT-MESSAGE" in serialized
    assert "old-user-0" not in serialized
    assert len(messages) <= 7


def test_history_read_uses_conversation_not_thread_pointer(tmp_path):
    db = str(tmp_path / "history.db")
    client = TestClient(create_app(db, model="fake"))
    conversation_id = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    conn = sqlite3.connect(db)
    MessageStore(conn).append(conversation_id, "user", "没有 thread 也要能读")
    conn.close()

    response = client.get(f"/conversations/{conversation_id}/messages")

    assert response.status_code == 200
    assert response.json()["items"][0]["content"] == "没有 thread 也要能读"


def test_form_resume_missing_or_mismatched_thread_is_conflict_and_does_not_allocate(tmp_path):
    db = str(tmp_path / "resume.db")
    client = TestClient(create_app(db, model="fake"))
    conversation_id = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]

    missing = client.post(
        "/actions/confirm",
        json={"conversation_id": conversation_id, "action": "confirm"},
    )

    assert missing.status_code == 409
    assert _status_row(db, conversation_id)["thread_id"] is None

    conn = sqlite3.connect(db)
    current = ConversationStatusStore(conn).ensure_thread(conversation_id)[0]
    conn.close()
    mismatch = client.post(
        "/actions/confirm",
        json={
            "conversation_id": conversation_id,
            "action": "confirm",
            "thread_id": "stale-thread",
        },
    )
    assert mismatch.status_code == 409
    assert _status_row(db, conversation_id)["thread_id"] == current


def test_approval_resume_with_stale_thread_is_conflict_without_replacement(tmp_path):
    from datetime import datetime, timedelta, timezone

    db = str(tmp_path / "approval-resume.db")
    conn = sqlite3.connect(db)
    status = ConversationStatusStore(conn)
    conversation_id = status.create_session("u1")["conversation_id"]
    current, _ = status.ensure_thread(conversation_id)
    ApprovalService(conn).insert_if_absent(
        "A-STALE",
        conversation_id,
        "stale-thread",
        {
            "approval_id": "A-STALE",
            "summary": "refund",
            "requester_user_id": "u1",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
        },
    )
    conn.close()
    client = TestClient(create_app(db, model="fake", seat_approval_secret="seat-secret"))

    response = client.post(
        "/approvals/A-STALE/approve",
        headers={
            "X-Seat-Approval-Credential": "seat-secret",
            "X-Seat-Actor-Id": "9",
        },
    )

    assert response.status_code == 409
    assert _status_row(db, conversation_id)["thread_id"] == current
