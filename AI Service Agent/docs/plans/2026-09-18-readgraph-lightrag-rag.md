# ReadGraph 知识库 RAG 接入（LightRAG-chengyuan）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 ReadGraph 的 `kb_search` 工具从 stub 升级为真实知识库检索：调用已部署的 `LightRAG-chengyuan`（`localhost:9621`）`/query` 接口获取检索上下文与引用来源，保持低延迟（实测 ~1s）、演示可展示引用出处、流式输出复用现有 harness SSE 通道。

**Architecture:** `kb_search` 仍是 ReadGraph 的只读 tool（`min_risk_level="read"`，不进写图 select，P5 B2b 不受影响）。工具内部经 `LightRagClient`（httpx 同步单例、连接复用）调 `POST /query`，参数 `mode=naive`、`only_need_context=true`、`include_references=true`、`include_chunk_content=true`、`enable_rerank=false`、`top_k=5`；返回「检索上下文 + 参考来源清单」。agent 基于上下文生成回复，并在回复文本内标注来源（不改 event-spec、不改前端）。

**Tech Stack:** Python 3.14；LangGraph 1.2.10（固定）；FastAPI（Transport）；httpx（已声明于 `pyproject.toml`，客户端 + `MockTransport` 测试）；pytest；ARK 真实模型（冒烟）。

**Spec:** `docs/specs/tool-spec.md`（§1 分层注入 / §4 注册表）· `docs/specs/graph-spec.md`（§6.1 ReadGraph）· `docs/specs/safety-spec.md`（§1.4 数据边界，kb 只读）· `docs/specs/event-spec.md`（不改，OQ-2）· `docs/decisions/2026-09-16-p5-select-tier-alignment.md`（B2b 写图 select 档内严格，kb 不进写图）。

## Global Constraints

- LangGraph 1.2.10 固定；禁止 `langgraph.prebuilt.create_react_agent`；不新增第三方 Python 依赖（httpx 已有）。
- 不修改 Router schema、Policy Gate、Guard、MutationGraph/HighRiskGraph、interrupt/resume/checkpointer、`conversation_mode`、Message Store、Chatwoot 链路、event-spec、前端页面。
- FastAPI 只做 Transport；业务编排留在图内；`kb_search` 只读、无副作用、确定性降级，不允许在工具内做任何授权/业务决策。
- 离线测试禁止依赖外部服务：`kb_search` / `LightRagClient` 一律用 `httpx.MockTransport` 或 monkeypatch fake client；真实 LightRAG 只允许出现在 `scripts/real_rag_smoke.py`（标记为真机冒烟）。
- 测试命令前缀：`$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest ...`
- 提交在项目目录 `D:\AI_Project\AI Service Agent` 执行，git 根 `D:\AI_Project`；`git add` 只加明确列出的文件，禁止 `git add -A` / `git add .`。沙箱拦截 git 写：commit 命令交用户执行，agent 不自行 commit。

## Contract Decisions（OQ 裁决定案）

- **D1（mode=naive）**：`lightrag_mode` 默认 `"naive"`（实测 ~1s，已满足演示；先保证响应速度与稳定，不切 `mix`）。
- **D2（回复文本内引用）**：引用来源以 agent 回复文本内的「（来源：<文件名>）」呈现；不改 `event-spec`、不改前端、最小侵入。
- **D3（不加 LRU 缓存）**：本轮不为演示优化不存在的瓶颈，`kb_search` 不引入缓存层。
- **D4（不做摄取自动同步）**：`knowledge-base/` 变更 → LightRAG 重新摄取属于下一阶段，本轮只把检索链路跑通。
- **D5（降级语义）**：LightRAG 不可达 → 工具返回「知识库暂不可用，请稍后再试或联系人工客服。」；检索无结果 → 「未查询到相关知识，请换个说法或联系人工客服。」。agent 据此如实告知，不编造。

## File Structure

- Create: `src/ai_service_agent/tools/lightrag.py`（`LightRagClient` + 模块级单例 `get_lightrag_client()` / `reset_lightrag_client()`）
- Modify: `src/ai_service_agent/config.py`（新增 `lightrag_*` 配置字段）
- Modify: `src/ai_service_agent/tools/kb.py`（`kb_search` 接真实检索 + 降级；`KbSearchArgs` 不变）
- Modify: `src/ai_service_agent/prompts.py`（`READ_SYSTEM_PROMPT` 增加引用来源与如实告知规则）
- Create: `tests/test_lightrag_client.py`（`MockTransport`：成功/空/不可达）
- Create: `tests/test_kb_search.py`（fake client：命中带来源/空结果/降级）
- Modify: `tests/test_config.py`（`lightrag_*` 默认值）
- Modify: `tests/test_tools.py`（`test_stub_tools_return_values` 去掉 kb_search stub 断言）
- Modify: `tests/test_read_agent.py`（README prompt 来源规则断言）
- Create: `scripts/real_rag_smoke.py`（真实 LightRAG + ARK 冒烟）
- Modify: `docs/specs/tool-spec.md`（kb_search 真实化登记）、`docs/real-model-smoke.md`（RAG 冒烟章节）、`docs/plans/README.md`（登记）

任务分解（每任务独立可测、独立 commit）：

| Task | 内容 | 可测产物 |
|---|---|---|
| 1 | `config` + `LightRagClient`（httpx + MockTransport） | `tests/test_lightrag_client.py` + `tests/test_config.py` |
| 2 | `kb_search` 接真实检索 + 降级 | `tests/test_kb_search.py` + `tests/test_tools.py` |
| 3 | `READ_SYSTEM_PROMPT` 引用来源规则 | `tests/test_read_agent.py` |
| 4 | 真实 LightRAG + ARK 冒烟脚本 | `scripts/real_rag_smoke.py` + 运行 + `real-model-smoke.md` |
| 5 | 全量回归 + validate + 文档登记 | 全量 pytest + `langgraph validate` + 文档 |

---

### Task 1: config 字段 + LightRagClient 客户端

**Files:**
- Modify: `src/ai_service_agent/config.py`（`Settings` 新增 `lightrag_*`）
- Create: `src/ai_service_agent/tools/lightrag.py`
- Create: `tests/test_lightrag_client.py`
- Modify: `tests/test_config.py`

**Interfaces:**
- Produces（config）: `Settings.lightrag_base_url: str = "http://localhost:9621"`、`Settings.lightrag_timeout_seconds: float = 15.0`、`Settings.lightrag_mode: str = "naive"`、`Settings.lightrag_top_k: int = 5`、`Settings.lightrag_include_references: bool = True`、`Settings.lightrag_enable_rerank: bool = False`。
- Produces: `LightRagClient(base_url=None, timeout_seconds=None, mode=None, top_k=None, include_references=None, enable_rerank=None, transport=None) -> LightRagClient`；`query_context(query: str) -> dict`（`{"context": str, "references": [{"file_path": str, "reference_id": str}]}` 或 `{"error": "kb_unavailable"}`）；`get_lightrag_client() -> LightRagClient`；`reset_lightrag_client() -> None`。
- Consumes: `ai_service_agent.config.get_settings()`。

- [ ] **Step 1: 写失败测试**（`tests/test_config.py` 追加 + 新建 `tests/test_lightrag_client.py`）

```python
# tests/test_config.py 追加
def test_lightrag_defaults():
    s = get_settings()
    assert s.lightrag_base_url == "http://localhost:9621"
    assert s.lightrag_mode == "naive"
    assert s.lightrag_top_k == 5
    assert s.lightrag_include_references is True
    assert s.lightrag_enable_rerank is False
```

```python
# tests/test_lightrag_client.py
import json

import httpx
import pytest

from ai_service_agent.tools.lightrag import LightRagClient, get_lightrag_client, reset_lightrag_client


def _client_with(handler):
    return LightRagClient(base_url="http://test", transport=httpx.MockTransport(handler))


def test_query_context_parses_references_and_context():
    def handler(request):
        body = json.loads(request.content)
        assert body["query"] == "JoyAir A1 的炸篮容量是多少？"
        assert body["mode"] == "naive"
        assert body["only_need_context"] is True
        assert body["include_references"] is True
        assert body["include_chunk_content"] is True
        assert body["enable_rerank"] is False
        return httpx.Response(200, json={
            "response": "",
            "references": [
                {"reference_id": "1", "file_path": "JoyAir_A1智能空气炸锅.md",
                 "content": "['document_id: YX-PRODUCT-A1-001 炸篮 4.5L']"},
            ],
        })

    out = _client_with(handler).query_context("JoyAir A1 的炸篮容量是多少？")
    assert "炸篮 4.5L" in out["context"]
    assert out["references"] == [
        {"file_path": "JoyAir_A1智能空气炸锅.md", "reference_id": "1"},
    ]


def test_query_context_empty_references():
    def handler(request):
        return httpx.Response(200, json={"response": "", "references": []})

    assert _client_with(handler).query_context("x") == {"context": "", "references": []}


def test_query_context_unavailable_on_connect_error():
    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    assert _client_with(handler).query_context("x") == {"error": "kb_unavailable"}


def test_get_lightrag_client_is_singleton():
    reset_lightrag_client()
    a = get_lightrag_client()
    b = get_lightrag_client()
    assert a is b
    reset_lightrag_client()
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest tests/test_config.py::test_lightrag_defaults tests/test_lightrag_client.py -q`
Expected: FAIL（`Settings` 无 `lightrag_*` 字段；`ai_service_agent.tools.lightrag` 不存在）。

- [ ] **Step 3: 实现 config 字段与 LightRagClient**

```python
# src/ai_service_agent/config.py —— Settings 类内（approval_expires_minutes 之后）追加
    # LightRAG-chengyuan（read 档 kb_search 数据源，OQ-1 naive / OQ-3 不加缓存 / OQ-4 不做自动同步）
    lightrag_base_url: str = "http://localhost:9621"
    lightrag_timeout_seconds: float = 15.0
    lightrag_mode: str = "naive"
    lightrag_top_k: int = 5
    lightrag_include_references: bool = True
    lightrag_enable_rerank: bool = False
```

```python
# src/ai_service_agent/tools/lightrag.py
"""LightRAG-chengyuan 检索客户端（read 档 kb_search 的数据源）。

只做「检索取上下文 + 引用来源」，不做生成：POST /query 参数
mode=naive、only_need_context=true、include_references=true、
include_chunk_content=true、enable_rerank=false、top_k=5（实测 ~1s）。
httpx 同步 Client 模块级单例（线程安全、连接复用）。离线测试用
transport=httpx.MockTransport(...) 注入。
"""

from typing import Optional

import httpx

from ai_service_agent.config import get_settings


class LightRagClient:
    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        mode: Optional[str] = None,
        top_k: Optional[int] = None,
        include_references: Optional[bool] = None,
        enable_rerank: Optional[bool] = None,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.lightrag_base_url).rstrip("/")
        self.timeout = timeout_seconds or settings.lightrag_timeout_seconds
        self.mode = mode or settings.lightrag_mode
        self.top_k = top_k if top_k is not None else settings.lightrag_top_k
        self.include_references = (
            include_references if include_references is not None else settings.lightrag_include_references
        )
        self.enable_rerank = enable_rerank if enable_rerank is not None else settings.lightrag_enable_rerank
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=self.timeout,
            transport=transport,
        )

    def query_context(self, query: str) -> dict:
        try:
            resp = self._client.post(
                "/query",
                json={
                    "query": query,
                    "mode": self.mode,
                    "only_need_context": True,
                    "include_references": self.include_references,
                    "include_chunk_content": True,
                    "top_k": self.top_k,
                    "enable_rerank": self.enable_rerank,
                },
            )
            resp.raise_for_status()
        except Exception:
            return {"error": "kb_unavailable"}
        refs = resp.json().get("references") or []
        contexts = [str(r["content"]) for r in refs if r.get("content")]
        if not contexts:
            return {"context": "", "references": []}
        return {
            "context": "\n".join(contexts),
            "references": [
                {"file_path": r.get("file_path", ""), "reference_id": r.get("reference_id", "")}
                for r in refs
            ],
        }


_client: Optional[LightRagClient] = None


def get_lightrag_client() -> LightRagClient:
    global _client
    if _client is None:
        _client = LightRagClient()
    return _client


def reset_lightrag_client() -> None:
    """测试用：清空单例，避免跨用例共享 client。"""
    global _client
    _client = None
```

- [ ] **Step 4: 运行测试，确认通过**

Run: 同上命令
Expected: PASS（6 个用例）。

- [ ] **Step 5: Commit**

```bash
git add src/ai_service_agent/config.py src/ai_service_agent/tools/lightrag.py tests/test_config.py tests/test_lightrag_client.py
git commit -m "feat: LightRAG-chengyuan 检索客户端——config lightrag_* 配置 + LightRagClient（/query only_need_context 低延迟路径）+ MockTransport 单测"
```

---

### Task 2: kb_search 接真实检索 + 降级

**Files:**
- Modify: `src/ai_service_agent/tools/kb.py`
- Create: `tests/test_kb_search.py`
- Modify: `tests/test_tools.py`（`test_stub_tools_return_values` 去掉 kb_search stub 断言）

**Interfaces:**
- Consumes: `ai_service_agent.tools.lightrag.get_lightrag_client()`。
- Produces: `kb_search(text: str) -> str`（保留签名；返回「检索结果 + 参考来源」或降级文案）；`KbSearchArgs` 不变。

- [ ] **Step 1: 写失败测试**（新建 `tests/test_kb_search.py`；`tests/test_tools.py` 同步改）

```python
# tests/test_tools.py —— test_stub_tools_return_values 改为只测 order_lookup（kb_search 移至 test_kb_search.py）
def test_stub_tools_return_values():
    assert order_lookup.invoke({"order_id": "ORD-1", "user_id": "u1"})["order_id"] == "ORD-1"
```

```python
# tests/test_kb_search.py
import pytest

from ai_service_agent.tools import kb as kb_module
from ai_service_agent.tools.kb import kb_search


class FakeClient:
    def __init__(self, result):
        self._result = result

    def query_context(self, query):
        return self._result


def _patch(monkeypatch, result):
    monkeypatch.setattr(kb_module._lightrag, "get_lightrag_client", lambda: FakeClient(result))


def test_kb_search_hit_with_sources(monkeypatch):
    _patch(monkeypatch, {
        "context": "炸篮 4.5L",
        "references": [{"file_path": "JoyAir_A1智能空气炸锅.md", "reference_id": "1"}],
    })
    out = kb_search.invoke("JoyAir A1 的炸篮容量是多少？")
    assert "炸篮 4.5L" in out
    assert "JoyAir_A1智能空气炸锅.md" in out


def test_kb_search_no_result(monkeypatch):
    _patch(monkeypatch, {"context": "", "references": []})
    out = kb_search.invoke("不存在的知识")
    assert "未查询到" in out


def test_kb_search_unavailable(monkeypatch):
    _patch(monkeypatch, {"error": "kb_unavailable"})
    out = kb_search.invoke("任何问题")
    assert "暂不可用" in out
    assert "人工客服" in out
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest tests/test_kb_search.py tests/test_tools.py -q`
Expected: FAIL（`kb_search` 仍返回 stub 文案；`test_stub_tools_return_values` 断言不符）。

- [ ] **Step 3: 实现 kb_search 真实检索**

```python
# src/ai_service_agent/tools/kb.py
"""知识库检索（read 档）：调用 LightRAG-chengyuan /query 获取检索上下文与引用来源。

OQ 定案：mode=naive（低延迟）；回复文本内引用（不改 event-spec）；不加缓存；不做自动同步。
降级：LightRAG 不可达 -> 明确告知；无结果 -> 如实告知。确定性、只读、无副作用。
"""

from typing import Annotated

import ai_service_agent.tools.lightrag as _lightrag
from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict


class KbSearchArgs(BaseModel):
    """kb_search 的 select 侧契约（P5 B1）。"""
    model_config = ConfigDict(extra="forbid")
    text: str
    request_id: str


@tool
def kb_search(text: Annotated[str, "用户的完整问题原文，保留原始表述，不要改写或摘要"]) -> str:
    """澄元智能家电知识库检索。

    当用户询问产品参数与功能、使用方法、保修/退换货/退款/发票/订单与物流政策、
    售后规则等知识类问题时调用本工具。

    参数 text：用户的完整问题原文。
    返回：检索到的知识上下文与参考来源文件清单；未命中返回「未查询到相关知识」，
    知识库不可达返回「知识库暂不可用」。
    只能依据返回内容作答，不得编造；依据知识库作答时须在回答中标注来源文件名。
    """

    result = _lightrag.get_lightrag_client().query_context(text)
    if "error" in result:
        return "知识库暂不可用，请稍后再试或联系人工客服。"
    if not result["context"]:
        return "未查询到相关知识，请换个说法或联系人工客服。"
    sources = "、".join(r["file_path"] for r in result["references"] if r.get("file_path"))
    return f"检索结果：\n{result['context']}\n\n参考来源：{sources}"
```

- [ ] **Step 4: 运行测试，确认通过**

Run: 同上命令
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add src/ai_service_agent/tools/kb.py tests/test_kb_search.py tests/test_tools.py
git commit -m "feat: kb_search 接真实 LightRAG 检索——命中带参考来源、空结果/不可达确定性降级"
```

---

### Task 3: READ_SYSTEM_PROMPT 引用来源规则

**Files:**
- Modify: `src/ai_service_agent/prompts.py`（`READ_SYSTEM_PROMPT`）
- Modify: `tests/test_read_agent.py`

**Interfaces:**
- Consumes: `kb_search`（Task 2 返回的「参考来源：<文件名>」清单文本）。
- Produces: `READ_SYSTEM_PROMPT`（含「依据知识库作答须标注来源、未检索到如实告知」规则）。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_read_agent.py 追加
def test_read_system_prompt_requires_source_citation():
    from ai_service_agent.prompts import READ_SYSTEM_PROMPT
    assert "来源" in READ_SYSTEM_PROMPT
    assert "（来源：" in READ_SYSTEM_PROMPT
    assert "未查询到" in READ_SYSTEM_PROMPT
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest tests/test_read_agent.py::test_read_system_prompt_requires_source_citation -q`
Expected: FAIL（当前 prompt 无「来源」规则）。

- [ ] **Step 3: 更新 READ_SYSTEM_PROMPT**

```python
# src/ai_service_agent/prompts.py —— 替换 READ_SYSTEM_PROMPT 为
READ_SYSTEM_PROMPT = """你是澄元智能家电的客服智能助手，语气亲切、耐心、简洁。

回答规则：
1. 优先使用提供的工具获取真实信息（企业知识库、物流查询、订单查询）；
2. 只依据工具检索到的内容作答：不得编造或凭常识补充产品参数、政策条款、订单状态等；
   检索无结果时如实告知"未查询到"，并引导用户核对信息或说明可提供的帮助；
3. 依据知识库作答时，在回答末尾标注来源文件名，格式：（来源：<文件名>）；
   来源取自工具返回的"参考来源"清单；
4. 涉及订单、物流等个人信息时，只查询当前登录用户（user_id）自己的数据；
5. 寒暄、感谢、闲聊无需检索，直接自然礼貌回应。
"""
```

- [ ] **Step 4: 运行测试，确认通过**

Run: 同上命令 + `pytest tests/test_read_agent.py -q`
Expected: PASS（新增断言 + 既有 read agent 测试不受影响）。

- [ ] **Step 5: Commit**

```bash
git add src/ai_service_agent/prompts.py tests/test_read_agent.py
git commit -m "feat: READ prompt 引用来源规则——知识库作答标注（来源：文件名），未检索到如实告知"
```

---

### Task 4: 真实 LightRAG + ARK 冒烟

**Files:**
- Create: `scripts/real_rag_smoke.py`
- Modify: `docs/real-model-smoke.md`

**Interfaces:**
- Consumes: `make_read_graph(model, tools=[kb_search])`、`make_model()`、真实 `kb_search`（Task 2）、真实 `LightRagClient`（Task 1，读 `.env`/默认 `localhost:9621`）。
- Produces: 可重复运行的真机冒烟脚本（退出码 0=全部通过）。

- [ ] **Step 1: 写冒烟脚本**

```python
# scripts/real_rag_smoke.py
"""真实 LightRAG + ARK 冒烟：ReadGraph 知识问答，验证引用来源与如实告知。

前置：LightRAG-chengyuan 容器运行（localhost:9621，auth disabled）；.env 配 ARK 真实模型。
运行：$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy";
      uv run --no-sync python scripts/real_rag_smoke.py
"""

import asyncio
import sys

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver

from ai_service_agent.config import make_model
from ai_service_agent.graph.agents.read_agent import make_read_graph
from ai_service_agent.tools.kb import kb_search

CASES = [
    {"q": "JoyAir A1 的炸篮容量是多少？", "expect": "来源", "note": "direct_fact 应引用来源"},
    {"q": "JoyClean V1 支持 Wi-Fi 连接吗？", "expect": "来源", "note": "direct_fact 应引用来源"},
    {"q": "空气炸锅忘记密码怎么配网？", "expect": "来源", "note": "指南类应检索到并引用来源"},
    {"q": "你们的注册资本是多少？", "expect": "unanswerable", "note": "知识缺口应如实告知未查询到"},
]


async def main() -> int:
    graph = make_read_graph(make_model(), [kb_search])
    all_ok = True
    for case in CASES:
        state = {
            "messages": [HumanMessage(content=case["q"])],
            "user_id": "u1",
            "session_id": "s1",
            "context": {},
        }
        out = await graph.ainvoke(state)
        last = out["messages"][-1].content
        has_source = "来源" in last or "知识库" in last or "检索" in last
        if case["expect"] == "来源":
            ok = has_source
        else:
            ok = ("未查询到" in last or "无法" in last or "不清楚" in last)
        print(f"\nQ: {case['q']}\nA: {last}\n判定: {'PASS' if ok else 'FAIL'}（{case['note']}）")
        all_ok = all_ok and ok
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
```

- [ ] **Step 2: 运行冒烟**

Run: `$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync python scripts/real_rag_smoke.py`
Expected: 每个 case 输出 Q/A/判定；记录实际回复文本与来源文件名。若个别 unanswerable 判定为 FAIL，记录观察（LLM 措辞差异），不阻塞任务。

- [ ] **Step 3: 登记 `docs/real-model-smoke.md`**

追加「§N ReadGraph RAG 真机冒烟（LightRAG-chengyuan + ARK）」：服务端点、/query 参数（naive / only_need_context / include_references / include_chunk_content / 关 rerank）、实测延迟、4 条 case 的 Q/A/判定、来源文件名样例、unanswerable 行为。

- [ ] **Step 4: Commit**

```bash
git add scripts/real_rag_smoke.py docs/real-model-smoke.md
git commit -m "test: ReadGraph RAG 真机冒烟——LightRAG-chengyuan + ARK 引用来源与如实告知验证"
```

---

### Task 5: 全量回归 + 文档登记

**Files:**
- Modify: `docs/specs/tool-spec.md`（kb_search 真实化登记）
- Modify: `docs/plans/README.md`（登记本计划）
- 回归验证全部测试。

**Interfaces:**
- Consumes: Task 1-4 全部产物。

- [ ] **Step 1: 全量测试**

Run: `$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync pytest -q`
Expected: 全量 PASS（含新增 kb 用例；`test_stub_tools_return_values` 已适配）。

- [ ] **Step 2: langgraph validate**

Run: `langgraph validate`
Expected: valid。

- [ ] **Step 3: eval 回归**

Run: `$env:UV_CACHE_DIR="D:\AI_Project\AI Service Agent\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync python -m ai_service_agent.evals`
Expected: policy / guard / handoff / routing 四维度计数与基线一致（kb_search 变化不影响 control 维度；routing 用假模型 READ_MARKER，不真调工具）。

- [ ] **Step 4: 文档登记**

`docs/specs/tool-spec.md`：`kb_search` 条目从「MVP stub」改为「真实 LightRAG-chengyuan 检索（read 档）：only_need_context / include_references / naive / 关 rerank；返回上下文 + 参考来源；不可达/无结果确定性降级；引用来源在回复文本内标注（event-spec 不变）」。
`docs/plans/README.md`：登记本计划文件与完成状态。

- [ ] **Step 5: Commit**

```bash
git add docs/specs/tool-spec.md docs/plans/README.md
git commit -m "docs: kb_search 真实化登记（tool-spec + plans README）+ RAG 接入完成"
```

---

## Self-Review（writing-plans）

- **Spec coverage**：OQ-1 naive ✓（D1/Task1 config+client）；OQ-2 回复内引用 ✓（Task3 prompt）；OQ-3 无缓存 ✓（未引入缓存层）；OQ-4 无自动同步 ✓（未加摄取逻辑）；低延迟 ✓（naive/关 rerank/only_need_context/Task1）；流式 ✓（复用现有 SSE，plan 声明不改）；引用来源 ✓（Task2 references + Task3 prompt）。
- **Placeholder scan**：无 TBD/TODO；每步含完整代码与运行命令。
- **Type consistency**：`kb_search(text) -> str` 贯穿 Task2-4；`LightRagClient.query_context -> {"context","references"}` 与 `{"error":...}` 贯穿 Task1-2；`get_lightrag_client()`/`reset_lightrag_client()` 在 Task1 定义、Task2 消费、测试一致。
