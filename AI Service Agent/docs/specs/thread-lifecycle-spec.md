# Demo Thread 生命周期规格

- 日期：2026-10-29
- 状态：Demo 范围定稿。#149 懒创建 / 复用 / 有界 hydration / 消息索引 / 离线清理、#150 安全边界轮换、#151 运行时 idle reaper（共享引擎 + 双锁 + 退避重试 + lifespan 调度）均已实现。仍明确延后：Form Interrupt 的 24 小时自动过期（尚未开单）、14 天最大年龄规则、多 worker 租约。
- 关联：`architecture.md`、`handoff-spec.md`、`approval-spec.md`、`state-spec.md`、`mutation-form-interrupt-spec.md`
- 环境基线：LangGraph 1.2.14、langgraph-checkpoint 4.2.0、langgraph-checkpoint-sqlite 3.1.1

## Problem Statement

当前 Demo 在创建会话时就生成 `thread_id`，但没有 thread 生命周期、复用/轮换规则或 checkpoint 清理机制。长期会话和无人返回的会话会持续累积 `checkpoints` / `writes`，而简单删除又可能破坏 Form Interrupt、审批恢复或 Handoff Resolve。

本地数据库已出现明显膨胀：约 205 MiB、5,969 条 checkpoints、24,746 条 writes，且 checkpoint 主要集中在少数长 thread。问题不是 LangGraph 自动关闭失败，而是应用没有管理 thread 生命周期。

## Solution

为单进程 Demo 提供最小 Thread Lifecycle Service：

- `conversation_id` 长期稳定，`thread_id` 表示当前可恢复的 Graph 执行状态；
- `POST /sessions` 只创建 conversation draft，不创建 thread；
- 第一次真正进入 AI Graph 时惰性创建 thread；
- 正常对话复用当前 thread；
- 已完成的 turn 达到 30 个，或超过 24 小时没有 AI Graph 执行时，在安全边界轮换；
- 旧 thread 进入 cleanup queue，由单进程定时任务通过官方 `adelete_thread()` 删除；
- Form Interrupt、待审批、Handoff Request 和在途执行优先于所有时间阈值；
- 新建与恢复使用两个独立服务入口；恢复失败不得静默创建新 thread；
- 不建设 generation 表、分布式租约、独立任务平台或复杂监控系统。

## User Stories

1. As a customer, I want opening a chat page not to create execution state, so that unused conversations do not consume checkpoint storage.
2. As a customer, I want my first AI request to create a thread only when Graph execution is actually needed.
3. As a customer, I want normal follow-up messages to reuse the current thread, so that multi-turn continuity is preserved.
4. As a customer, I want a human-only message or explicit Handoff Request not to create a Graph checkpoint.
5. As a returning customer, I want re-login with the same conversation to reuse its thread when it is still valid.
6. As a returning customer, I want a new thread when the previous thread is idle beyond 24 hours, so that stale execution state is not reused.
7. As a customer, I want my conversation to rotate after 30 completed turns at a safe boundary, so that checkpoint state remains bounded.
8. As a customer, I want my current message and a bounded recent context loaded into a new thread, so that rotation does not rely on the old checkpoint.
9. As a customer, I want conversation-level fallback and clarification counters to survive rotation, so that policy behavior stays consistent.
10. As a customer, I want a pending Form Interrupt to pin its thread, so that my form submission resumes the correct workflow.
11. As a customer, I want an expired form callback to fail explicitly, so that it cannot affect a newer form or thread.
12. As an approver, I want a pending approval to pin its thread, so that approval resume targets the exact high-risk operation.
13. As an approver, I want approved-but-not-executed operations to remain pinned, so that cleanup cannot erase the only state needed to complete execution.
14. As a customer, I want a waiting Handoff Request to pin its recovery state, so that Seat Takeover or Handoff Resolve does not lose the original execution context.
15. As an operator, I want active human handoff not to expire automatically, so that an ongoing human conversation is never silently terminated by thread cleanup.
16. As an operator, I want an explicit close operation that returns conflict while pending work exists.
17. As an operator, I want a cleanup worker to retire idle threads even when the customer never returns.
18. As an operator, I want cleanup failures recorded and retried without blocking new AI conversations.
19. As an operator, I want thread deletion to remove only checkpoints and writes, so that messages, approvals, handoffs, Chatwoot mappings, and Store data remain intact.
20. As a runtime maintainer, I want Form Interrupt and approval recovery to keep using official `Command(resume=...)` on the original thread.
21. As a runtime maintainer, I want pending interrupt checks to use `graph.aget_state()` as the authority.
22. As a runtime maintainer, I want no synthetic thread-creation checkpoint and no hand-written checkpoint deletion.
23. As a runtime maintainer, I want completed-turn counting to be idempotent, so webhook retries and duplicate resumes do not trigger rotation early.
24. As a runtime maintainer, I want one conversation-level critical section covering thread resolution and Graph execution, so rotation and cleanup cannot race an in-flight request.
25. As a runtime maintainer, I want the SQLite Demo to reject unsupported multi-worker deployment.
26. As a runtime maintainer, I want a bounded recent-context hydration path for newly created threads.
27. As a runtime maintainer, I want existing conversations and threads backfilled into the Demo lifecycle fields.
28. As a runtime maintainer, I want lifecycle logs for creation, reuse, rotation, queueing, deletion, retry, and resume mismatch.
29. As a runtime maintainer, I want the authoritative architecture and handoff specs to reflect lazy thread creation after implementation.
30. As a runtime maintainer, I want the Demo scope to avoid production-only generation, lease, pruning, and monitoring infrastructure.

## Implementation Decisions

### Official LangGraph components and boundaries

- Keep `StateGraph`, `interrupt()`, and `Command(resume=...)` as the official HITL primitives. Do not add `HumanInTheLoopMiddleware` or a parallel pending-task state machine.
- Keep `AsyncSqliteSaver` as the Demo checkpointer.
- Use `graph.aget_state(config).interrupts` as the authoritative runtime check for pending Form Interrupt, approval interrupt, and handoff interrupt.
- Use `AsyncSqliteSaver.adelete_thread(thread_id)` to delete all checkpoints and writes for a thread. Wrap it in a project adapter for error classification, logging, and retry.
- LangGraph has no explicit business `create_thread()` object. A thread becomes materialized by its first checkpoint/write; application metadata stores the current `thread_id` before invocation.
- Do not use `prune()` / `aprune()` in this Demo: the installed `AsyncSqliteSaver` inherits base methods that raise `NotImplementedError`.
- Do not use `get_state_history()` in runtime paths; checkpoint history inspection is not needed for lifecycle decisions.
- Do not introduce LangGraph Store in this feature. Long-term Store memory remains out of scope.
- Thread scheduling is application infrastructure, not a LangGraph primitive. Use one FastAPI lifespan background task for the single-process Demo, with shutdown cancellation.

### Minimal persistence changes

- Reuse `conversation_status` as the current conversation record.
- Add nullable current `thread_id`; an unused conversation has no thread.
- Add `completed_turns`, `last_completed_turn_id`, and `last_ai_execution_at`.
- Add a small conversation-level context payload only for state that must survive rotation: at minimum `fallback_count`, `clarify_count`, `clarify_route_intent`, `clarify_target_graph`, and complaint-offer state.
- Add `thread_cleanup_queue` with `thread_id`, `conversation_id`, `reason`, `queued_at`, `retry_count`, and `last_error`.
- Add approval `execution_status` values sufficient to distinguish `not_started`, `running`, `succeeded`, `failed`, and `uncertain`.
- Add `form_session_id` as an optional request field for explicit Form Resume and require validation when a form resume targets a thread.
- For graph-origin Handoff Request, persist the originating `thread_id`; add an expiry only for `awaiting_seat`, never for active human handoff.

### Thread creation

- `POST /sessions` creates a new conversation draft with no thread.
- Existing conversations continue by using their existing `conversation_id`; `POST /sessions` does not implicitly return the latest conversation.
- `ensure_active_thread(conversation_id)` creates a new UUID thread only when:
  - the conversation has no current thread;
  - or the current thread is eligible for rotation at the start of a new AI turn.
- Human-only messages, explicit Handoff Request turns, page views, login, and transport-only actions do not create a thread.
- Thread activation updates the conversation pointer before invoking the Graph. No synthetic checkpoint is written to mark creation.

### New-turn and resume paths

- `ensure_active_thread(conversation_id)` is used only for a new AI turn.
- `resolve_resume_thread(conversation_id, expected_thread_id, task_reference)` is used for Form Resume, approval resume, and Handoff Resolve.
- Resume validates conversation, current `thread_id`, task reference, and task status.
- Missing or mismatched thread/task identity returns an explicit conflict/expired response and never creates a replacement thread.
- Widget `/chat` and AgentBot use the same lifecycle service.
- `/actions/confirm`, approval decision resume, and Handoff Resolve use the resume path.

### Rotation rules

- Rotation is evaluated only before a new AI turn and only after the previous turn is terminal.
- Rotation triggers when either `completed_turns >= 30` or `last_ai_execution_at` is at least 24 hours old. The conditions are OR.
- The 14-day maximum-age rule is not implemented in this Demo.
- Rotation is skipped while a Graph invocation is in flight or any valid Form Interrupt, approval execution gap, or handoff recovery reference exists.
- Under one conversation-level critical section, the service verifies safety, generates the next `thread_id`, atomically switches the conversation pointer, and inserts the old thread into `thread_cleanup_queue`.
- The new thread hydrates only the current user message, at most the recent context allowed by the existing context policies, required authoritative business data, and externalized conversation-level counters.
- The new thread must not copy the old checkpoint State or full message history.

实现记录（#150）：

- 判定纯函数 `harness/threads.decide_rotation`（`turn_limit` 优先于 `idle_timeout`）；保护判定单点 `harness/thread_guard.collect_blockers`，与离线清理共用。
- 「会话级计数跨代保留」用**读旧 checkpoint 的 `context` + 白名单键注入新 thread 初始 state** 实现，不引入计数表、不改 clarify / fallback 节点。白名单见 `threads.PRESERVED_CONTEXT_KEYS`。
- 探测能力（`interrupt_loader`）未注入时 **fail closed**：无法证明无挂起就不轮换。
- 旧 thread 在轮换点**立即 best-effort 删除**；失败只 `retry_count` / `last_error` / `next_retry_at` 记账，由 #151 的 reaper 按指数退避重试。
- 运行时 reaper 与离线 CLI 共用 `harness/thread_reaper.py`；CLI 保留 dry-run / `--vacuum`，lifespan 永不 VACUUM。

### Background cleanup (#151)

- `cleanup_once(now, apply, batch_size)` is the single maintenance entry point; the offline CLI and the FastAPI lifespan both delegate to it.
- Candidates are conversations whose `last_ai_execution_at` is at least 24 hours old **and whose mode is not `human`**, plus rows already in `thread_cleanup_queue` whose `next_retry_at` has passed.
- Per candidate the reaper acquires `conversation:{conversation_id}` and then the thread lock keyed by the **raw `thread_id`** — the same keys used by `/chat`, AgentBot, `/actions/confirm`, handoff resolve, seat takeover and the seat approval `decide()` endpoint.
- After both locks are held it re-reads the conversation pointer, re-verifies the idle predicate (`is_idle_candidate`, so a conversation that became active between scan and lock is skipped as `no_longer_idle`), and re-runs `collect_blockers`; the pre-lock snapshot is never used for the final decision.
- Pointer clearing and queue insertion share one SQLite transaction; deletion calls only `AsyncSqliteSaver.adelete_thread()`.
- Failed deletions are retried with exponential backoff (30 min → 1 h → 2 h → 4 h cap) recorded in `next_retry_at`. A protected candidate is **not** a failure and does not advance the retry counter.
- Form Interrupts pin their thread indefinitely: their 24-hour TTL is not implemented yet (no tracking issue opened), so a conversation that ended on a Form Interrupt is never reclaimed by this stage. Reports label it `blocked_by_form`.
- The scheduler runs in a single FastAPI lifespan task, is disabled by default (`THREAD_REAPER_INTERVAL_SECONDS=0`), and only supports one worker. `main.py` passes an app object to `uvicorn.run`, so `--workers` cannot take effect anyway.
- **idle 轮换的实际覆盖面比字面窄**：任何 checkpoint 中断都会钉住 thread，而 Form Interrupt 的 24 小时自动过期本切片未实现，所以「以 Form 中断收尾」的会话（mutation 流程的常见终态）永远不会因空闲而轮换。idle 轮换实际只对以纯聊天收尾的会话生效。

### Conversation-level concurrency

- Use a ref-counted process-local lock keyed by `conversation_id`.
- The lock covers thread resolution, generation/task validation, the entire Graph invoke/resume, final persistence, rotation, and cleanup decision.
- Do not expose a resolved `thread_id` to a caller that performs Graph work after releasing the lock.
- The SQLite Demo must start in single-worker mode; detect unsupported worker configuration and fail startup rather than running with false safety.
- Multi-instance leases and Postgres coordination remain out of scope.

### Completed turn accounting

- A completed turn is one persisted user request that reaches a terminal response with no pending resume task and whose final assistant message is accepted for persistence.
- Clarification and fallback responses count as completed turns when they reach a terminal response.
- Form Resume, approval resume, Handoff Resolve, duplicate webhook delivery, and duplicate completion callbacks do not increment the counter.
- Idempotency uses `last_completed_turn_id`; AgentBot uses the Chatwoot message identity, while Widget uses a server-generated **per-request** inbound turn identity (`chat:{uuid}`). The idempotency window is therefore the immediately preceding turn only, and a Widget client retry of the same message counts as a new turn; both are unreachable duplicates in practice (AgentBot early-returns on `message_id`, Widget ids are per request).
- `completed_turns` resets when a new thread becomes current.

### Pending-task protection and expiry

- Any valid checkpoint interrupt pins its thread. For rotation this includes an already-expired `approval_required` interrupt: deleting the thread would make the official expired-resume path unreachable, so rotation blocks until that resume runs (`collect_blockers(block_expired_approvals=True)`). Offline cleanup keeps the opposite rule — expired approvals are reclaimable.
- Form Interrupt default protection window is 24 hours after the last AI execution. In the Demo, expiry may be implemented by resuming the pending form with official `Command(resume={"action":"cancel"})` under the conversation lock, then allowing cleanup. A late form callback must return expired/conflict and must not attach to a newer form.
- Approval TTL reuses the existing Approval Service `expires_at` (default 30 minutes). Do not introduce a second 24-hour approval TTL.
- An expired pending approval may be finalized through the existing official expired-resume path.
- A decision that has been made but whose execution is not terminal pins the thread. Approved-not-executed work must be recovered through its original task reference or remain pinned with an error.
- `awaiting_seat` Handoff Request may use a configurable 7-day Demo expiry only if no active human takeover exists. Active human handoff has no automatic expiry and requires explicit Handoff Resolve or an administrative close.
- Expired task callbacks must return explicit conflict/expired errors and must not mutate newer thread state.

### Cleanup

- A single `cleanup_once(now, batch_size)` behavior is the testable cleanup seam.
- The FastAPI lifespan runner invokes it every 30-60 minutes in single-worker mode and cancels it on shutdown.
- Cleanup scans conversations idle beyond 24 hours and pending entries in `thread_cleanup_queue`.
- Before queueing or deleting, it acquires the conversation lock and re-reads the current pointer, re-verifies the idle predicate, checkpoint interrupts, approvals, and handoffs.
- Queue rows get a reserved share of each batch (half when idle candidates exist): permanently protected idle conversations (`blocked_by_form`) never leave the idle set and would otherwise fill the batch and starve the queue forever.
- For an idle conversation with no pending work, cleanup clears the current thread pointer and enqueues the old thread in the same SQLite transaction.
- Cleanup calls `adelete_thread()` only for threads no longer referenced by the conversation and with no valid pending task.
- On successful deletion, the queue row is removed. On failure, `retry_count`, `last_error` and `next_retry_at` are updated and the row remains for retry.
- Queue failures do not block creation of a new thread.
- Thread deletion must not delete conversation messages, Store data, approvals, handoffs, Chatwoot mappings, or users.

### Close behavior

- Provide an explicit conversation close operation.
- Closing with any valid pending task returns HTTP conflict.
- Closing without pending work clears the current thread pointer, enqueues the thread, and marks the conversation closed while preserving message history.

### Backfill and documentation

- Existing conversations receive the new lifecycle fields through an idempotent backfill.
- Derive `last_ai_execution_at` from available message/checkpoint timestamps and pin conversations with valid pending tasks.
- Existing stale threads without pending work may enter the cleanup queue.
- The implementation updates the existing architecture and handoff statements that currently create a thread during session creation.
- Conversation Mode remains `ai | human`; thread lifecycle metadata is separate.

### Observability

- Emit structured logs for thread creation, reuse, rotation, enqueue, delete success, delete failure, pending-task pin, expiry, and resume mismatch.
- Log rotation reason (`turn_limit`, `idle_timeout`, `manual_reset`) and identifiers without message bodies or sensitive data.
- No monitoring platform is required for the Demo.

## Testing Decisions

### Primary seam

- Use one primary seam: the Thread Lifecycle Service with a real temporary SQLite database, real `AsyncSqliteSaver`, fake models, and an injected clock.
- Cover `ensure_active_thread`, `resolve_resume_thread`, completed-turn accounting, rotation, close, pending-task expiry, `cleanup_once`, and retry behavior.
- Existing FastAPI and AgentBot integration seams verify only that all transports call the same lifecycle service and preserve current behavior.

### External behavior

- Tests assert thread identity, message preservation, checkpoint existence, HTTP outcomes, no silent replacement on resume failure, and no deletion under pending work.
- Do not assert private helper call order or checkpoint SQL details.

### Required scenarios

1. Creating a session does not create a thread.
2. First AI execution creates a thread; normal follow-up reuses it.
3. Human-only and explicit handoff turns do not create a checkpoint.
4. Thirty completed turns cause one safe rotation.
5. Duplicate webhook or completion does not increment the counter.
6. An idle thread rotates on the next AI request after 24 hours.
7. The background cleanup path retires an idle thread when the customer never returns.
8. Pending Form Interrupt, approval execution gap, waiting handoff, and active handoff each pin the thread.
9. Expired form and approval callbacks cannot affect the current or newer thread.
10. `resolve_resume_thread` with a missing or mismatched thread/task reference returns conflict and creates no thread.
11. Approved-not-executed work is not deleted.
12. Rotation preserves required conversation-level counters and loads bounded recent context.
13. Conversation-level concurrency prevents a cleanup from deleting an in-flight thread.
14. Deletion is idempotent and retry-safe.
15. Thread deletion preserves messages, approvals, handoffs, Store data, and mappings.
16. Existing conversation backfill is idempotent and pins valid pending work.
17. Unsupported multi-worker SQLite startup fails clearly.

### Prior art

- Existing FastAPI integration tests using `create_app()` and `TestClient`.
- Existing AgentBot integration tests.
- Existing concurrency tests using a real SQLite saver.
- Existing Form Resume, approval side-channel, and handoff tests.

## Out of Scope

- Full generation table or multi-stage thread lifecycle state machine.
- `prune()` / `aprune()` partial checkpoint pruning.
- Multi-instance distributed locks, leases, or Postgres migration.
- LangGraph Store long-term memory.
- Automatic conversation summarization.
- Deletion or retention policy for `conversation_message`.
- Complex monitoring or alerting infrastructure.
- Active human handoff auto-expiry.
- Silently recreating a thread for a failed resume.

## Further Notes

- Current `AsyncSqliteSaver.adelete_thread()` is available and deletes checkpoint plus write rows; this is the only supported whole-thread deletion path for this Demo.
- The installed async saver does not provide a working `prune()` / `aprune()` implementation; do not use direct SQL to simulate it.
- A thread is not an explicit LangGraph object before its first checkpoint write.
- The 30-turn and 24-hour values are initial Demo policies, not production performance guarantees.
- The one-time backfill and stale-thread cleanup should run with a database backup and dry-run counts before deletion.
