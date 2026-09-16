"""select 节点字段契约（2026-09-15 真机冒烟修复）：系统提示必须携带 schema 字段清单。"""

from langchain_core.messages import HumanMessage

from ai_service_agent.graph.workflows.select import make_select_node
from ai_service_agent.testing import ToolCallingFakeModel, select_json_message
from ai_service_agent.tools import DEFAULT_TOOL_REGISTRY


class RecordingSelectModel(ToolCallingFakeModel):
    """记录最后一次系统提示（select 的 json_mode 契约提示）。"""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        object.__setattr__(self, "seen_system", next(
            (m.content for m in messages if getattr(m, "type", "") == "system"), ""
        ))
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def _select_node():
    model = RecordingSelectModel(responses=[
        select_json_message("account_update", {"address": "朝阳区"}),
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
    assert "account_update" in model.seen_system


def test_select_system_template_tier_strict_no_read_tools():
    """B2b：写图 select 系统提示只列该档写工具，只读工具不进入允许集。"""
    model, node = _select_node()
    node({"messages": [HumanMessage(content="改一下收货地址")], "context": {}})
    assert "account_update" in model.seen_system
    assert "kb_search" not in model.seen_system
    assert "order_lookup" not in model.seen_system
