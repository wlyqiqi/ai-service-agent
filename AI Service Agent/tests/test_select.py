"""select 节点字段契约（2026-09-15 真机冒烟修复）：系统提示必须携带 schema 字段清单。"""

from typing import Callable
from unittest.mock import MagicMock, call

from langchain_core.messages import AIMessage, HumanMessage

from ai_service_agent.graph.workflows.select import make_select_node
from ai_service_agent.testing import ToolCallingFakeModel, select_json_message
from ai_service_agent.tools import DEFAULT_TOOL_REGISTRY
from ai_service_agent.tools.registry import ToolMeta, ToolRegistry


class RecordingSelectModel(ToolCallingFakeModel):
    """记录最后一次系统提示（select 的 json_mode 契约提示）。"""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        object.__setattr__(self, "seen_system", next(
            (m.content for m in messages if getattr(m, "type", "") == "system"), ""
        ))
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def _select_node():
    model = RecordingSelectModel(responses=[
        select_json_message("update_address", {"address": "朝阳区"}),
    ])
    node = make_select_node(
        model, "mutation", DEFAULT_TOOL_REGISTRY.for_risk_level("mutation"), DEFAULT_TOOL_REGISTRY
    )
    return model, node


def test_select_system_template_carries_schema_field_contract():
    model, node = _select_node()
    node({"messages": [HumanMessage(content="改一下收货地址")], "context": {}})
    assert "参数：" in model.seen_system
    assert "address" in model.seen_system
    assert "phone" in model.seen_system


def test_select_system_template_forbids_extra_fields():
    model, node = _select_node()
    node({"messages": [HumanMessage(content="改一下收货地址")], "context": {}})
    assert "不要输出 schema 之外的字段" in model.seen_system


def test_select_system_template_has_example():
    model, node = _select_node()
    node({"messages": [HumanMessage(content="改一下收货地址")], "context": {}})
    assert "示例：" in model.seen_system
    assert "update_address" in model.seen_system


def test_select_system_template_tier_strict_no_read_tools():
    """B2b：写图 select 系统提示只列该档写工具，只读工具不进入允许集。"""
    model, node = _select_node()
    node({"messages": [HumanMessage(content="改一下收货地址")], "context": {}})
    assert "update_address" in model.seen_system
    assert "kb_search" not in model.seen_system
    assert "order_lookup" not in model.seen_system


def test_select_system_template_carries_new_tools_and_schema():
    model, node = _select_node()
    node({"messages": [HumanMessage(content="改一下收货地址")], "context": {}})
    assert "update_address" in model.seen_system
    assert "create_invoice" in model.seen_system
    assert "address" in model.seen_system
    assert "company" in model.seen_system
    assert "account_update" not in model.seen_system


def test_select_system_template_forbids_extra_fields_and_fabrication():
    model, node = _select_node()
    node({"messages": [HumanMessage(content="改一下收货地址")], "context": {}})
    assert "不要输出 schema 之外的字段" in model.seen_system
    assert "严禁编造" in model.seen_system


def test_select_system_template_examples_match_tier():
    model, node = _select_node()
    node({"messages": [HumanMessage(content="改一下收货地址")], "context": {}})
    assert '"tool": "update_address"' in model.seen_system
    assert '"tool": "refund_apply"' not in model.seen_system   # 跨档示例不注入
    assert "参数不完整" in model.seen_system                     # 缺参示例


def test_select_real_model_uses_bind_tools():
    bound_model = MagicMock()
    bound_model.invoke.return_value = AIMessage(
        content="",
        tool_calls=[{"name": "update_phone", "args": {"phone": "13800138000"}, "id": "call_1"}],
    )
    model = MagicMock()
    model.bind_tools.return_value = bound_model

    node, tool = _real_model_node(model)
    output = node({
        "messages": [HumanMessage(content="把手机号改成13800138000")],
        "user_id": "u1",
        "context": {},
    })

    assert model.bind_tools.call_args == call([tool], tool_choice="any")
    assert output["context"]["pending_tool"]["tool"] == "update_phone"


def _real_model_node(model) -> tuple[Callable[[dict], dict], MagicMock]:
    tool = MagicMock()
    tool.name = "update_phone"
    registry = ToolRegistry()
    registry.register(
        ToolMeta(name="update_phone", description="修改联系电话", min_risk_level="mutation"),
        tool,
    )
    return make_select_node(model, "mutation", [tool], registry), tool


def test_select_real_model_rejects_missing_tool_calls():
    bound_model = MagicMock()
    bound_model.invoke.return_value = AIMessage(content="", tool_calls=[])
    model = MagicMock()
    model.bind_tools.return_value = bound_model
    node, _ = _real_model_node(model)

    output = node({
        "messages": [HumanMessage(content="把手机号改成13800138000")],
        "user_id": "u1",
        "context": {},
    })

    assert output["context"]["precheck_reason"] == "select_parse_failed"
    assert output["execution_status"] == "completed"
    assert "pending_tool" not in output["context"]


def test_select_real_model_rejects_invocation_failure():
    bound_model = MagicMock()
    bound_model.invoke.side_effect = RuntimeError("provider unavailable")
    model = MagicMock()
    model.bind_tools.return_value = bound_model
    node, _ = _real_model_node(model)

    output = node({
        "messages": [HumanMessage(content="把手机号改成13800138000")],
        "user_id": "u1",
        "context": {},
    })

    assert output["context"]["precheck_reason"] == "select_parse_failed"
    assert output["execution_status"] == "completed"
