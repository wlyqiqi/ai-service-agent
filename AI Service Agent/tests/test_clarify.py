"""Clarify 机制（P4 D1）：select 参数完备性触发，普通轮非 interrupt。"""

import asyncio
import importlib.util
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langchain_core.messages import HumanMessage

from ai_service_agent.config import get_settings
from ai_service_agent.domain.orders import SQLiteOrderRepository
from ai_service_agent.graph.builder import build_graph
from ai_service_agent.graph.workflows.copy import clarify_missing_text
from ai_service_agent.graph.workflows.copy import clarify_fallback_text
from ai_service_agent.graph.workflows.select import make_select_node
from ai_service_agent.harness.streaming import event_stream
from ai_service_agent.testing import ToolCallingFakeModel, router_json_message, select_json_message
from ai_service_agent.tools import DEFAULT_TOOL_REGISTRY
from ai_service_agent.tools.kb import kb_search
from conftest import ainvoke_graph


ROOT = Path(__file__).resolve().parents[1]


def _load_seed_module():
    spec = importlib.util.spec_from_file_location(
        "seed_mock_db_clarify", ROOT / "scripts" / "seed_mock_db.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def isolated_mock_db(monkeypatch, tmp_path):
    module = _load_seed_module()
    db_path = tmp_path / "mock_ecommerce.db"
    module.build_database(str(db_path))
    from ai_service_agent.tools import orders as orders_mod, write as write_mod
    from ai_service_agent.graph import builder as builder_mod

    class _Settings:
        mock_ecommerce_db_path = str(db_path)

    monkeypatch.setattr(orders_mod, "get_settings", lambda: _Settings())
    monkeypatch.setattr(write_mod, "get_settings", lambda: _Settings())
    monkeypatch.setattr(builder_mod, "SQLiteOrderRepository", lambda path: SQLiteOrderRepository(str(db_path)))


def _graph(model):
    return build_graph(model, tools=[kb_search], checkpointer=MemorySaver())


def _invoke(graph, text, thread_id, user_id="u1", context=None):
    return ainvoke_graph(graph,
        {"messages": [HumanMessage(content=text)], "user_id": user_id, "session_id": thread_id, "context": context or {}},
        {"configurable": {"thread_id": thread_id}},
    )


def _highrisk_model(args: dict):
    """refund_apply 属 high_risk 档，须走 high_risk 图（mutation 图里它会 unknown_tool）。"""
    return ToolCallingFakeModel(responses=[
        router_json_message("refund_apply_execute", 0.9),
        select_json_message("refund_apply", args),
    ])


def test_clarify_text_maps_labels():
    assert "订单号" in clarify_missing_text(["order_id"])
    assert "订单号" in clarify_missing_text(["order_id", "amount"])
    assert "金额" in clarify_missing_text(["order_id", "amount"])
    assert "order_no" in clarify_missing_text(["order_no"])  # 未映射字段回落原字段名


def test_config_clarify_max_turns_default():
    assert get_settings().clarify_max_turns == 2


@pytest.mark.asyncio
async def test_select_incomplete_mutation_sets_pending_tool_for_form():
    """T7: mutation select 缺参 -> pending_tool（供 Form Interrupt），不再 clarify_missing。"""
    model = ToolCallingFakeModel(responses=[select_json_message("update_address", {})])
    node = make_select_node(model, "mutation", DEFAULT_TOOL_REGISTRY.for_risk_level("mutation"), DEFAULT_TOOL_REGISTRY)
    out = await node({"messages": [HumanMessage(content="改一下收货地址")], "context": {"clarify_count": 1}})
    assert "clarify_missing" not in out["context"]
    pending = out["context"]["pending_tool"]
    assert pending["tool"] == "update_address"
    assert pending["request_id"]
    assert out["context"]["clarify_count"] == 0


@pytest.mark.asyncio
async def test_select_invoice_placeholders_mutation_pending_not_invalid_args():
    """T7: create_invoice 空串/amount=0 占位 -> mutation pending_tool，不 safety_rejected。"""
    model = ToolCallingFakeModel(responses=[select_json_message(
        "create_invoice",
        {"order_id": "", "amount": 0, "company": "", "tax_no": ""},
    )])
    node = make_select_node(model, "mutation", DEFAULT_TOOL_REGISTRY.for_risk_level("mutation"), DEFAULT_TOOL_REGISTRY)
    out = await node({"messages": [HumanMessage(content="我要开发票")], "context": {}})
    assert "clarify_missing" not in out["context"]
    assert out["context"]["pending_tool"]["tool"] == "create_invoice"
    assert out["context"].get("precheck_reason") is None


@pytest.mark.asyncio
async def test_select_invoice_non_positive_amount_mutation_keeps_other_prefills():
    """T7: 仅金额占位为 0 时，其余字段进 pending_tool prefills，缺 amount 由表单补。"""
    model = ToolCallingFakeModel(responses=[select_json_message(
        "create_invoice",
        {
            "order_id": "ORD-PAID-1",
            "amount": 0,
            "company": "星辰科技有限公司",
            "tax_no": "91110108MA01AB12CD",
        },
    )])
    node = make_select_node(model, "mutation", DEFAULT_TOOL_REGISTRY.for_risk_level("mutation"), DEFAULT_TOOL_REGISTRY)
    out = await node({"messages": [HumanMessage(content="帮我开票")], "context": {}})
    assert "clarify_missing" not in out["context"]
    args = out["context"]["pending_tool"]["args"]
    assert args["order_id"] == "ORD-PAID-1"
    assert args["company"] == "星辰科技有限公司"
    assert args["tax_no"] == "91110108MA01AB12CD"
    assert not args.get("amount")


@pytest.mark.asyncio
async def test_select_invoice_bad_amount_type_still_invalid_args():
    """真类型错误仍拒绝，不误判为缺参澄清。"""
    model = ToolCallingFakeModel(responses=[select_json_message(
        "create_invoice",
        {
            "order_id": "ORD-PAID-1",
            "amount": "abc",
            "company": "星辰科技有限公司",
            "tax_no": "91110108MA01AB12CD",
        },
    )])
    node = make_select_node(model, "mutation", DEFAULT_TOOL_REGISTRY.for_risk_level("mutation"), DEFAULT_TOOL_REGISTRY)
    out = await node({"messages": [HumanMessage(content="帮我开票")], "context": {}})
    assert out["context"].get("precheck_reason") == "invalid_args"
    assert "clarify_missing" not in out["context"]


@pytest.mark.asyncio
async def test_select_complete_resets_clarify_count():
    model = ToolCallingFakeModel(responses=[select_json_message("update_address", {"order_id": "ORD-PAID-1", "address": "北京市朝阳区"})])
    node = make_select_node(model, "mutation", DEFAULT_TOOL_REGISTRY.for_risk_level("mutation"), DEFAULT_TOOL_REGISTRY)
    out = await node({"messages": [HumanMessage(content="改地址")], "context": {"clarify_count": 1}})
    assert out["context"]["clarify_count"] == 0  # 完整 select 清零（D1）
    assert out["context"]["pending_tool"]["tool"] == "update_address"
    assert "clarify_missing" not in out["context"]


def test_highrisk_missing_required_clarifies():
    out = _invoke(_graph(_highrisk_model({})), "退了吧", "c1", user_id="user-with-no-paid-order")
    assert not out.get("__interrupt__")
    assert out["execution_status"] == "clarifying"
    assert out["context"]["clarify_missing"] == ["order_id"]
    assert "订单号" in out["messages"][-1].content


def test_highrisk_empty_required_clarifies():
    """pydantic 接受 order_id=''，须显式判空走 clarify。"""
    out = _invoke(_graph(_highrisk_model({"order_id": ""})), "退了吧", "c2", user_id="user-with-no-paid-order")
    assert out["execution_status"] == "clarifying"
    assert out["context"]["clarify_missing"] == ["order_id"]


def test_mutation_empty_args_opens_form_interrupt():
    """T7: mutation 空参 select（tool 已知）-> Form Interrupt，不再聊天缺参澄清。"""
    model = ToolCallingFakeModel(responses=[
        router_json_message("account_profile_update", 0.9),
        select_json_message("update_address", {}),
    ])
    out = _invoke(_graph(model), "改一下收货地址", "c3")
    assert out.get("__interrupt__")
    assert out["execution_status"] != "clarifying"
    assert "clarify_missing" not in out.get("context", {})
    assert out["__interrupt__"][0].value.get("tool") == "update_address"


def test_clarify_over_limit_falls_back():
    out = _invoke(_graph(_highrisk_model({})), "退了吧", "c4", user_id="user-with-no-paid-order", context={"clarify_count": 2})
    assert out["execution_status"] == "completed"
    assert clarify_fallback_text() in out["messages"][-1].content


async def _acollect(gen):
    return [e async for e in gen]


def test_clarify_streams_event_and_done():
    events = asyncio.run(_acollect(event_stream(
        _graph(_highrisk_model({})),
        {"configurable": {"thread_id": "c6"}},
        [HumanMessage(content="退了吧")],
        extra_state={"user_id": "user-with-no-paid-order"},
    )))
    types = [e["type"] for e in events]
    assert "clarification_required" in types
    assert types[-1] == "done"


def _build(model, saver):
    return build_graph(model, tools=[kb_search], checkpointer=saver)


def _turn(graph, text, thread_id, user_id="u1"):
    # 续接轮不传 context：context 由 checkpoint 保留（真实 /chat 入口同语义），仅追加新消息
    return ainvoke_graph(graph,
        {"messages": [HumanMessage(content=text)], "user_id": user_id, "session_id": thread_id},
        {"configurable": {"thread_id": thread_id}},
    )


def test_multi_turn_form_then_confirm_enters_highrisk():
    """有可退款订单时缺 order_id 开 Form；Confirm 后进入审批，审批前不写库。"""
    from langgraph.types import Command

    saver = MemorySaver()
    graph = _build(_highrisk_model({}), saver)
    t1 = _turn(graph, "退了吧", "c7", user_id="u0084")
    assert t1.get("__interrupt__")
    assert t1["__interrupt__"][0].value.get("tool") == "refund_apply"
    assert not (t1["__interrupt__"][0].value.get("prefills") or {}).get("order_id")

    t2 = ainvoke_graph(
        graph,
        Command(resume={"action": "confirm", "order_id": "ORD-2026-10015"}),
        {"configurable": {"thread_id": "c7"}},
    )
    interrupts = [it.value for it in t2.get("__interrupt__", [])]
    approval = next((v for v in interrupts if v.get("type") == "approval_required"), None)
    assert approval is not None
    assert "ORD-2026-10015" in approval["summary"]
    assert not any(getattr(m, "type", "") == "tool" for m in t2["messages"])


def test_multi_turn_clarify_over_limit_falls_back():
    """0076 跨 turn：连续 3 轮不补齐 -> 超过 clarify_max_turns=2 -> 确定性 fallback，绝不默认执行。"""
    saver = MemorySaver()
    r1 = _turn(_build(_highrisk_model({}), saver), "退了吧", "c8", user_id="user-with-no-paid-order")
    assert r1["execution_status"] == "clarifying" and r1["context"]["clarify_count"] == 1
    r2 = _turn(_build(_highrisk_model({}), saver), "还是没有订单号", "c8", user_id="user-with-no-paid-order")
    assert r2["execution_status"] == "clarifying" and r2["context"]["clarify_count"] == 2
    r3 = _turn(_build(_highrisk_model({}), saver), "那就这样吧", "c8", user_id="user-with-no-paid-order")
    assert r3["execution_status"] == "completed"
    assert clarify_fallback_text() in r3["messages"][-1].content
    assert not r3.get("__interrupt__")


def _tier_model(tool_name, risk):
    return ToolCallingFakeModel(responses=[
        router_json_message("cancel_order_execute" if risk == "high_risk" else "order_mutation", 0.9),
        select_json_message(tool_name, {}),
    ])


def test_highrisk_read_tool_selected_rejected_by_policy():
    """B2b：route_policy 限定 high_risk 候选后，只读工具漂移被安全拒绝，不进审批。"""
    out = _invoke(_graph(_tier_model("order_lookup", "high_risk")), "退了吧", "b2-h1")
    assert out["execution_status"] == "completed"
    assert not out.get("__interrupt__")
    assert out["context"].get("precheck_reason") == "tool_not_allowed"
    assert "pending_tool" not in out["context"]


def test_mutation_read_tool_selected_rejected_by_policy():
    """B2b：route_policy 限定 mutation 候选后，只读工具漂移被安全拒绝。"""
    out = _invoke(_graph(_tier_model("kb_search", "mutation")), "先查一下再改", "b2-m1")
    assert out["execution_status"] == "completed"
    assert out["context"].get("precheck_reason") == "tool_not_allowed"
    assert not out.get("__interrupt__")


def test_policy_tool_boundary_streams_rejection_event():
    """B2b：事件契约——route_policy 越界候选发 safety_rejected，末事件 done。"""
    events = asyncio.run(_acollect(event_stream(
        _graph(_tier_model("order_lookup", "high_risk")),
        {"configurable": {"thread_id": "b2-s1"}},
        [HumanMessage(content="退了吧")],
    )))
    types = [e["type"] for e in events]
    assert "safety_rejected" in types
    rejection = next(e for e in events if e["type"] == "safety_rejected")
    assert rejection["data"].get("reason") == "tool_not_allowed"
    assert types[-1] == "done"


def test_clarify_labels_invoice_fields():
    """2026-09-17 工具集定案：create_invoice 必填字段标签可读，update_order_note 字段 note 标签。"""
    labels_text = clarify_missing_text(["order_id", "amount", "company", "tax_no", "note"])
    assert "订单号" in labels_text
    assert "金额" in labels_text
    assert "公司名" in labels_text
    assert "税号" in labels_text
    assert "订单备注" in labels_text
