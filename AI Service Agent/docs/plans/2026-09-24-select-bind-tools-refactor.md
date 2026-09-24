# Select 节点 bind_tools 重构 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 Select 节点从 json_mode prompt 约束改为 LangChain 原生 `bind_tools + tool_choice="any"` function calling，使 LLM 输出的 tool name 由 API schema 强制为英文 ID，消除中文描述问题。

**Architecture:** 真实模型走 `model.bind_tools(tier_tools, tool_choice="any")` → 解析 `AIMessage.tool_calls[0]["name"]`（API 级约束保证英文 ID）；fake 模型保持现有 json_mode 路径不变。下游校验（allowed/schema/clarify/reject）完全不动。

**Tech Stack:** LangChain `bind_tools` / `tool_choice` / `AIMessage.tool_calls`；LangGraph select 节点。

**Spec:** `docs/specs/graph-spec.md` §3（select 节点）；`docs/specs/tool-spec.md` §1（工具分层注入）。

## Global Constraints

- Python 3.14 / langchain-core 1.5.3 / langgraph 1.2.10
- 离线测试不得依赖外部服务（AGENTS.md §15）
- 真实模型验证需 `.env` 配置 ARK_API_KEY（测试前确认存在）
- 不修改 Router / Policy Gate / harness / web
- 下游确定性校验逻辑（allowed check / args schema / clarify / reject）不得改动

---

### Task 1: 重构 select 节点绑定与解析

**Files:**
- Modify: `src/ai_service_agent/graph/workflows/select.py`
- Modify: `src/ai_service_agent/prompts.py`

**Interfaces:**
- Consumes: `ToolRegistry.for_risk_level()` → `list[BaseTool]`（LangChain 原生 `@tool` 对象，`.name` 为英文 ID）
- Produces: `make_select_node(model, tier, tools, registry)` 返回的 `select(state)` 函数签名不变

- [x] **Step 1: 写失败测试——验证真实模型路径使用 bind_tools**

在 `tests/test_select.py` 中新增：

```python
def test_select_real_model_uses_bind_tools(monkeypatch):
    """真实模型路径：select 使用 bind_tools + tool_choice，解析 tool_calls 而非 json_mode。"""
    from unittest.mock import MagicMock, patch
    from langchain_core.messages import AIMessage
    from ai_service_agent.graph.workflows.select import make_select_node
    from ai_service_agent.tools.registry import ToolRegistry, ToolMeta

    # 构造 mock real model（不是 FakeMessagesListChatModel）
    mock_model = MagicMock()
    mock_model.__class__ = type("RealModel", (), {})  # 不是 FakeMessagesListChatModel

    # 模拟 bind_tools 返回的响应
    mock_bound = MagicMock()
    mock_tool_call = {"name": "update_phone", "args": {"phone": "13800138000"}, "id": "call_1"}
    mock_bound.invoke.return_value = AIMessage(content="", tool_calls=[mock_tool_call])
    mock_model.bind_tools.return_value = mock_bound

    registry = ToolRegistry()
    registry.register(
        ToolMeta(name="update_phone", description="修改联系电话", min_risk_level="mutation"),
        MagicMock(name="update_phone_fn"),
    )

    node = make_select_node(mock_model, "mutation", [MagicMock()], registry)
    state = {
        "messages": [HumanMessage(content="把手机号改成13800138000")],
        "user_id": "u1",
        "context": {},
    }
    out = node(state)

    # 验证 bind_tools 被调用（而非 json_mode / with_structured_output）
    mock_model.bind_tools.assert_called_once()
    assert out["context"]["pending_tool"]["tool"] == "update_phone"
```

- [x] **Step 2: 运行测试验证失败**

```bash
uv run --no-sync pytest tests/test_select.py::test_select_real_model_uses_bind_tools -v
```
Expected: FAIL（`with_structured_output` 被调用而非 `bind_tools`）

- [x] **Step 3: 修改 select.py 绑定逻辑**

将 `make_select_node` 中的绑定和解析替换为：

```python
is_fake = isinstance(model, FakeMessagesListChatModel)
if not is_fake:
    bound = model.bind_tools(tier_tools, tool_choice="any")
else:
    bound = _bind_json(model)
```

在 `select()` 函数内，将消息构建和解析改为分路径：

```python
last = ""
for m in reversed(state.get("messages", [])):
    if getattr(m, "type", "") == "human":
        last = m.content if isinstance(m.content, str) else str(m.content)
        break

if is_fake:
    # fake 模型路径：保持现有 json_mode prompt（需要 tool_list 上下文）
    tool_list = "\n".join(lines_by_name[t.name] for t in selected_tools)
    messages = [
        SystemMessage(content=SELECT_SYSTEM_TEMPLATE.format(
            tool_list=tool_list,
            examples=_build_examples(selected_tools, registry),
        )),
        HumanMessage(content=last),
    ]
    resp = bound.invoke(messages)
    try:
        payload = json.loads(resp.content)
        tool_name = payload["tool"]
        args = dict(payload.get("args") or {})
    except Exception:
        return _reject(state, "select_parse_failed")
else:
    # 真实模型路径：bind_tools 已通过 function definitions 传递工具信息
    # prompt 只需说明意图提取规则，不需要列工具清单
    messages = [
        SystemMessage(content=SELECT_BINDTOOLS_SYSTEM),
        HumanMessage(content=last),
    ]
    resp = bound.invoke(messages)
    if not resp.tool_calls:
        return _reject(state, "select_parse_failed")
    tool_call = resp.tool_calls[0]
    tool_name = tool_call["name"]
    args = dict(tool_call.get("args") or {})
```

- [x] **Step 4: 在 prompts.py 新增 SELECT_BINDTOOLS_SYSTEM**

```python
SELECT_BINDTOOLS_SYSTEM = """你是客服操作选择器。

根据用户当前消息，从绑定的工具中选择一个最匹配的，并提取调用所需的参数。

规则：
1. 从绑定的工具中选择最匹配的一个（通过 tool_call 格式返回）。
2. 参数必须来自用户消息或对话上下文中已明确提供的信息。
3. 严禁编造、猜测或补全用户没有提供的信息。
4. 用户没有提供的必填参数不要虚构；后续由系统负责参数校验和澄清。
5. 保留用户提供的原始值，不要擅自修改或格式化。
6. 如果用户请求的操作不属于绑定的工具，选择最接近的工具；系统会校验并拒绝不匹配的请求。
"""
```

- [x] **Step 5: 删除 `_build_select_schema` 函数**

`select.py` 中删除以下代码块（不再需要）：

```python
def _build_select_schema(allowed_names: list[str]) -> type[BaseModel]:
    ...
```

同时清理顶部不再需要的 import：`Literal`、`BaseModel`、`Field`、`create_model`（如果只有 `_build_select_schema` 使用）。

- [x] **Step 6: 运行 Step 1 测试验证通过**

```bash
uv run --no-sync pytest tests/test_select.py::test_select_real_model_uses_bind_tools -v
```
Expected: PASS

- [x] **Step 7: 运行现有 select/mutation/highrisk 离线测试**

```bash
uv run --no-sync pytest tests/test_select.py tests/test_mutation_graph.py tests/test_high_risk_graph.py -q
```
Expected: 59 passed（fake 路径不变，全部通过）

- [x] **Step 8: 运行 allowed_tools prompt 测试**

```bash
uv run --no-sync pytest tests/test_allowed_tools_execution.py -q
```
Expected: 全过（真实模型路径不再包含 tool_list，prompt 泄漏问题自然消除）

- [x] **Step 9: Commit**

```bash
git add src/ai_service_agent/graph/workflows/select.py src/ai_service_agent/prompts.py tests/test_select.py
git commit -m "refactor: select node uses bind_tools + tool_choice for real models"
```

### Task 2: 全量回归 + 真实模型验证

**Files:**
- No new files

**Interfaces:**
- Consumes: Task 1 的 select 节点
- Produces: 验证报告

- [x] **Step 1: 全量离线回归**

```bash
uv run --no-sync pytest -q
```
Expected: 468+ passed, 0 failed

- [x] **Step 2: 真实模型 smoke 验证**

```bash
uv run --no-sync python scripts/real_model_http_smoke.py 2>&1 | Select-String 'hit_rate|selected_tool|### '
```
Expected:
- `hit_rate` ≥ 8/10（tool name 为英文 ID）
- `selected_tool` 全部为英文 ID（`update_phone` 而非 `联系电话修改`）
- 文本质量不变（语气由 agent 层控制）

- [x] **Step 3: 如果 hit_rate 仍低**

检查 ARK 模型是否支持 `tool_choice="any"`：
```python
# 临时调试：验证 resp.tool_calls 是否非空
print(f"tool_calls: {resp.tool_calls}")
```
如果 ARK 不支持 `tool_choice`，降级为 `tool_choice=None`（默认自由选择）——tool name 仍由 function definitions 约束。

- [x] **Step 4: Commit 验证报告**

```bash
git add docs/reports/
git commit -m "test: verify select bind_tools refactor with real model"
```

## Execution Record — 2026-09-25

- Task 1: completed by commits `6172455` and review fix `4bc48ec`; real models use native `bind_tools(..., tool_choice="any")`, while fake models retain JSON mode.
- Ruling: mutation confirmation summaries intentionally show human-readable Chinese labels. The smoke harness now reverse-maps those display labels to English tool IDs; display labels are not evidence that the model emitted Chinese tool names.
- Ruling: ToolMatrix hit cases must contain complete required arguments and use an owned seeded order, so the probe measures English tool selection rather than clarification or order-boundary rejection.
- Ruling: offline scripted tests disable JEV routing in-process to prevent repository `.env` from consuming scripted fake router responses.
- Task 2: real-model ToolMatrix result is 9/10, and every emitted tool selection is an English ID. The only miss did not enter select (router/read path). Non-ToolMatrix smoke failures remain operational concerns outside this plan's acceptance criteria.
- Regression: 473 passed after isolating the route-intent default test from repository `.env` (`a36a6c8`).
