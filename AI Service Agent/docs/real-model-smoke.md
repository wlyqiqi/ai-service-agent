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


## 7. CT-RL-0076 真机复验：模糊退款请求（2026-09-16 实测：P5 修复前 FAIL 2 次 → P5 D3 复验 PASS）

- 输入：`退了吧`（ambiguous，未指明订单/原因/金额）。
- 判据（D2）：SSE 必须含 `clarification_required`、不得含 `approval_required`；`execution_status=clarifying`。
- **P5 修复前（B1/B2b 前，2 次运行均 FAIL，漂移稳定）**：
  - Run 1：timeline `[confirmation_required, done]`，interrupt=「order_lookup」{request_id}——Router 判入 mutation 图，select 选只读工具 order_lookup。
  - Run 2：timeline `[approval_required, done]`，interrupt=「order_lookup」{request_id}——Router 判入 high_risk 图，select 仍选 order_lookup。
  - 根因：order_lookup/kb_search 无 `ToolMeta.schema` → D1 参数完备性 Clarify 不触发 → 只读工具直接进闸门
    （Run 2 甚至对只读操作发起审批）。
- **P5 D3 复验（B1/B2b 落地后，2026-09-16）**：PASS。
  - SSE 时间线：`[clarification_required, done]`；`approval_required` 未出现；`execution_status=clarifying`。
  - 本轮真实模型选择 `refund_apply` 空参 → **D1 字段澄清**：assistant「请提供：订单号。」（Message Store 佐证，
    conversation `60be0a3c`）；`thread_id` 全程一致；`conversation_mode` 保持 `ai`；无 interrupt、无工具执行。
  - 注记：本轮未复现"选只读工具"路径；B2b 的 tier_mismatch 澄清路径由离线测试确定性锁定
    （`tests/test_clarify.py::test_highrisk_read_tool_selected_tier_clarifies`）。
- 对照同批四场景回归：FAQ / Mutation / HighRisk / Handoff 全部 PASS（Mutation 确认→工具仅执行 1 次；
  HighRisk 审批→refund_apply 单次执行；Handoff 人工 Resolve 后 AI 恢复并正常答复）。
- 结论：0076 的"模糊高风险请求"在真实模型下已回到澄清路径，不再触发不该发生的审批流。残余风险维持登记：
  写工具必填参数来源校验未实现（模型可能编造 order_id 导致 select 放行），见
  `docs/decisions/2026-09-16-p5-select-tier-alignment.md`（D3 关联）。


## 8. Mutation 工具集重构 + select 提示词升级 真机复验（2026-09-17 实测，6 场景全 PASS）

- 触发：MutationGraph 写工具集从单一 `account_update` 重构为 5 个聚焦工具
  （`update_address` / `update_phone` / `update_order_note` / `create_invoice` / `create_service_ticket`），
  `order_cancel` 重命名为 `cancel_order` 并留在 high_risk 档；select 提示词升级为 5 规则版 + 真实业务示例
  （相似工具不跨选、集合外不强行映射、缺参不编造）。离线全量 153 passed 后做本次真机复验。
- 运行：`uv run --no-sync python scripts/real_model_http_smoke.py`（`.env` 真实 ARK 模型）。
- 判据与实测（ARK 真实模型）：

| 场景 | 输入 | SSE timeline / 实测 | 结果 |
|---|---|---|---|
| FAQ/Read | 你好，有什么商品推荐吗？ | token×N → tool_call → tool_result → done；read agent 实际调用 kb_search；done.final 非空 | PASS |
| Mutation | 把收货地址改成北京市朝阳区 → 确认 | turn1 confirmation_required → done；turn2 确认 → confirmation_updated → tool_call → tool_result → done。真实模型选 `update_address`、`args.address=北京市朝阳区`；工具恰执行 1 次；`done.final="操作已完成。"` + Message Store 落库 | PASS |
| MutationClarifyMultiTurn | 我要改个地址 → 新地址是北京市朝阳区 → 确认 | turn1 clarification_required（缺参澄清，未编造地址）、clarify_count=1；turn2 confirmation_required；turn3 确认 → 工具恰执行 1 次 → completed、clarify_count 清零 | PASS |
| Clarify0076 | 退了吧 | clarification_required、无 approval_required；`done.final="请提供：订单号。"`（缺参不编造 order_id） | PASS |
| HighRisk | 订单10086申请退款 → /approvals/{id}/approve | approval_required → approve → refund_apply 恰执行 1 次；审批通过文案落 Message Store | PASS |
| Handoff | 我要转人工 → /handoffs/{id}/resolve → 你好，还在吗？ | mode human→ai；AI 恢复自然回复（"在的呢，我一直都在～"） | PASS |

- 关键结论：
  1. 新 select 提示词在真实模型下工具名选择准确——未出现旧名 `account_update`、未编造工具名，
     `update_address` 等新工具被正确命中；
  2. 参数来自用户原话（`address=北京市朝阳区`）；缺参不编造——两个澄清场景均正确进入 clarify，
     D3 残余风险（模型编造必填参数）本次未复现；
  3. 工具仍恰执行 1 次；resume 轮 `done.final` 与 Message Store 双入口落库（P4 D7 行为延续）；
  4. `thread_id` 全程一致；无异常/超时/竞态/状态不一致。
- 注记：本文档 §2/§3 的 `account_update` 为 2026-09-15 历史记录（工具集重构前）；当前工具集权威清单
  见 `docs/specs/tool-spec.md` §4。


## 9. 新工具矩阵真机探针（2026-09-17 实测：判定修正后 7/7 命中）

- 目的：真实 ARK 模型对 7 个写工具（`update_address` / `update_phone` / `update_order_note` /
  `create_invoice` / `create_service_ticket` / `cancel_order` / `refund_apply`）的
  **工具选择命中率 + 参数提取成功率**——工具集重构验收的最后一环。
- 实现：`scripts/real_model_http_smoke.py` 新增 `scenario_tool_matrix`（`TOOL_MATRIX` 7 case，
  每 case 一条完整参数话术走 `/chat` 链路，从 confirmation/approval interrupt 的 summary
  解析模型所选工具与 args；`reason` 等可选字段只要求存在非空——值来自用户原话，探针不做精确比较）。
- 实测（ARK 真实模型）：
  - **Run 1（判定修正前）5/7**：`cancel_order` 判为 args mismatch（模型提取 `reason="不要了"`，
    忠实原话，探针期望值 `"不想要了"` 过严）；`create_service_ticket` 漂移到 `refund_apply`
    （"申请售后"被映射为退款申请，走 approval 审批流）。
  - **Run 2（判定修正后）7/7 全命中**：7 个工具名全部正确；参数提取正确
    （`create_invoice` 四必填 order_id/amount/company/tax_no 全命中；`create_service_ticket`
    reason="屏幕有裂痕"；`cancel_order` reason="不要了"；`refund_apply` amount=199.0）；
    全部落到 confirmation/approval 闸，无编造工具名、无缺参澄清。
- 登记观察项（非稳定失败，与 CT-RL-0076 同类）：`申请售后` 与 `退款申请` 意图边界在真实模型下
  存在**偶发漂移**——Run 1 中 `create_service_ticket` 被判为 `refund_apply`，触发不必要的审批流；
  Run 2 恢复正确。后续如需收敛，可评估 select high_risk 档示例强化"售后工单 vs 退款"边界，
  或将该歧义纳入 Router/select 澄清触发。维持平级排队，暂不实现。
- 结论：新工具集在真实模型下工具名选择稳定（修正后 7/7）、参数来自用户原话、缺参不编造；
  工具矩阵探针纳入冒烟脚本可重复执行（`SMOKE_MODEL=fake` 离线基线 7/7）。
