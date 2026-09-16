# 真机冒烟 Checklist（ARK 真实模型）

- 目的：用 `.env` 中的真实 ARK 模型验证 Router / Guard / select / Mutation / HighRisk
  在真实模型下的行为；**不进离线 eval**（依赖真实 API Key 与网络，非确定性）。
- 前置：`.env` 已配置 `ARK_MODEL / ARK_BASE_URL / ARK_API_KEY`；离线全量测试保持全绿。
- 运行方式：`$env:UV_CACHE_DIR="<repo>\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync python -` 内联脚本。

## 1. Router json_mode 解析（15/15，2026-09-15 实测）

- 输入：`tests/evals/control/adversarial.jsonl` 5 条 + `ambiguous.jsonl` 10 条。
- 判据：`_parse_router_content` 不抛异常；输出经 `RouterSchema` 校验。
- 已知分歧（非阻塞）：CTRL-0071 / CTRL-0076 / CTRL-0077 分类与 expected 不一致，见 P4 Open Questions。

## 2. select 字段契约（2026-09-15 漂移修复后复验）

- 输入与期望输出（含值输入）：

| 输入 | 期望工具 | 期望 args 关键字段 |
|---|---|---|
| 把收货地址改成北京市朝阳区 | account_update | `address=北京市朝阳区` |
| 把绑定的手机号改成13800000000 | account_update | `phone=13800000000` |
| 订单10086申请退款 | refund_apply | `order_id=10086` |

- 判据：args 字段名必须与 `ToolMeta.schema` 一致（`extra="forbid"` 下未知字段会被 select 拒绝为
  `invalid_args`，绝不静默丢参）；关键字段值非空。
- 注意：**无值输入**（如「改一下收货地址」未给新地址）下真实模型返回空 args 是正确行为——
  模型拒绝编造值，确认闸摘要会出现 null；此时应走澄清流程（P4 Open Question，CT-RL-0076 关联）。

## 3. 全链路（Mutation / HighRisk）

- mutation：`把收货地址改成北京市朝阳区` → 必须到达 `confirmation_required` interrupt，
  且 summary 中 `address` 非 None。
- high_risk：`订单10086申请退款` → 必须到达 `approval_required` interrupt
  （预检通过，不再因缺参被 invalid_args 误拒）。
- Router 期望：`account/mutation`、`order/high_risk`（confidence ≥ 阈值 0.6）；Guard 应为 pass。

## 4. 2026-09-15 实测结果

- Router 15/15 解析成功（阻断项修复后稳定）；Guard v1 对 5 条 adversarial 5/5 拦截、
  6/6 同义改写绕过（D2 过渡声明复现，LLM Guard 升级维持平级排队）。
- select 漂移修复（998489d）后：含值输入字段对齐 ✓；mutation 确认闸摘要非全 None ✓；
  high_risk 到达 approval_required ✓。

## 5. 端到端 HTTP + SSE 冒烟（2026-09-15 实测，4 场景全 PASS）

- 脚本：`scripts/real_model_http_smoke.py`（可重复执行；每次用全新临时 SQLite DB；
  内置 uvicorn 起服 + httpx 消费 SSE + DB/checkpoint 状态核验）。
- 用法：`$env:UV_CACHE_DIR="<repo>\\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync python scripts/real_model_http_smoke.py`
- 覆盖链路：FastAPI Transport → SSE → Conversation Runtime → CustomerServiceGraph → Checkpointer/Resume → Tool/Approval/Handoff。
- 判据与实测（ARK 真实模型）：

| 场景 | 输入 | SSE timeline（实测） | 关键断言 | 结果 |
|---|---|---|---|---|
| FAQ/Read | 你好，有什么商品推荐吗？ | token×88 → tool_call → tool_result → done | done.final 非空；read agent 实际调用 kb_search | PASS |
| Mutation | 把收货地址改成北京市朝阳区 → 确认 | turn1: confirmation_required → done；turn2: confirmation_updated → tool_call → tool_result → done | 中断→resume→工具恰执行 1 次；最终 execution_status=completed | PASS |
| HighRisk | 订单10086申请退款 → /approvals/{id}/approve | approval_required → done（审批走 HTTP 端点） | 审批 pending→approved；refund_apply 恰执行 1 次；无残留中断 | PASS |
| Handoff | 我要转人工 → /handoffs/{id}/resolve → 你好，还在吗？ | token(转接文案) → handoff_created → error(ModeChanged) → done；resolve 后新 turn：token×24 → done | mode human→ai；record resolved；AI 恢复接管正常回复 | PASS |

- 全程核验项：HTTP 状态均 200；thread_id 每会话全程一致（checkpoint 全部落在映射 thread_id）；
  interrupt/resume 生命周期正确（确认闸、审批闸、人工接管均无残留中断）；无异常/超时/竞态/状态不一致。
- 已知观察（非本次失败，登记为后续关注项）：
  1. **resume turn 不回传 final 文本**：Mutation「确认」/ Approval approve 后的最终回复（如"操作已完成。"）
     在 checkpoint 中可见，但该轮 SSE `done.final=null`、不写入 Message Store——resume 轮 final 只取流式增量，
     确定性节点写入不产生 messages 流。若演示要求"确认后用户能看到完成文案"，需在 P4 评估
     resume 轮 final 文本提取/落库。
     - **→ P4 D7（2026-09-15）已落地**：resume 轮 final 经 pre/post message diff 提取
       （`harness/streaming.py` `snapshot_assistant_ids` / `new_assistant_text_and_id`），
       SSE `done.final`（`/chat` done 分支落 Message Store）与 `/approvals/{id}/approve`
       （`_approval_resume`）双入口同步落库；真机复验结果见 §6。
  2. **handoff turn 的 error(ModeChanged) 是设计行为**（handoff-spec.md §1.4：AI 回复发送前实时校验
     mode，已切走则丢弃），不是故障。

## 6. P4 D7 复验：resume 轮 final 提取与落库（2026-09-15 真机实测）

- 复验脚本：`scripts/real_model_http_smoke.py`（本次新增 D7 断言：Mutation「确认」resume 轮
  `done.final` 精确文案 + Message Store 落库；HighRisk approve 后 Message Store 落库）。
- 判据：`docs/decisions/2026-09-15-p4-clarify-precheck-resume.md` D7——resume 轮 final 经
  pre/post message diff 提取（`streaming.py`），SSE `done.final` 与 Message Store 双入口同步，
  不恢复历史消息。
- 实测（ARK 真实模型，4 场景全 PASS）：

| 场景 | D7 断言 | 实测 | 结果 |
|---|---|---|---|
| Mutation「确认」 | resume 轮 `done.final == "操作已完成。"` | `done.final="操作已完成。"`（message_id=4f7fd3eb…）；Message Store 含 assistant「操作已完成。」 | PASS |
| HighRisk approve | `/approvals/{id}/approve` 后 Message Store 含「审批通过，操作已完成。」 | approve → status=approved；Message Store 含 assistant「审批通过，操作已完成。」 | PASS |

- 结论：§5 已知观察 1 已解决——resume 轮 final 不再为 null，且同时落入 SSE `done.final` 与
  Message Store（`/chat` done 分支 + `_approval_resume` 双入口，各落库一次）。tool 仍恰执行 1 次
  （Mutation 1×、HighRisk 1×）；execution_status=completed；无残留中断；thread_id 全程一致。


## 7. CT-RL-0076 真机复验：模糊退款请求——真实模型行为漂移（2026-09-16 实测，2 次运行）

- 输入：`退了吧`（ambiguous，未指明订单/原因/金额）。
- 判据（D2）：SSE 必须含 `clarification_required`、不得含 `approval_required`；`execution_status=clarifying`。
- 实测（2 次运行均 FAIL，漂移稳定）：
  - Run 1：timeline `[confirmation_required, done]`，interrupt=「order_lookup」{request_id}——Router 判入 mutation 图，select 选只读工具 order_lookup。
  - Run 2：timeline `[approval_required, done]`，interrupt=「order_lookup」{request_id}——Router 判入 high_risk 图，select 仍选 order_lookup。
- 根因：真实模型对"退了吧"**稳定选择只读工具 order_lookup**（而非 refund_apply 空参）；order_lookup/kb_search
  无 `ToolMeta.schema` → D1 参数完备性 Clarify 不触发 → 直接进闸门（Run 2 甚至对只读工具发起审批）。
- D3 探针：Run 2 实际**命中 D3 残余风险类**——模型对模糊请求未澄清、直接推进到审批闸（工具为只读，无害但不达目的）。
- 对照同批：HighRisk"订单10086申请退款" Run 1 误判（select=order_lookup/确认闸）、Run 2 恢复正确
  （refund_apply→approval→PASS），**该输入为偶发漂移**；FAQ/Mutation/Handoff 两轮均 PASS。
- 结论：0076 冒烟断言如实 FAIL；离线契约（select 缺必填→clarify）仍由 `tests/test_clarify.py` 确定性锁定，
  不受真实模型选择漂移影响。order_lookup/kb_search 无 schema + 只读工具可入写图 select 允许集 → P5 open question。
