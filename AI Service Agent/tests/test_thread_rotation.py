"""#150：安全边界上的自动 thread 轮换。

存储层与纯判定先行；Transport 接线用例在文件后半段。
"""

import asyncio
import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from ai_service_agent.harness.session import ConversationStatusStore, parse_utc_timestamp
from ai_service_agent.harness.threads import (
    DEFAULT_ROTATION_IDLE_HOURS,
    DEFAULT_ROTATION_MAX_TURNS,
    PRESERVED_CONTEXT_KEYS,
    RotationPolicy,
    ThreadLifecycleService,
    decide_rotation,
)

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)


def _store(tmp_path):
    conn = sqlite3.connect(tmp_path / "rotation.db")
    return ConversationStatusStore(conn), conn


# --- 纯判定 -----------------------------------------------------------------


def test_decide_rotation_rotates_at_turn_limit():
    assert (
        decide_rotation(
            completed_turns=DEFAULT_ROTATION_MAX_TURNS,
            last_ai_execution_at=NOW,
            now=NOW,
            policy=RotationPolicy(),
        )
        == "turn_limit"
    )


def test_decide_rotation_does_not_rotate_one_turn_before_limit():
    assert (
        decide_rotation(
            completed_turns=DEFAULT_ROTATION_MAX_TURNS - 1,
            last_ai_execution_at=NOW,
            now=NOW,
            policy=RotationPolicy(),
        )
        is None
    )


def test_decide_rotation_rotates_after_idle_timeout():
    idle = NOW - timedelta(hours=DEFAULT_ROTATION_IDLE_HOURS)
    assert (
        decide_rotation(
            completed_turns=1,
            last_ai_execution_at=idle,
            now=NOW,
            policy=RotationPolicy(),
        )
        == "idle_timeout"
    )


def test_decide_rotation_ignores_idle_below_threshold():
    recent = NOW - timedelta(hours=DEFAULT_ROTATION_IDLE_HOURS) + timedelta(minutes=1)
    assert (
        decide_rotation(
            completed_turns=1,
            last_ai_execution_at=recent,
            now=NOW,
            policy=RotationPolicy(),
        )
        is None
    )


def test_decide_rotation_turn_limit_wins_when_both_conditions_hold():
    idle = NOW - timedelta(hours=48)
    assert (
        decide_rotation(
            completed_turns=DEFAULT_ROTATION_MAX_TURNS,
            last_ai_execution_at=idle,
            now=NOW,
            policy=RotationPolicy(),
        )
        == "turn_limit"
    )


# --- 完成轮次计数 -----------------------------------------------------------


def test_complete_turn_counts_each_distinct_turn_once(tmp_path):
    store, conn = _store(tmp_path)
    conversation_id = store.create_session("u1")["conversation_id"]

    assert store.complete_turn(conversation_id, "turn-1") is True
    assert store.complete_turn(conversation_id, "turn-1") is False  # 重复投递 / 重复回调
    assert store.complete_turn(conversation_id, "turn-2") is True

    assert store.turn_state(conversation_id)["completed_turns"] == 2
    assert store.turn_state(conversation_id)["last_completed_turn_id"] == "turn-2"
    conn.close()


def test_complete_turn_ignores_unknown_conversation(tmp_path):
    store, conn = _store(tmp_path)
    assert store.complete_turn("missing", "turn-1") is False
    conn.close()


# --- 原子轮换 ---------------------------------------------------------------


def test_rotate_thread_switches_pointer_and_queues_old_thread(tmp_path):
    store, conn = _store(tmp_path)
    conversation_id = store.create_session("u1")["conversation_id"]
    old = store.ensure_thread(conversation_id, at=NOW)[0]
    store.complete_turn(conversation_id, "turn-1")
    store.complete_turn(conversation_id, "turn-2")

    new = store.rotate_thread(
        conversation_id, expected_thread_id=old, reason="turn_limit", at=NOW
    )

    assert new is not None and new != old
    assert store.get_thread_id(conversation_id) == new
    assert store.turn_state(conversation_id)["completed_turns"] == 0
    assert store.turn_state(conversation_id)["last_completed_turn_id"] is None
    assert [row["thread_id"] for row in store.list_cleanup_queue()] == [old]
    conn.close()


def test_rotate_thread_is_a_noop_when_pointer_already_moved(tmp_path):
    store, conn = _store(tmp_path)
    conversation_id = store.create_session("u1")["conversation_id"]
    current = store.ensure_thread(conversation_id, at=NOW)[0]

    assert (
        store.rotate_thread(
            conversation_id, expected_thread_id="someone-else", reason="turn_limit", at=NOW
        )
        is None
    )
    assert store.get_thread_id(conversation_id) == current
    assert store.list_cleanup_queue() == []
    conn.close()


def test_rotate_thread_refreshes_last_ai_execution_at(tmp_path):
    store, conn = _store(tmp_path)
    conversation_id = store.create_session("u1")["conversation_id"]
    old = store.ensure_thread(conversation_id, at=NOW - timedelta(days=2))[0]

    store.rotate_thread(
        conversation_id, expected_thread_id=old, reason="idle_timeout", at=NOW
    )

    last = parse_utc_timestamp(store.turn_state(conversation_id)["last_ai_execution_at"])
    assert last == NOW
    conn.close()


# --- 保留键白名单 -----------------------------------------------------------


def test_preserved_context_keys_cover_policy_counters():
    assert {
        "fallback_count",
        "clarify_count",
        "clarify_route_intent",
        "clarify_target_graph",
        "complaint_offer_pending",
        "complaint_summary",
    } <= set(PRESERVED_CONTEXT_KEYS)


# --- 轮换编排（prepare_ai_turn） ---------------------------------------------

from ai_service_agent.harness.approvals import ApprovalService
from ai_service_agent.harness.handoff import HandoffService
from ai_service_agent.harness.messages import MessageStore


class _Harness:
    """真实 sqlite + 注入时钟；checkpoint 检查与删除用注入桩。"""

    def __init__(self, tmp_path, *, interrupts=None, old_context=None, delete_error=None):
        self.conn = sqlite3.connect(tmp_path / "rotation-orchestration.db")
        self.store = ConversationStatusStore(self.conn)
        self.messages = MessageStore(self.conn)
        self.interrupts = list(interrupts or [])
        self.old_context = old_context
        self.delete_error = delete_error
        self.deleted: list[str] = []
        self.service = ThreadLifecycleService(
            self.store,
            interrupt_loader=self._load_interrupts,
            context_reader=self._read_context,
            delete_thread=self._delete,
        )

    async def _load_interrupts(self, thread_id):
        return list(self.interrupts)

    async def _read_context(self, thread_id):
        return self.old_context

    async def _delete(self, thread_id):
        if self.delete_error:
            raise RuntimeError(self.delete_error)
        self.deleted.append(thread_id)

    def seed(self, *, completed_turns=0, idle_hours=0.0):
        conversation_id = self.store.create_session("u1")["conversation_id"]
        thread_id, _ = self.store.ensure_thread(conversation_id, at=NOW)
        for index in range(completed_turns):
            self.store.complete_turn(conversation_id, f"turn-{index}")
        if idle_hours:
            self.store.mark_ai_execution(
                conversation_id, thread_id, at=NOW - timedelta(hours=idle_hours)
            )
        return conversation_id, thread_id


@pytest.mark.asyncio
async def test_prepare_ai_turn_rotates_at_turn_limit_and_deletes_old_thread(tmp_path):
    harness = _Harness(tmp_path)
    conversation_id, old_thread = harness.seed(completed_turns=DEFAULT_ROTATION_MAX_TURNS)

    turn = await harness.service.prepare_ai_turn(conversation_id, harness.messages, at=NOW)

    assert turn.thread_id != old_thread
    assert turn.rotated_from == old_thread
    assert turn.rotation_reason == "turn_limit"
    assert harness.store.get_thread_id(conversation_id) == turn.thread_id
    assert harness.deleted == [old_thread]
    assert harness.store.list_cleanup_queue() == []
    harness.conn.close()


@pytest.mark.asyncio
async def test_prepare_ai_turn_does_not_rotate_below_thresholds(tmp_path):
    harness = _Harness(tmp_path)
    conversation_id, thread_id = harness.seed(completed_turns=3)

    turn = await harness.service.prepare_ai_turn(conversation_id, harness.messages, at=NOW)

    assert turn.thread_id == thread_id
    assert turn.rotated_from is None
    assert harness.deleted == []
    harness.conn.close()


@pytest.mark.asyncio
async def test_prepare_ai_turn_rotates_after_idle_timeout(tmp_path):
    harness = _Harness(tmp_path)
    conversation_id, old_thread = harness.seed(completed_turns=1, idle_hours=25)

    turn = await harness.service.prepare_ai_turn(conversation_id, harness.messages, at=NOW)

    assert turn.rotation_reason == "idle_timeout"
    assert turn.thread_id != old_thread
    harness.conn.close()


@pytest.mark.asyncio
async def test_pending_form_interrupt_blocks_rotation(tmp_path):
    harness = _Harness(tmp_path, interrupts=[{"type": "confirmation_required"}])
    conversation_id, thread_id = harness.seed(completed_turns=DEFAULT_ROTATION_MAX_TURNS)

    turn = await harness.service.prepare_ai_turn(conversation_id, harness.messages, at=NOW)

    assert turn.thread_id == thread_id
    assert turn.rotated_from is None
    assert harness.deleted == []
    harness.conn.close()


@pytest.mark.asyncio
async def test_approval_execution_gap_blocks_rotation(tmp_path):
    harness = _Harness(tmp_path)
    conversation_id, thread_id = harness.seed(completed_turns=DEFAULT_ROTATION_MAX_TURNS)
    approvals = ApprovalService(harness.conn)
    approvals.insert_if_absent(
        "ap-1",
        conversation_id,
        thread_id,
        {"summary": "退款", "expires_at": (NOW + timedelta(minutes=30)).isoformat()},
    )
    approvals.mark_decided("ap-1", "approved", decided_by="seat:1")

    turn = await harness.service.prepare_ai_turn(conversation_id, harness.messages, at=NOW)

    assert turn.thread_id == thread_id
    assert turn.rotated_from is None
    harness.conn.close()


@pytest.mark.asyncio
async def test_waiting_handoff_blocks_rotation(tmp_path):
    harness = _Harness(tmp_path)
    conversation_id, thread_id = harness.seed(completed_turns=DEFAULT_ROTATION_MAX_TURNS)
    HandoffService(harness.conn, harness.store).request_handoff(conversation_id, "explicit", "转人工")

    turn = await harness.service.prepare_ai_turn(conversation_id, harness.messages, at=NOW)

    assert turn.thread_id == thread_id
    assert turn.rotated_from is None
    harness.conn.close()


@pytest.mark.asyncio
async def test_delete_failure_keeps_old_thread_queued_and_still_returns_turn(tmp_path):
    harness = _Harness(tmp_path, delete_error="disk busy")
    conversation_id, old_thread = harness.seed(completed_turns=DEFAULT_ROTATION_MAX_TURNS)

    turn = await harness.service.prepare_ai_turn(conversation_id, harness.messages, at=NOW)

    assert turn.thread_id != old_thread
    queued = harness.store.list_cleanup_queue()
    assert [row["thread_id"] for row in queued] == [old_thread]
    assert queued[0]["retry_count"] == 1
    assert "disk busy" in (queued[0]["last_error"] or "")
    harness.conn.close()


@pytest.mark.asyncio
async def test_rotation_carries_only_preserved_context_keys(tmp_path):
    harness = _Harness(
        tmp_path,
        old_context={
            "fallback_count": 2,
            "clarify_count": 1,
            "clarify_route_intent": "order_mutation",
            "complaint_offer_pending": True,
            "pending_tool": "cancel_order",
            "mutation_form": {"x": 1},
        },
    )
    conversation_id, _ = harness.seed(completed_turns=DEFAULT_ROTATION_MAX_TURNS)

    turn = await harness.service.prepare_ai_turn(conversation_id, harness.messages, at=NOW)

    assert turn.context_seed == {
        "fallback_count": 2,
        "clarify_count": 1,
        "clarify_route_intent": "order_mutation",
        "complaint_offer_pending": True,
    }
    harness.conn.close()


@pytest.mark.asyncio
async def test_rotated_thread_hydrates_bounded_recent_context(tmp_path):
    harness = _Harness(tmp_path)
    conversation_id, _ = harness.seed(completed_turns=DEFAULT_ROTATION_MAX_TURNS)
    for index in range(20):
        harness.messages.append(conversation_id, "user", f"问题 {index}")
        harness.messages.append(conversation_id, "assistant", f"回答 {index}")
    harness.messages.append(conversation_id, "user", "现在的问题")

    turn = await harness.service.prepare_ai_turn(conversation_id, harness.messages, at=NOW)

    assert 0 < len(turn.history) < 40
    assert turn.history[-1].content == "现在的问题"
    harness.conn.close()


@pytest.mark.asyncio
async def test_rotation_is_skipped_when_checkpoint_cannot_be_inspected(tmp_path):
    conn = sqlite3.connect(tmp_path / "no-loader.db")
    store = ConversationStatusStore(conn)
    conversation_id = store.create_session("u1")["conversation_id"]
    thread_id, _ = store.ensure_thread(conversation_id, at=NOW)
    for index in range(DEFAULT_ROTATION_MAX_TURNS):
        store.complete_turn(conversation_id, f"turn-{index}")

    service = ThreadLifecycleService(store)  # 未注入检查能力 -> 无法证明安全 -> 不轮换
    turn = await service.prepare_ai_turn(conversation_id, MessageStore(conn), at=NOW)

    assert turn.thread_id == thread_id
    assert turn.rotated_from is None
    conn.close()


# --- Transport 接线 ---------------------------------------------------------

from fastapi.testclient import TestClient

from ai_service_agent.harness.chatwoot import ChatwootMappingStore
from ai_service_agent.harness.server import create_app
from ai_service_agent.testing import ApprovalFlowFakeModel


def _chat(client, conversation_id: str, message: str = "你好") -> str:
    response = client.post(
        "/chat",
        json={"user_id": "u1", "session_id": conversation_id, "message": message},
    )
    assert response.status_code == 200
    return response.text


def _row(db: str, conversation_id: str) -> dict:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM conversation_status WHERE conversation_id = ?", (conversation_id,)
    ).fetchone()
    conn.close()
    assert row is not None
    return dict(row)


def _set_completed_turns(db: str, conversation_id: str, turns: int) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE conversation_status SET completed_turns = ? WHERE conversation_id = ?",
        (turns, conversation_id),
    )
    conn.commit()
    conn.close()


def _checkpoint_threads(db: str) -> set[str]:
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute("SELECT DISTINCT thread_id FROM checkpoints").fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    return {row[0] for row in rows}


def test_chat_counts_completed_turns_and_reuses_thread(tmp_path):
    db = str(tmp_path / "count.db")
    client = TestClient(create_app(db, model="fake"))
    conversation_id = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]

    _chat(client, conversation_id)
    first = _row(db, conversation_id)
    _chat(client, conversation_id, "再问一句")
    second = _row(db, conversation_id)

    assert first["thread_id"] is not None
    assert first["completed_turns"] == 1
    assert second["thread_id"] == first["thread_id"]
    assert second["completed_turns"] == 2


def test_next_turn_after_limit_rotates_once_and_keeps_conversation(tmp_path):
    db = str(tmp_path / "rotate.db")
    client = TestClient(create_app(db, model="fake"))
    conversation_id = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    _chat(client, conversation_id)
    _set_completed_turns(db, conversation_id, DEFAULT_ROTATION_MAX_TURNS)
    old_thread = _row(db, conversation_id)["thread_id"]

    _chat(client, conversation_id, "第 31 轮")

    after = _row(db, conversation_id)
    assert after["thread_id"] != old_thread
    assert after["completed_turns"] == 1  # 轮换清零后本轮重新计 1
    assert after["conversation_id"] == conversation_id
    items = client.get(f"/conversations/{conversation_id}/messages").json()["items"]
    assert len(items) >= 4  # 历史消息全在，不随 thread 轮换丢失
    assert old_thread not in _checkpoint_threads(db)

    _chat(client, conversation_id, "第 32 轮")
    assert _row(db, conversation_id)["thread_id"] == after["thread_id"]


@pytest.mark.asyncio
async def test_agent_bot_uses_the_same_rotation_threshold(tmp_path):
    from ai_service_agent.harness.agent_bot import handle_agent_bot_message

    conn = sqlite3.connect(tmp_path / "agent-bot-rotate.db")
    store = ConversationStatusStore(conn)
    messages = MessageStore(conn)
    conversation_id = store.create_session("u-bot")["conversation_id"]
    old_thread, _ = store.ensure_thread(conversation_id, at=NOW)
    for index in range(DEFAULT_ROTATION_MAX_TURNS):
        store.complete_turn(conversation_id, f"turn-{index}")
    ChatwootMappingStore(conn).put(conversation_id, 4242)

    deleted: list[str] = []

    async def interrupt_loader(thread_id):
        return []

    async def context_reader(thread_id):
        return {"fallback_count": 2}

    async def delete_thread(thread_id):
        deleted.append(thread_id)

    service = ThreadLifecycleService(
        store,
        interrupt_loader=interrupt_loader,
        context_reader=context_reader,
        delete_thread=delete_thread,
    )
    payload = {
        "event": "message_created",
        "id": "900",
        "content": "你好",
        "message_type": "incoming",
        "content_type": "text",
        "conversation": {"id": 4242},
        "contact": {"id": 9, "identifier": "u-bot", "name": "bot-user"},
    }

    class _Chatwoot:
        async def send_bot_message(self, *args, **kwargs):
            return None

        async def prompt_handoff_request(self, *args, **kwargs):
            return None

    await handle_agent_bot_message(
        payload,
        _Chatwoot(),
        status=store,
        messages_store=messages,
        mapping=ChatwootMappingStore(conn),
        threads=service,
    )

    assert store.get_thread_id(conversation_id) != old_thread
    assert deleted == [old_thread]
    # 轮换清零后本轮重新计 1（graph=None 的桩回复也算一次交付）。
    assert store.turn_state(conversation_id)["completed_turns"] == 1
    conn.close()


# --- 并发 -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_turns_allocate_exactly_one_thread(tmp_path):
    import asyncio

    harness = _Harness(tmp_path)
    conversation_id = harness.store.create_session("u1")["conversation_id"]

    turns = await asyncio.gather(
        *[harness.service.prepare_ai_turn(conversation_id, harness.messages, at=NOW) for _ in range(5)]
    )

    assert len({turn.thread_id for turn in turns}) == 1
    assert harness.store.get_thread_id(conversation_id) == turns[0].thread_id
    harness.conn.close()


def test_concurrent_rotation_moves_the_pointer_only_once(tmp_path):
    harness = _Harness(tmp_path)
    conversation_id, old_thread = harness.seed(completed_turns=DEFAULT_ROTATION_MAX_TURNS)

    first = harness.store.rotate_thread(
        conversation_id, expected_thread_id=old_thread, reason="turn_limit", at=NOW
    )
    second = harness.store.rotate_thread(
        conversation_id, expected_thread_id=old_thread, reason="turn_limit", at=NOW
    )

    assert first is not None
    assert second is None  # 指针已移动，第二个请求不得再切一次
    assert harness.store.get_thread_id(conversation_id) == first
    assert [row["thread_id"] for row in harness.store.list_cleanup_queue()] == [old_thread]
    harness.conn.close()


@pytest.mark.asyncio
async def test_rotation_race_with_cleanup_never_reuses_detached_thread(tmp_path):
    """指针被清理摘掉时（rotate 返回 None 且 current 为 NULL），必须新建 thread。

    实测教训：``Command(resume=...)`` 在已删 thread 上不报错，会静默从 START 重跑。
    """
    harness = _Harness(tmp_path)
    conversation_id, old_thread = harness.seed(completed_turns=DEFAULT_ROTATION_MAX_TURNS)

    original_rotate = harness.store.rotate_thread

    def detach_then_fail(*args, **kwargs):
        harness.store.clear_thread_pointer(conversation_id, expected_thread_id=old_thread)
        return None

    harness.store.rotate_thread = detach_then_fail  # type: ignore[method-assign]
    try:
        turn = await harness.service.prepare_ai_turn(conversation_id, harness.messages, at=NOW)
    finally:
        harness.store.rotate_thread = original_rotate  # type: ignore[method-assign]

    assert turn.thread_id != old_thread
    assert harness.store.get_thread_id(conversation_id) == turn.thread_id
    harness.conn.close()


@pytest.mark.asyncio
async def test_expired_pending_approval_still_blocks_rotation(tmp_path):
    """review I1：checkpoint 里还有 approval_required 中断时，即使审批已过期也不得轮换。

    轮换会删掉旧 thread，导致「审批已过期」的收尾回复永远发不出去（spec §4.1）。
    """
    harness = _Harness(tmp_path)
    conversation_id, thread_id = harness.seed(completed_turns=DEFAULT_ROTATION_MAX_TURNS)
    approvals = ApprovalService(harness.conn)
    approvals.insert_if_absent(
        "ap-expired",
        conversation_id,
        thread_id,
        {"summary": "退款", "expires_at": (NOW - timedelta(minutes=5)).isoformat()},
    )
    harness.interrupts = [
        {"type": "approval_required", "approval_id": "ap-expired",
         "expires_at": (NOW - timedelta(minutes=5)).isoformat()}
    ]

    turn = await harness.service.prepare_ai_turn(conversation_id, harness.messages, at=NOW)

    assert turn.thread_id == thread_id
    assert turn.rotated_from is None
    assert harness.deleted == []
    harness.conn.close()


@pytest.mark.asyncio
async def test_unexpired_pending_approval_blocks_rotation(tmp_path):
    harness = _Harness(tmp_path)
    conversation_id, thread_id = harness.seed(completed_turns=DEFAULT_ROTATION_MAX_TURNS)
    ApprovalService(harness.conn).insert_if_absent(
        "ap-live",
        conversation_id,
        thread_id,
        {"summary": "退款", "expires_at": (NOW + timedelta(minutes=10)).isoformat()},
    )

    turn = await harness.service.prepare_ai_turn(conversation_id, harness.messages, at=NOW)

    assert turn.thread_id == thread_id
    assert turn.rotated_from is None
    harness.conn.close()


# --- 端到端：context_seed 真的进了新 thread 的 state -------------------------

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from ai_service_agent.harness.graph import make_ai_graph
from ai_service_agent.testing import make_fake_model


def _write_thread_context(db: str, thread_id: str, context: dict) -> None:
    async def run():
        async with AsyncSqliteSaver.from_conn_string(db) as saver:
            graph = make_ai_graph(make_fake_model(), saver)
            await graph.aupdate_state(
                {"configurable": {"thread_id": thread_id}},
                {"context": context},
                as_node="complaint_graph",
            )

    asyncio.run(run())


def _read_thread_context(db: str, thread_id: str) -> dict:
    async def run():
        async with AsyncSqliteSaver.from_conn_string(db) as saver:
            graph = make_ai_graph(make_fake_model(), saver)
            state = await graph.aget_state({"configurable": {"thread_id": thread_id}})
            return dict(state.values.get("context") or {})

    return asyncio.run(run())


def test_rotation_seeds_preserved_context_into_new_thread_state(tmp_path):
    """AC #10 的端到端接线：白名单键要真的落到新 thread 的图 state 里。"""
    db = str(tmp_path / "seed-e2e.db")
    client = TestClient(create_app(db, model="fake"))
    conversation_id = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    _chat(client, conversation_id)
    old_thread = _row(db, conversation_id)["thread_id"]
    _write_thread_context(
        db, old_thread, {"fallback_count": 2, "pending_tool": "cancel_order"}
    )
    _set_completed_turns(db, conversation_id, DEFAULT_ROTATION_MAX_TURNS)

    _chat(client, conversation_id, "再问一句")

    new_thread = _row(db, conversation_id)["thread_id"]
    assert new_thread != old_thread
    context = _read_thread_context(db, new_thread)
    assert context.get("fallback_count") == 2  # 白名单键跨代保留
    assert "pending_tool" not in context  # 非白名单键不复制


@pytest.mark.asyncio
async def test_agent_bot_takes_the_conversation_level_lock(tmp_path):
    """AC #12/#13：AgentBot 与 /chat 共用 conversation:{id} 锁。"""
    from ai_service_agent.harness.agent_bot import handle_agent_bot_message

    conn = sqlite3.connect(tmp_path / "agent-bot-lock.db")
    store = ConversationStatusStore(conn)
    messages = MessageStore(conn)
    conversation_id = store.create_session("u-lock")["conversation_id"]
    ChatwootMappingStore(conn).put(conversation_id, 5252)
    keys: list[str] = []

    async def factory(key):
        keys.append(key)
        return asyncio.Lock()

    class _Chatwoot:
        async def send_bot_message(self, *args, **kwargs):
            return None

    await handle_agent_bot_message(
        {
            "event": "message_created",
            "id": "901",
            "content": "你好",
            "message_type": "incoming",
            "content_type": "text",
            "conversation": {"id": 5252},
            "contact": {"id": 9, "identifier": "u-lock", "name": "lock-user"},
        },
        _Chatwoot(),
        status=store,
        messages_store=messages,
        mapping=ChatwootMappingStore(conn),
        conversation_lock_factory=factory,
    )

    assert keys == [f"conversation:{conversation_id}"]
    conn.close()


# --- 端到端：context_seed 真的进了新 thread 的 state -------------------------

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from ai_service_agent.harness.graph import make_ai_graph
from ai_service_agent.testing import make_fake_model


def _write_thread_context(db: str, thread_id: str, context: dict) -> None:
    async def run():
        async with AsyncSqliteSaver.from_conn_string(db) as saver:
            graph = make_ai_graph(make_fake_model(), saver)
            await graph.aupdate_state(
                {"configurable": {"thread_id": thread_id}},
                {"context": context},
                as_node="complaint_graph",
            )

    asyncio.run(run())


def _read_thread_context(db: str, thread_id: str) -> dict:
    async def run():
        async with AsyncSqliteSaver.from_conn_string(db) as saver:
            graph = make_ai_graph(make_fake_model(), saver)
            state = await graph.aget_state({"configurable": {"thread_id": thread_id}})
            return dict(state.values.get("context") or {})

    return asyncio.run(run())


def test_rotation_seeds_preserved_context_into_new_thread_state(tmp_path):
    """AC #10 的端到端接线：白名单键要真的落到新 thread 的图 state 里。"""
    db = str(tmp_path / "seed-e2e.db")
    client = TestClient(create_app(db, model="fake"))
    conversation_id = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    _chat(client, conversation_id)
    old_thread = _row(db, conversation_id)["thread_id"]
    _write_thread_context(
        db, old_thread, {"fallback_count": 2, "pending_tool": "cancel_order"}
    )
    _set_completed_turns(db, conversation_id, DEFAULT_ROTATION_MAX_TURNS)

    _chat(client, conversation_id, "再问一句")

    new_thread = _row(db, conversation_id)["thread_id"]
    assert new_thread != old_thread
    context = _read_thread_context(db, new_thread)
    assert context.get("fallback_count") == 2  # 白名单键跨代保留
    assert "pending_tool" not in context  # 非白名单键不复制


@pytest.mark.asyncio
async def test_agent_bot_takes_the_conversation_level_lock(tmp_path):
    """AC #12/#13：AgentBot 与 /chat 共用 conversation:{id} 锁。"""
    from ai_service_agent.harness.agent_bot import handle_agent_bot_message

    conn = sqlite3.connect(tmp_path / "agent-bot-lock.db")
    store = ConversationStatusStore(conn)
    messages = MessageStore(conn)
    conversation_id = store.create_session("u-lock")["conversation_id"]
    ChatwootMappingStore(conn).put(conversation_id, 5252)
    keys: list[str] = []

    async def factory(key):
        keys.append(key)
        return asyncio.Lock()

    class _Chatwoot:
        async def send_bot_message(self, *args, **kwargs):
            return None

    await handle_agent_bot_message(
        {
            "event": "message_created",
            "id": "901",
            "content": "你好",
            "message_type": "incoming",
            "content_type": "text",
            "conversation": {"id": 5252},
            "contact": {"id": 9, "identifier": "u-lock", "name": "lock-user"},
        },
        _Chatwoot(),
        status=store,
        messages_store=messages,
        mapping=ChatwootMappingStore(conn),
        conversation_lock_factory=factory,
    )

    assert keys == [f"conversation:{conversation_id}"]
    conn.close()
