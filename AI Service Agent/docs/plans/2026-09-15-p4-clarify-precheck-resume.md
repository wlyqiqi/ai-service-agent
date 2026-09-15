# P4 Clarify / 预检规则增量 / 读侧边界 / resume final 提取 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 落地 P4 四项能力：select 参数完备性触发的 Clarify（普通轮非 interrupt）、预检规则增量（必填非空 + 订单状态业务规则 + 读侧属主边界）、resume 轮 final 文本提取（/chat 与 /approvals 双入口）、Guard LLM 评估报告，并把对应 eval skip 转为真实断言。

**Architecture:** Clarify 作为 mutation/high_risk 子图内 select 后的普通轮分支（条件边路由），确定性模板文案 + `execution_status=clarifying` + `clarification_required` 事件，`clarify_count` 存 context、完整 select 清零、>N=2 转 fallback；业务规则的事实由新确定性节点 `precheck_fetch` 从最小订单状态 stub 读取写入 `context["precheck_data"]`（仅事实），裁决仍由 `safety_check` 完成；读侧边界由 `order_lookup` 经 `langgraph.prebuilt.InjectedState` 注入 `user_id` 自行校验；resume 轮 final 通过 pre/post assistant id diff 只取本轮新增文本。

**Tech Stack:** Python 3.14；LangGraph 1.2.11 / langchain-core 1.6.3；`langchain.agents.create_agent`（禁止 `create_react_agent`）；Router/select 一律 json_mode 契约；`langgraph.prebuilt.InjectedState`（**不在** `langchain_core.tools`，2026-09-15 实测）；pytest + 离线 fake model；无新依赖。

**Spec:**
- `docs/decisions/2026-09-15-p4-clarify-precheck-resume.md`（D1–D8 + OQ-3 裁定，本计划唯一决策依据）
- `docs/specs/graph-spec.md`（§6.2/§6.3/§9）
- `docs/specs/safety-spec.md`（§1.2/§1.3）
- `docs/specs/state-spec.md`（§1）
- `docs/specs/event-spec.md`（§2/§2.1）
- `docs/specs/tool-spec.md`（§4）

## Global Constraints

- **json_mode 契约已定**：Router/select 一律 `model.bind(response_format={"type":"json_object"})`（真实模型）或 fake 直接 JSON 字符串；**禁止** ToolStrategy 描述性文本解析依赖。
- **决策路由一律条件边**：LangGraph 1.2.11 实测 `Command(goto)` 不抑制静态出边会并发双写；新增 clarify / precheck_fetch 分支必须用条件边（节点返回普通 dict）。
- **Clarify 非 interrupt**：不新增 resume schema 分支；用户澄清后下一条消息正常重走 Router → 子图 → select。
- **决策在图内**：precheck_fetch 只提供事实（订单状态快照），授权/业务裁决 100% 留在 `safety_check`；`order_lookup` 的属主校验是工具层强制（safety-spec §1.4），不加 ReadGraph 预检节点。
- **resume final 不回落历史**：只取本轮新增 assistant 消息（id diff），历史文本绝不重复回传/落库（P1 原则）。
- **eval 纪律（AGENTS.md §16）**：离线确定性，不依赖真实模型 key；业务规则种子数据必须确定（种子订单固定 id/状态/时间戳）。
- **不新增依赖**；依赖统一在 `pyproject.toml` 声明。
- 运行前缀：`$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy";`，一律 `uv run --no-sync ...`。
- git 根在 `D:\AI_Project`；在 `AI Service Agent` 目录内执行 git，commit 自动带前缀。
- 每个任务结束：该任务测试全绿 + 用户手动 `git commit`；`langgraph validate` 保持通过。
- **环境事实（实测钉死）**：`InjectedState` 在 `langgraph.prebuilt`（`langchain_core.tools` 无此名）；pydantic 接受 `order_id=""`（D2 规则必要）；`FieldInfo.is_required()` 可用。

---

### Task 1: Clarify 机制（D1）

**Files:**
- Create: `src/ai_service_agent/graph/workflows/clarify.py`
- Modify: `src/ai_service_agent/graph/workflows/select.py`（完备性判定 + `clarify_missing` + 完整 select 清零 `clarify_count`）
- Modify: `src/ai_service_agent/graph/workflows/mutation_agent.py`、`src/ai_service_agent/graph/workflows/high_risk_agent.py`（select 后条件边 → clarify）
- Modify: `src/ai_service_agent/graph/builder.py`（`finalize` 条件化，保留 `clarifying`/`waiting_*`/`failed`）
- Modify: `src/ai_service_agent/config.py`（`clarify_max_turns: int = 2`）
- Test: Create `tests/test_clarify.py`

**Interfaces:**
- Consumes: `SELECT_SYSTEM_TEMPLATE`/`make_select_node`（现有）、`CustomerServiceState`、`Settings.clarify_max_turns`
- Produces:
  - `select` 节点输出：完整 → `context["pending_tool"]` + `context["clarify_count"]=0`；不完整 → `context["clarify_missing"]: list[str]`（不含 `pending_tool`）
  - `clarify.make_clarify_node() -> Callable[[dict], dict]`（读 `clarify_missing`/`clarify_count`，写 `messages` + `clarify_count` 自增 + `execution_status=clarifying`，发 `clarification_required`；>N 写 fallback 文案 + `completed`）
  - `builder.finalize`：`execution_status` 为 `clarifying`/`waiting_*`/`failed` 时不覆盖

- [x] **Step 1: 写失败测试**（`tests/test_clarify.py`）

```python
from ai_service_agent.graph.workflows.clarify import make_clarify_node
from ai_service_agent.config import get_settings

def _run(state): return make_clarify_node()(state)

def test_clarify_node_writes_deterministic_text_and_event():
    node = make_clarify_node()
    out = node({"messages": [], "context": {"clarify_missing": ["order_id"], "clarify_count": 0}})
    assert out["execution_status"] == "clarifying"
    assert out["context"]["clarify_count"] == 1
    assert "订单号" in out["messages"][0].content

def test_clarify_node_over_limit_falls_back():
    node = make_clarify_node()
    out = node({"messages": [], "context": {"clarify_missing": ["order_id"], "clarify_count": 2}})
    assert out["execution_status"] == "completed"
    assert "客服" in out["messages"][0].content

def test_clarify_fires_when_required_field_empty():
    # 经 make_mutation_graph：select 输出 refund_apply {"order_id": ""} -> clarify 而非确认闸
    ...
```

- [x] **Step 2: 运行确认失败**（RED：`clarify` 模块不存在 / select 无完备性判定 / builder 覆盖 clarifying）
- [x] **Step 3: 实现 `clarify.py`**（label 映射 + 模板 + fallback 文案 + `get_stream_writer` 发 `clarification_required`）
- [x] **Step 4: 实现 select 完备性判定**（必填缺失/空串，或全业务字段空 → `clarify_missing`；成功 → `pending_tool` + 清零 `clarify_count`；解析失败/未知工具/schema 错误仍 `_reject`）
- [x] **Step 5: 接线 mutation/high_risk 条件边**（`_select_target`：`clarify_missing` → `clarify`；`pending_tool` → `precheck_fetch`/`safety_check`；否则 `safety_final`）
- [x] **Step 6: builder `finalize` 条件化**
- [x] **Step 7: 全量测试 + validate**（预期：新测试绿、既有 112 不回归——注意 `test_mutation_graph` 既有用例 args 均完整）
- [x] **Step 8: 提交**

### Task 2: 必填标识符非空（D2）

**Files:**
- Modify: `src/ai_service_agent/graph/workflows/safety.py`（`_check` 通用非空规则）
- Test: `tests/test_high_risk_graph.py`（扩展：`_check` 纯函数 + safety_check/recheck 微型图节点直测；mutation 档无必填业务字段，无对应用例）

**Interfaces:** `_check` 对 required schema 字段（除 `request_id`）值 `""` → `invalid_args`。

- [x] **Step 1: 写失败测试**（Option A 裁定：select 空必填仍走 clarify；D2 为预检层兜底——`_check` 直测 + safety_check/recheck 节点注入空必填 `pending_tool` → `invalid_args`、无 interrupt）
- [x] **Step 2: RED**
- [x] **Step 3: 实现**（`meta.schema.model_fields` 遍历 required 且值 `""` → 拒绝）
- [x] **Step 4: 全量测试 + validate + 提交**

**裁定（2026-09-15）**：用户选 A——D2 为「预检层兜底」（决策记录 D2「双层」语义），
正常图路径 select 先 clarify，`_check` 兜底防 select 之外来源的 args；不反向修改 Task 1。
实施：`_check` 在 schema 校验通过后遍历 required（除 request_id）空串 → `invalid_args`，safety_check 与 recheck 共用覆盖。

### Task 3: 订单状态 stub + precheck-fetch + 业务规则 + CTRL-0089 验证（D3 + D5）

**Files:**
- Modify: `src/ai_service_agent/tools/orders.py`（`ORDER_STORE` 种子 + `lookup_order_state(order_id) -> dict | None`）
- Modify: `src/ai_service_agent/graph/workflows/safety.py`（`make_precheck_fetch_node()` + 时限/状态机规则）
- Modify: `src/ai_service_agent/graph/workflows/mutation_agent.py`、`high_risk_agent.py`（`select → precheck_fetch → safety_check`）
- Modify: `src/ai_service_agent/evals/runner.py`（`DENY_SKIP_IDS` 移除 0086/0089/0090 + scripted select + 断言）
- Test: `tests/test_tools.py`（种子）、`tests/test_high_risk_graph.py`（0086/0090 为 high_risk 工具）、`tests/test_mutation_graph.py`（0089 D5）、`tests/test_evals.py`（unskip + 基线）

**Interfaces:** `lookup_order_state(order_id) -> {"order_id","user_id","status","signed_at","cancelled_at"} | None`；种子：`ORD-10086`（signed，signed_at=10 天前，owner u1）、`ORD-CANCEL-1`（cancelled）；`make_precheck_fetch_node() -> Callable`（写 `context["precheck_data"]`，仅事实）。

- [x] **Step 1: D5 验证 CTRL-0089**：写测试确认当前拦截路径 = select `unknown_tool`（scripted select 输出不存在工具 → `precheck_reason=unknown_tool`、无 interrupt）；若实际路径不同，**停下来回报**再改 eval 语义
- [x] **Step 2: 写种子与 `lookup_order_state` 的测试（RED）**
- [x] **Step 3: 实现种子 + `lookup_order_state`**
- [x] **Step 4: 写 precheck_fetch + 规则测试（RED）**（0086：refund_apply + `ORD-10086` → 拒绝；0090：order_cancel + `ORD-CANCEL-1` → 拒绝）
- [x] **Step 5: 实现 `precheck_fetch` 节点 + `safety_check` 时限/状态机规则**（消费 `precheck_data`）
- [x] **Step 6: 接线两子图**（`select → precheck_fetch → safety_check`，条件边）
- [x] **Step 7: eval unskip 0086/0089/0090**（`DENY_SKIP_IDS` 移除；scripted select 用种子订单 id；断言 `precheck_reason` 对应值）
- [x] **Step 8: 全量测试 + validate + 提交**

**实施记录（2026-09-15）**：
- D5 验证通过：CTRL-0089 当前拦截路径 = select `unknown_tool`（registry 无删除类工具），已按预期转真实断言。
- 数据集语义修正：CTRL-0086 / CTRL-0090 `expected_risk_level` mutation → **high_risk**（D3 规则绑定 refund_apply / order_cancel，均为 high_risk 档，原标签与工具分档矛盾）。
- eval 基线：routing 87 → **90**（+0086/0089/0090），skip precheck_rule_pending 4 → **1**（仅 0087，留 Task 4）。
- 新 reason 码：`refund_window_expired`（0086）、`order_already_cancelled`（0090），写入 `DENY_EXPECTED_REASON` 断言。

### Task 4: 读侧数据边界（D4 + CTRL-0087）

**Files:**
- Modify: `src/ai_service_agent/tools/orders.py`（`order_lookup` 加 `user_id: Annotated[str, InjectedState("user_id")]`，属主校验）
- Modify: `tests/test_tools.py`（`order_lookup.invoke` 签名适配）、read agent 相关测试
- Modify: `src/ai_service_agent/evals/runner.py`（0087 unskip + 读路径 deny 脚本：fake model 发 `order_lookup` tool_call → ToolNode 返回边界拒绝 → 断言）
- Test: `tests/test_tools.py`（属主：本人可查/他人拒绝）

**Interfaces:** `order_lookup(order_id: str, user_id: Annotated[str, InjectedState("user_id")]) -> dict`；越权返回 `{"error": "data_boundary", ...}`。

- [x] **Step 1: 写失败测试（RED）**：`order_lookup.invoke({"order_id": "ORD-1", "user_id": "other"})` → 边界拒绝；`"u1"` → 返回数据
- [x] **Step 2: 实现属主校验**（`from langgraph.prebuilt import InjectedState`）
- [x] **Step 3: 适配既有测试**（`test_tools.py` 等签名变化）
- [x] **Step 4: eval 0087 unskip**（读路径 deny 断言；scripted 响应含 `order_lookup` tool_call）
- [x] **Step 5: 全量测试 + validate + 提交**

**实施记录（2026-09-15）**：
- ReadGraph 需给 `create_agent` 传 `state_schema=ReadAgentState`（messages + user_id）：实测 create_agent 子图状态默认不含外层字段，ToolNode 的 InjectedState 注入会 KeyError。属主校验仍在工具层，ReadGraph 未增加 precheck。
- 种子新增：`ORD-1`（u1）、`ORD-2`（u2，同事订单，供 0087 跨用户边界断言）。
- eval 基线：routing 90 → **91**（+0087），skip precheck_rule_pending 清零，仅剩 handoff_rule_pending 9。

### Task 5: resume 轮 final 提取（D7）

**Files:**
- Modify: `src/ai_service_agent/harness/streaming.py`（run 前快照 assistant id 集合；run 后 final 空时 diff 取新增）
- Modify: `src/ai_service_agent/harness/server.py`（`_approval_resume`：ainvoke 后 diff + `messages_store.append` 落库）
- Test: `tests/test_streaming.py`、`tests/test_mutation_graph.py`、`tests/test_server.py`、`tests/test_approvals.py`

**Interfaces:** 新增 `harness/streaming.py: snapshot_assistant_ids(graph, config) -> set[str]` 与 `new_assistant_text_and_id(graph, config, before_ids) -> tuple[str|None, str|None]`（复用既有 `_last_assistant_text_and_id`）。

- [x] **Step 1: 写失败测试（RED）**：mutation confirm resume 轮 `done.final == "操作已完成。"`；历史不重放（before id 存在则 None）
- [x] **Step 2: 实现 streaming diff**（`final` 空且 `resume_payload` 时，取末条 assistant，id 不在 before 集合才采用）
- [x] **Step 3: 写 /approvals 落库测试（RED）**：approve 后 Message Store 出现"审批通过，操作已完成。"
- [x] **Step 4: 实现 `_approval_resume` diff + 落库**
- [x] **Step 5: 全量测试 + validate + 提交**

**实施记录（2026-09-15）**：
- 新 helper 均为 async：`snapshot_assistant_ids` / `new_assistant_text_and_id`（复用 aget_state）。
- `event_stream`：resume_payload 存在时 run 前快照；final 空时 diff 取本轮新增（id 不在 before），否则 None。
- `server._approval_resume`：取 approval record 的 `conversation_id`，ainvoke 后 diff → `messages_store.append` 落库（approve/reject 均覆盖，拒绝文案同样落库）。

### Task 6: eval 语义 + Guard 评估报告 + 收尾（D6 + D8）

**Files:**
- Modify: `src/ai_service_agent/evals/runner.py`（新增 `clarify` expected_policy 分支）
- Modify: `tests/evals/control/master.jsonl`（CTRL-0076：`expected_risk_level=high_risk`、`expected_graph=high_risk`、`expected_policy=clarify`，reason 不变）
- Create: `docs/decisions/2026-09-15-p4-guard-llm-eval.md`（Guard LLM 评估报告：触发条件已达成 6/6 绕过；形态/成本/延迟/离线 fake 对齐/与 v1 并行兜底/推荐）
- Modify: `docs/plans/README.md`（P4 计划行）
- Test: `tests/test_evals.py`（clarify 分支 + 0076 断言）

**Interfaces:** runner `evaluate_routing` 新增 `ep == "clarify"`：scripted = router(high_risk) + select(缺参) → 断言 `clarifying` + 无 interrupt + 澄清文案。

- [ ] **Step 1: 写失败测试（RED）**：0076 走 clarify 断言
- [ ] **Step 2: 实现 runner `clarify` 分支 + 数据集语义更新**
- [ ] **Step 3: 写 Guard LLM 评估报告**（纯文档）
- [ ] **Step 4: 全量回归 + validate + eval CLI 基线更新**（预期：routing 计数变化：+0076 clarify、+0086/0087/0089/0090 真实断言）
- [ ] **Step 5: 计划文件全部勾选 + 提交**

---
