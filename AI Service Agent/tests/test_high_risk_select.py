"""HighRisk Select seam (CONTEXT.md / ADR 0005): dedicated node, prompts, graph name."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, call

from langchain_core.messages import AIMessage, HumanMessage

from ai_service_agent.graph.builder import build_graph
from ai_service_agent.prompts import (
    HIGH_RISK_SELECT_BINDTOOLS_TEMPLATE,
    HIGH_RISK_SELECT_SYSTEM_TEMPLATE,
)
from ai_service_agent.testing import make_fake_model, select_json_message
from ai_service_agent.tools import make_tool_registry
from ai_service_agent.tools.registry import ToolMeta, ToolRegistry
from ai_service_agent.tools.write import CancelOrderArgs


def _select_result(coro):
    return asyncio.run(coro)


def test_high_risk_select_prompts_are_high_risk_only():
    """Dedicated prompts: cancel/refund only; no Mutation tools / inline Operation Clarify."""
    for template in (HIGH_RISK_SELECT_SYSTEM_TEMPLATE, HIGH_RISK_SELECT_BINDTOOLS_TEMPLATE):
        assert "高风险操作选择器" in template
        assert "cancel_order" in template
        assert "refund_apply" in template
        assert "create_service_ticket" not in template
        assert "update_address" not in template
        assert "update_phone" not in template
        assert "create_invoice" not in template
        assert "operation_unclear" not in template
        assert "tool\": null" not in template and "tool\":null" not in template

    assert "只输出 JSON" in HIGH_RISK_SELECT_SYSTEM_TEMPLATE
    assert "只输出 JSON" not in HIGH_RISK_SELECT_BINDTOOLS_TEMPLATE
    assert "不要发起 tool_call" in HIGH_RISK_SELECT_BINDTOOLS_TEMPLATE
    assert "不要用纯文本追问" in HIGH_RISK_SELECT_BINDTOOLS_TEMPLATE
    assert "示例仅说明意图与参数" in HIGH_RISK_SELECT_BINDTOOLS_TEMPLATE
    assert '{"tool":' not in HIGH_RISK_SELECT_BINDTOOLS_TEMPLATE


def test_make_high_risk_select_node_has_no_tier_parameter():
    from ai_service_agent.graph.workflows.high_risk_select import make_high_risk_select_node
    import inspect

    params = inspect.signature(make_high_risk_select_node).parameters
    assert "tier" not in params


def test_high_risk_graph_node_is_high_risk_select_llm():
    from ai_service_agent.tools.kb import kb_search

    graph = build_graph(make_fake_model(), tools=[kb_search])
    high_risk_graph = graph.nodes["high_risk_graph"].bound
    assert "high_risk_select_llm" in high_risk_graph.nodes
    assert "select_llm" not in high_risk_graph.nodes
    mutation_graph = graph.nodes["mutation_graph"].bound
    assert "mutation_select_llm" in mutation_graph.nodes
    assert "high_risk_select_llm" not in mutation_graph.nodes


def test_high_risk_select_fake_empty_tool_writes_tier_clarify():
    from ai_service_agent.graph.workflows.high_risk_select import make_high_risk_select_node
    from ai_service_agent.testing import ToolCallingFakeModel
    from unittest.mock import MagicMock

    registry = make_tool_registry(MagicMock())
    model = ToolCallingFakeModel(
        responses=[AIMessage(content='{"tool": "", "args": {}}')],
    )
    node = make_high_risk_select_node(
        model,
        registry.for_risk_level("high_risk"),
        registry,
    )
    output = _select_result(
        node({"messages": [HumanMessage(content="帮我处理一下")], "context": {}})
    )
    assert output["context"].get("tier_clarify") is True
    assert "pending_tool" not in output["context"]
    assert "messages" not in output


def test_high_risk_select_bindtools_uses_dedicated_template():
    from ai_service_agent.graph.workflows.high_risk_select import make_high_risk_select_node

    bound_model = AsyncMock()
    bound_model.ainvoke.return_value = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "cancel_order",
                "args": {"order_id": "ORD-10086", "reason": "不想要了"},
                "id": "call_1",
            }
        ],
    )
    model = MagicMock()
    model.bind_tools.return_value = bound_model
    tool = MagicMock()
    tool.name = "cancel_order"
    registry = ToolRegistry()
    registry.register(
        ToolMeta(
            name="cancel_order",
            description="取消订单",
            min_risk_level="high_risk",
            schema=CancelOrderArgs,
        ),
        tool,
    )
    node = make_high_risk_select_node(model, [tool], registry)
    output = _select_result(
        node(
            {
                "messages": [
                    HumanMessage(content="订单 ORD-10086 不要了，帮我取消")
                ],
                "user_id": "u1",
                "context": {},
            }
        )
    )
    assert model.bind_tools.call_args == call([tool], tool_choice="auto")
    system = bound_model.ainvoke.await_args.args[0][0].content
    assert "高风险操作选择器" in system
    assert "cancel_order" in system
    assert "示例仅说明意图与参数" in system
    assert '{"tool":' not in system
    assert "发起 tool_call：" in system
    assert output["context"]["pending_tool"]["tool"] == "cancel_order"


def test_high_risk_select_uses_system_template_for_fake():
    from ai_service_agent.graph.workflows.high_risk_select import make_high_risk_select_node
    from ai_service_agent.testing import ToolCallingFakeModel
    from unittest.mock import MagicMock

    class Recording(ToolCallingFakeModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            object.__setattr__(
                self,
                "seen_system",
                next(
                    (m.content for m in messages if getattr(m, "type", "") == "system"),
                    "",
                ),
            )
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    registry = make_tool_registry(MagicMock())
    model = Recording(
        responses=[
            select_json_message(
                "cancel_order",
                {"order_id": "ORD-10086", "reason": "不想要了"},
            )
        ]
    )
    node = make_high_risk_select_node(
        model,
        registry.for_risk_level("high_risk"),
        registry,
    )
    _select_result(
        node(
            {
                "messages": [
                    HumanMessage(content="订单 ORD-10086 不要了，帮我取消")
                ],
                "user_id": "u1",
                "context": {},
            }
        )
    )
    assert "高风险操作选择器" in model.seen_system
    assert "只输出 JSON" in model.seen_system


# --- 澄清标记互斥（2026-10-29 实测：明确说「取消订单」却一直回「无法确定」） -----


def test_clarify_clears_stale_tier_flag_when_missing_order_id():
    """先无 tool_call（写 tier_clarify），后发 tool_call 缺单号：不得残留 tier 标记。"""
    from ai_service_agent.graph.workflows.high_risk_select import make_high_risk_select_node
    from ai_service_agent.testing import ToolCallingFakeModel

    registry = make_tool_registry(MagicMock())
    model = ToolCallingFakeModel(
        responses=[AIMessage(content='{"tool": "cancel_order", "args": {}}')],
    )
    repository = MagicMock()
    repository.list_actionable_orders.return_value = []
    node = make_high_risk_select_node(
        model,
        registry.for_risk_level("high_risk"),
        registry,
        order_repository=repository,
    )

    output = _select_result(
        node(
            {
                "messages": [HumanMessage(content="取消订单")],
                "user_id": "u1",
                # 上一轮模型没发 tool_call 留下的标记
                "context": {"tier_clarify": True},
            }
        )
    )

    ctx = output["context"]
    assert "tier_clarify" not in ctx
    assert ctx["clarify_missing"] == ["order_id"]
    # 查过了没有可取消订单：要说「没有符合条件的订单」，不是「请补充订单号」
    assert ctx["cancellation_candidates"] == []


def test_tier_clarify_clears_stale_missing():
    from ai_service_agent.graph.workflows.high_risk_select import make_high_risk_select_node
    from ai_service_agent.testing import ToolCallingFakeModel

    registry = make_tool_registry(MagicMock())
    model = ToolCallingFakeModel(responses=[AIMessage(content='{"tool": "", "args": {}}')])
    node = make_high_risk_select_node(
        model,
        registry.for_risk_level("high_risk"),
        registry,
    )

    output = _select_result(
        node(
            {
                "messages": [HumanMessage(content="帮我处理一下")],
                "context": {"clarify_missing": ["order_id"], "cancellation_candidates": []},
            }
        )
    )

    ctx = output["context"]
    assert ctx["tier_clarify"] is True
    assert "clarify_missing" not in ctx
    assert "cancellation_candidates" not in ctx


def test_clarify_node_prefers_missing_over_stale_tier_flag(monkeypatch):
    """两个标记都残留时，按「没有可取消订单」说话，而不是「无法确定要执行哪一项」。"""
    from ai_service_agent.graph.workflows import clarify as clarify_module
    from ai_service_agent.graph.workflows.copy import clarify_no_candidates_text

    monkeypatch.setattr(clarify_module, "get_stream_writer", lambda: (lambda _ev: None))
    node = clarify_module.make_clarify_node()

    output = node(
        {
            "context": {
                "tier_clarify": True,
                "clarify_missing": ["order_id"],
                "cancellation_candidates": [],
                "clarify_count": 0,
            }
        }
    )

    assert output["messages"][0].content == clarify_no_candidates_text()
    assert "无法确定" not in output["messages"][0].content


def test_no_tool_call_falls_back_to_unique_keyword():
    """模型没发 tool_call（自己判断"这单不该执行"）时，按关键词兜底而不是澄清。"""
    from ai_service_agent.graph.workflows.high_risk_select import make_high_risk_select_node
    from ai_service_agent.testing import ToolCallingFakeModel

    registry = make_tool_registry(MagicMock())
    model = ToolCallingFakeModel(responses=[AIMessage(content='{"tool": "", "args": {}}')])
    repository = MagicMock()
    repository.list_actionable_orders.return_value = []
    node = make_high_risk_select_node(
        model,
        registry.for_risk_level("high_risk"),
        registry,
        order_repository=repository,
    )

    output = _select_result(
        node({"messages": [HumanMessage(content="取消订单")], "user_id": "u1", "context": {}})
    )

    ctx = output["context"]
    assert "tier_clarify" not in ctx
    assert ctx["clarify_missing"] == ["order_id"]
    assert ctx["cancellation_candidates"] == []


def test_ambiguous_keywords_still_clarify():
    """一句话同时点了取消和退款，仍交给澄清（不猜）。"""
    from ai_service_agent.graph.workflows.high_risk_select import make_high_risk_select_node
    from ai_service_agent.testing import ToolCallingFakeModel

    registry = make_tool_registry(MagicMock())
    model = ToolCallingFakeModel(responses=[AIMessage(content='{"tool": "", "args": {}}')])
    node = make_high_risk_select_node(
        model,
        registry.for_risk_level("high_risk"),
        registry,
        order_repository=MagicMock(),
    )

    output = _select_result(
        node(
            {
                "messages": [HumanMessage(content="帮我取消订单然后退款")],
                "user_id": "u1",
                "context": {},
            }
        )
    )

    assert output["context"]["tier_clarify"] is True


def test_negated_keyword_does_not_force_a_tool():
    from ai_service_agent.graph.workflows.high_risk_select import unique_high_risk_tool

    assert unique_high_risk_tool("不要取消订单", ["cancel_order"]) is None
    assert unique_high_risk_tool("先别退款", ["refund_apply"]) is None
    assert unique_high_risk_tool("取消订单", ["cancel_order"]) == "cancel_order"
    assert unique_high_risk_tool("我要退个款", ["refund_apply"]) == "refund_apply"
    # 工具不在本档允许集合里时不得兜底
    assert unique_high_risk_tool("取消订单", ["refund_apply"]) is None


def test_no_candidates_clarify_does_not_burn_clarify_budget(monkeypatch):
    """「没有可取消的已付款订单」是有效结果，不该被轮次上限换成"我没理解"。"""
    from ai_service_agent.graph.workflows import clarify as clarify_module
    from ai_service_agent.graph.workflows.copy import clarify_no_candidates_text

    monkeypatch.setattr(clarify_module, "get_stream_writer", lambda: (lambda _ev: None))
    node = clarify_module.make_clarify_node()

    output = node(
        {
            "context": {
                "clarify_missing": ["order_id"],
                "cancellation_candidates": [],
                "clarify_count": 3,  # 已经到上限
            }
        }
    )

    assert output["messages"][0].content == clarify_no_candidates_text()
    assert output["execution_status"] == "clarifying"
    assert output["context"]["clarify_count"] == 0


def test_missing_arg_clarify_still_counts_toward_limit(monkeypatch):
    """真正"缺参数"的澄清仍要计入轮次，否则会无限追问。"""
    from ai_service_agent.graph.workflows import clarify as clarify_module
    from ai_service_agent.graph.workflows.copy import clarify_missing_text

    monkeypatch.setattr(clarify_module, "get_stream_writer", lambda: (lambda _ev: None))
    node = clarify_module.make_clarify_node()

    output = node({"context": {"clarify_missing": ["order_id"], "clarify_count": 1}})

    assert output["messages"][0].content == clarify_missing_text(["order_id"])
    assert output["context"]["clarify_count"] == 2


def test_no_candidates_clarify_does_not_burn_clarify_budget(monkeypatch):
    """「没有可取消的已付款订单」是有效结果，不该被轮次上限换成"我没理解"。"""
    from ai_service_agent.graph.workflows import clarify as clarify_module
    from ai_service_agent.graph.workflows.copy import clarify_no_candidates_text

    monkeypatch.setattr(clarify_module, "get_stream_writer", lambda: (lambda _ev: None))
    node = clarify_module.make_clarify_node()

    output = node(
        {
            "context": {
                "clarify_missing": ["order_id"],
                "cancellation_candidates": [],
                "clarify_count": 3,  # 已经到上限
            }
        }
    )

    assert output["messages"][0].content == clarify_no_candidates_text()
    assert output["execution_status"] == "clarifying"
    assert output["context"]["clarify_count"] == 0


def test_missing_arg_clarify_still_counts_toward_limit(monkeypatch):
    """真正"缺参数"的澄清仍要计入轮次，否则会无限追问。"""
    from ai_service_agent.graph.workflows import clarify as clarify_module
    from ai_service_agent.graph.workflows.copy import clarify_missing_text

    monkeypatch.setattr(clarify_module, "get_stream_writer", lambda: (lambda _ev: None))
    node = clarify_module.make_clarify_node()

    output = node({"context": {"clarify_missing": ["order_id"], "clarify_count": 1}})

    assert output["messages"][0].content == clarify_missing_text(["order_id"])
    assert output["context"]["clarify_count"] == 2
