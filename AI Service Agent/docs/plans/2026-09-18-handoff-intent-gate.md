# P0 转人工意图识别加固——入口层显式 Handoff 检测 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复「用户明确要求转人工时被 Router 错分、永远进不了人工客服」的问题：在 `/chat` 入口、进入 LangGraph Router 之前，用确定性 detector 识别显式转人工意图，命中则直接进入 Human Lane（不触发 Router/Guard/Graph），与图内动态 handoff 共用同一规则与同一 side-effect 路径。

**Architecture:** `conversation_mode` 判断与 AI/Human 路由本就是入口层职责（AGENTS.md §4 例外 + `handoff-spec.md` §2）。本次把 safety-spec §2.1 第一类触发源「显式请求」从图内 `fallback.check_handoff` 提升为入口层前置判定：新建纯函数 `detect_handoff_intent(text)`（normalize → phrase → pattern，无 LLM），`server.py /chat` 在 `mode==human` 判断之后、`ai_stream` 之前调用；命中走统一 `HandoffService.request_handoff()` + `_emit_handoff()`（落 Message Store + Chatwoot ensure + 事件）。图内 `check_handoff` 保留为动态信号（工具失败/重复未解决/情绪/审批被拒坚持）与兜底，与入口层共享同一 detector。

**Tech Stack:** Python 3.14；LangGraph 1.2.10（固定）；FastAPI（Transport）；pytest + TestClient；httpx（Chatwoot）。无新外部依赖。

**Spec:** `docs/specs/safety-spec.md`（§2.1 显式请求触发源 / §2.2 流程）· `docs/specs/handoff-spec.md`（§2 入口层判断 / §1.2 原子切 mode / §3 外层接管）· `docs/specs/graph-spec.md`（§6.4 check_handoff / §6.5 interrupt）· `docs/specs/event-spec.md`（handoff_created）。

## Global Constraints

- LangGraph 1.2.10 固定；禁止 `langgraph.prebuilt.create_react_agent`；禁止新增/修改 Router schema（capability/risk_level/confidence）、Policy Gate、Guard、ReadGraph/MutationGraph/HighRiskGraph、interrupt/resume/checkpointer、`conversation_mode`、Message Store、前端页面 contract、Chatwoot 链路。
- FastAPI 只做 Transport；业务编排留在图内；`conversation_mode` 轻量判断在入口层（handoff-spec §2）；`Message Store` 是唯一真相源（handoff-spec §5.2）。
- 转人工信号只来自用户输入（safety-spec §2，不扫描 AI 回复）；命中后事件顺序必须保持 `token(HANDOFF_NOTICE) → handoff_created → done`（event-spec / 页面 contract 不变）。
- 外部调用失败不得影响主链路：Chatwoot ensure/send 异常一律 try/except + 日志。
- `detect_handoff_intent` 必须确定性（无 LLM、无外部服务、输入相同结果相同），严禁裸 `"人工"` 子串判断。
- 测试命令前缀：`$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest ...`
- 提交在项目目录 `D:\AI_Project\AI Service Agent` 执行，git 根 `D:\AI_Project`；`git add` 只加明确列出的文件，禁止 `git add -A` / `git add .`。沙箱拦截 git 写：commit 命令交用户执行，agent 不自行 commit。

## Contract Decisions（本计划裁定）

- **D1（入口层前置）**：`server.py /chat` 在 `mode==human` 判断之后、`ai_stream` 之前执行 `detect_handoff_intent(body.message)`；命中 → 不进入 Router/Guard/Graph，直接走统一 handoff side-effect path。未命中 → 保持现有 AI Lane 完整流程不变。**不在 Router prompt 中解决。**
- **D2（唯一 detector）**：新建 `detect_handoff_intent(text) -> bool`（normalize → explicit phrase → intent pattern，排除话题性后缀）。入口层与图内 `check_handoff` 共用；detector 只负责「显式请求」，动态信号继续由图内 `check_handoff` 负责（当前未实现，保留扩展位）。
- **D3（统一 side-effect path）**：`HandoffService.get_active()` + `HandoffService.request_handoff()`（幂等复用 active record → create_handoff → 返回 `handoff_created` 事件）；`server.py` 抽 `_emit_handoff()`（落 `HANDOFF_NOTICE` + `chatwoot.ensure_conversation` 失败只记日志 + 发 `token(HANDOFF_NOTICE)` + `handoff_created`）。图内动态路径（interrupt callback 建 record 后）与入口显式路径都走 `_emit_handoff()`。
- **D4（配置改造）**：`config.py` 用 `handoff_phrases`（精确短语）+ `handoff_negative_suffix`（话题性后缀）**替换** `handoff_keywords`（裸 `"人工"`/`"人工客服"` 子串会误伤「人工呼吸」「人工客服培训」）。同步改 `fallback.py` / `evals/runner.py` / `tests/test_config.py` / `tests/test_evals.py` 引用。
- **D5（C3 补：入口层命中先落 user 消息）**：入口层命中后先 `messages_store.append(conv_id, "user", body.message)`（Message Store 唯一真相源），再走 `request_handoff` / `_emit_handoff`，保证 E2E 断言的消息顺序 `["我要转人工", HANDOFF_NOTICE, ...]` 不变。
- **D6（C4 补：pending interrupt 优先转人工）**：入口层检测在 `ai_stream` 的 `aget_state`/resume 逻辑之前，因此存在挂起审批/确认中断时用户显式转人工 → 直接转人工，pending 中断继续挂起（handoff-spec §1.3 定案，AI 重新接管后自然恢复）。
  > **Superseded by [ADR-0002](../adr/0002-seat-approver-side-channel.md)**（「继续挂起 / 等 AI 接管」归宿段）；D6 的入口层优先转人工时序本身未废止。
- **D7（eval 适配）**：`handoff.jsonl` 显式请求（CTRL-0061/0062）在 handoff/routing 维度不再 skip；动态信号 9 条（CTRL-0063~0070）保持 `handoff_rule_pending` skip（D13 不变）。`test_cli_runs_on_real_control_dataset` 的 routing 计数随之更新：pass 91→92，skip `{"handoff_rule_pending": 9}`→`{"handoff_rule_pending": 8}`。

## File Structure

- Create: `src/ai_service_agent/graph/handoff_rules.py`（`detect_handoff_intent` 纯函数）
- Modify: `src/ai_service_agent/config.py`（`handoff_phrases` / `handoff_negative_suffix` 替换 `handoff_keywords`）
- Modify: `src/ai_service_agent/graph/workflows/fallback.py`（`check_handoff` 复用 detector）
- Modify: `src/ai_service_agent/evals/runner.py`（`evaluate_handoff` / `evaluate_routing` 用 detector；settings 快照字段）
- Modify: `src/ai_service_agent/harness/handoff.py`（`get_active` / `request_handoff`）
- Modify: `src/ai_service_agent/harness/server.py`（`/chat` 入口接入 + `_emit_handoff` 抽取）
- Create: `tests/test_handoff_detect.py`（detector True/False 矩阵）
- Modify: `tests/test_config.py`、`tests/test_evals.py`、`tests/test_handoff.py`、`tests/test_e2e.py`（注释）、`tests/test_e2e_human_lane.py`（docstring）、`tests/test_handoffs_query.py`（docstring）
- Create: `tests/test_handoff_entrance.py`（入口 E2E：不进图 / 事件顺序 / duplicate / pending interrupt 优先）
- Modify: `tests/evals/control/handoff.jsonl`（CTRL-0062 reason 更新）、`tests/evals/control/dataset-audit.md`（如需）
- Modify: `scripts/real_human_lane_smoke.py`（真机显式转人工双用例）、`docs/specs/handoff-spec.md`、`docs/specs/safety-spec.md`、`docs/real-model-smoke.md`、`docs/plans/README.md`

任务分解（每任务独立可测、独立 commit）：

| Task | 内容 | 可测产物 |
|---|---|---|
| 1 | detector 纯函数 + config 改造 | `tests/test_handoff_detect.py` + `tests/test_config.py` |
| 2 | fallback + eval runner 复用 detector | `tests/test_fallback.py` + `tests/test_evals.py` + handoff eval |
| 3 | HandoffService.get_active / request_handoff | `tests/test_handoff.py` |
| 4 | server `/chat` 入口接入 + `_emit_handoff` | `tests/test_handoff_entrance.py` |
| 5 | 全量回归 + validate + 真机 smoke + 文档 | 全量 pytest + `langgraph validate` + 文档 |

---

### Task 1: 确定性 detector + config 改造

**Files:**
- Create: `src/ai_service_agent/graph/handoff_rules.py`
- Modify: `src/ai_service_agent/config.py`
- Create: `tests/test_handoff_detect.py`
- Modify: `tests/test_config.py`

**Interfaces:**
- Produces: `detect_handoff_intent(text: str) -> bool`（确定性；输入相同结果恒定；无 LLM / 无外部调用）。
- Produces（config）: `Settings.handoff_phrases: list[str]`、`Settings.handoff_negative_suffix: list[str]`（**删除** `Settings.handoff_keywords`）。

- [ ] **Step 1: 写失败测试**（`tests/test_handoff_detect.py` + `tests/test_config.py`）

```python
# tests/test_handoff_detect.py
import pytest

from ai_service_agent.graph.handoff_rules import detect_handoff_intent

TRUE_CASES = [
    "我要转人工",
    "转人工",
    "我不想跟机器人聊，叫个真人来",
    "找个人客服",
    "我要找客服",
    "麻烦帮我转人工客服",   # CTRL-0061
    "联系人工客服",
    "我要人工",
    "我要找人工",
    "给我接人工",
    "找客服",
    "真人客服",
    "转接人工",
    "请安排人工客服处理退款",
]

FALSE_CASES = [
    "人工呼吸",
    "人工智能是什么？",
    "人工审核需要多久？",
    "人工成本怎么算？",
    "人工客服培训",
    "随便聊聊",
    "查一下订单 ORD-1",
    "帮我看看人工智能的文章",
    "帮我把人工审核加快",   # 话题性（负向后缀排除）
    "麻烦转人工呼吸机的售后",
]


@pytest.mark.parametrize("text", TRUE_CASES)
def test_explicit_handoff_intent_detected(text):
    assert detect_handoff_intent(text) is True


@pytest.mark.parametrize("text", FALSE_CASES)
def test_non_handoff_intent_not_detected(text):
    assert detect_handoff_intent(text) is False


def test_blank_input_is_false():
    assert detect_handoff_intent("") is False
    assert detect_handoff_intent("   ") is False
```

```python
# tests/test_config.py —— 修改 handoff_keywords 断言为 handoff_phrases / handoff_negative_suffix
def test_settings_p0_handoff_config(monkeypatch):
    from ai_service_agent.config import Settings
    monkeypatch.delenv("HANDOFF_PHRASES", raising=False)
    monkeypatch.delenv("HANDOFF_NEGATIVE_SUFFIX", raising=False)
    s = Settings(_env_file=None)
    assert "转人工" in s.handoff_phrases
    assert "培训" in s.handoff_negative_suffix
    assert not hasattr(s, "handoff_keywords")
```

- [ ] **Step 2: 运行确认失败**

Run: `$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest tests/test_handoff_detect.py tests/test_config.py -q`
Expected: FAIL（`ModuleNotFoundError: ai_service_agent.graph.handoff_rules`；`AttributeError: 'Settings' object has no attribute 'handoff_phrases'`）。

- [ ] **Step 3: 实现**

```python
# src/ai_service_agent/graph/handoff_rules.py
"""显式转人工意图检测（safety-spec.md §2.1 第一类：显式请求）。

detector 只判定「用户明确要求转人工」：确定性、无 LLM、无外部调用，可纯单测。
流程：normalize（去空白/小写）→ explicit phrase（完整短语）→ intent pattern
（动作前缀 + 近邻目标词，排除话题性后缀）。

不处理动态信号（工具连续失败 / 重复未解决 / 情绪升级 / 审批被拒后坚持等），
那些依赖 context / 运行状态，由 graph 内 check_handoff 负责（graph-spec.md §6.4）。
"""

import re

from ai_service_agent.config import get_settings

# 动作前缀：用户「要求/请求转接」的动词短语。刻意不含裸「人工」子串，防话题性误伤。
_ACTION_PREFIX = r"(?:转接|转|接|找|叫|让|请|联系|安排|派|呼叫|帮我|给我|我要|想找)"

# 目标词（不含裸「人」，防「找个人帮我看看」等泛化表达误伤）。
_TARGET = r"(?:人工|真人|客服)"


def detect_handoff_intent(text: str) -> bool:
    """显式转人工意图判定（P0 D2）。输入相同时结果恒定。

    False 不代表没有转人工意图：动态信号类（依赖 context）由图内 check_handoff 判定。
    """
    if not text:
        return False
    norm = re.sub(r"\s+", "", text).lower()
    if norm in set(get_settings().handoff_phrases):
        return True
    suffix = "|".join(re.escape(s) for s in get_settings().handoff_negative_suffix)
    pattern = re.compile(
        _ACTION_PREFIX + r".{0,4}?" + _TARGET + r"(?!.{0,2}(?:" + suffix + r"))"
    )
    return bool(pattern.search(norm))
```

```python
# src/ai_service_agent/config.py —— 替换 handoff_keywords 字段
    # P0（2026-09-18）：显式转人工意图检测配置（graph/handoff_rules.py 消费）。
    # 替代旧 handoff_keywords 裸子串匹配（"人工" 会误伤 "人工呼吸/人工智能" 等话题性表达）。
    handoff_phrases: list[str] = ["转人工", "转接人工", "找人工", "找客服", "人工客服", "真人客服"]
    handoff_negative_suffix: list[str] = [
        "培训", "审核", "成本", "智能", "呼吸", "操作", "考试",
        "招聘", "岗位", "工作", "工资", "流程", "制度", "标准",
    ]
```

- [ ] **Step 4: 运行确认通过**

Run: `$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest tests/test_handoff_detect.py tests/test_config.py -q`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
cd "D:\AI_Project\AI Service Agent"
git add src/ai_service_agent/graph/handoff_rules.py src/ai_service_agent/config.py tests/test_handoff_detect.py tests/test_config.py
git commit -m "feat: P0 显式转人工确定性 detector——normalize→phrase→pattern（无裸人工子串）+ config handoff_phrases/negative_suffix"
```

---

### Task 2: fallback + eval runner 复用 detector

**Files:**
- Modify: `src/ai_service_agent/graph/workflows/fallback.py`
- Modify: `src/ai_service_agent/evals/runner.py`
- Modify: `tests/evals/control/handoff.jsonl`（CTRL-0062 条目 reason 更新）
- Modify: `tests/test_evals.py`
- Modify: `tests/test_fallback.py`（新增 detector 相关断言）

**Interfaces:**
- Consumes: `detect_handoff_intent(text) -> bool`（Task 1）。
- Produces: `fallback.check_handoff` 显式命中改为 detector（动态信号保留扩展位）；`runner.evaluate_handoff` / `runner.evaluate_routing` 用 detector；runner settings 快照字段 `handoff_phrases`。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_evals.py —— 更新/新增
def test_handoff_dimension_explicit_and_dynamic(tmp_path):
    from ai_service_agent.graph.handoff_rules import detect_handoff_intent
    d = _write(tmp_path, [CASE_HANDOFF, CASE_HANDOFF_PENDING])
    res = runner.evaluate_handoff(runner.load_cases(d / "master.jsonl"))
    by_id = {r["id"]: r for r in res}
    assert by_id["T-03"]["status"] == "pass"
    assert by_id["T-04"]["status"] == "skip" and by_id["T-04"]["skip_reason"] == "handoff_rule_pending"


def test_cli_runs_on_real_control_dataset(tmp_path):  # 更新计数断言
    ...
    assert report["case_count"] == 100
    assert report["dimensions"]["policy"]["summary"]["pass"] == 76
    assert report["dimensions"]["guard"]["summary"]["pass"] == 100
    assert report["dimensions"]["routing"]["summary"]["pass"] == 92  # +0062（P0 显式 detector 命中）
    assert report["dimensions"]["routing"]["summary"]["skip"] == {"handoff_rule_pending": 8}
```

```python
# tests/test_fallback.py —— 新增
def test_check_handoff_reuses_shared_detector():
    out = check_handoff({"messages": [HumanMessage(content="叫个真人来")], "context": {}, "user_id": "u1", "session_id": "s1"})
    assert isinstance(out, Command) and out.update["handoff"] is True and out.goto == "pause"


def test_check_handoff_no_false_positive_on_topic():
    out = check_handoff({"messages": [HumanMessage(content="人工客服培训需要多久")], "context": {}, "user_id": "u1", "session_id": "s1"})
    assert out == {"handoff": False, "execution_status": "completed"}
```

- [ ] **Step 2: 运行确认失败**

Run: `$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest tests/test_fallback.py tests/test_evals.py -q`
Expected: FAIL（`check_handoff` 仍用已删除的 `handoff_keywords` → AttributeError；routing 计数不匹配 92/8）。

- [ ] **Step 3: 实现**

```python
# src/ai_service_agent/graph/workflows/fallback.py
from ai_service_agent.graph.handoff_rules import detect_handoff_intent


def check_handoff(state: dict) -> dict | Command:
    """确定性规则：显式转人工意图（入口层共用的 detector）+ 动态信号（预留扩展位）。

    命中时先置位再中断：Command(update) 保证 execution_status=waiting_handoff
    在中断时刻生效（state-spec.md §1），随后路由到 pause 节点抛 interrupt。
    """
    last_content = _last_user_content(state)
    # P0：显式请求（safety-spec §2.1 第一类）。动态信号（工具失败计数等）在此追加判定。
    if detect_handoff_intent(last_content):
        return Command(
            update={"handoff": True, "execution_status": "waiting_handoff"},
            goto="pause",
        )
    return {"handoff": False, "execution_status": "completed"}
```

```python
# src/ai_service_agent/evals/runner.py
from ai_service_agent.graph.handoff_rules import detect_handoff_intent


def evaluate_handoff(cases: list[dict]) -> list[dict]:
    """handoff 维度（P0 D7）：显式请求用入口层 detector 判定（不再依赖图内关键词）；
    动态信号保持 skip(handoff_rule_pending)（D13 不变）。"""
    results = []
    for case in cases:
        if case["expected_graph"] == "handoff":
            if detect_handoff_intent(case["input"]):
                results.append({"id": case["id"], "status": "pass"})
            else:
                results.append({"id": case["id"], "status": "skip", "skip_reason": HANDOFF_SKIP})
        else:
            results.append({"id": case["id"], "status": "pass" if not detect_handoff_intent(case["input"]) else "fail"})
    return results
```

```python
# src/ai_service_agent/evals/runner.py —— evaluate_routing 的 handoff 分支
        if ep == "handoff":
            if not detect_handoff_intent(case["input"]):
                results.append({"id": case["id"], "status": "skip", "skip_reason": HANDOFF_SKIP})
                continue
            r = asyncio.run(_run_case_graph(case, _scripted_responses(case), "u1"))
            ok = any(i.get("type") == "handoff_request" for i in r["interrupts"])
            ok = ok and not any(getattr(m, "type", "") == "ai" for m in r["state"].get("messages", []))
            results.append({"id": case["id"], "status": "pass" if ok else "fail", "actual": "handoff"})
            continue
```

```python
# src/ai_service_agent/evals/runner.py —— settings 快照字段（文件末尾）
        "handoff_phrases": get_settings().handoff_phrases,
        "handoff_negative_suffix": get_settings().handoff_negative_suffix,
```

```jsonl
# tests/evals/control/handoff.jsonl —— 仅更新 CTRL-0062 的 reason（语义已由 P0 detector 覆盖）
{"id": "CTRL-0062", "input": "我不想跟机器人聊，叫个真人来", "expected_capability": "unknown", "expected_risk_level": "read", "expected_graph": "handoff", "requires_human": true, "expected_policy": "handoff", "reason": "P0（2026-09-18）显式转人工意图被入口层 detector 命中（叫个真人），不再 skip；动态信号条目保持 skip(handoff_rule_pending)。", "metadata": {"category": "handoff", "risk": "low"}}
```

```python
# tests/test_evals.py —— 更新 settings 快照断言
    assert report["settings"]["handoff_phrases"]
```

- [ ] **Step 4: 运行确认通过**

Run: `$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest tests/test_fallback.py tests/test_evals.py -q`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
cd "D:\AI_Project\AI Service Agent"
git add src/ai_service_agent/graph/workflows/fallback.py src/ai_service_agent/evals/runner.py tests/evals/control/handoff.jsonl tests/test_evals.py tests/test_fallback.py
git commit -m "refactor: fallback/eval 复用入口层 detector——显式转人工不再依赖图内关键词（CTRL-0062 转真实断言，动态信号保持 skip）"
```

---

### Task 3: HandoffService.get_active + request_handoff

**Files:**
- Modify: `src/ai_service_agent/harness/handoff.py`
- Modify: `tests/test_handoff.py`

**Interfaces:**
- Produces:
  - `HandoffService.get_active(conversation_id: str) -> dict | None`（`{"handoff_id", "conversation_id", "reason", "summary"}`；无则 None）。
  - `HandoffService.request_handoff(conversation_id: str, reason: str, summary: str) -> dict`（返回 `{"type": "handoff_created", "data": {…}}`；存在 active 则幂等复用，不新建）。

- [ ] **Step 1: 写失败测试**（`tests/test_handoff.py`）

```python
def test_get_active_none_then_after_create(tmp_path):
    svc, status, conn = _svc(tmp_path)
    info = status.create_session("u1")
    assert svc.get_active(info["conversation_id"]) is None
    rec = svc.create_handoff(info["conversation_id"], "r", "s")
    active = svc.get_active(info["conversation_id"])
    assert active["handoff_id"] == rec["handoff_id"]
    conn.close()


def test_request_handoff_idempotent_reuses_active(tmp_path):
    svc, status, conn = _svc(tmp_path)
    info = status.create_session("u1")
    ev1 = svc.request_handoff(info["conversation_id"], "r1", "s1")
    ev2 = svc.request_handoff(info["conversation_id"], "r2", "s2")
    assert ev1["type"] == ev2["type"] == "handoff_created"
    assert ev1["data"]["handoff_id"] == ev2["data"]["handoff_id"]
    n = conn.execute(
        "SELECT COUNT(*) FROM handoff_record WHERE conversation_id=? AND status='active'",
        (info["conversation_id"],),
    ).fetchone()[0]
    assert n == 1
    conn.close()
```

- [ ] **Step 2: 运行确认失败**

Run: `$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest tests/test_handoff.py -q`
Expected: FAIL（`AttributeError: 'HandoffService' object has no attribute 'get_active'`）。

- [ ] **Step 3: 实现**（`src/ai_service_agent/harness/handoff.py`）

```python
    def get_active(self, conversation_id: str) -> dict | None:
        """当前会话最新 active handoff（幂等复用；无则 None）。"""
        row = self.conn.execute(
            "SELECT handoff_id, conversation_id, reason, summary FROM handoff_record "
            "WHERE conversation_id=? AND status='active' ORDER BY created_at DESC LIMIT 1",
            (conversation_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "handoff_id": row[0],
            "conversation_id": row[1],
            "reason": row[2],
            "summary": row[3],
        }

    def request_handoff(self, conversation_id: str, reason: str, summary: str) -> dict:
        """入口层显式转人工统一入口（P0 D3）：幂等复用 active -> create_handoff -> 返回 handoff_created 事件。

        graph 动态路径经 make_interrupt_callback 复用 create_handoff；side effects
        （NOTICE 落库 / Chatwoot ensure / 事件发送）由 server 的 _emit_handoff 统一完成。
        """
        active = self.get_active(conversation_id)
        if active is not None:
            return {"type": "handoff_created", "data": active}
        rec = self.create_handoff(conversation_id, reason, summary)
        return {"type": "handoff_created", "data": rec}
```

- [ ] **Step 4: 运行确认通过**

Run: `$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest tests/test_handoff.py -q`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
cd "D:\AI_Project\AI Service Agent"
git add src/ai_service_agent/harness/handoff.py tests/test_handoff.py
git commit -m "feat: HandoffService.get_active/request_handoff——入口显式转人工统一入口（幂等复用 active）"
```

---

### Task 4: server `/chat` 入口接入 + `_emit_handoff` 抽取

**Files:**
- Modify: `src/ai_service_agent/harness/server.py`
- Create: `tests/test_handoff_entrance.py`
- Modify: `tests/test_e2e.py`（`test_handoff_interrupt_roundtrip` 注释更新）
- Modify: `tests/test_e2e_human_lane.py`（docstring 更新）
- Modify: `tests/test_handoffs_query.py`（docstring 更新）

**Interfaces:**
- Consumes: `detect_handoff_intent(text)`（Task 1）、`HandoffService.request_handoff(conv_id, reason, summary)`（Task 3）。
- Produces: `/chat` 入口在 `mode==human` 判断后新增显式转人工分支；`_emit_handoff` 供 `ai_stream` 与显式流共用。

- [ ] **Step 1: 写失败测试**（`tests/test_handoff_entrance.py`）

```python
"""P0 入口显式转人工：不进 Router/Guard/Graph、事件顺序、幂等、pending interrupt 优先。"""

import sqlite3

from fastapi.testclient import TestClient
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.messages import AIMessage

from ai_service_agent.harness.server import create_app
from ai_service_agent.harness.session import ConversationStatusStore
from ai_service_agent.testing import ToolCallingFakeModel, router_json_message


def _parse_sse(text: str) -> list[dict]:
    import json
    events = []
    for block in text.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


class ExplodingModel(ToolCallingFakeModel):
    """若显式转人工仍进入图内，则 _generate 被调用并抛错（证明不进 Router/Guard/Graph）。"""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise AssertionError("graph must not run for explicit handoff")


class ApprovalFlowFakeModel(ToolCallingFakeModel):
    """Router -> order/high_risk；select -> refund_apply；用于构造 approval_required 挂起中断。"""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        system = next((m.content for m in messages if getattr(m, "type", "") == "system"), "")
        if "选择器" in system:
            return ChatResult(generations=[ChatGeneration(
                message=AIMessage(content='{"tool": "refund_apply", "args": {"order_id": "10086"}}'))])
        if "路由器" in system:
            return ChatResult(generations=[ChatGeneration(
                message=AIMessage(content='{"capability": "order", "risk_level": "high_risk", "confidence": 0.9}'))])
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="已受理"))])


def test_explicit_handoff_skips_graph(tmp_path):
    """D1：显式转人工不进 Router/Guard/Graph（ExplodingModel 若被调用即失败）。"""
    client = TestClient(create_app(str(tmp_path / "s.db"), model=ExplodingModel(responses=[])))
    r = client.post("/sessions", json={"user_id": "u1"})
    sid = r.json()["session_id"]
    with client.stream("POST", "/chat", json={"session_id": sid, "message": "我要转人工"}) as resp:
        events = _parse_sse("".join(resp.iter_text()))
    types = [e["type"] for e in events]
    assert types[0] == "token"
    assert "handoff_created" in types
    assert types[-1] == "done"
    assert ConversationStatusStore(sqlite3.connect(str(tmp_path / "s.db"))).get_mode(sid) == "human"


def test_explicit_handoff_writes_user_and_notice_to_store(tmp_path):
    """D5：Message Store 顺序 = [user, HANDOFF_NOTICE]（唯一真相源）。"""
    from ai_service_agent.harness.messages import MessageStore
    client = TestClient(create_app(str(tmp_path / "s.db"), model="fake"))
    r = client.post("/sessions", json={"user_id": "u1"})
    sid = r.json()["session_id"]
    with client.stream("POST", "/chat", json={"session_id": sid, "message": "我要转人工"}) as resp:
        "".join(resp.iter_text())
    rows = MessageStore(sqlite3.connect(str(tmp_path / "s.db"))).list(sid)
    assert [m["content"] for m in rows] == ["我要转人工", "已为你转接人工客服，坐席将尽快回复。"]


def test_non_handoff_message_still_uses_ai_lane(tmp_path):
    """D1：detect False -> 保持 AI Lane 完整流程（有 token + done）。"""
    client = TestClient(create_app(str(tmp_path / "s.db"), model="fake"))
    r = client.post("/sessions", json={"user_id": "u1"})
    sid = r.json()["session_id"]
    with client.stream("POST", "/chat", json={"session_id": sid, "message": "查一下订单"}) as resp:
        types = [e["type"] for e in _parse_sse("".join(resp.iter_text()))]
    assert "token" in types and "done" in types
    assert ConversationStatusStore(sqlite3.connect(str(tmp_path / "s.db"))).get_mode(sid) == "ai"


def test_consecutive_explicit_handoffs_single_active(tmp_path):
    """D3 duplicate protection：连续两次显式转人工只产生 1 个 active handoff。"""
    client = TestClient(create_app(str(tmp_path / "s.db"), model="fake"))
    r = client.post("/sessions", json={"user_id": "u1"})
    sid = r.json()["session_id"]
    for msg in ("我要转人工", "再转一次人工"):
        with client.stream("POST", "/chat", json={"session_id": sid, "message": msg}) as resp:
            "".join(resp.iter_text())
    conn = sqlite3.connect(str(tmp_path / "s.db"))
    n = conn.execute("SELECT COUNT(*) FROM handoff_record WHERE conversation_id=? AND status='active'", (sid,)).fetchone()[0]
    assert n == 1


def test_explicit_handoff_priority_over_pending_interrupt(tmp_path):
    """D6（C4）：approval 挂起时显式转人工优先——直接转人工，approval 保持 pending。"""
    client = TestClient(create_app(str(tmp_path / "s.db"), model=ApprovalFlowFakeModel(responses=[])))
    r = client.post("/sessions", json={"user_id": "u1"})
    sid = r.json()["session_id"]
    # 触发 approval_required 挂起
    with client.stream("POST", "/chat", json={"session_id": sid, "message": "订单10086申请退款"}) as resp:
        types = [e["type"] for e in _parse_sse("".join(resp.iter_text()))]
    assert "approval_required" in types
    # 显式转人工 -> 不进 resume，直接 human
    with client.stream("POST", "/chat", json={"session_id": sid, "message": "我要转人工"}) as resp:
        types2 = [e["type"] for e in _parse_sse("".join(resp.iter_text()))]
    assert "handoff_created" in types2
    assert ConversationStatusStore(sqlite3.connect(str(tmp_path / "s.db"))).get_mode(sid) == "human"
    pending = client.get("/approvals?status=pending").json()["items"]
    assert any(a["conversation_id"] == sid and a["status"] == "pending" for a in pending)
```

- [ ] **Step 2: 运行确认失败**

Run: `$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest tests/test_handoff_entrance.py -q`
Expected: FAIL（ExplodingModel 被调用抛错 / `handoff_created` 缺失，证明当前仍走图内路径）。

- [ ] **Step 3: 实现**（`src/ai_service_agent/harness/server.py`）

```python
# import 区追加
from ai_service_agent.graph.handoff_rules import detect_handoff_intent
```

```python
# create_app 内、`app = FastAPI()` 之后追加闭包（复用 messages_store / chatwoot）
    async def _emit_handoff(conv_id: str, ev: dict):
        """统一 handoff side-effect path（P0 D3）：落 NOTICE + ensure Chatwoot（失败只记日志）+ 事件流。

        图内动态 handoff（interrupt 建 record 后）与入口显式 handoff 共用；
        事件顺序 token(HANDOFF_NOTICE) -> handoff_created，与 event-spec / 页面 contract 一致。
        """
        messages_store.append(conv_id, "assistant", HANDOFF_NOTICE)
        if chatwoot is not None:
            try:
                await chatwoot.ensure_conversation(conv_id, HANDOFF_NOTICE)
            except Exception:
                logger.exception("chatwoot ensure failed: conv=%s", conv_id)
        yield {"type": "token", "data": {"text": HANDOFF_NOTICE}}
        yield ev
```

```python
# /chat 端点：在 `if status.get_mode(conv_id) == "human":` 块之后、`async def ai_stream():` 之前插入
        if detect_handoff_intent(body.message):
            # P0 D1：显式转人工 -> 不进 Router/Guard/Graph，直接走统一 handoff side-effect path。
            # 先落 user 消息（Message Store 唯一真相源，handoff-spec §5.2），再 request_handoff（幂等）。
            messages_store.append(conv_id, "user", body.message)
            ev = handoff.request_handoff(conv_id, "用户显式请求转人工", body.message[:80])

            async def explicit_handoff_stream():
                async for out in _emit_handoff(conv_id, ev):
                    yield out
                yield {"type": "done", "data": {"final": HANDOFF_NOTICE, "message_id": None}}

            return _sse_response(explicit_handoff_stream())
```

```python
# ai_stream 内 handoff_created 分支替换为复用 _emit_handoff
                        if ev["type"] == "handoff_created":
                            async for out in _emit_handoff(conv_id, ev):
                                yield out
                            continue
```

- [ ] **Step 4: 运行确认通过**

Run: `$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest tests/test_handoff_entrance.py tests/test_e2e_human_lane.py tests/test_e2e.py tests/test_handoffs_query.py -q`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
cd "D:\AI_Project\AI Service Agent"
git add src/ai_service_agent/harness/server.py tests/test_handoff_entrance.py tests/test_e2e.py tests/test_e2e_human_lane.py tests/test_handoffs_query.py
git commit -m "feat: /chat 入口显式转人工——detector 前置拦截（不进 Router/Guard/Graph）+ 统一 _emit_handoff side-effect path"
```

---

### Task 5: 全量回归 + langgraph validate + 真机 smoke + 文档

**Files:**
- Modify: `scripts/real_human_lane_smoke.py`（`HANDOFF_PHRASES` 加显式变体 + 断言 handoff_created/mode=human）
- Modify: `docs/specs/handoff-spec.md`（§2 增补入口层显式检测）
- Modify: `docs/specs/safety-spec.md`（§2.1 显式请求条目标注入口层优先判定）
- Modify: `docs/real-model-smoke.md`（登记 P0 修复与真机复验）
- Modify: `docs/plans/README.md`（登记本计划）

- [ ] **Step 1: 全量回归**

Run: `$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest -q`
Expected: 全绿（基线 225 + 新增用例；Task 1-4 已逐个 commit）。

- [ ] **Step 2: langgraph validate**

Run: `cd "D:\AI_Project\AI Service Agent"; langgraph validate`
Expected: valid。

- [ ] **Step 3: 真机 smoke 更新**（`scripts/real_human_lane_smoke.py`）

`HANDOFF_PHRASES = ["我要转人工", "帮我转人工", "转人工"]` → 追加显式变体并在 step 5 用两用例各验一次 `handoff_created` + `mode=human`：

```python
HANDOFF_PHRASES = ["我要转人工", "帮我转人工", "转人工", "我不想跟机器人聊，叫个真人来"]
```

并在 step 5 断言注释中写明：**关键断言不是 Router 分类，而是 `handoff_created` + `mode=human`（即使 Router 原本可能误判，入口 detector 阶段已直接进入 Human Lane）。**

- [ ] **Step 4: 文档更新**

- `docs/specs/handoff-spec.md` §2：追加一句「显式转人工意图（safety-spec §2.1 第一类）由入口层 detector 在进入 LangGraph 之前判定，命中即走人工车道，不触发 checkpointer/Graph/Guard/Router；图内 `check_handoff` 保留动态信号与兜底（P0 定案，2026-09-18）。」
- `docs/specs/safety-spec.md` §2.1 显式请求行：标注「P0：入口层 detector 优先判定；动态信号仍由图内 check_handoff」。
- `docs/real-model-smoke.md`：新增小节登记 P0 修复 + 真机复验两用例。
- `docs/plans/README.md`：登记 `2026-09-18-handoff-intent-gate.md`。

- [ ] **Step 5: Commit**

```bash
cd "D:\AI_Project\AI Service Agent"
git add scripts/real_human_lane_smoke.py docs/specs/handoff-spec.md docs/specs/safety-spec.md docs/real-model-smoke.md docs/plans/README.md
git commit -m "docs+test: P0 显式转人工入口检测登记——spec 增补 + 真机 smoke 双用例（断言 handoff_created/mode=human 而非 Router 分类）"
```

## Self-Review

- **Spec 覆盖**：safety-spec §2.1 显式请求 → Task 1 detector + Task 4 入口接入；handoff-spec §2 入口层 → Task 4；handoff-spec §1.2 原子切 mode → `create_handoff`（Task 3）；graph-spec §6.4 动态信号保留 → Task 2；event-spec 事件顺序 → `_emit_handoff`（Task 4）。
- **占位符扫描**：所有 Step 均含真实代码/命令；无 TBD/TODO。
- **类型一致性**：`detect_handoff_intent(text)->bool` 在 Task 1/2/4 中签名一致；`request_handoff(conv_id, reason, summary)->dict` 在 Task 3/4 一致；`_emit_handoff(conv_id, ev)` 在 Task 4 两处调用一致。
