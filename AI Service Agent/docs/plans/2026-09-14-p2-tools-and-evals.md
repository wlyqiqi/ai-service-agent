# P2 工具注入 + 离线评估基线 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 ToolRegistry 的 read 档工具真正注入 `make_ai_graph` / ReadGraph，并建立基于 `tests/evals/control/` 的离线确定性 eval runner（policy mapping / handoff trigger / routing wiring），全程不依赖真实模型 API key，输出可回归的评估结果。

**Architecture:** 工具注入走「默认注册表 + 构建期按档过滤」：`tools/__init__.py` 定义 `DEFAULT_TOOL_REGISTRY`（注册 `kb_search` / `order_lookup`，`min_risk_level="read"`），`make_ai_graph(model, checkpointer, registry=None)` 以 `registry.for_risk_level("read")` 取工具传给 `build_graph`（server 两端点调用不变，向后兼容）。评估走独立 `src/ai_service_agent/evals/` 包：`runner.py` 加载 `master.jsonl`（100 cases）跑三个维度（policy 映射 / handoff 触发 / 整图路由接线），每条 case 输出 `pass | fail | skip(reason)`，`skip` 原因固定词表，报告含 settings 快照；`__main__.py` 提供 CLI（`python -m ai_service_agent.evals`）。

**Tech Stack:** Python 3.14；LangGraph 1.2.11 / langchain-core 1.6.3（实测安装版本）；`langchain.agents.create_agent`（禁止 `create_react_agent`）；pytest + 离线 fake model（`ToolCallingFakeModel`）；无新依赖。

**Spec:**
- `docs/specs/tool-spec.md`（§1 ToolRegistry 注入模型 / §4 v1 工具清单）
- `docs/specs/graph-spec.md`（§5 Policy / §6.1 ReadGraph）
- `docs/specs/safety-spec.md`（§2 handoff 触发规则——界定 eval 断言边界）
- `docs/specs/architecture.md`（§8 目录结构）
- `AGENTS.md` §16（eval 纪律：离线、确定性、可重复）

## Global Constraints

- **工具分层注入**：ReadGraph 只拿 `min_risk_level == "read"` 的工具（构建期过滤，非运行期拦截，tool-spec §1）；`make_ai_graph` 是 `/chat` 与 resolve 的**唯一**图构建入口，tools 参数两处必须一致。
- **不提前实现写/高危能力**：P2 不注册 mutation / high_risk 工具、不实现 MutationGraph / HighRiskGraph / Guard / 安全预检 / 扩展 handoff 规则（open questions OQ-3/OQ-4 界定）；注册表与 `for_risk_level` 的 read/mutation/high_risk 分层能力仅保持可扩展。
- **eval 纪律（AGENTS.md §16）**：离线、确定性数据、不依赖真实模型 key；每条 case 状态必须由代码判定（`pass/fail/skip`），不得人工临时判断；报告输出确定性排序（按 `id`）与 settings 快照（`handoff_keywords` / `router_conf_threshold`）。
- **eval 预期数值以默认 settings 为准**：执行 eval 前不得通过 `.env` 覆盖 `handoff_keywords` / `router_conf_threshold`；报告内嵌 settings 快照以便复核。
- **不新增依赖**：evals 只用标准库 + 现有包（langchain_core / langgraph / ai_service_agent）；依赖统一在 `pyproject.toml` 声明，禁止 import 未登记包。
- 运行命令前缀（本机）：`$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy";`，一律 `uv run --no-sync ...`。
- git 根在 `D:\AI_Project`；**在 `D:\AI_Project\AI Service Agent` 目录内执行 git，路径用相对路径**（如 `git add src/... tests/...`），commit 自动带 `AI Service Agent/` 前缀；git 写 `.git` 需 escalation（已批 `git add` / `git commit`）。
- 现有 48 个测试**预期全部保持不变**（实现后全量回归确认；若有行为性破坏，先停下说明再适配）。
- `langgraph validate` 保持通过（`builder.py:app` 默认不动，见 OQ-5）。
- 每个任务结束时跑：该任务新增/涉及测试全绿 + `git commit`。

---

## Open Questions（规划期发现的 spec/实现/数据冲突，执行前需确认）

- **OQ-1（tool-spec §1 ↔ registry.py 实现）**：tool-spec 定义 `ToolMeta.schema: type[BaseModel] | None` 字段，`tools/registry.py` 的 `ToolMeta` 没有该字段。**建议**：Task 1 补齐（默认 `None`，向后兼容，带测试）。若不补：删除 Task 1 的「OQ-1 schema 字段」步骤即可，其余任务不受影响。
- **OQ-2（AGENTS.md §16 "路由准确率" 的离线语义）**：无真实模型 key 时无法评估 LLM 语义分类准确率。**建议**：P2 的 routing 维度 = 确定性图路由接线（给定分类 → 正确子图，fake model 按 case 期望分类驱动），语义准确率留待后续带真实模型/黄金转录的评估（可复用本 loader 增加 `--model` 模式）。
- **OQ-3（handoff.jsonl ↔ 当前 check_handoff）**：实测 10 条 handoff case 中仅 `CTRL-0061` 命中当前关键词规则（`转人工/人工客服/找真人/人工`）；其余 9 条依赖投诉/情绪/连续失败/策略上限/安全等未实现规则（safety-spec §2.1，Guard 相关，P3/P4）。**建议**：P2 不扩展 `check_handoff` 规则，eval 记为 `skip(handoff_rule_pending)`。若要实现这些规则，属扩大范围，需单独确认。
- **OQ-4（policy.jsonl / adversarial.jsonl ↔ 实现）**：`deny`（9 条）与 `guard_reject`（5 条）依赖 Guard 节点与安全预检（graph-spec §2 / safety-spec §1，P3/P4）。**建议**：runner 加载并记为 `skip(guard_pending)`，用于覆盖率统计。
- **OQ-5（builder.py:app 平台入口）**：`graph/builder.py` 的 LangGraph 平台入口 `app = build_graph(make_model(), tools=[])` 仍无工具。**建议**：Task 4 提供可选步骤，把平台入口也接 `DEFAULT_TOOL_REGISTRY` read 档（一行改动，保持平台与 server 行为一致）；若不采纳，平台图在平台运行时无工具（P2 不阻塞）。
- **OQ-6（观察，不在 P2 修）**：整图 invoke 在 handoff 中断时返回 `execution_status="routing"` 而非 `waiting_handoff`（`Command(update)+goto=pause` 的待提交更新在中断时未落 checkpoint；fallback 子图单独 invoke 时才可见 `waiting_handoff`）。属 MVP/P1 既有行为，当前流程无读取方，P2 不修改；handoff 路由断言按实测语义写（见 Task 3）。



### Open Questions 裁决（2026-09-15 用户确认）

- OQ-1 **采纳**：Task 1 补齐 `ToolMeta.schema`（默认 `None`，向后兼容）。
- OQ-2 **采纳**：routing 维度 = 确定性 graph wiring，报告/测试中不称为 LLM semantic accuracy。
- OQ-3 **采纳**：handoff 未实现规则保持 `skip(handoff_rule_pending)`，不扩展 `check_handoff` 规则。
- OQ-4 **采纳**：`deny`/`guard_reject` 保持 `skip(guard_pending)`，P2 不实现 Guard。
- OQ-5 **不采纳**：P2 不修改 `builder.py:app`；**删除 Task 4 原 Step 4（可选接线）**，保持 `langgraph validate` 现状。
- OQ-6 观察项：P2 不修改。
- 一致性修正 1：Task 1 增加「/chat 与 /handoffs/{id}/resolve 共用 make_ai_graph 入口」行为测试（pinning）。
- 一致性修正 2：eval CLI 统一为 `python -m ai_service_agent.evals`（`evals/__main__.py`），不使用 `evals.run` 命名。

---

### Task 1: ToolRegistry 默认注册表 + `make_ai_graph` read 档工具注入

**Files:**
- Modify: `src/ai_service_agent/tools/__init__.py`（空文件 → 默认注册表）
- Modify: `src/ai_service_agent/tools/registry.py`（OQ-1：`ToolMeta` 补 `schema` 字段）
- Modify: `src/ai_service_agent/harness/graph.py`（`make_ai_graph` 接 registry；删除 `AI_GRAPH_TOOLS`）
- Test: `tests/test_tools.py`、`tests/test_harness_graph.py`

**Interfaces:**
- Consumes: `ToolRegistry` / `ToolMeta`（`tools/registry.py`，`for_risk_level(level)` 已实现）、`kb_search`（`tools/kb.py`）、`order_lookup`（`tools/orders.py`）、`build_graph(model, tools, checkpointer)`（`graph/builder.py`）。
- Produces:
  - `ai_service_agent.tools.DEFAULT_TOOL_REGISTRY: ToolRegistry`（注册 `kb_search` / `order_lookup`，`min_risk_level="read"`；**不注册** mutation/high_risk 工具）
  - `make_ai_graph(model, checkpointer, registry=None)`：`registry` 缺省用 `DEFAULT_TOOL_REGISTRY`；`tools = registry.for_risk_level("read")`；向后兼容（既有 `(model, checkpointer)` 调用不变，P1 调用点：`server.py` 两端点 / `test_concurrency.py` / `test_server.py` / `test_harness_graph.py`）。
  - `ToolMeta.schema: type[BaseModel] | None = None`（OQ-1，字段顺序 `name, description, min_risk_level, schema=None, idempotent=False`，既有位置传参不受影响）。

- [ ] **Step 1: 写失败测试**

`tests/test_tools.py` 追加：

```python
from ai_service_agent.tools import DEFAULT_TOOL_REGISTRY


def test_default_registry_read_only():
    read_tools = {t.name for t in DEFAULT_TOOL_REGISTRY.for_risk_level("read")}
    assert read_tools == {"kb_search", "order_lookup"}
    # P2 不注册 mutation / high_risk 工具：仅 read 档（tool-spec.md §4 v1 清单）
    assert {t.name for t in DEFAULT_TOOL_REGISTRY.for_risk_level("mutation")} == read_tools
    assert {t.name for t in DEFAULT_TOOL_REGISTRY.for_risk_level("high_risk")} == read_tools


def test_tool_meta_carries_schema_field():  # OQ-1
    meta = ToolMeta("kb_search", "检索", "read")
    assert meta.schema is None
    meta2 = ToolMeta("order_cancel", "取消", "high_risk", schema=None, idempotent=True)
    assert meta2.idempotent is True
```

`tests/test_harness_graph.py` 追加：

```python
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver

from ai_service_agent.tools import ToolMeta, ToolRegistry
from ai_service_agent.tools.kb import kb_search
from ai_service_agent.testing import ToolCallingFakeModel


def _invoke(graph, text, thread_id):
    return graph.invoke(
        {"messages": [HumanMessage(content=text)], "user_id": "u1", "session_id": "s1", "context": {}},
        {"configurable": {"thread_id": thread_id}},
    )


def test_make_ai_graph_injects_read_tools_by_default():
    model = ToolCallingFakeModel(responses=[
        AIMessage(content='{"capability":"faq","risk_level":"read","confidence":0.9}'),
        AIMessage(content="", tool_calls=[{"name": "kb_search", "args": {"text": "退款"}, "id": "call_1", "type": "tool_call"}]),
        AIMessage(content="查到了。"),
    ])
    out = _invoke(make_ai_graph(model, MemorySaver()), "保修多久", "t1")
    assert out["execution_status"] == "completed"
    tool_msgs = [m for m in out["messages"] if getattr(m, "type", "") == "tool"]
    assert tool_msgs and getattr(tool_msgs[0], "name", "") == "kb_search"
    assert "知识库检索结果" in str(tool_msgs[0].content)


def test_default_registry_order_lookup_injected():
    model = ToolCallingFakeModel(responses=[
        AIMessage(content='{"capability":"faq","risk_level":"read","confidence":0.9}'),
        AIMessage(content="", tool_calls=[{"name": "order_lookup", "args": {"order_id": "ORD-1"}, "id": "call_2", "type": "tool_call"}]),
        AIMessage(content="完成。"),
    ])
    out = _invoke(make_ai_graph(model, MemorySaver()), "查订单", "t2")
    tool_msgs = [m for m in out["messages"] if getattr(m, "type", "") == "tool"]
    assert tool_msgs and "已发货" in str(tool_msgs[0].content)


def test_make_ai_graph_respects_registry_read_tier():
    """注入的注册表决定工具集：只注册 kb_search 时，order_lookup 不在 read 档 -> ToolNode 未知工具。"""
    reg = ToolRegistry()
    reg.register(ToolMeta("kb_search", "检索", "read"), kb_search)
    model = ToolCallingFakeModel(responses=[
        AIMessage(content='{"capability":"faq","risk_level":"read","confidence":0.9}'),
        AIMessage(content="", tool_calls=[{"name": "order_lookup", "args": {"order_id": "ORD-1"}, "id": "call_3", "type": "tool_call"}]),
        AIMessage(content="完成。"),
    ])
    out = _invoke(make_ai_graph(model, MemorySaver(), registry=reg), "查订单", "t3")
    tool_msgs = [m for m in out["messages"] if getattr(m, "type", "") == "tool"]
    assert tool_msgs and getattr(tool_msgs[0], "name", "") == "order_lookup"
    assert str(tool_msgs[0].content).startswith("Error:")  # 未注入 -> 未知工具错误 ToolMessage
```


`tests/test_server.py` 追加（一致性修正 1：/chat 与 resolve 必须共用同一 `make_ai_graph` 入口）：

```python
def test_chat_and_resolve_share_make_ai_graph_entry(tmp_path, monkeypatch):
    """/chat 与 /handoffs/{id}/resolve 都必须经同一个 make_ai_graph 入口（P2 一致性要求）。"""
    from ai_service_agent.harness import server as server_mod
    from ai_service_agent.harness.graph import make_ai_graph as _real_make_ai_graph

    calls = []

    def spy(model, checkpointer, registry=None):
        calls.append((model, checkpointer))
        return _real_make_ai_graph(model, checkpointer, registry=registry)

    monkeypatch.setattr(server_mod, "make_ai_graph", spy)
    client = TestClient(create_app(str(tmp_path / "g.db"), model="fake"))
    session_id = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]

    with client.stream("POST", "/chat", json={"session_id": session_id, "message": "hi"}) as r:
        "".join(r.iter_text())
    assert len(calls) == 1  # /chat ai 路径经 make_ai_graph

    conn = sqlite3.connect(str(tmp_path / "g.db"))
    status = ConversationStatusStore(conn)
    rec = HandoffService(conn, status).create_handoff(session_id, "r", "s")
    conn.close()
    r = client.post(f"/handoffs/{rec['handoff_id']}/resolve")
    assert r.status_code == 200
    assert len(calls) == 2  # resolve 路径也走同一入口
    assert all(model is not None and checkpointer is not None for model, checkpointer in calls)
```

> 注：本测试为 pinning 测试（实现前即应 PASS，防未来某端点改用 `build_graph` 直连）；Task 1 的 RED 由 `DEFAULT_TOOL_REGISTRY` 缺失 / 空工具注入的两个 FAIL 承担。
- [ ] **Step 2: 跑测试确认失败**

Run: `$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest tests/test_tools.py tests/test_harness_graph.py tests/test_server.py -q`
Expected: FAIL（`DEFAULT_TOOL_REGISTRY` 不存在 → ImportError；`make_ai_graph` 注入空工具 → tool 结果为未知工具 Error）。

- [ ] **Step 3: 实现**

`src/ai_service_agent/tools/__init__.py`（整文件替换为空内容）：

```python
from ai_service_agent.tools.kb import kb_search
from ai_service_agent.tools.orders import order_lookup
from ai_service_agent.tools.registry import ToolMeta, ToolRegistry

# 默认注册表：MVP/P2 只注册 read 档只读工具（tool-spec.md §4 v1 清单）；
# mutation / high_risk 工具在 P3/P4 子图落地时按档注册。
DEFAULT_TOOL_REGISTRY = ToolRegistry()
DEFAULT_TOOL_REGISTRY.register(ToolMeta("kb_search", "知识库检索", "read"), kb_search)
DEFAULT_TOOL_REGISTRY.register(ToolMeta("order_lookup", "订单查询", "read"), order_lookup)
```

`src/ai_service_agent/tools/registry.py`（OQ-1：顶部加 import 与字段）：

```python
from pydantic import BaseModel
```
并把 `ToolMeta` 改为：

```python
@dataclass
class ToolMeta:
    name: str
    description: str
    min_risk_level: str
    schema: type[BaseModel] | None = None
    idempotent: bool = False
```

`src/ai_service_agent/harness/graph.py`（整文件替换；删除 `AI_GRAPH_TOOLS`）：

```python
from ai_service_agent.graph.builder import build_graph
from ai_service_agent.tools import DEFAULT_TOOL_REGISTRY


def make_ai_graph(model, checkpointer, registry=None):
    """/chat 与 /handoffs/{id}/resolve 共用的图构建入口。

    两处必须调用本函数，保证 build_graph 参数（model / tools / checkpointer）完全一致。
    tools 按 read 档从 ToolRegistry 注入（tool-spec.md §1）：ReadGraph 只拿到
    min_risk_level == "read" 的工具；mutation / high_risk 档在 P3/P4 按需扩展。
    """
    reg = registry if registry is not None else DEFAULT_TOOL_REGISTRY
    tools = reg.for_risk_level("read")
    return build_graph(model, tools=tools, checkpointer=checkpointer)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest tests/test_tools.py tests/test_harness_graph.py tests/test_server.py -q`
Expected: PASS（test_tools 既有 2 + 新增 2；test_harness_graph 既有 3 + 新增 3；test_server 既有 7 + pinning 新增 1）。

- [ ] **Step 5: Commit**

```bash
git add src/ai_service_agent/tools/__init__.py src/ai_service_agent/tools/registry.py src/ai_service_agent/harness/graph.py tests/test_tools.py tests/test_harness_graph.py tests/test_server.py
git commit -m "feat: ToolRegistry 默认注册表 + make_ai_graph read 档工具注入（tool-spec §1/§4）"
```

### Task 2: evals 包骨架 + policy mapping / handoff trigger 维度

**Files:**
- Create: `src/ai_service_agent/evals/__init__.py`、`src/ai_service_agent/evals/runner.py`
- Test: `tests/test_evals.py`

**Interfaces:**
- Consumes: `builder.POLICY`（`graph/builder.py`）、`check_handoff`（`graph/workflows/fallback.py`，返回 `dict | Command`）、`get_settings()`（`config.py`）。
- Produces:
  - `runner.load_cases(path) -> list[dict]`（jsonl → dict；必填键 `id/input/expected_capability/expected_risk_level/expected_graph/expected_policy`，缺失抛 `ValueError`）
  - `runner.SKIP_REASONS: tuple` = `("mutation_pending", "high_risk_pending", "guard_pending", "handoff_rule_pending", "not_applicable")`
  - `runner.policy_target(capability, risk_level) -> str`（归一化：`read_graph -> "read"`，其余按 POLICY 返回值）
  - `runner.evaluate_policy(cases) -> list[dict]`（每项 `{"id", "status", "skip_reason"?, "actual"?}`）
  - `runner.evaluate_handoff(cases) -> list[dict]`（调 `check_handoff`；`Command` 分支读 `update["handoff"]`）
  - `runner.summarize(results) -> dict`（`{total, pass, fail, skip: {reason: count}}`）
  - `runner.run_all(control_dir) -> dict`（`{dataset, case_count, dimensions: {policy, handoff}, settings}`；routing 维度 Task 3 加入）
- Task 3 消费：`runner.evaluate_*` / `runner.run_all`。

- [ ] **Step 1: 写失败测试（`tests/test_evals.py`）**

```python
import json
from pathlib import Path

from ai_service_agent.evals import runner

BASE = {"expected_policy": "allow", "requires_human": False, "reason": "", "metadata": {"category": "read", "risk": "low"}}
CASE_READ = {"id": "T-01", "input": "查订单 ORD-1", "expected_capability": "order", "expected_risk_level": "read", "expected_graph": "read", **BASE}
CASE_MUT = {"id": "T-02", "input": "改收货地址", "expected_capability": "order", "expected_risk_level": "mutation", "expected_graph": "mutation", **BASE}
CASE_HANDOFF = {"id": "T-03", "input": "麻烦帮我转人工客服", "expected_capability": "unknown", "expected_risk_level": "read", "expected_graph": "handoff", **BASE}
CASE_HANDOFF_PENDING = {"id": "T-04", "input": "气死了！垃圾！", "expected_capability": "unknown", "expected_risk_level": "read", "expected_graph": "handoff", **BASE}


def _write(tmp_path, cases):
    p = tmp_path / "master.jsonl"
    p.write_text("\n".join(json.dumps(c, ensure_ascii=False) for c in cases), encoding="utf-8")
    return tmp_path


def test_load_cases_rejects_missing_keys(tmp_path):
    bad = {"id": "X", "input": "hi"}
    d = _write(tmp_path, [bad])
    try:
        runner.load_cases(d / "master.jsonl")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_policy_dimension_pass_and_skip(tmp_path):
    d = _write(tmp_path, [CASE_READ, CASE_MUT])
    res = runner.evaluate_policy(runner.load_cases(d / "master.jsonl"))
    assert res[0]["id"] == "T-01" and res[0]["status"] == "pass"
    assert res[1]["id"] == "T-02" and res[1]["status"] == "skip"
    assert res[1]["skip_reason"] == "mutation_pending"


def test_handoff_dimension_hit_miss_and_no_false_trigger(tmp_path):
    d = _write(tmp_path, [CASE_READ, CASE_HANDOFF, CASE_HANDOFF_PENDING])
    res = runner.evaluate_handoff(runner.load_cases(d / "master.jsonl"))
    by_id = {r["id"]: r for r in res}
    assert by_id["T-01"]["status"] == "pass"        # 非 handoff case 不得误触发
    assert by_id["T-03"]["status"] == "pass"        # 关键词命中 -> 触发
    assert by_id["T-04"]["status"] == "skip"        # 规则未实现（Guard/情绪，OQ-3）
    assert by_id["T-04"]["skip_reason"] == "handoff_rule_pending"


def test_summarize_and_determinism(tmp_path):
    d = _write(tmp_path, [CASE_READ, CASE_MUT])
    r1 = runner.summarize(runner.evaluate_policy(runner.load_cases(d / "master.jsonl")))
    r2 = runner.summarize(runner.evaluate_policy(runner.load_cases(d / "master.jsonl")))
    assert r1 == r2
    assert r1["total"] == 2 and r1["pass"] == 1 and r1["fail"] == 0
    assert r1["skip"] == {"mutation_pending": 1}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest tests/test_evals.py -q`
Expected: FAIL（`ai_service_agent.evals` 模块不存在 → ImportError）。

- [ ] **Step 3: 实现**

`src/ai_service_agent/evals/__init__.py`：

```python
"""离线确定性评估（AGENTS.md §16）：基于 tests/evals/control/ 数据集，不依赖真实模型。"""
```

`src/ai_service_agent/evals/runner.py`：

```python
import json
from pathlib import Path

from langchain_core.messages import HumanMessage
from langgraph.types import Command

from ai_service_agent.config import get_settings
from ai_service_agent.graph.builder import POLICY
from ai_service_agent.graph.workflows.fallback import check_handoff

# skip 原因固定词表：mutation/high_risk/guard 为 P3/P4 未实现能力；handoff_rule_pending 为
# safety-spec §2.1 中未实现规则（投诉/情绪/连续失败/安全等，OQ-3）；not_applicable 为本维度不适用。
SKIP_REASONS = ("mutation_pending", "high_risk_pending", "guard_pending", "handoff_rule_pending", "not_applicable")

GRAPH_POLICY_SKIP = {"mutation": "mutation_pending", "high_risk": "high_risk_pending", "blocked": "guard_pending"}
HANDOFF_SKIP = "handoff_rule_pending"


def load_cases(path: str | Path) -> list[dict]:
    required = {"id", "input", "expected_capability", "expected_risk_level", "expected_graph", "expected_policy"}
    cases = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            case = json.loads(line)
            missing = required - case.keys()
            if missing:
                raise ValueError(f"{Path(path).name}: {case.get('id')} missing {sorted(missing)}")
            cases.append(case)
    return cases


def policy_target(capability: str, risk_level: str) -> str:
    """确定性 Policy 归一化（graph-spec.md §5）：read_graph -> read。"""
    target = POLICY.get((capability, risk_level), "fallback")
    return "read" if target == "read_graph" else target


def _handoff_triggered(node_out) -> bool:
    if isinstance(node_out, Command):
        return bool(node_out.update.get("handoff"))
    return bool(node_out.get("handoff"))


def evaluate_policy(cases: list[dict]) -> list[dict]:
    results = []
    for case in cases:
        expected = case["expected_graph"]
        if expected in GRAPH_POLICY_SKIP:
            results.append({"id": case["id"], "status": "skip", "skip_reason": GRAPH_POLICY_SKIP[expected]})
            continue
        if expected == "handoff":  # blocked 已在上文按 guard_pending 处理
            results.append({"id": case["id"], "status": "skip", "skip_reason": "not_applicable"})
            continue
        actual = policy_target(case["expected_capability"], case["expected_risk_level"])
        results.append({"id": case["id"], "status": "pass" if actual == expected else "fail", "actual": actual})
    return results


def evaluate_handoff(cases: list[dict]) -> list[dict]:
    results = []
    for case in cases:
        node_out = check_handoff({"messages": [HumanMessage(content=case["input"])]})
        triggered = _handoff_triggered(node_out)
        if case["expected_graph"] == "handoff":
            if triggered:
                results.append({"id": case["id"], "status": "pass"})
            else:
                # 未命中当前关键词规则：属 safety-spec §2.1 未实现规则（OQ-3）
                results.append({"id": case["id"], "status": "skip", "skip_reason": HANDOFF_SKIP})
        else:
            results.append({"id": case["id"], "status": "pass" if not triggered else "fail"})
    return results


def summarize(results: list[dict]) -> dict:
    summary = {"total": len(results), "pass": 0, "fail": 0, "skip": {}}
    for r in results:
        if r["status"] == "pass":
            summary["pass"] += 1
        elif r["status"] == "fail":
            summary["fail"] += 1
        else:
            reason = r.get("skip_reason", "unknown")
            summary["skip"][reason] = summary["skip"].get(reason, 0) + 1
    return summary


def run_all(control_dir: str | Path) -> dict:
    control_dir = Path(control_dir)
    cases = load_cases(control_dir / "master.jsonl")
    dimensions = {
        "policy": {"results": evaluate_policy(cases), "summary": None},
        "handoff": {"results": evaluate_handoff(cases), "summary": None},
    }
    for dim in dimensions.values():
        dim["summary"] = summarize(dim["results"])
    return {
        "dataset": str(control_dir / "master.jsonl"),
        "case_count": len(cases),
        "dimensions": dimensions,
        "settings": {
            "handoff_keywords": get_settings().handoff_keywords,
            "router_conf_threshold": get_settings().router_conf_threshold,
        },
    }
```

- [ ] **Step 4: 跑测试确认通过**

Run: `$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest tests/test_evals.py -q`
Expected: PASS（4 个新测试）。

- [ ] **Step 5: Commit**

```bash
git add src/ai_service_agent/evals/__init__.py src/ai_service_agent/evals/runner.py tests/test_evals.py
git commit -m "feat: evals runner——policy mapping + handoff trigger 离线确定性维度"
```

### Task 3: evals routing 维度（整图接线）+ CLI

**Files:**
- Modify: `src/ai_service_agent/evals/runner.py`（加 `evaluate_routing`，扩展 `run_all`）
- Create: `src/ai_service_agent/evals/__main__.py`（CLI）
- Modify: `docs/specs/architecture.md`（§8 目录树加 `evals/` 一行）
- Test: `tests/test_evals.py`（追加）

**Interfaces:**
- Consumes: Task 2 的 `load_cases` / `summarize` / `run_all`；`make_ai_graph`（Task 1，read 档注入）；`ToolCallingFakeModel`（`testing.py`）。
- Produces:
  - `runner.evaluate_routing(cases) -> list[dict]`：整图 fake 驱动（OQ-2），断言子图选择；skip 原因沿用 Task 2 词表。
  - `runner.run_all(control_dir)` 增加 `dimensions.routing`。
  - `python -m ai_service_agent.evals [--data DIR] [--out FILE]`：输出/落盘 JSON 报告。
- 语义（实测锚点，见 Global Constraints 的 OQ-6）：整图 invoke 在 handoff 中断时返回 `execution_status="routing"` + `__interrupt__`（含 `handoff_request`），且图内无 AI 文案；read/fallback 返回 `execution_status="completed"`。

- [ ] **Step 1: 写失败测试（追加到 `tests/test_evals.py`）**

```python
CASE_FALLBACK = {"id": "T-05", "input": "随便聊聊", "expected_capability": "unknown", "expected_risk_level": "read", "expected_graph": "fallback", **BASE}


def test_routing_dimension_fixtures(tmp_path):
    d = _write(tmp_path, [CASE_READ, CASE_FALLBACK, CASE_HANDOFF, CASE_HANDOFF_PENDING])
    res = runner.evaluate_routing(runner.load_cases(d / "master.jsonl"))
    by_id = {r["id"]: r for r in res}
    assert by_id["T-01"]["status"] == "pass"            # read -> ReadGraph 完成
    assert by_id["T-05"]["status"] == "pass"            # fallback -> explain 完成
    assert by_id["T-03"]["status"] == "pass"            # handoff 关键词命中 -> interrupt
    assert by_id["T-04"]["status"] == "skip"
    assert by_id["T-04"]["skip_reason"] == "handoff_rule_pending"


def test_run_all_includes_three_dimensions(tmp_path):
    d = _write(tmp_path, [CASE_READ, CASE_MUT])
    report = runner.run_all(d)
    assert set(report["dimensions"].keys()) == {"policy", "handoff", "routing"}
    assert report["case_count"] == 2
    assert report["dimensions"]["routing"]["summary"]["pass"] == 1
    assert report["dimensions"]["routing"]["summary"]["skip"] == {"mutation_pending": 1}
    assert report["settings"]["handoff_keywords"]  # settings 快照存在


def test_cli_runs_on_real_control_dataset(tmp_path):
    import os
    import subprocess
    import sys

    repo = Path(__file__).resolve().parents[1]
    out = tmp_path / "report.json"
    env = {**os.environ, "PYTHONPATH": str(repo / "src")}
    proc = subprocess.run(
        [
            sys.executable, "-m", "ai_service_agent.evals",
            "--data", str(repo / "tests" / "evals" / "control"),
            "--out", str(out),
        ],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["case_count"] == 100
    assert report["dimensions"]["policy"]["summary"]["pass"] == 33
```

- [ ] **Step 2: 跑测试确认失败**

Run: `$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest tests/test_evals.py -q`
Expected: FAIL（`evaluate_routing` 不存在 / `run_all` 无 routing 维度 / CLI 模块不存在）。

- [ ] **Step 3: 实现**

`src/ai_service_agent/evals/runner.py` 追加（顶部 import 增加 `json` 已有、`AIMessage`/`HumanMessage`、`MemorySaver`、`make_ai_graph`、`ToolCallingFakeModel`）：

```python
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver

from ai_service_agent.harness.graph import make_ai_graph
from ai_service_agent.testing import ToolCallingFakeModel

GRAPH_ROUTING_SKIP = {"mutation": "mutation_pending", "high_risk": "high_risk_pending", "blocked": "guard_pending"}
READ_MARKER = "READ-OK"
FALLBACK_EXPLAIN = "我暂时无法处理，请换个说法或联系人工客服。"
```

函数体：

```python
def _router_payload(case: dict) -> dict:
    """离线确定性驱动（OQ-2）：由 case 期望分类构造 Router 输出，不调真实 LLM。"""
    if case["expected_graph"] in ("handoff", "fallback"):
        return {"capability": "unknown", "risk_level": "read", "confidence": 0.2}
    return {"capability": case["expected_capability"], "risk_level": case["expected_risk_level"], "confidence": 0.9}


def evaluate_routing(cases: list[dict]) -> list[dict]:
    results = []
    for case in cases:
        expected = case["expected_graph"]
        if expected in GRAPH_ROUTING_SKIP:
            results.append({"id": case["id"], "status": "skip", "skip_reason": GRAPH_ROUTING_SKIP[expected]})
            continue
        if expected == "handoff" and not any(k in case["input"] for k in get_settings().handoff_keywords):
            results.append({"id": case["id"], "status": "skip", "skip_reason": HANDOFF_SKIP})
            continue
        payload = _router_payload(case)
        model = ToolCallingFakeModel(responses=[
            AIMessage(content=json.dumps(payload, ensure_ascii=False)),
            AIMessage(content=READ_MARKER),
        ])
        graph = make_ai_graph(model, MemorySaver())
        out = graph.invoke(
            {"messages": [HumanMessage(content=case["input"])], "user_id": "u1", "session_id": "s1", "context": {}},
            {"configurable": {"thread_id": f"eval-{case['id']}"}},
        )
        last = out.get("messages", [])[-1] if out.get("messages") else None
        last_text = getattr(last, "content", "") if last is not None else ""
        if expected == "read":
            ok = out.get("execution_status") == "completed" and last_text == READ_MARKER
        elif expected == "fallback":
            ok = out.get("execution_status") == "completed" and last_text == FALLBACK_EXPLAIN
        else:  # handoff（关键词已命中）
            interrupts = out.get("__interrupt__", ())
            ok = any(getattr(it, "value", {}).get("type") == "handoff_request" for it in interrupts)
            ok = ok and not any(getattr(m, "type", "") == "ai" for m in out.get("messages", []))
        results.append({"id": case["id"], "status": "pass" if ok else "fail", "actual": expected})
    return results
```

`run_all` 的 `dimensions` 字典加入 routing：

```python
        "routing": {"results": evaluate_routing(cases), "summary": None},
```

`src/ai_service_agent/evals/__main__.py`：

```python
import argparse
import json
from pathlib import Path

from ai_service_agent.evals import runner


def main() -> None:
    parser = argparse.ArgumentParser(description="P2 离线确定性评估（AGENTS.md §16）")
    parser.add_argument("--data", default="tests/evals/control", help="control 数据集目录（含 master.jsonl）")
    parser.add_argument("--out", default=None, help="JSON 报告输出路径（缺省打印 stdout）")
    args = parser.parse_args()
    report = runner.run_all(args.data)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        print(text)


if __name__ == "__main__":
    main()
```

`docs/specs/architecture.md` §8 目录树，在 `harness/` 行后加一行：

```text
    evals/                  # offline eval runner（AGENTS.md §16）
```

- [ ] **Step 4: 跑测试确认通过**

Run: `$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest tests/test_evals.py -q`
Expected: PASS（既有 4 + 新增 3 = 7）。

- [ ] **Step 5: Commit**

```bash
git add src/ai_service_agent/evals/runner.py src/ai_service_agent/evals/__main__.py tests/test_evals.py docs/specs/architecture.md
git commit -m "feat: evals routing 维度（整图接线）+ CLI（python -m ai_service_agent.evals）"
```

### Task 4: 全量评估 + 全量回归 + validate + 收尾

**Files:**
- Modify: 无（如回归发现破坏，回到对应 Task 修复）
- Verify: 全量 pytest、`python -m ai_service_agent.evals` 全量报告、`langgraph validate`

- [ ] **Step 1: 全量评估（真实数据集）**

Run: `$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync python -m ai_service_agent.evals --data tests/evals/control --out "$env:TEMP\p2-eval-report.json"`
Expected（默认 settings 下的确定性汇总；先核对 JSON 中 `settings.handoff_keywords == ["转人工", "人工客服", "找真人", "人工"]`、`router_conf_threshold == 0.6`）：

| 维度 | total | pass | fail | skip 明细 |
|---|---|---|---|---|
| policy | 100 | 33 | 0 | `mutation_pending: 23, high_risk_pending: 20, guard_pending: 14, not_applicable: 10` |
| handoff | 100 | 91 | 0 | `handoff_rule_pending: 9` |
| routing | 100 | 34 | 0 | `mutation_pending: 23, high_risk_pending: 20, guard_pending: 14, handoff_rule_pending: 9` |

> 说明：routing 维度对 read/fallback/handoff 命中 case 整图编译运行（约 34 次 invoke，耗时 30–60s 属正常）。数值依据：read 23 + fallback 10 + handoff 命中 1 = 34 pass；mutation 23 / high_risk 20 / blocked 14 / handoff 未命中 9 按词表 skip。若 settings 非默认（.env 覆盖），数值会变——先确认未覆盖，或按报告中 `settings` 快照人工复核。

- [ ] **Step 2: 全量测试**

Run: `$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest -q`
Expected: 全绿（48 个既有 + 新增 10 = 58）。

- [ ] **Step 3: langgraph validate**

Run: `langgraph validate`
Expected: `Configuration file ... is valid. (1 graph found)`。

- [ ] **Step 4: 收尾提交（如有修复）**

```bash
git add -A src tests docs
git commit -m "chore: P2 收尾（如有）"
```

- [ ] **Step 5: 更新计划勾选与验收汇报**

- 逐任务勾选本文件 checkbox。
- 汇报：commit 列表、新增测试数、48 个既有测试保持不变的回归结果、eval 全量汇总表、OQ-1–OQ-6 的裁定结果。

## Self-Review（writing-plans 强制）

**1. Spec 覆盖：**
- tool-spec §1（按档注入）→ Task 1；§4（v1 清单 kb_search/order_lookup read）→ Task 1 默认注册表。
- graph-spec §5（Policy 确定性映射）→ Task 2 `policy_target`；§6.1（ReadGraph 只读工具）→ Task 1。
- safety-spec §2（handoff 规则边界）→ Task 2/3 的 `skip(handoff_rule_pending)` 与 OQ-3。
- AGENTS.md §16（离线确定性可重复评估）→ Task 2/3 runner + Task 4 全量报告；「安全预检误拒率」依赖 P3/P4 预检实现，P2 不覆盖（OQ-4 说明）。
- 用户范围 1（Tool Injection）→ Task 1；范围 2（Offline Eval Baseline）→ Task 2/3；范围 3（Regression）→ Task 4。
- 保持 read/mutation/high_risk 分层扩展 → Task 1 使用 `for_risk_level("read")`，注册表保留三档能力。
- 不提前实现 Mutation/HighRisk 业务能力 → 无对应任务；`mutation/high_risk` 数据集 case 一律 skip。
- 不引入真实外部系统依赖 → 无新依赖、stub 工具不变。

**2. Placeholder 扫描：** 无 TBD/TODO；每步含完整代码与运行命令；预期数值（33/91/34）由 Step 1 给出并与数据集分布核对（read 23 + fallback 10 + handoff 命中 1）。

**3. 类型一致：**
- `make_ai_graph(model, checkpointer, registry=None)`：Task 1 定义、Task 3 `evaluate_routing` 以 `(model, MemorySaver())` 调用（registry 缺省）一致。
- `run_all(control_dir)` 返回 `dimensions: {policy, handoff, routing}`：Task 2 定义两维、Task 3 扩展三维；测试 `test_run_all_includes_three_dimensions` 一致。
- `evaluate_*` 均返回 `list[{"id", "status", "skip_reason"?...}]`：Task 2/3 测试断言一致。
- `skip_reason` 词表 `SKIP_REASONS` 全计划一致（`mutation_pending/high_risk_pending/guard_pending/handoff_rule_pending/not_applicable`）。
- `ToolMeta(name, description, min_risk_level, schema=None, idempotent=False)`：Task 1 实现与 OQ-1 测试一致；既有位置传参（3 参）兼容。
- `check_handoff` 返回 `dict | Command`：Task 2 `_handoff_triggered` 双分支处理与实现一致。
- 整图 handoff 中断断言按实测语义（`__interrupt__` + 无 AI 文案），不用 `execution_status == "waiting_handoff"`（OQ-6 实测，Task 3 一致）。







