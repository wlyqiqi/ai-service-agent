# Thread 空闲回收（#151）Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 在单进程 Demo 里提供一个可测的 `cleanup_once(now)`，自动回收 24 小时无 AI 请求的 thread，失败可退避重试，且绝不删除在途 / 挂起 / 人工接管中的 thread。

**Architecture:** 把现有离线 CLI `ops/thread_cleanup.py` 里的清理循环抽成 `harness/thread_reaper.py` 的 `ThreadReaper.cleanup_once()`；CLI 与 FastAPI lifespan 都只做薄入口。运行时入口对每条候选先取 `conversation:{id}` 锁、再取裸 `thread_id` 锁，**两把锁都拿到后重读指针并重跑 `collect_blockers`**，然后摘指针 + 入队（同一事务）、调用官方 `adelete_thread()`。

**Tech Stack:** Python 3.13 / FastAPI lifespan / LangGraph `AsyncSqliteSaver` / sqlite3（单连接）/ pytest + `ApprovalFlowFakeModel` / pydantic-settings。

---

## 执行者必读：四个会直接做错的地方

### 1. 工作目录

```
D:\AI_Project\AI Service Agent\.worktrees\thread-idle-reaper\AI Service Agent
```
这是 worktree 的**下一层**。根目录是 `.worktrees\thread-idle-reaper`（git root）。所有命令都在项目目录跑。

共享 venv 的 `.pth` 指向**主 worktree** 的 src，所以：
- `pytest` 安全（`tests/conftest.py` 把 `ROOT/src` 插到 `sys.path[0]`）
- 裸跑 `python -c "import ai_service_agent"` 会**静默加载主 worktree 代码**。需要时加 `PYTHONPATH=<本目录>/src`
- 不要 `pip install -e` 到共享 venv

首次跑测试前先 seed 业务库（否则审批类用例会因 `refund_not_allowed` 假失败）：

```bash
cd "D:/AI_Project/AI Service Agent/.worktrees/thread-idle-reaper/AI Service Agent"
"D:/AI_Project/AI Service Agent/.venv/Scripts/python.exe" scripts/seed_mock_db.py
```

### 2. 锁的 key 是裸 `thread_id`，不是 `thread:{id}`

`server.py:154` 的 `_thread_lock(key)` **不拼前缀**：

| 调用点 | 传入的 key |
|---|---|
| `server.py:912`（`/chat`） | `f"conversation:{conv_id}"` |
| `server.py:604`（handoff resolve） | **裸 `thread_id`** |
| `server.py:654`（seat takeover） | **裸 `thread_id`** |
| `server.py:1065`（`/actions/confirm`） | **裸 `thread_id`** |
| `approval_side_channel.py:419`（坐席 `decide()`） | **裸 `thread_id`** |
| `agent_bot.py:197` | `f"chatwoot:{cw_id}"` |
| `agent_bot.py:284` | `f"conversation:{conv_id}"` |
| `agent_bot.py:596` | **裸 `thread_id`** |

写 `f"thread:{thread_id}"` 会新建一把**谁都拿不到**的锁，四条 resume 路径的并发保护等于零，而且测试全绿。

### 3. 同步 sqlite 不包 `to_thread`

`create_app` 用单连接 `sqlite3.connect(db_path, check_same_thread=False)`。把它丢进 `asyncio.to_thread` 会让同一连接跨线程并发使用，比阻塞更危险；而单条候选的判定只有 3 次索引查询（毫秒级），真正的耗时是 `adelete_thread()`（aiosqlite，`await` 时让出事件循环）。所以：**不 `to_thread`**，改为每条候选处理后 `await asyncio.sleep(0)` 让出，并用 `batch_size` 限制单轮工作量。

两个已知的 SQLite 层事实（不是本计划引入，但别被误导）：

- 所有 `*Store` 共用 `create_app` 里那一条连接，而 `adelete_thread()` 走 aiosqlite 的**另一条**连接。跨会话并发写会短暂 `database is locked`；Python `sqlite3` 默认 `timeout=5.0` 会自动重试，Demo 量级够用。
- 首轮 reaper 在启动后**先等一个 interval** 才跑（`_run_thread_reaper` 循环开头就 `sleep`），避免启动瞬间打库。

### 4. lifespan 默认必须关闭

`app = FastAPI()` 现在没有 lifespan。加上之后，**每个 `TestClient(create_app(...))` 都会启动一个 reaper 协程** —— 现有 1200 个用例里所有用到 TestClient 的都会被牵连。所以 `THREAD_REAPER_INTERVAL_SECONDS` 默认 `0`（关闭），测试里再显式钉一次。

`main.py` 用 `uvicorn.run(app, ...)` 传的是 **app 对象**，不是 import string，`--workers` 在这种启动方式下根本无法生效 —— 单进程约束天然成立，不需要运行时探测。

---

## Task 1: 队列退避（`next_retry_at`）

**Files:**
- Modify: `src/ai_service_agent/harness/session.py:34-41`（DDL）、`:399-405`（`record_cleanup_failure`）、`:378-393`（`list_cleanup_queue`）
- Test: `tests/test_thread_cleanup.py`（追加）

**Step 1: 写失败测试**

追加到 `tests/test_thread_cleanup.py`：

```python
# --- #151：队列退避 ---------------------------------------------------------

from ai_service_agent.harness.session import retry_backoff


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
```

**Step 2: 跑测试确认失败**

```bash
"D:/AI_Project/AI Service Agent/.venv/Scripts/python.exe" -m pytest tests/test_thread_cleanup.py -q -p no:randomly -k "backoff or next_retry"
```
Expected: FAIL —— `ImportError: cannot import name 'retry_backoff'`

**Step 3: 实现**

`session.py` 顶部加常量与纯函数（放在 `parse_utc_timestamp` 之后）：

```python
#: 队列删除失败后的退避：30 分钟起，翻倍，4 小时封顶。
RETRY_BACKOFF_BASE = timedelta(minutes=30)
RETRY_BACKOFF_MAX = timedelta(hours=4)


def retry_backoff(retry_count: int) -> timedelta:
    """``retry_count`` 是**本次失败后**的累计次数（>= 1）。"""
    exponent = max(0, retry_count - 1)
    return min(RETRY_BACKOFF_BASE * (2**exponent), RETRY_BACKOFF_MAX)
```

DDL 加列（`thread_cleanup_queue`）：

```sql
CREATE TABLE IF NOT EXISTS thread_cleanup_queue (
    thread_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    queued_at TEXT NOT NULL,
    retry_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    next_retry_at TEXT
);
```

`_migrate` **末尾**（`last_completed_turn_id` 分支之后，当前 `session.py:113`）补幂等加列。`SCHEMA` 用的是 `CREATE TABLE IF NOT EXISTS`，对已有库不会补列，所以必须 `ALTER`：

```python
        queue_columns = {
            item[1]
            for item in self.conn.execute("PRAGMA table_info(thread_cleanup_queue)").fetchall()
        }
        if queue_columns and "next_retry_at" not in queue_columns:
            self.conn.execute("ALTER TABLE thread_cleanup_queue ADD COLUMN next_retry_at TEXT")
```

`queue_columns` 为空说明表不存在（理论上 `SCHEMA` 已建，防御而已），此时跳过。

`record_cleanup_failure` 改为：

```python
    def record_cleanup_failure(
        self,
        thread_id: str,
        error: str,
        *,
        at: datetime | None = None,
    ) -> None:
        """记一次删除失败并安排下次重试。

        退避用 Python 侧算好再写：``queued_at`` / ``next_retry_at`` 都是
        ``isoformat()`` 产物，在 SQL 里做字符串比较会在同一秒内判错
        （``+00:00`` 与 ``.123456+00:00`` 的字典序不等于时间序）。
        """
        row = self.conn.execute(
            "SELECT retry_count FROM thread_cleanup_queue WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        if row is None:
            return
        next_count = int(row[0] or 0) + 1
        next_at = (at or datetime.now(timezone.utc)) + retry_backoff(next_count)
        self.conn.execute(
            "UPDATE thread_cleanup_queue "
            "SET retry_count = ?, last_error = ?, next_retry_at = ? WHERE thread_id = ?",
            (next_count, error[:2000], _utc_now_iso(next_at), thread_id),
        )
        self.conn.commit()
```

`list_cleanup_queue` 加 `now` 过滤（Python 侧解析，绝不写 SQL 字符串比较）：

```python
    def list_cleanup_queue(self, *, now: datetime | None = None) -> list[dict]:
        rows = self.conn.execute(
            "SELECT thread_id, conversation_id, reason, queued_at, retry_count, last_error, "
            "next_retry_at FROM thread_cleanup_queue ORDER BY queued_at, thread_id"
        ).fetchall()
        result: list[dict] = []
        for row in rows:
            next_retry_at = row[6]
            if now is not None:
                due = parse_utc_timestamp(next_retry_at)
                if due is not None and due > now:
                    continue
            result.append(
                {
                    "thread_id": row[0],
                    "conversation_id": row[1],
                    "reason": row[2],
                    "queued_at": row[3],
                    "retry_count": row[4],
                    "last_error": row[5],
                    "next_retry_at": next_retry_at,
                }
            )
        return result
```

`session.py` 顶部 `from datetime import datetime, timezone` 需要补 `timedelta`。

**Step 4: 跑测试确认通过**

```bash
"D:/AI_Project/AI Service Agent/.venv/Scripts/python.exe" -m pytest tests/test_thread_cleanup.py -q -p no:randomly
```
Expected: PASS（原有 6 例 + 新增 3 例）

**Step 5: Commit**

```bash
git add src/ai_service_agent/harness/session.py tests/test_thread_cleanup.py
git commit -m "feat(session): queue retry backoff via next_retry_at (#151)"
```

---

## Task 2: 候选排除 `mode='human'`，Form 中断单独标记

**Files:**
- Modify: `src/ai_service_agent/harness/session.py:407-432`（`idle_conversations`）
- Modify: `src/ai_service_agent/harness/thread_guard.py:77-108`（`interrupt_protection`）
- Test: `tests/test_thread_cleanup.py`（追加）

**Step 1: 写失败测试**

```python
# --- #151：候选范围与 Form 标记 ---------------------------------------------


def test_idle_conversations_excludes_human_mode_by_default(tmp_path):
    db = str(tmp_path / "human.db")
    conn = sqlite3.connect(db)
    status = ConversationStatusStore(conn)
    now = datetime.now(timezone.utc)
    old = now - timedelta(hours=48)

    ai_cid = status.create_session("u1")["conversation_id"]
    ai_tid, _ = status.ensure_thread(ai_cid)
    status.mark_ai_execution(ai_cid, ai_tid, at=old)

    human_cid = status.create_session("u2")["conversation_id"]
    human_tid, _ = status.ensure_thread(human_cid)
    status.mark_ai_execution(human_cid, human_tid, at=old)
    status.set_mode(human_cid, "human")

    default_ids = [row["conversation_id"] for row in status.idle_conversations(now=now, idle_after_seconds=3600)]
    with_human = [
        row["conversation_id"]
        for row in status.idle_conversations(
            now=now, idle_after_seconds=3600, exclude_human=False
        )
    ]

    assert default_ids == [ai_cid]
    assert set(with_human) == {ai_cid, human_cid}
    conn.close()


@pytest.mark.asyncio
async def test_form_interrupt_is_labelled_blocked_by_form():
    from ai_service_agent.harness.thread_guard import interrupt_protection

    value = {"tool": "cancel_order", "form_session_id": "fs-1", "summary": "确认取消"}
    now = datetime.now(timezone.utc)

    assert interrupt_protection(value, [], now) == "blocked_by_form"
```

**Step 2: 跑测试确认失败**

```bash
"D:/AI_Project/AI Service Agent/.venv/Scripts/python.exe" -m pytest tests/test_thread_cleanup.py -q -p no:randomly -k "human_mode or blocked_by_form"
```
Expected: FAIL —— `TypeError: idle_conversations() got an unexpected keyword argument 'exclude_human'`，以及 `assert 'checkpoint_interrupt' == 'blocked_by_form'`

**Step 3: 实现**

`idle_conversations` 加参数与 SQL 过滤：

```python
    def idle_conversations(
        self,
        *,
        now: datetime,
        idle_after_seconds: float,
        exclude_human: bool = True,
    ) -> list[dict]:
        """空闲会话候选。

        ``exclude_human=True``（默认）跳过人工接管中的会话：``last_ai_execution_at``
        在 human 模式下不再更新，不排除会让「坐席还在服务」的会话被 AI 空闲计时器回收。
        """
        sql = (
            "SELECT conversation_id, thread_id, last_ai_execution_at, created_at "
            "FROM conversation_status WHERE thread_id IS NOT NULL "
        )
        if exclude_human:
            sql += "AND COALESCE(mode, 'ai') != 'human' "
        sql += "ORDER BY COALESCE(last_ai_execution_at, created_at), conversation_id"
        rows = self.conn.execute(sql).fetchall()
        # ...（以下循环保持不变）
```

`thread_guard.interrupt_protection` 加 Form 分支（在 `approval_required` 判断之后、`checkpoint_interrupt` 兜底之前）：

```python
    if "form_session_id" in value:
        # Mutation / HighRisk 的 Form Interrupt：确认表单还没提交。
        # 这类挂起目前**永不自动过期**（24h TTL 属 #155），所以会一直钉住 thread。
        return "blocked_by_form"
```

完整顺序应为：

```python
    if not isinstance(value, dict):
        return "checkpoint_interrupt"
    if value.get("type") == "approval_required":
        ...（原有审批逻辑）...
    if "form_session_id" in value:
        return "blocked_by_form"
    return "checkpoint_interrupt"
```

**Step 4: 跑测试确认通过**

```bash
"D:/AI_Project/AI Service Agent/.venv/Scripts/python.exe" -m pytest tests/test_thread_cleanup.py tests/test_thread_rotation.py -q -p no:randomly
```
Expected: PASS。**`test_thread_rotation.py` 必须仍全绿** —— 改的是标签，不是保护与否。

**Step 5: Commit**

```bash
git add src/ai_service_agent/harness/session.py src/ai_service_agent/harness/thread_guard.py tests/test_thread_cleanup.py
git commit -m "feat(thread): skip human-mode conversations and label form interrupts (#151)"
```

---

## Task 3: `ThreadReaper` 引擎（双锁 + 锁后重查）

**Files:**
- Create: `src/ai_service_agent/harness/thread_reaper.py`
- Test: `tests/test_thread_reaper.py`

**Step 1: 写失败测试**

新建 `tests/test_thread_reaper.py`：

```python
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
            ApprovalService(conn).insert_if_absent(
                "ap-late",
                conversation_id,
                thread_id,
                {"summary": "退款", "expires_at": (NOW + timedelta(minutes=20)).isoformat()},
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
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_message WHERE conversation_id = ?", (conversation_id,)
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM chatwoot_conversation WHERE conversation_id = ?",
        (conversation_id,),
    ).fetchone()[0] == 1
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
```

**Step 2: 跑测试确认失败**

```bash
"D:/AI_Project/AI Service Agent/.venv/Scripts/python.exe" -m pytest tests/test_thread_reaper.py -q -p no:randomly
```
Expected: FAIL —— `ModuleNotFoundError: No module named 'ai_service_agent.harness.thread_reaper'`

**Step 3: 实现引擎**

新建 `src/ai_service_agent/harness/thread_reaper.py`：

```python
"""运行时 thread 空闲回收（#151）。

离线 CLI（``ops/thread_cleanup.py``）与 FastAPI lifespan 共用本引擎；
本模块不含 argparse / vacuum / JSON 报告等离线语义。

安全边界：
- 每条候选先取 ``conversation:{conversation_id}`` 锁、再取**裸** ``thread_id`` 锁
  （``server._thread_lock`` 的既有 key 约定），与 ``/chat``、AgentBot、
  ``/actions/confirm``、handoff resolve、seat takeover、坐席 ``decide()`` 全部互斥。
- 两把锁都拿到后**重读指针并重跑** ``collect_blockers``；加锁前的快照一律不用于最终判断。
- 摘指针与入队在同一个 SQLite 事务；删除只调用官方 ``adelete_thread()``。
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from ai_service_agent.harness.session import ConversationStatusStore
from ai_service_agent.harness.thread_guard import collect_blockers

logger = logging.getLogger(__name__)

DEFAULT_IDLE_AFTER = timedelta(hours=24)
DEFAULT_REAP_BATCH_SIZE = 50


class _NullLock:
    """CLI 单进程离线场景：没有并发，但保留同一条 ``async with`` 代码路径。"""

    async def __aenter__(self) -> "_NullLock":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


@dataclass
class ReapItem:
    thread_id: str
    conversation_id: str
    reason: str
    status: str = "candidate"
    protected_by: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class ReapReport:
    mode: str
    candidates: list[ReapItem]
    deleted: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    vacuumed: bool = False
    db_size_before: int = 0
    db_size_after: int = 0

    @property
    def protected(self) -> list[str]:
        return [item.thread_id for item in self.candidates if item.status == "protected"]

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "candidates": [asdict(item) for item in self.candidates],
            "deleted": self.deleted,
            "protected": self.protected,
            "failed": self.failed,
            "vacuumed": self.vacuumed,
            "db_size_before": self.db_size_before,
            "db_size_after": self.db_size_after,
        }

    def summary(self) -> str:
        """一行日志，给 lifespan 用。"""
        return (
            f"mode={self.mode} candidates={len(self.candidates)} "
            f"deleted={len(self.deleted)} protected={len(self.protected)} "
            f"failed={len(self.failed)}"
        )


class ThreadReaper:
    """一轮 ``cleanup_once`` = 扫描 -> 双锁 -> 锁后重查 -> 摘指针 -> 官方删除。"""

    def __init__(
        self,
        *,
        status: ConversationStatusStore,
        db_path: str,
        idle_after: timedelta = DEFAULT_IDLE_AFTER,
        lock_factory: Callable[[str], Awaitable[Any]] | None = None,
        interrupt_loader: Callable[[str], Awaitable[list]] | None = None,
        delete_thread: Callable[[str], Awaitable[None]] | None = None,
    ):
        self.status = status
        self.db_path = str(db_path)
        self.idle_after = idle_after
        self._lock_factory = lock_factory
        self._interrupt_loader = interrupt_loader
        self._delete_thread = delete_thread

    async def _lock(self, key: str) -> Any:
        if self._lock_factory is None:
            return _NullLock()
        return await self._lock_factory(key)

    def _scan(self, now: datetime, batch_size: int) -> list[dict]:
        """同步 sqlite 扫描：只读、不加锁（候选随时可能失效，锁后要重查）。"""
        idle = self.status.idle_conversations(
            now=now,
            idle_after_seconds=self.idle_after.total_seconds(),
            exclude_human=True,
        )
        queue = self.status.list_cleanup_queue(now=now)
        merged: dict[str, dict] = {}
        for row in idle:
            merged[row["thread_id"]] = {**row, "reason": "idle_timeout", "queued": False}
        for row in queue:
            merged[row["thread_id"]] = {**row, "queued": True}
        return list(merged.values())[:batch_size]

    async def _blockers(self, thread_id: str, conversation_id: str, now: datetime) -> list[str]:
        return await collect_blockers(
            conn=self.status.conn,
            thread_id=thread_id,
            conversation_id=conversation_id,
            now=now,
            interrupt_loader=self._interrupt_loader,
            # 清理侧：已过期的审批视为可回收（与轮换侧相反，见 thread_guard 文档）。
            block_expired_approvals=False,
        )

    async def cleanup_once(
        self,
        *,
        now: datetime | None = None,
        apply: bool = True,
        batch_size: int = DEFAULT_REAP_BATCH_SIZE,
    ) -> ReapReport:
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        candidates = self._scan(moment, batch_size)
        report = ReapReport(mode="apply" if apply else "dry-run", candidates=[])
        for candidate in candidates:
            item = await self._process(candidate, now=moment, apply=apply)
            report.candidates.append(item)
            if item.status == "deleted":
                report.deleted.append(item.thread_id)
            elif item.status == "failed":
                report.failed.append(item.thread_id)
            # 同步 sqlite 是毫秒级；让出事件循环，避免长批次霸占 loop。
            await asyncio.sleep(0)
        return report

    async def _process(self, candidate: dict, *, now: datetime, apply: bool) -> ReapItem:
        thread_id = candidate["thread_id"]
        conversation_id = candidate["conversation_id"]
        item = ReapItem(
            thread_id=thread_id,
            conversation_id=conversation_id,
            reason=candidate["reason"],
        )

        if not apply:
            blockers = await self._blockers(thread_id, conversation_id, now)
            if candidate["queued"] and self.status.get_thread_id(conversation_id) == thread_id:
                blockers.append("current_thread_pointer")
            item.status = "protected" if blockers else "deletable"
            item.protected_by = sorted(set(blockers))
            return item

        conversation_lock = await self._lock(f"conversation:{conversation_id}")
        async with conversation_lock:
            thread_lock = await self._lock(thread_id)
            async with thread_lock:
                return await self._reap_locked(candidate, item, now)

    async def _reap_locked(self, candidate: dict, item: ReapItem, now: datetime) -> ReapItem:
        thread_id = candidate["thread_id"]
        conversation_id = candidate["conversation_id"]

        # 锁后重查：指针与保护判定都不复用加锁前的快照。
        pointer = self.status.get_thread_id(conversation_id)
        blockers = await self._blockers(thread_id, conversation_id, now)
        if candidate["queued"] and pointer == thread_id:
            blockers.append("current_thread_pointer")
        if blockers:
            item.status = "protected"
            item.protected_by = sorted(set(blockers))
            logger.info(
                "thread reap blocked: conversation=%s thread=%s blockers=%s",
                conversation_id,
                thread_id,
                item.protected_by,
            )
            return item

        if pointer == thread_id:
            detached = self.status.detach_thread_and_enqueue(
                conversation_id, thread_id, candidate["reason"], at=now
            )
            if not detached:
                item.status = "protected"
                item.protected_by = ["current_thread_pointer_changed"]
                return item
        elif not candidate["queued"]:
            self.status.enqueue_thread(thread_id, conversation_id, candidate["reason"], at=now)

        if self._delete_thread is None:
            item.status = "failed"
            item.error = "delete_thread not configured"
            return item

        try:
            await self._delete_thread(thread_id)
        except Exception as exc:
            item.status = "failed"
            item.error = str(exc)
            logger.warning(
                "thread reap delete failed (kept in queue): thread=%s error=%s", thread_id, exc
            )
            try:
                self.status.record_cleanup_failure(thread_id, str(exc), at=now)
            except Exception:
                logger.exception("record cleanup failure failed: thread=%s", thread_id)
        else:
            item.status = "deleted"
            try:
                self.status.remove_cleanup_item(thread_id)
            except Exception:
                logger.exception("remove cleanup item failed: thread=%s", thread_id)
        return item
```

**Step 4: 跑测试确认通过**

```bash
"D:/AI_Project/AI Service Agent/.venv/Scripts/python.exe" -m pytest tests/test_thread_reaper.py -q -p no:randomly
```
Expected: 9 passed

**Step 5: 补一条「清理后新消息新建 thread」的接线测试**

追加到 `tests/test_thread_reaper.py`：

```python
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
```

跑：

```bash
"D:/AI_Project/AI Service Agent/.venv/Scripts/python.exe" -m pytest tests/test_thread_reaper.py -q -p no:randomly
```
Expected: 10 passed

**Step 6: Commit**

```bash
git add src/ai_service_agent/harness/thread_reaper.py tests/test_thread_reaper.py
git commit -m "feat(thread): reaper engine with dual locks and post-lock recheck (#151)"
```

---

## Task 4: CLI 改成薄入口

**Files:**
- Modify: `src/ai_service_agent/ops/thread_cleanup.py`（整体重写，保留 argparse / vacuum / JSON）
- Test: `tests/test_thread_cleanup.py`（现有用例必须原样通过）

**Step 1: 先跑现有用例确认基线**

```bash
"D:/AI_Project/AI Service Agent/.venv/Scripts/python.exe" -m pytest tests/test_thread_cleanup.py -q -p no:randomly
```
Expected: PASS（这是重构前的绿基线，记下数量）

**Step 2: 重写 `ops/thread_cleanup.py`**

删掉 `CleanupItem` / `CleanupReport` / 清理循环 / `_pending_protections`，只留 CLI 外壳 + vacuum：

```python
"""离线 Demo checkpoint 清理 CLI。

引擎在 ``harness/thread_reaper.py``（与运行时 reaper 共用）。本模块只保留离线语义：
dry-run 默认、``--vacuum``、JSON 报告。**不要**把这里的任何默认值搬进 lifespan。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Awaitable, Callable

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from ai_service_agent.harness.session import ConversationStatusStore
from ai_service_agent.harness.thread_probes import (
    delete_thread as delete_thread_probe,
    load_thread_interrupts,
)
from ai_service_agent.harness.thread_reaper import ReapReport, ThreadReaper

#: 兼容旧导入名（runbook / 外部脚本用过 CleanupReport）。
CleanupReport = ReapReport


class ThreadCleanupService:
    """离线清理一轮：无锁（要求服务已停止），可 dry-run，可选 VACUUM。"""

    def __init__(
        self,
        db_path: str,
        *,
        idle_after: timedelta = timedelta(hours=24),
        interrupt_loader: Callable[[str], Awaitable[list]] | None = None,
        delete_thread: Callable[[str], Awaitable[None]] | None = None,
    ):
        self.db_path = str(db_path)
        self.idle_after = idle_after
        self._interrupt_loader = interrupt_loader or (
            lambda thread_id: load_thread_interrupts(thread_id, self.db_path)
        )
        self._delete_thread = delete_thread or (
            lambda thread_id: delete_thread_probe(thread_id, self.db_path)
        )

    async def run(
        self,
        *,
        apply: bool = False,
        vacuum: bool = False,
        now: datetime | None = None,
        batch_size: int = 1000,
    ) -> ReapReport:
        if vacuum and not apply:
            raise ValueError("VACUUM requires explicit apply mode")
        db_size_before = Path(self.db_path).stat().st_size
        conn = sqlite3.connect(self.db_path)
        try:
            reaper = ThreadReaper(
                status=ConversationStatusStore(conn),
                db_path=self.db_path,
                idle_after=self.idle_after,
                interrupt_loader=self._interrupt_loader,
                delete_thread=self._delete_thread,
            )
            report = await reaper.cleanup_once(now=now, apply=apply, batch_size=batch_size)
        finally:
            conn.close()
        if vacuum:
            await asyncio.to_thread(self._vacuum)
            report.vacuumed = True
        report.db_size_before = db_size_before
        report.db_size_after = Path(self.db_path).stat().st_size
        return report

    def _vacuum(self) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("VACUUM")
        finally:
            conn.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="offline thread checkpoint cleanup")
    parser.add_argument("--db", default="data/ai_service.db", help="SQLite runtime database")
    parser.add_argument("--apply", action="store_true", help="execute deletions; default is dry-run")
    parser.add_argument("--vacuum", action="store_true", help="run offline VACUUM after apply")
    parser.add_argument("--idle-hours", type=float, default=24.0)
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    return parser


async def _run_cli(args: argparse.Namespace) -> ReapReport:
    service = ThreadCleanupService(args.db, idle_after=timedelta(hours=args.idle_hours))
    return await service.run(apply=args.apply, vacuum=args.vacuum, batch_size=args.batch_size)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.vacuum and not args.apply:
        parser.error("--vacuum requires --apply and the service must be stopped")
    report = asyncio.run(_run_cli(args))
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(
            f"mode={report.mode} candidates={len(report.candidates)} "
            f"deleted={len(report.deleted)} protected={len(report.protected)} "
            f"failed={len(report.failed)} vacuumed={report.vacuumed} "
            f"db_size={report.db_size_before}->{report.db_size_after}"
        )
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
```

**Step 3: 跑现有用例（必须原样通过，不许改断言）**

```bash
"D:/AI_Project/AI Service Agent/.venv/Scripts/python.exe" -m pytest tests/test_thread_cleanup.py tests/test_thread_reaper.py -q -p no:randomly
```
Expected: PASS，数量与 Step 1 基线一致（+ Task 3 的 10 例）

若失败，**改实现不改测试**。特别是 `test_cleanup_dry_run_is_default_noop_and_apply_deletes_only_checkpoints` 里 `dry.candidates[0].thread_id` 与 `applied.deleted` 的字段名必须保持。

**Step 4: 冒烟 CLI 真跑一次（在副本上）**

```bash
"D:/AI_Project/AI Service Agent/.venv/Scripts/python.exe" -m ai_service_agent.ops.thread_cleanup --db data/ai_service.db --json | head -20
```
Expected: 合法 JSON，`mode` 为 `dry-run`，`deleted` 为空。

**Step 5: Commit**

```bash
git add src/ai_service_agent/ops/thread_cleanup.py
git commit -m "refactor(ops): make cleanup CLI a thin wrapper over the reaper engine (#151)"
```

---

## Task 5: lifespan 接线与配置

**Files:**
- Modify: `src/ai_service_agent/config.py`（Settings 加字段）
- Modify: `src/ai_service_agent/harness/server.py:249-267`（`create_app` 签名）、`:335`（`FastAPI()`）、文件末尾（lifespan 与 loop）
- Modify: `tests/conftest.py:11-23`
- Test: `tests/test_thread_reaper.py`（追加）

**Step 1: 写失败测试**

追加到 `tests/test_thread_reaper.py`：

```python
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
```

**Step 2: 跑测试确认失败**

```bash
"D:/AI_Project/AI Service Agent/.venv/Scripts/python.exe" -m pytest tests/test_thread_reaper.py -q -p no:randomly -k "lifespan"
```
Expected: FAIL —— `ImportError: cannot import name '_run_thread_reaper'`

**Step 3: 加配置**

`config.py` 在 `db_path` 附近加：

```python
    # #151 后台 idle reaper：0 = 关闭（默认）。生产显式设 1800（30 分钟）。
    # 默认关闭是为了不让 TestClient 用例被动启动后台删除任务。
    thread_reaper_interval_seconds: int = Field(
        default=0, ge=0, validation_alias="THREAD_REAPER_INTERVAL_SECONDS"
    )
    thread_reaper_batch_size: int = Field(
        default=50, ge=1, validation_alias="THREAD_REAPER_BATCH_SIZE"
    )
```

`tests/conftest.py` 的 `pytest_configure` 里加：

```python
    os.environ["THREAD_REAPER_INTERVAL_SECONDS"] = "0"
```

**Step 4: 实现 lifespan**

`server.py` 顶部补导入（现状：有 `asyncio` / `logging` / `from typing import Any`，**没有** `datetime`、`contextlib`、`Callable`）：

```python
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from typing import Any, Callable

from ai_service_agent.harness.thread_probes import sqlite_lifecycle_probes
from ai_service_agent.harness.thread_reaper import ThreadReaper
```

`from typing import Any` 那行改成 `from typing import Any, Callable`，不要新增重复的 typing 导入行。

在 `create_app` 参数表末尾加：

```python
    thread_reaper_interval_seconds: int | None = None,
```

在 `app = FastAPI()` **之前**插入 lifespan 定义与工厂：

```python
    reaper_interval = (
        settings.thread_reaper_interval_seconds
        if thread_reaper_interval_seconds is None
        else thread_reaper_interval_seconds
    )

    def _build_reaper() -> ThreadReaper:
        # 探测能力复用 #150 的 thread_probes（同一套官方 API 调用）。
        probes = sqlite_lifecycle_probes(db_path)
        return ThreadReaper(
            status=status,
            db_path=db_path,
            lock_factory=_thread_lock,
            interrupt_loader=probes["interrupt_loader"],
            delete_thread=probes["delete_thread"],
        )

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        task: asyncio.Task | None = None
        if reaper_interval > 0:
            logger.info(
                "thread reaper enabled: interval=%ss batch=%s single-worker only",
                reaper_interval,
                settings.thread_reaper_batch_size,
            )
            task = asyncio.create_task(
                _run_thread_reaper(
                    _build_reaper(),
                    interval_seconds=reaper_interval,
                    batch_size=settings.thread_reaper_batch_size,
                )
            )
        else:
            logger.info("thread reaper disabled (THREAD_REAPER_INTERVAL_SECONDS=0)")
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
                logger.info("thread reaper stopped")

    app = FastAPI(lifespan=_lifespan)
```

把原来的 `app = FastAPI()` 一行替换掉（不要保留两个）。

在文件末尾（`_wrap` 之后）加模块级函数：

```python
async def _run_thread_reaper(
    reaper,
    *,
    interval_seconds: float,
    batch_size: int = 50,
    clock: Callable[[], datetime] | None = None,
) -> None:
    """周期性调用 ``cleanup_once``；``CancelledError`` 必须原样抛回，否则 shutdown 挂住。"""
    now = clock or (lambda: datetime.now(timezone.utc))
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            report = await reaper.cleanup_once(now=now(), batch_size=batch_size)
        except asyncio.CancelledError:
            raise
        except Exception:
            # 一轮失败不能杀死循环，否则一次瞬时错误就永久停摆。
            logger.exception("thread reaper pass failed")
            continue
        logger.info("thread reaper pass: %s", report.summary())
```

注意 `_run_thread_reaper` 用 `Callable` —— `server.py` 是否已导入？若无则补 `from typing import Callable`（检查现有导入，已用到 `Callable` 的话跳过）。

**Step 5: 跑测试确认通过**

```bash
"D:/AI_Project/AI Service Agent/.venv/Scripts/python.exe" -m pytest tests/test_thread_reaper.py -q -p no:randomly
```
Expected: 12 passed

**Step 6: 跑相邻回归（确认 lifespan 没污染现有用例）**

```bash
"D:/AI_Project/AI Service Agent/.venv/Scripts/python.exe" -m pytest tests/test_server.py tests/test_agent_bot.py tests/test_chatwoot_webhook.py tests/test_thread_rotation.py -q -p no:randomly
```
Expected: 全绿，且**没有一个用例变慢到秒级**（若变慢，说明 reaper 被意外启动）。

**Step 7: Commit**

```bash
git add src/ai_service_agent/config.py src/ai_service_agent/harness/server.py tests/conftest.py tests/test_thread_reaper.py
git commit -m "feat(server): run idle thread reaper from FastAPI lifespan (#151)"
```

---

## Task 6: 文档与验收

**Files:**
- Modify: `docs/specs/thread-lifecycle-spec.md`（状态行、`Background cleanup` 段）
- Modify: `docs/specs/architecture.md`（Checkpoint 清理段）
- Modify: `docs/ops/thread-checkpoint-cleanup.md`（新增「运行时 reaper」段）
- Modify: `.env.example`（两个新变量，注释状态）
- Modify: `AGENTS.md`（若 §5 表格需要加一行说明默认关闭）

**Step 1: spec 更新**

`thread-lifecycle-spec.md` 第 4 行状态改为：

```markdown
- 状态：Demo 范围定稿。#149 懒创建 / 复用 / 有界 hydration / 消息索引 / 离线清理、#150 安全边界轮换、#151 运行时 idle reaper（共享引擎 + 双锁 + 退避重试 + lifespan 调度）均已实现。仍明确延后：Form Interrupt 的 24 小时自动过期（#155）、14 天最大年龄规则、多 worker 租约。
```

第 120-121 行改为：

```markdown
- 旧 thread 在轮换点**立即 best-effort 删除**；失败只 `retry_count` / `last_error` / `next_retry_at` 记账，由 #151 的 reaper 按指数退避重试。
- 运行时 reaper 与离线 CLI 共用 `harness/thread_reaper.py`；CLI 保留 dry-run / `--vacuum`，lifespan 永不 VACUUM。
```

新增 `### Background cleanup (#151)` 小节（放在 Rotation rules 之后）：

```markdown
### Background cleanup (#151)

- `cleanup_once(now, apply, batch_size)` is the single maintenance entry point; the offline CLI and the FastAPI lifespan both delegate to it.
- Candidates are conversations whose `last_ai_execution_at` is at least 24 hours old **and whose mode is not `human`**, plus rows already in `thread_cleanup_queue` whose `next_retry_at` has passed.
- Per candidate the reaper acquires `conversation:{conversation_id}` and then the thread lock keyed by the **raw `thread_id`** — the same keys used by `/chat`, AgentBot, `/actions/confirm`, handoff resolve, seat takeover and the seat approval `decide()` endpoint.
- After both locks are held it re-reads the conversation pointer and re-runs `collect_blockers`; the pre-lock snapshot is never used for the final decision.
- Pointer clearing and queue insertion share one SQLite transaction; deletion calls only `AsyncSqliteSaver.adelete_thread()`.
- Failed deletions are retried with exponential backoff (30 min → 1 h → 2 h → 4 h cap) recorded in `next_retry_at`. A protected candidate is **not** a failure and does not advance the retry counter.
- Form Interrupts pin their thread indefinitely: their 24-hour TTL is issue #155, so a conversation that ended on a Form Interrupt is never reclaimed by this stage. Reports label it `blocked_by_form`.
- The scheduler runs in a single FastAPI lifespan task, is disabled by default (`THREAD_REAPER_INTERVAL_SECONDS=0`), and only supports one worker. `main.py` passes an app object to `uvicorn.run`, so `--workers` cannot take effect anyway.
```

**Step 2: architecture / ops / env**

- `architecture.md` 的「Checkpoint 清理」段补一句：运行时入口是 lifespan 里的 `ThreadReaper.cleanup_once`，与离线 CLI 共用引擎；默认关闭。
- `docs/ops/thread-checkpoint-cleanup.md` 新增 `## 8. 运行时 reaper（#151）`：如何开启（`THREAD_REAPER_INTERVAL_SECONDS=1800`）、日志长什么样（`thread reaper pass: mode=apply candidates=N ...`）、如何确认它没在删人工会话、Form 会话永不回收的已知缺口、以及「必须单 worker」。
- `.env.example` 追加（注释掉，避免本地意外开启）：

```
# #151 后台 idle thread 回收（0=关闭；生产建议 1800）
# THREAD_REAPER_INTERVAL_SECONDS=1800
# THREAD_REAPER_BATCH_SIZE=50
```

**Step 3: 全量回归**

```bash
cd "D:/AI_Project/AI Service Agent/.worktrees/thread-idle-reaper/AI Service Agent"
"D:/AI_Project/AI Service Agent/.venv/Scripts/python.exe" -m pytest tests --ignore=tests/evals -q -p no:randomly 2>&1 | tail -20
```

Expected: 全绿。**不要只看退出码** —— summary 行可能被 safe-delete 钩子输出吞掉。核对方式：

```bash
"D:/AI_Project/AI Service Agent/.venv/Scripts/python.exe" -m pytest tests --ignore=tests/evals -q --collect-only 2>&1 | tail -3
```
收集数应等于进度点数量 + skip 数，且 `--lf` 不应列出任何用例。

**Step 4: 副本演练**

```bash
"D:/AI_Project/AI Service Agent/.venv/Scripts/python.exe" - <<'PY'
import sqlite3, pathlib
pathlib.Path("data/backups").mkdir(parents=True, exist_ok=True)
src = sqlite3.connect("data/ai_service.db")
dst = sqlite3.connect("data/backups/rehearsal-151.db")
src.backup(dst); dst.close(); src.close()
print("backup ok")
PY
"D:/AI_Project/AI Service Agent/.venv/Scripts/python.exe" -m ai_service_agent.ops.thread_cleanup --db data/backups/rehearsal-151.db --apply --json 2>/dev/null | head -c 600
```

Expected: `deleted` 数量与 #149 演练同量级；`protected` 里能看到 `blocked_by_form`（若库里确有 Form 收尾的会话）。演练完删副本：

```bash
rm -f data/backups/rehearsal-151.db
```

**Step 5: Commit**

```bash
git add docs/specs/thread-lifecycle-spec.md docs/specs/architecture.md docs/ops/thread-checkpoint-cleanup.md .env.example AGENTS.md
git commit -m "docs: document runtime thread reaper scope and known Form-interrupt gap (#151)"
```

---

## 验收对照（对应 issue #151 的 AC）

| AC | 落点 |
|---|---|
| 24h 空闲自动回收 | Task 3 `test_cleanup_once_deletes_idle_thread_and_clears_pointer` + Task 5 lifespan |
| 先摘指针再删除 | `_reap_locked` 里 `detach_thread_and_enqueue` 在 `delete_thread` 之前；`test_delete_failure_keeps_queue_row_with_retry_metadata` |
| 清理后新消息新建 thread 且保留历史 | Task 3 `test_next_ai_message_after_cleanup_creates_new_thread` |
| 会话锁 + thread 锁 | `test_cleanup_once_takes_conversation_then_thread_lock` |
| 双锁后重查指针与保护 | `test_cleanup_once_rechecks_blockers_after_locks` |
| 锁序不死锁 | 同一用例断言 key 顺序为 `conversation:*` → 裸 `thread_id` |
| 挂起保护 | 复用 `collect_blockers`；`test_cleanup_protects_pending_interrupts_handoffs_and_unexpired_approvals` 保持绿 |
| ISO 时间戳解析 | Task 1 全部用 `parse_utc_timestamp`，无 SQL 字符串比较 |
| 队列重试 | Task 1 三个用例 + `test_cleanup_once_skips_rows_waiting_for_retry` |
| shutdown 干净取消 | `test_lifespan_cancels_reaper_cleanly` |
| 真 SQLite + AsyncSqliteSaver + 注入时钟 | `_write_checkpoint` + `NOW` 常量 |
| 不引入 pruning / generation / 租约 | 本计划无相关改动 |

## 明确不做（写进 PR 描述）

- Form Interrupt 24h 自动过期 → **#155**
- 多 worker 租约 / Postgres 行锁
- checkpoint 部分 pruning
- 单 worker 运行时探测（`uvicorn.run(app)` 本身就不支持 `--workers`）
