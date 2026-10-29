# 计划：安全边界上的自动 thread 轮换（#150）

- 日期：2026-10-29
- 状态：**已实现**
- 关联：issue #150、父 spec #148、`docs/specs/thread-lifecycle-spec.md`
- 前置：PR #152（#149）已合并——懒创建、有界 hydration、消息索引、离线清理

## 结论与取舍

| 决策 | 选择 | 理由 |
|---|---|---|
| 轮换判定位置 | `prepare_ai_turn` 内、**图执行前** | 只在安全边界（上一轮已终结）轮换；不需要新节点，不进 Graph |
| 判定纯函数 | `threads.decide_rotation(...)` | 可单测、无 IO；OR 语义（30 轮 / 24h） |
| 保护判定 | 抽出 `harness/thread_guard.py`，**轮换与离线清理共用** | 两处各写一份必然漂移：一处认为可删、另一处认为还挂着 |
| 会话级计数跨代 | **读旧 checkpoint 的 `context`，只带白名单键**，注入新 thread 初始 state | 2 次 IO，不改 builder / clarify / fallback 节点；比「计数外置到表」便宜一个量级 |
| 旧 thread 删除时机 | 轮换后**立即 best-effort 删**，失败留队列 | 轮换点已确认无挂起；删除耗时 ~5ms，不值得为它推迟到「维护窗口」（后台 reaper 属 #151） |
| 会话级锁 | 复用既有 `_thread_lock("conversation:{id}")`，AgentBot 也用它 | 锁本来就存在（handoff 那批），只是 AgentBot 用的是 `chatwoot:{id}`，与 `/chat` 不同 key |
| 阈值可配置 | 暂不做成 settings，用常量 `RotationPolicy()` | spec 明确「30 轮 / 24h 是初始 Demo 策略，不是生产承诺」 |

## 改动落点

| 文件 | 改动 |
|---|---|
| `harness/session.py` | `conversation_status` 增 `completed_turns` / `last_completed_turn_id`（含旧库 ALTER 迁移）；`complete_turn`（幂等）、`rotate_thread`（指针切换 + 清零 + 入队同事务）、`turn_state` |
| `harness/threads.py` | `RotationPolicy` / `decide_rotation` / `PRESERVED_CONTEXT_KEYS`；`prepare_ai_turn` 改 async，命中阈值→查 blocker→轮换→读旧 context→best-effort 删旧 thread；`ThreadTurn` 增 `rotated_from` / `rotation_reason` / `context_seed` |
| `harness/thread_guard.py`（新） | `has_active_handoff` / `approval_records` / `approval_protection` / `interrupt_protection` / `collect_blockers`；`interrupt_loader is None` → fail closed |
| `harness/thread_probes.py`（新） | SQLite 探测实现：`load_thread_interrupts`（官方 `aget_state().interrupts`）、`load_thread_context`（直接读 `channel_values.context`）、`delete_thread`（`adelete_thread`） |
| `harness/server.py` | `ThreadLifecycleService(status, **sqlite_lifecycle_probes(db_path))`；`/chat` 注入 `context_seed` 到 `extra_state`、终态且非 resume 才 `complete_turn`；AgentBot 接线加 `conversation_lock_factory` |
| `harness/agent_bot.py` | 拆 `_handle_locked_agent_bot_message`（解析身份 + 会话级锁）与 `_handle_agent_bot_turn`（原逻辑）；`resume_turn` 不计数；`turn_id` 用 Chatwoot message id |
| `ops/thread_cleanup.py` | `_pending_protections` 改为委托 `collect_blockers`（删掉本地重复实现） |

## 为什么 AgentBot 要拆函数

会话级锁必须在**知道 `conversation_id` 之后**获取（之前只有 `cw_conv_id`），而原函数把身份解析和 380 行执行体混在一起。拆成「解析 → 加锁 → 执行」后，两个 Transport 用的是同一个 `conversation:{id}` key，锁内覆盖 thread 解析到图执行完成。

`chatwoot:{cw_id}` 锁保留：它在解析之前，保护「首次触达 → create_session + mapping.put」不被并发重复建会话。

## 验证

```
tests/test_thread_rotation.py                         32 passed
tests/test_session.py                                 9 passed（含迁移事务/恢复）
tests/test_server.py::test_approval_turn_and_resume_…  passed
全量 tests --ignore=tests/evals                       见 PR 描述
```

覆盖：阈值边界（29 不轮换 / 30 轮换）、幂等计数、原子轮换、指针已被移动时不重复切、
五类挂起阻止轮换（pending Form Interrupt、**未过期** approval、**已过期但 checkpoint 仍挂着**的 approval、
approval 执行缺口、awaiting handoff）、删除失败仍返回本轮且留队列可重试、
只带白名单 context、有界 hydration、**context_seed 真的落到新 thread 的图 state**、
Widget/AgentBot 同阈值 + 同一把会话锁、并发只建一个 thread、清理竞态不复活旧 thread。

### Code review 后的修正（2026-10-29）

| 级别 | 问题 | 修法 |
|---|---|---|
| Important | checkpoint 上挂着**已过期**的 `approval_required` 时仍会轮换 → 旧 thread 被删，「审批已过期」收尾路径永远不可达，审批行永久 pending | `collect_blockers(block_expired_approvals=True)`：轮换模式下一个 pending 审批无论是否过期都挡；清理模式保持「过期可回收」 |
| Important | `conversation_status` 重建未包事务，崩溃会留下「新表空、数据在旧表」并静默丢全部会话 | 重建放进 `BEGIN IMMEDIATE`；另加残留表恢复逻辑 |
| Important | `context_seed` 与 AgentBot 会话锁零端到端覆盖（删掉接线测试仍全绿） | 加 `/chat` 断言新 thread 图 state 含 `fallback_count`；加 AgentBot 锁 key 断言 |
| Important | `complete_turn` 幂等窗口只有上一条；Widget turn_id 是每请求 uuid | 记为已知边界（两条路径实际都不可达重复），写进 docstring 与 spec |
| Minor | `rotated is None / current 非空` 分支仍全量水合 | 该分支也置 `use_bounded=True` |
| Minor | `_delete_old_thread` 的记账本身失败会抛出，反而让用户轮失败 | 记账再包一层 try/except，只记日志 |
| Minor | `messages_store is None` 时仍计完成轮次 | 改为以「确实落库」为准 |

## 明确不做（属 #151）

后台 idle reaper、`cleanup_once(now)` 运行时入口、lifespan 定时任务、队列的周期性重试调度。本单只在**轮换点**顺带 best-effort 处理队列里的那一笔。
