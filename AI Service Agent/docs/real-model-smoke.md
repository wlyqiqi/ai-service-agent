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

### 9.1 复测：12 条全新案例（2026-09-17 实测：命中 9/10 + 观察 2/2）

- 目的：换全新案例复测 7 个写工具的选择/参数行为，覆盖更自然的表达与边界
  （`TOOL_MATRIX` 12 条：10 条计入命中率 + 2 条观察项；`expect` 字段区分统计口径）。
- 实测（ARK 真实模型）：
  - **命中组 9/10**：
    - 全命中：`update_address`（上海市浦东新区张江高科）、`update_phone`（18600001111）、
      `update_order_note`（工作日晚上六点后配送）、`create_invoice`（88888/350 元/蓝海贸易/91310115…，
      四必填全对）、`cancel_order`（"退掉订单 66666"未漂移到 refund_apply）、`refund_apply`（55555/88 元）。
    - 边界命中：相似工具名未跨选（`把收货手机号改成 13900002222` → `update_phone`，未被"收货"误导到
      `update_address`）；中文数字金额正确转换（`申请退款一百五十块` → `amount=150.0`）；
      `订单 44444 坏了，申请售后处理` → `create_service_ticket` 命中。
    - 1 条 wrong_tool：`我要提交售后，订单 77777，问题是想退货但包装丢了` → 模型选 `refund_apply`
      （`reason="想退货但包装丢了"`），走 approval 审批流。
  - **观察组 2/2 符合预期**：`我要开发票`（缺参）→ `clarification_required`，
    final=`请提供：订单号、金额、公司名、税号。`（缺参不编造）；`帮我把订单改成已发货`（集合外）→
    `safety_rejected(unknown_tool)`，未强行映射到其它工具。
- **观察升级**：`售后/退款` 意图漂移从"偶发"升级为**可复现倾向**——3 次工具矩阵运行中 2 次漂移
  （Run1 `申请售后`、本次 `提交售后…想退货`），且均与"退货/退款"字样相关；`申请售后处理` 则稳定正确。
  登记为 Open Question：是否在 select high_risk 档示例强化"售后工单 vs 退款申请"边界（如补充
  `售后工单≠退款` 对照示例），或将"售后+退货"歧义纳入澄清触发。维持平级排队，暂不实现。
- 离线基线：`SMOKE_MODEL=fake` 命中 10/10 + 观察 2/2（探针逻辑与 fake 分支同步扩展）。


## 10. Chatwoot 坐席端真机联调（2026-09-17 实测：6 步骤全 PASS）

- 目的：验证「FastAPI Transport → Chatwoot API inbox（坐席 UI/输入层）→ 人工车道推送 →
  webhook 回写 Message Store → handoff resolve 恢复 AI」全链路；Chatwoot 故障不影响 Runtime 主链路。
- 环境：Chatwoot v4.17.1 自托管（Docker；inbox 2「澄元AI客服」，Channel::Api，account_id=3）；
  Runtime 需绑定 `0.0.0.0:8000`（容器经宿主机 LAN IP `172.99.46.165:8000` 访问；
  `host.docker.internal` 在本机 Docker 环境不可达，改用手动配置的 LAN IP）。
- 前置：`.env` 配置 `CHATWOOT_*`；Chatwoot UI 中 inbox webhook URL =
  `http://172.99.46.165:8000/webhooks/chatwoot`（inbox 级 webhook 只能 UI 配置，API 无法更新）。

| 步骤 | 输入/动作 | 实测 | 结果 |
|---|---|---|---|
| 1 建会话 | POST /sessions | mode=ai | PASS |
| 2 转人工 | POST /chat "我要转人工" | SSE: token(转接文案) → handoff_created → error(ModeChanged) → done（error 为 mode 守卫设计内行为）；Chatwoot 新建 conversation（sender=user-<conv8>，首条=转人工提示） | PASS |
| 3 人工消息推送 | POST /chat "在吗"（mode=human） | Message Store 落 user+自动回复；Chatwoot conversation 出现 "在吗"（sender_type=contact） | PASS |
| 4 坐席回复 | Chatwoot 侧发 outgoing 消息 | webhook 送达 Runtime（HMAC 校验通过）→ Message Store 追加 assistant 回复 | PASS |
| 5 resolve | POST /handoffs/{id}/resolve | mode=ai；record resolved | PASS |
| 6 AI 恢复 | POST /chat 正常问题 | AI 车道 token×N → done，真实 ARK 回复；无残留中断 | PASS |

- 核验项：HTTP 均 200；thread_id 全程一致；interrupt/resume 无残留；工具无重复执行；
  Message Store 无重复落库（转人工提示仅 1 条）；Chatwoot 消息状态 sent（投递成功）。
- **真机发现并修复的 v4.17.1 契约缺陷**（离线测试曾全绿，真机暴露）：
  1. contact 创建响应为 `payload.contact.id`（非 `payload.id`）→ `ChatwootClient._create_contact` 修正；
  2. 消息端点 `content` 必须在顶层（`message.content` 会建空消息）→ `send_as_contact` body 修正；
  3. webhook 签名 = `HMAC(secret, "<ts>.<raw body>")`，需 `X-Chatwoot-Timestamp` 头（非旧版
     `HMAC(body)`）→ `verify_chatwoot_signature` 增加 timestamp 参数，端点读取该头；
  4. webhook 载荷坐席 `sender.type = "user"`（非 "Agent"；Contact 无 type）→ 过滤器改为 `!= "user"`；
  5. 回显重复：Runtime 经 ensure_conversation 创建的首条消息（转人工提示）会触发
     `message_created` webhook 回显导致二次落库 → 新增 `chatwoot_runtime_message` 表登记
     Runtime 自建消息 id，webhook 命中即跳过（方案 A）。
- 已知观察（非失败，登记后续关注项）：
  1. **Router 真实模型波动**：`我要转人工` 需 Router 分类到 unknown→Fallback 才触发 handoff；
     实测 3 次中 1 次被分到其它能力直接 AI 回复（重试新会话后触发）。handoff 触发依赖
     Router 分类路径，非纯关键词直连——后续可评估在 Router/入口层增加显式 handoff 意图识别。
  2. Chatwoot 消息 webhook 会收到 incoming（Contact）与 conversation_created 等事件，
     端点按 message_type/event 过滤，不落库；账户级 webhook 与本链路无关（投递走 inbox
     channel.webhook_url）。
- 回归：全量 pytest 204 passed + `langgraph validate` valid。

## 11. 人工车道 + 页面契约 真机复验（2026-09-18 脚本化：10 步全 PASS）

- 目的：契约对齐（user-page-spec §10/§11）后，按页面 `createApi` 实际调用形状复验整条
  「用户端页面契约 ↔ Runtime ↔ Chatwoot 坐席端 ↔ webhook 回写」人工车道；可重复执行。
- 脚本：`scripts/real_human_lane_smoke.py`（真实 ARK 模型 + 真实 Chatwoot；Runtime 绑
  `0.0.0.0:8000` 供 webhook 回推；每次全新临时 SQLite）。
- 运行：`uv run --no-sync python scripts/real_human_lane_smoke.py`
- 实测（2026-09-18，inbox 2 澄元AI客服，account_id=3）：
  1. `POST /auth/login` → `{token, user{id,name}}` PASS
  2. `POST /sessions {user_id}` → `{session_id, mode:ai}` PASS
  3. `GET /conversations/{id}/messages` → `{items, mode}`（页面形状）PASS
  4. AI 车道 `POST /chat "你好"` → `token×N → done` PASS
  5. 转人工 `POST /chat` → `handoff_created` + `mode=human`；`error(ModeChanged)` 为
     mode 守卫设计内行为（同 §10）PASS
  6. 人工消息 `POST /chat "在吗"` → Message Store 落 user+自动回复；Chatwoot 侧出现
     contact 消息（`message_type=0` incoming）PASS
  6b. Chatwoot 收到 contact 消息 PASS（探测会话：notice / 在吗 / 坐席回复 三条齐全）
  7. 坐席 outgoing 消息 → webhook 回写 Message Store，无重复落库 PASS
  8. `GET messages` 含坐席回复 + `mode=human` PASS
  9. `POST /handoffs/{id}/resolve` → `mode=ai` + record resolved PASS
  10. AI 恢复 `POST /chat` → `token×N → done`（无残留中断）PASS
- **真机修复**：`ChatwootClient` 默认 `httpx.AsyncClient(trust_env=False)`——本机代理会
  拦截 `localhost:3000` 导致 `_create_contact` 400；Chatwoot 为本地基础设施，不走环境代理。
- 已知观察：step-10 曾有 1 次 `["error","done"]`（无 token），复跑确认偶发（真实模型
  单轮失败），非系统性 resume 故障；脚本已对 step-10 捕获 error 详情 + 换问重试一次。
- 回归：离线 `test_server_chatwoot / test_e2e_human_lane / test_server_auth` 全绿。

## 12. 用户页面真机 E2E（2026-09-18 Chrome headless + CDP：7 步全 PASS）

- 目的：契约对齐 + 静态同源托管后，改版页面在真实浏览器里对真实 Runtime 全链路验证
  （登录 → AI 对话 → 转人工 → 人工车道 → 坐席回复经 webhook 回写 + 页面轮询 → resolve → AI 恢复）。
- 前置：Runtime（`ai-service-agent`，0.0.0.0:8000，真实 ARK + Chatwoot）运行中；
  页面由 Runtime 同源托管于 "/"（`file://` 会强制 mock，勿用）。
- 脚本：`scripts/page_e2e_smoke.mjs`（Chrome headless + CDP；Node ≥ 22 内置 WebSocket/fetch）。
- 运行：`node scripts/page_e2e_smoke.mjs`
- 实测（2026-09-18）：7 步全 PASS；`handoff_id` 经 `GET /handoffs?conversation_id=` 获取
  （CDP `Network.getResponseBody` 对流式 SSE 不可靠，故新增该查询端点，镜像 `GET /approvals`）；
  页面无 console 异常（仅 favicon 404 等无害资源）。
- 新增 Transport：`GET /handoffs?conversation_id=`（server.py）+ `HandoffService.get_by_conversation()`；
  `main.py` host/port 可配（默认 0.0.0.0）；`server.py` 静态同源挂载 `web/`。
- 回归：全量 pytest 225 passed。

## 13. P0 显式转人工入口检测（2026-09-18）

- 目的：验证「用户明确要求转人工」在入口层 `detect_handoff_intent` 阶段直接进入 Human Lane，
  即使 Router 原本可能把文本误判成其它 capability；**关键断言是 `handoff_created` + `mode=human`，不是 Router 分类**。
- 前置：Runtime（`ai-service-agent`，0.0.0.0:8000，真实 ARK + Chatwoot）运行中。
- 脚本：`scripts/real_human_lane_smoke.py`（step 5 多短语按序重试兜底）。
- 复验用例（至少两条）：
  1. `我要转人工` → 必须出现 `handoff_created`、`mode=human`。
  2. `我不想跟机器人聊，叫个真人来` → 同上（该句旧版依赖 Router/关键词可能漏判，P0 detector 命中「叫个真人」）。
- 入口行为：命中 detector 时**不进 Router / Guard / Graph**（直接 `request_handoff` + `_emit_handoff`）；
  图内 `check_handoff` 保留动态信号与兜底。决策与实现见 `docs/plans/2026-09-18-handoff-intent-gate.md`。
- 回归：全量 pytest 258 passed；`langgraph validate` valid。
## 14. ReadGraph RAG 真机冒烟（LightRAG-chengyuan + ARK，2026-09-18 实测）

- 目的：用真实 LightRAG-chengyuan 检索 + 真实 ARK 模型验证 ReadGraph `kb_search` 链路：
  依据知识库作答时标注来源文件名（D2）、知识缺口如实告知「未查询到」（D5）。
- 前置：LightRAG-chengyuan 容器运行于 `http://localhost:9621`（auth disabled，已摄取 31 份文档）；
  `.env` 配 `ARK_MODEL / ARK_BASE_URL / ARK_API_KEY`（`make_model` 读取）。
- 脚本：`scripts/real_rag_smoke.py`（可重复执行；退出码 0=全部通过）。
- 运行：`$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy";
  uv run --no-sync python scripts/real_rag_smoke.py`
- 服务端点：`kb_search` → `LightRagClient`（httpx 单例，默认 `localhost:9621`）→ `POST /query`。
- `/query` 参数：`mode=naive`、`only_need_context=true`、`include_references=true`、
  `include_chunk_content=true`、`enable_rerank=false`（关 rerank）、`top_k=5`。
- 实测延迟（2026-09-18，复跑确认）：CASE 1 6.51s / CASE 2 8.12s / CASE 3 13.95s / CASE 4 6.49s，总计 35.07s。
  两次复跑总计分别为 41.69s / 35.07s，存在运行间波动（主要来自 ARK 模型生成 + 工具往返）。修复轮起脚本对 kb_search 单独计时：检索本身实测 0.35–0.66s/case（CASE1 0.66s / CASE2 0.35s / CASE3 0.35s / CASE4 0.57s，2026-09-18 复跑）。

| CASE | 问题 | 判定 | 回复要点 |
|---|---|---|---|
| 1 | JoyAir A1 的炸篮容量是多少？ | PASS | 答 4.5L，注明炸篮 BA-A1-45 仅适用 A1；文末（来源：JoyAir_A1智能空气炸锅.md、配件目录.md） |
| 2 | JoyClean V1 支持 Wi-Fi 连接吗？ | PASS | 答不支持 Wi-Fi（蓝牙 5.0 仅电量/滤网提醒），并推荐 S1；文末（来源：JoyClean_V1无线吸尘器.md、Wi-Fi与配网支持.md） |
| 3 | 空气炸锅忘记密码怎么配网？ | PASS | 给出 App 解绑重加/2.4GHz/配网模式等步骤；文末（来源：Wi-Fi与配网支持.md、App服务与账号政策.md、JoyAir_A1智能空气炸锅.md） |
| 4 | 你们的注册资本是多少？ | PASS | 如实告知知识库未查询到确定数据，引导以官方渠道为准（来源：知识缺口说明.md） |

- 来源文件名样例（工具「参考来源」清单实际值）：`JoyAir_A1智能空气炸锅.md`、`JoyClean_V1无线吸尘器.md`、
  `Wi-Fi与配网支持.md`、`App服务与账号政策.md`、`知识缺口说明.md`、`配件目录.md` 等（top_k=5 命中文件）。
- unanswerable 行为：知识缺口问题（注册资本）检索到的是「知识缺口说明.md」而非空结果，模型如实告知
  「暂未查询到已公布的确定数据」「不便推测或估算」，未编造数值 → 判定 PASS（断言按「未查询到/无法/不清楚」措辞命中）。
- 观察项：无 FAIL，4/4 PASS。unanswerable 判定依赖 LLM 措辞（"未查询到"），若模型换说法仍可能误判，属措辞差异非功能缺陷。
- 回归：本次未跑全量 pytest（由 Task 5 全量回归）；`kb_search` / `LightRagClient` 离线用例保持全绿。

## 15. Retrieval Context Budget + LightRAG CHUNK_TOP_K（2026-09-30）

- LightRAG 服务端保持解耦：`.env` 增加 `CHUNK_TOP_K=3`，控制 naive 检索候选 chunk 数；该配置为 query-time 生效，不需要重建索引。
- 项目客户端继续负责下游 prompt 预算：`top_k=5`、`max_chunk_chars=1200`、`max_context_chars=4000`，保留 `file_path` / source 信息。
- 真机验证（「你们有可以做果汁的破壁机吗」）：`kb_search` context 从 12,418 chars 降至 3,759 chars；下游 prompt 从 7,520 tokens 降至 2,783 tokens。
- 直接 LightRAG eval：果汁破壁机 3 chunks / 3,008 chars / 1.86s；除扫地机器人外产品 3 / 2,698 / 1.56s；电饭煲价格 3 / 2,013 / 2.88s；发票政策 3 / 1,891 / 1.44s。
- Router 同步回归：产品列表、电饭煲价格、发票政策均命中 faq（置信度 0.97–0.99），ReadGraph 真机调用 `kb_search` 正常。
- 回归：全量 pytest 531 passed / 2 skipped。
- LightRAG 与客户端职责边界：服务端 `CHUNK_TOP_K` 是未指定时的默认召回数；客户端 `top_k` 作为 query-time 参数发送给 LightRAG API，控制单次检索返回 chunk 数（两者均为检索配置，不是"客户端配置服务端"）。客户端 `max_chunk_chars` / `max_context_chars` 仅负责下游 prompt 截断预算。
- JEV 路由真机回归：10/10 正确；「我要开发票」连续 5/5 命中 `order_mutation`，confidence 1.0。
- Studio 调试入口：`langgraph.studio.json` 仅用于调试子图；生产 `langgraph.json` 保持只暴露根 `agent`。
