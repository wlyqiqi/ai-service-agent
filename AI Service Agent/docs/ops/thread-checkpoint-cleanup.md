# Thread Checkpoint 清理 Runbook（Demo）

适用范围：单进程 SQLite Demo。目标是清理无 pending work 的旧 `checkpoints` / `writes`，同时保留 conversation messages、users、approvals、handoffs、Chatwoot mappings 与其他业务表。

此流程不实现自动轮换、后台 idle reaper 或生产调度；必须离线执行。清理默认 dry-run，只有显式 `--apply` 才删除，`VACUUM` 还需显式 `--vacuum`。

## 1. 停止服务并备份

先停止所有访问该数据库的 API / AgentBot 进程，确认无写入者。

```powershell
$db = "data/ai_service.db"
$backup = "data/backups/ai_service-$(Get-Date -Format 'yyyyMMdd-HHmmss').db"
New-Item -ItemType Directory -Force (Split-Path $backup) | Out-Null
Copy-Item -LiteralPath $db -Destination $backup
```

备份后做基本完整性检查：

```powershell
sqlite3 $backup "PRAGMA integrity_check;"
sqlite3 $backup "SELECT COUNT(*) FROM conversation_message;"
sqlite3 $backup "SELECT COUNT(*) FROM approval_record;"
sqlite3 $backup "SELECT COUNT(*) FROM handoff_record;"
```

任一步失败即停止，不要进入 apply。

## 2. 在数据库副本上演练

```powershell
$rehearsal = "data/backups/rehearsal.db"
Copy-Item -LiteralPath $db -Destination $rehearsal -Force
uv run --no-sync python -m ai_service_agent.ops.thread_cleanup --db $rehearsal --json
uv run --no-sync python -m ai_service_agent.ops.thread_cleanup --db $rehearsal --apply --vacuum --json
Get-Item $db, $rehearsal | Select-Object FullName, Length
```

验证输出中：

- `protected` 包含当前 thread、pending checkpoint interrupt、awaiting/active handoff、未过期 approval、approved-but-not-terminal execution；
- `deleted` 只来自可清理 thread；
- `failed` 为空；
- `db_size_after < db_size_before`（有可回收空间时）；
- 副本的 conversation messages / approvals / handoffs / Chatwoot mappings 数量与备份一致。

> 首次在任何库上运行都会做一次幂等 schema 迁移（`conversation_status.thread_id` 改为可空 + 新建 `thread_cleanup_queue`），
> 所以首跑 `db_size_after` 可能略大于 `db_size_before`，这不代表清理失败。

## 3. 正式 dry-run

```powershell
uv run --no-sync python -m ai_service_agent.ops.thread_cleanup --db data/ai_service.db --json
```

核对 `candidates`、`protected`、`deleted`（dry-run 中应为空）与 `failed`。dry-run 不修改 checkpoint、writes、conversation pointer 或业务记录。

## 4. 正式 apply

```powershell
uv run --no-sync python -m ai_service_agent.ops.thread_cleanup --db data/ai_service.db --apply --json
```

实现顺序保证：当前指针先清空并入队，再调用官方 `AsyncSqliteSaver.adelete_thread()`；删除失败时 queue 行保留 `retry_count` / `last_error`，会话可安全创建新 thread，后续重跑可重试。已经删除的 thread 再次处理是幂等 no-op。

### 4.1 历史 `approved` 记录会永久受保护（实测，需人工确认）

`approval_record.execution_status` 是本功能新增列；历史 `status='approved'` 行由迁移默认填 `not_started`，而 `not_started` 不是终态，因此这些 thread 永远命中 `approval_execution_incomplete`，`--apply` 不会删除。

2026-10-29 在 216 个会话的数据库副本上实测：198 个候选里 16 个受保护，共 112.9 MiB / 2310 行 checkpoint，其中 2 个 thread（51.8 MiB + 58.0 MiB）就是被这条规则钉住的 → 165 MiB 只回收 72.9 MiB。

先逐条确认这些审批确已处理完（退款已执行 / 已作废 / 客户已放弃），再标为终态后重跑；**不要无条件全表更新**：

```sql
UPDATE approval_record SET execution_status = 'uncertain'
WHERE status = 'approved' AND execution_status = 'not_started'
  AND approval_id IN ('<逐条确认后的 approval_id>');
```

未确认前保持受保护是正确行为：`approved` 但执行未终态是「唯一能完成执行的 state」，删掉等于静默丢单（见 `approval-spec.md`）。

## 5. 可选 VACUUM

仅在服务仍停止时执行：

```powershell
uv run --no-sync python -m ai_service_agent.ops.thread_cleanup --db data/ai_service.db --apply --vacuum --json
```

`VACUUM` 是离线维护步骤，不在在线服务生命周期内运行。

## 6. Apply 后验证

```powershell
sqlite3 data/ai_service.db "PRAGMA integrity_check;"
sqlite3 data/ai_service.db "SELECT COUNT(*) FROM conversation_message;"
sqlite3 data/ai_service.db "SELECT COUNT(*) FROM users;"
sqlite3 data/ai_service.db "SELECT COUNT(*) FROM approval_record;"
sqlite3 data/ai_service.db "SELECT COUNT(*) FROM handoff_record;"
sqlite3 data/ai_service.db "SELECT COUNT(*) FROM chatwoot_conversation;"
sqlite3 data/ai_service.db "SELECT COUNT(*) FROM thread_cleanup_queue;"
```

要求：

- `integrity_check` 返回 `ok`；
- 业务表行数与备份相比只允许由真实业务变更解释，清理工具不得减少；
- `thread_cleanup_queue = 0`，或剩余行均为明确失败并带 `last_error`，可重跑；
- protected thread 的 checkpoint 数量仍大于 0；
- 被清理 conversation 的 `thread_id IS NULL`，可正常在下一次 AI turn 懒建新 thread。

最后重启服务并做一条新建 session + 首条 AI 消息 smoke：`/sessions` 返回 `session_id`，首次 `/chat` 后 `conversation_status.thread_id` 非空且 checkpoints 只出现该 thread。

## 7. 回滚

如验证失败，停止服务并从第 1 步备份恢复数据库：

```powershell
Copy-Item -LiteralPath $backup -Destination $db -Force
```

恢复后再次执行 `PRAGMA integrity_check`，确认服务可用后再开放流量。

## 8. 运行时 reaper（#151）

第 1–7 节是**离线**流程。服务正常运行时由 FastAPI lifespan 里的 `ThreadReaper.cleanup_once`
自动跑同一套引擎，**不需要也不应该**再手工执行本 runbook 的 apply。

### 开启

```bash
# .env
THREAD_REAPER_INTERVAL_SECONDS=1800   # 30 分钟；0 = 关闭（默认）
THREAD_REAPER_BATCH_SIZE=50           # 每轮最多处理多少条候选
```

默认关闭是**刻意**的：`create_app()` 被 1200+ 个 TestClient 用例调用，开着会让每个用例
都起一个后台删除任务。测试里 `tests/conftest.py` 也会把该变量钉成 `0`。

### 日志

```
thread reaper enabled: interval=1800s batch=50 single-worker only
thread reaper pass: mode=apply candidates=12 deleted=10 protected=2 failed=0
thread reap blocked: conversation=... thread=... blockers=['blocked_by_form']
thread reap delete failed (kept in queue): thread=... error=...
thread reaper stopped
```

- `protected` 不是失败，不推进 `retry_count`；`blocked_by_form` 表示会话停在 Form Interrupt 上。
- 删除失败的 thread 留在 `thread_cleanup_queue`，按 `next_retry_at`（30m → 1h → 2h → 4h 封顶）重试。

### 如何确认它没在动人工会话

```sql
-- 候选里永远不会出现 human 会话；这条查询应恒为 0
SELECT COUNT(*) FROM conversation_status
WHERE thread_id IS NOT NULL AND mode = 'human'
  AND COALESCE(last_ai_execution_at, created_at) < datetime('now', '-24 hours');
```

`idle_conversations(..., exclude_human=True)` 是默认值，CLI 也继承——**离线清理同样不再回收人工会话**。

### 已知缺口

- **Form Interrupt 永不自动过期**（24h TTL 尚未开单）。以 Form 中断收尾的会话会被
  `blocked_by_form` 永久钉住，reaper 每轮都会跳过它；这是设计取舍，不是 bug。
- 只支持**单 worker**。`main.py` 把 app 对象传给 `uvicorn.run`，`--workers` 本就不生效；
  不要改成 `--workers N` 后仍开 reaper（没有跨进程租约）。
- lifespan **永不 VACUUM**：VACUUM 需要独占，只能在停机后的离线流程里做（见第 5 节）。
