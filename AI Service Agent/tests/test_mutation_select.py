"""Mutation Select seam (CONTEXT.md / ADR 0004 follow-on): dedicated node, prompts, graph name."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from ai_service_agent.graph.builder import build_graph
from ai_service_agent.prompts import (
    MUTATION_SELECT_BINDTOOLS_TEMPLATE,
    MUTATION_SELECT_SYSTEM_TEMPLATE,
)
from ai_service_agent.testing import make_fake_model, select_json_message
from ai_service_agent.tools import DEFAULT_TOOL_REGISTRY
from ai_service_agent.tools.registry import ToolMeta, ToolRegistry
from ai_service_agent.tools.write import UpdatePhoneArgs


def _select_result(coro):
    return asyncio.run(coro)


def test_mutation_select_prompts_are_mutation_only():
    """Dedicated prompts: write-ops clarify; no HighRisk refund/cancel / tier_clarify framing."""
    assert "写操作选择器" in MUTATION_SELECT_BINDTOOLS_TEMPLATE
    assert "不要发起 tool_call" in MUTATION_SELECT_BINDTOOLS_TEMPLATE
    assert "表单" in MUTATION_SELECT_BINDTOOLS_TEMPLATE
    assert "refund_apply" not in MUTATION_SELECT_BINDTOOLS_TEMPLATE
    assert "cancel_order" not in MUTATION_SELECT_BINDTOOLS_TEMPLATE
    assert "tier_clarify" not in MUTATION_SELECT_BINDTOOLS_TEMPLATE
    assert "tier_mismatch" not in MUTATION_SELECT_BINDTOOLS_TEMPLATE

    assert '"tool": null' in MUTATION_SELECT_SYSTEM_TEMPLATE or '"tool":null' in MUTATION_SELECT_SYSTEM_TEMPLATE
    assert "content" in MUTATION_SELECT_SYSTEM_TEMPLATE
    assert "refund_apply" not in MUTATION_SELECT_SYSTEM_TEMPLATE
    assert "cancel_order" not in MUTATION_SELECT_SYSTEM_TEMPLATE


def test_mutation_select_templates_share_write_and_business_boundaries():
    """Fake / Real 规则同语义：写操作边界、追问约束、列表外退款/取消；输出通道各自保留。"""
    for template in (MUTATION_SELECT_SYSTEM_TEMPLATE, MUTATION_SELECT_BINDTOOLS_TEMPLATE):
        assert "写操作边界" in template
        assert "已修改" in template
        assert "已提交" in template
        assert "已办妥" in template
        assert "request_id" in template
        assert "严禁编造" in template
        assert "只列举当前" in template
        assert "退款" in template
        assert "取消" in template
        assert "改联系方式" in template
        assert "开票" in template
        assert "加备注" in template
        assert "当前允许列表含 create_invoice" in template
        assert "唯一开票意图" in template
        assert "「开个发票」" in template
        assert "「开个发票吧」" in template
        assert "即使没有订单号、抬头、税号、金额" in template
        assert "args 可为空或只含已知字段" in template
        assert "禁止用纯文本追问订单号、抬头、税号、金额" in template
        assert "缺参交后续表单" in template
        assert "refund_apply" not in template
        assert "cancel_order" not in template
    assert "必须输出 JSON 工具 create_invoice" in MUTATION_SELECT_SYSTEM_TEMPLATE
    assert "必须输出 JSON 工具 create_invoice" not in MUTATION_SELECT_BINDTOOLS_TEMPLATE
    assert "必须发起 tool_call create_invoice" in MUTATION_SELECT_BINDTOOLS_TEMPLATE
    assert "tool_choice" not in MUTATION_SELECT_BINDTOOLS_TEMPLATE
    assert "只输出 JSON" in MUTATION_SELECT_SYSTEM_TEMPLATE
    assert "只输出 JSON" not in MUTATION_SELECT_BINDTOOLS_TEMPLATE
    assert '{"tool":' not in MUTATION_SELECT_BINDTOOLS_TEMPLATE
    assert "示例仅说明意图与参数" in MUTATION_SELECT_BINDTOOLS_TEMPLATE
    assert "tool_call 或纯文本" in MUTATION_SELECT_BINDTOOLS_TEMPLATE
    rendered = MUTATION_SELECT_SYSTEM_TEMPLATE.format(tool_list="- update_phone: 修改联系电话", examples="（示例）")
    assert '{"tool": null' in rendered or '{"tool":null' in rendered


def test_make_mutation_select_node_has_no_tier_parameter():
    from ai_service_agent.graph.workflows.mutation_select import make_mutation_select_node
    import inspect

    params = inspect.signature(make_mutation_select_node).parameters
    assert "tier" not in params


def test_mutation_select_fake_null_tool_clarifies_inline(monkeypatch):
    from ai_service_agent.graph.workflows.mutation_select import make_mutation_select_node
    from ai_service_agent.testing import ToolCallingFakeModel

    events = []
    monkeypatch.setattr(
        "ai_service_agent.graph.workflows.mutation_select.get_stream_writer",
        lambda: events.append,
    )
    model = ToolCallingFakeModel(
        responses=[
            AIMessage(content='{"tool": null, "content": "您是要改地址还是开票？"}'),
        ]
    )
    node = make_mutation_select_node(
        model,
        DEFAULT_TOOL_REGISTRY.for_risk_level("mutation"),
        DEFAULT_TOOL_REGISTRY,
    )
    output = _select_result(
        node({"messages": [HumanMessage(content="帮我弄一下订单")], "context": {}})
    )
    assert output["execution_status"] == "clarifying"
    assert output["messages"][-1].content == "您是要改地址还是开票？"
    assert events == [
        {"type": "clarification_required", "data": {"missing": [], "reason": "operation_unclear"}}
    ]
    assert "pending_tool" not in output["context"]


def test_mutation_select_bindtools_uses_dedicated_template():
    from ai_service_agent.graph.workflows.mutation_select import make_mutation_select_node

    bound_model = AsyncMock()
    bound_model.ainvoke.return_value = AIMessage(
        content="",
        tool_calls=[{"name": "update_phone", "args": {"phone": "13800138000"}, "id": "call_1"}],
    )
    model = MagicMock()
    model.bind_tools.return_value = bound_model
    tool = MagicMock()
    tool.name = "update_phone"
    registry = ToolRegistry()
    registry.register(
        ToolMeta(
            name="update_phone",
            description="修改联系电话",
            min_risk_level="mutation",
            schema=UpdatePhoneArgs,
        ),
        tool,
    )
    node = make_mutation_select_node(model, [tool], registry)
    output = _select_result(
        node(
            {
                "messages": [HumanMessage(content="把手机号改成13800138000")],
                "user_id": "u1",
                "context": {},
            }
        )
    )
    assert model.bind_tools.call_args == call([tool], tool_choice="auto")
    system = bound_model.ainvoke.await_args.args[0][0].content
    assert "写操作选择器" in system
    assert "表单" in system
    assert output["context"]["pending_tool"]["tool"] == "update_phone"


def test_mutation_graph_node_is_mutation_select_llm():
    from ai_service_agent.tools.kb import kb_search

    graph = build_graph(make_fake_model(), tools=[kb_search])
    mutation_graph = graph.nodes["mutation_graph"].bound
    assert "mutation_select_llm" in mutation_graph.nodes
    assert "select_llm" not in mutation_graph.nodes
    high_risk_graph = graph.nodes["high_risk_graph"].bound
    assert "high_risk_select_llm" in high_risk_graph.nodes
    assert "select_llm" not in high_risk_graph.nodes
    assert "mutation_select_llm" not in high_risk_graph.nodes


def test_mutation_select_uses_mutation_system_template_for_fake():
    from ai_service_agent.graph.workflows.mutation_select import make_mutation_select_node
    from ai_service_agent.testing import ToolCallingFakeModel

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

    model = Recording(responses=[select_json_message("update_phone", {"phone": "13800138000"})])
    node = make_mutation_select_node(
        model,
        DEFAULT_TOOL_REGISTRY.for_risk_level("mutation"),
        DEFAULT_TOOL_REGISTRY,
    )
    _select_result(
        node({"messages": [HumanMessage(content="换手机号 13800138000")], "context": {}})
    )
    assert "写操作选择器" in model.seen_system or "tool\": null" in model.seen_system.replace(" ", "")
    assert "refund_apply" not in model.seen_system


def _example_section(system: str) -> str:
    marker = "## 示例"
    index = system.rfind(marker)
    assert index != -1
    return system[index:]


def _seen_fake_system(allowed_tools=None) -> str:
    from ai_service_agent.graph.workflows.mutation_select import make_mutation_select_node
    from ai_service_agent.testing import ToolCallingFakeModel

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

    model = Recording(responses=[select_json_message("update_phone", {"phone": "13800138000"})])
    node = make_mutation_select_node(
        model,
        DEFAULT_TOOL_REGISTRY.for_risk_level("mutation"),
        DEFAULT_TOOL_REGISTRY,
    )
    state = {"messages": [HumanMessage(content="换手机号 13800138000")], "context": {}}
    if allowed_tools is not None:
        state["allowed_tools"] = allowed_tools
    _select_result(node(state))
    return model.seen_system


def test_fake_invoice_example_keeps_only_fields_in_the_user_sentence():
    """Schema 字段是 company / tax_no；用户句没有金额，示例不编造 amount。"""
    examples = _example_section(_seen_fake_system())
    assert "订单 ORD-10086 开票，抬头星辰科技有限公司，税号 91110108MA01AB12CD" in examples
    assert '"tool": "create_invoice"' in examples
    assert '"order_id": "ORD-10086"' in examples
    assert '"company": "星辰科技有限公司"' in examples
    assert '"tax_no": "91110108MA01AB12CD"' in examples
    assert "amount" not in examples
    assert "199" not in examples
    assert "title" not in examples
    assert "tax_id" not in examples
    assert "request_id" not in examples


def test_fake_missing_arg_examples_cover_profile_and_order_tools():
    examples = _example_section(_seen_fake_system())
    assert "我想改个收货地址（参数不完整）" in examples
    assert '"tool": "update_address", "args": {}' in examples
    assert "给订单加个备注（参数不完整）" in examples
    assert '"tool": "update_order_note", "args": {}' in examples


def test_spoken_invoice_missing_args_examples_select_empty_create_invoice():
    """「开个发票」/「开个发票吧」缺参仍选 create_invoice，不改成纯文本追问。"""
    examples = _example_section(
        _seen_fake_system(["update_order_note", "create_invoice", "create_service_ticket"])
    )
    payload = '{"tool": "create_invoice", "args": {}}'
    for phrase in ("开个发票", "开个发票吧"):
        assert f"用户：{phrase}\n输出：{payload}" in examples


def test_fake_clarify_examples_list_only_current_capabilities():
    examples = _example_section(_seen_fake_system())
    assert "帮我改一下订单" in examples
    assert "我要退款" in examples
    assert '"tool": null' in examples
    assert "改收货地址" in examples
    assert "改联系电话" in examples
    assert "加订单备注" in examples
    assert "开发票" in examples
    assert "申请售后" in examples
    assert "帮我改一下联系方式" in examples
    assert "既要开票又要加备注" in examples
    assert "refund_apply" not in examples
    assert "cancel_order" not in examples


def test_profile_allowlist_examples_omit_order_capabilities():
    examples = _example_section(_seen_fake_system(["update_address", "update_phone"]))
    assert "我想改个收货地址（参数不完整）" in examples
    assert '"tool": "update_address"' in examples
    assert "帮我改一下联系方式" in examples
    assert "改收货地址" in examples
    assert "改联系电话" in examples
    assert "update_order_note" not in examples
    assert "create_invoice" not in examples
    assert "create_service_ticket" not in examples
    assert "开发票" not in examples
    assert "加订单备注" not in examples
    assert "申请售后" not in examples
    assert "既要开票又要加备注" not in examples
    assert "开个发票" not in examples
    assert "refund_apply" not in examples


def test_order_allowlist_examples_include_sparse_order_args_only():
    examples = _example_section(
        _seen_fake_system(["update_order_note", "create_invoice", "create_service_ticket"])
    )
    assert "给订单加个备注（参数不完整）" in examples
    assert '"tool": "update_order_note", "args": {}' in examples
    assert "既要开票又要加备注" in examples
    assert "update_address" not in examples
    assert "update_phone" not in examples
    assert "改收货地址" not in examples
    assert "改联系电话" not in examples
    assert "帮我改一下联系方式" not in examples
    assert "amount" not in examples
    assert "199" not in examples


def test_bind_tools_examples_describe_tool_calls_without_json_payloads():
    from ai_service_agent.graph.workflows.mutation_select import make_mutation_select_node

    bound_model = AsyncMock()
    bound_model.ainvoke.return_value = AIMessage(
        content="",
        tool_calls=[{"name": "update_phone", "args": {"phone": "13800138000"}, "id": "call_1"}],
    )
    model = MagicMock()
    model.bind_tools.return_value = bound_model
    node = make_mutation_select_node(
        model,
        DEFAULT_TOOL_REGISTRY.for_risk_level("mutation"),
        DEFAULT_TOOL_REGISTRY,
    )
    _select_result(
        node(
            {
                "messages": [HumanMessage(content="换手机号 13800138000")],
                "user_id": "u1",
                "context": {},
            }
        )
    )
    system = bound_model.ainvoke.await_args.args[0][0].content
    examples = _example_section(system)
    assert "示例仅说明意图与参数" in system
    assert "不要输出 JSON" in system
    assert '{"tool":' not in system
    assert "发起 tool_call：update_address(order_id=" in examples
    assert "发起 tool_call：create_invoice(" in examples
    assert 'company="星辰科技有限公司"' in examples
    assert 'tax_no="91110108MA01AB12CD"' in examples
    assert "amount" not in examples
    assert "199" not in examples
    assert "我想改个收货地址（参数不完整）" in examples
    assert "发起 tool_call：update_address()" in examples
    assert "给订单加个备注（参数不完整）" in examples
    assert "发起 tool_call：update_order_note()" in examples
    for phrase in ("开个发票", "开个发票吧"):
        assert f"用户：{phrase}\n行为：发起 tool_call：create_invoice()" in examples
    assert "禁止用纯文本追问" in examples
    assert "不发起 tool_call，纯文本追问：" in examples
    assert "我要退款" in examples
    assert "退款不在当前能办的范围内" in examples
    assert "refund_apply" not in system
    assert "cancel_order" not in system
    assert "request_id" not in examples


def test_inline_clarify_fallback_follows_current_allowlist():
    """模型没给话术时，兜底文案只列当前白名单能力。

    account_profile_update 的白名单只有改地址/改电话；旧兜底会提到开票和售后，
    等于告知用户系统此刻不会做的事。
    """
    from ai_service_agent.graph.workflows.mutation_select import make_mutation_select_node
    from ai_service_agent.testing import ToolCallingFakeModel

    model = ToolCallingFakeModel(
        responses=[AIMessage(content='{"tool": null, "content": ""}')]
    )
    node = make_mutation_select_node(
        model,
        DEFAULT_TOOL_REGISTRY.for_risk_level("mutation"),
        DEFAULT_TOOL_REGISTRY,
    )
    output = _select_result(
        node(
            {
                "messages": [HumanMessage(content="帮我弄一下订单")],
                "allowed_tools": ["update_address", "update_phone"],
                "context": {},
            }
        )
    )
    text = output["messages"][-1].content
    assert output["execution_status"] == "clarifying"
    assert "改收货地址" in text and "改联系电话" in text
    assert "开发票" not in text
    assert "售后" not in text
    assert "备注" not in text


def test_inline_clarify_drops_json_payload_in_content():
    """模型误把 JSON 载荷当 content 返回时丢弃，改走模板文案。"""
    from ai_service_agent.graph.workflows.mutation_select import make_mutation_select_node
    from ai_service_agent.testing import ToolCallingFakeModel

    model = ToolCallingFakeModel(
        responses=[
            AIMessage(
                content='{"tool": null, "content": "{\\"tool\\": \\"update_phone\\", \\"args\\": {}}"}'
            )
        ]
    )
    node = make_mutation_select_node(
        model,
        DEFAULT_TOOL_REGISTRY.for_risk_level("mutation"),
        DEFAULT_TOOL_REGISTRY,
    )
    output = _select_result(
        node(
            {
                "messages": [HumanMessage(content="帮我弄一下订单")],
                "allowed_tools": ["update_phone"],
                "context": {},
            }
        )
    )
    text = output["messages"][-1].content
    assert '"tool"' not in text
    assert "update_phone" not in text
    assert "改联系电话" in text


_SLOT_ASK = "请问订单号是多少？新的收货地址是什么？"
_INVOICE_SLOT_ASK = "请提供订单号、发票抬头、税号和开票金额。"
_PROFILE_TOOLS = ["update_address", "update_phone"]
_ORDER_WRITE_TOOLS = ["update_order_note", "create_invoice", "create_service_ticket"]
_SPOKEN_INVOICE = ("开个发票", "开个发票吧", "帮我开个发票", "给我开个发票")


def _bind_tools_clarify_node(content: str, tool_calls=None):
    """Real path: bind_tools model that does not emit a tool_call."""
    from ai_service_agent.graph.workflows.mutation_select import make_mutation_select_node

    bound_model = AsyncMock()
    bound_model.ainvoke.return_value = AIMessage(
        content=content,
        tool_calls=[] if tool_calls is None else tool_calls,
    )
    model = MagicMock()
    model.bind_tools.return_value = bound_model
    node = make_mutation_select_node(
        model,
        DEFAULT_TOOL_REGISTRY.for_risk_level("mutation"),
        DEFAULT_TOOL_REGISTRY,
    )
    return node, bound_model


def _force_state(text: str, allowed_tools=None, context=None) -> dict:
    return {
        "messages": [HumanMessage(content=text)],
        "allowed_tools": list(allowed_tools or _PROFILE_TOOLS),
        "user_id": "u1",
        "context": context
        if context is not None
        else {"operation_clarify": {"stale": True}, "clarify_count": 2},
    }


@pytest.mark.parametrize("text", ["我要改收货地址", "我要改个地址"])
def test_force_form_address_when_real_model_omits_tool_call(text, monkeypatch):
    """#830: unique address wording must pending_tool even when the model asks in prose."""
    events = []
    monkeypatch.setattr(
        "ai_service_agent.graph.workflows.mutation_select.get_stream_writer",
        lambda: events.append,
    )
    node, bound_model = _bind_tools_clarify_node(_SLOT_ASK)
    output = _select_result(node(_force_state(text)))

    assert bound_model.ainvoke.await_count == 1
    pending = output["context"]["pending_tool"]
    assert pending["tool"] == "update_address"
    assert pending["request_id"]
    assert pending["args"].keys() == {"request_id"}
    assert "messages" not in output
    assert output.get("execution_status") != "clarifying"
    assert "operation_clarify" not in output["context"]
    assert output["context"]["clarify_count"] == 0
    assert _SLOT_ASK not in str(output)
    assert "订单号是多少" not in str(output)
    assert "新的收货地址是什么" not in str(output)
    assert events == []


def test_fake_null_tool_unique_address_still_pending(monkeypatch):
    """JSON select path shares the same gate: tool=null does not win over a unique phrase."""
    from ai_service_agent.graph.workflows.mutation_select import make_mutation_select_node
    from ai_service_agent.testing import ToolCallingFakeModel, select_clarify_message

    events = []
    monkeypatch.setattr(
        "ai_service_agent.graph.workflows.mutation_select.get_stream_writer",
        lambda: events.append,
    )
    model = ToolCallingFakeModel(responses=[select_clarify_message(_SLOT_ASK)])
    node = make_mutation_select_node(
        model,
        DEFAULT_TOOL_REGISTRY.for_risk_level("mutation"),
        DEFAULT_TOOL_REGISTRY,
    )
    output = _select_result(node(_force_state("我要改收货地址")))
    pending = output["context"]["pending_tool"]
    assert pending["tool"] == "update_address"
    assert pending["args"].keys() == {"request_id"}
    assert "messages" not in output
    assert events == []


def test_vague_order_edit_stays_operation_clarify(monkeypatch):
    events = []
    monkeypatch.setattr(
        "ai_service_agent.graph.workflows.mutation_select.get_stream_writer",
        lambda: events.append,
    )
    node, _bound = _bind_tools_clarify_node("您是要改收货地址，还是改联系电话？")
    output = _select_result(node(_force_state("帮我改一下订单")))

    assert output["execution_status"] == "clarifying"
    assert "pending_tool" not in output["context"]
    assert "operation_clarify" not in output["context"]
    assert output["messages"][-1].content == "您是要改收货地址，还是改联系电话？"
    assert events == [
        {"type": "clarification_required", "data": {"missing": [], "reason": "operation_unclear"}}
    ]


def test_contact_method_ambiguity_is_not_forced():
    node, _bound = _bind_tools_clarify_node("您是要改收货地址，还是改联系电话？")
    output = _select_result(node(_force_state("改联系方式")))

    assert output["execution_status"] == "clarifying"
    assert "pending_tool" not in output["context"]
    assert output["messages"][-1].content == "您是要改收货地址，还是改联系电话？"


def test_force_form_carries_order_id_and_address_from_utterance():
    node, _bound = _bind_tools_clarify_node(_SLOT_ASK)
    output = _select_result(
        node(
            _force_state("订单 ORD-10086 收货地址改成北京市朝阳区望京SOHO")
        )
    )
    args = output["context"]["pending_tool"]["args"]
    assert output["context"]["pending_tool"]["tool"] == "update_address"
    assert args["order_id"] == "ORD-10086"
    assert args["address"] == "北京市朝阳区望京SOHO"
    assert set(args) == {"request_id", "order_id", "address"}
    assert "messages" not in output


def test_existing_tool_call_is_not_replaced_by_force_form():
    """A real tool_call stays as the model emitted it, even if the phrase maps elsewhere."""
    node, _bound = _bind_tools_clarify_node(
        "",
        tool_calls=[
            {
                "name": "update_phone",
                "args": {"phone": "13800138000"},
                "id": "call_1",
                "type": "tool_call",
            }
        ],
    )
    output = _select_result(node(_force_state("我要改收货地址")))
    assert output["context"]["pending_tool"]["tool"] == "update_phone"
    assert output["context"]["pending_tool"]["args"]["phone"] == "13800138000"


def test_address_phrase_outside_allowlist_stays_clarify():
    node, _bound = _bind_tools_clarify_node("目前可以帮您改联系电话。")
    output = _select_result(
        node(_force_state("我要改收货地址", allowed_tools=["update_phone"]))
    )
    assert output["execution_status"] == "clarifying"
    assert "pending_tool" not in output["context"]


@pytest.mark.parametrize("text", _SPOKEN_INVOICE)
def test_fake_spoken_invoice_without_tool_still_pending_create_invoice(text, monkeypatch):
    """缺参开票口语：模型只吐追问时，仍 pending create_invoice，不把槽位留在聊天里。"""
    from ai_service_agent.graph.workflows.mutation_select import make_mutation_select_node
    from ai_service_agent.testing import ToolCallingFakeModel, select_clarify_message

    events = []
    monkeypatch.setattr(
        "ai_service_agent.graph.workflows.mutation_select.get_stream_writer",
        lambda: events.append,
    )
    model = ToolCallingFakeModel(responses=[select_clarify_message(_INVOICE_SLOT_ASK)])
    node = make_mutation_select_node(
        model,
        DEFAULT_TOOL_REGISTRY.for_risk_level("mutation"),
        DEFAULT_TOOL_REGISTRY,
    )
    output = _select_result(node(_force_state(text, allowed_tools=_ORDER_WRITE_TOOLS)))
    pending = output["context"]["pending_tool"]
    assert pending["tool"] == "create_invoice"
    assert set(pending["args"]) == {"request_id"}
    assert "messages" not in output
    assert output.get("execution_status") != "clarifying"
    assert "发票抬头" not in str(output)
    assert "税号" not in str(output)
    assert "开票金额" not in str(output)
    assert events == []


def test_fake_spoken_invoice_json_tool_pending_create_invoice():
    """模型按契约返回 create_invoice 空 args 时，直接进 Form。"""
    from ai_service_agent.graph.workflows.mutation_select import make_mutation_select_node
    from ai_service_agent.testing import ToolCallingFakeModel

    model = ToolCallingFakeModel(responses=[select_json_message("create_invoice", {})])
    node = make_mutation_select_node(
        model,
        DEFAULT_TOOL_REGISTRY.for_risk_level("mutation"),
        DEFAULT_TOOL_REGISTRY,
    )
    output = _select_result(
        node(_force_state("开个发票吧", allowed_tools=_ORDER_WRITE_TOOLS))
    )
    pending = output["context"]["pending_tool"]
    assert pending["tool"] == "create_invoice"
    assert set(pending["args"]) == {"request_id"}
    assert "messages" not in output
    assert output.get("execution_status") != "clarifying"


@pytest.mark.parametrize("text", _SPOKEN_INVOICE)
def test_real_spoken_invoice_without_tool_call_still_pending_create_invoice(text, monkeypatch):
    """Real 路径漏 tool_call 时，Force-form 在同一次调用后补上 create_invoice。"""
    events = []
    monkeypatch.setattr(
        "ai_service_agent.graph.workflows.mutation_select.get_stream_writer",
        lambda: events.append,
    )
    node, bound_model = _bind_tools_clarify_node(_INVOICE_SLOT_ASK)
    output = _select_result(node(_force_state(text, allowed_tools=_ORDER_WRITE_TOOLS)))

    assert bound_model.ainvoke.await_count == 1
    pending = output["context"]["pending_tool"]
    assert pending["tool"] == "create_invoice"
    assert set(pending["args"]) == {"request_id"}
    assert "messages" not in output
    assert output.get("execution_status") != "clarifying"
    assert "发票抬头" not in str(output)
    assert events == []


def test_spoken_invoice_tool_call_is_not_replaced_by_force_form():
    """已有 tool_call 时，开票口语正则不得改绑。"""
    node, bound_model = _bind_tools_clarify_node(
        "",
        tool_calls=[
            {
                "name": "update_order_note",
                "args": {"order_id": "ORD-10086", "note": "放驿站就行"},
                "id": "call_1",
                "type": "tool_call",
            }
        ],
    )
    output = _select_result(
        node(_force_state("开个发票吧", allowed_tools=_ORDER_WRITE_TOOLS))
    )
    assert bound_model.ainvoke.await_count == 1
    assert output["context"]["pending_tool"]["tool"] == "update_order_note"
    assert output["context"]["pending_tool"]["args"]["note"] == "放驿站就行"


def test_spoken_invoice_keeps_order_id_from_the_utterance():
    node, _bound = _bind_tools_clarify_node(_INVOICE_SLOT_ASK)
    output = _select_result(
        node(_force_state("订单 ORD-10086 开个发票吧", allowed_tools=_ORDER_WRITE_TOOLS))
    )
    args = output["context"]["pending_tool"]["args"]
    assert output["context"]["pending_tool"]["tool"] == "create_invoice"
    assert args["order_id"] == "ORD-10086"
    assert "company" not in args
    assert "tax_no" not in args
    assert "amount" not in args


def test_spoken_invoice_plus_note_stays_clarify():
    node, _bound = _bind_tools_clarify_node("您是要开发票，还是加订单备注？")
    output = _select_result(
        node(_force_state("开个发票吧，再加个备注", allowed_tools=_ORDER_WRITE_TOOLS))
    )
    assert output["execution_status"] == "clarifying"
    assert "pending_tool" not in output["context"]


def test_mixed_invoice_and_note_stays_clarify():
    node, _bound = _bind_tools_clarify_node("您是要开发票，还是加订单备注？")
    output = _select_result(
        node(
            _force_state(
                "订单 ORD-10086 既要开票又要加备注",
                allowed_tools=["update_order_note", "create_invoice", "create_service_ticket"],
            )
        )
    )
    assert output["execution_status"] == "clarifying"
    assert "pending_tool" not in output["context"]


def test_real_select_without_tool_call_opens_address_form(monkeypatch, tmp_path):
    """Select omits tool_call; the gate still reaches Form Interrupt, not slot text."""
    import importlib.util
    from pathlib import Path

    from langgraph.checkpoint.memory import MemorySaver

    from ai_service_agent.domain.orders import SQLiteOrderRepository
    from ai_service_agent.graph import builder as builder_mod
    from ai_service_agent.graph.messages import message_text
    from ai_service_agent.testing import ToolCallingFakeModel, router_json_message
    from ai_service_agent.tools import orders as orders_mod
    from ai_service_agent.tools import write as write_mod
    from ai_service_agent.tools.kb import kb_search
    from conftest import ainvoke_graph

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "seed_mock_db_force_form", root / "scripts" / "seed_mock_db.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    db_path = tmp_path / "force_form.db"
    module.build_database(str(db_path))

    class _Settings:
        mock_ecommerce_db_path = str(db_path)

    monkeypatch.setattr(orders_mod, "get_settings", lambda: _Settings())
    monkeypatch.setattr(write_mod, "get_settings", lambda: _Settings())
    monkeypatch.setattr(
        builder_mod,
        "SQLiteOrderRepository",
        lambda path: SQLiteOrderRepository(str(db_path)),
    )

    bound_model = AsyncMock()
    bound_model.ainvoke.return_value = AIMessage(content=_SLOT_ASK, tool_calls=[])
    select_model = MagicMock()
    select_model.bind_tools.return_value = bound_model
    graph = build_graph(
        ToolCallingFakeModel(
            responses=[router_json_message("account_profile_update", 0.9)]
        ),
        tools=[kb_search],
        checkpointer=MemorySaver(),
        mutation_select_model=select_model,
    )
    output = ainvoke_graph(
        graph,
        {
            "messages": [HumanMessage(content="我要改收货地址")],
            "user_id": "u1",
            "session_id": "force-form",
            "context": {},
        },
        {"configurable": {"thread_id": "force-form"}},
    )

    assert output.get("__interrupt__")
    assert output["__interrupt__"][0].value.get("tool") == "update_address"
    assert output.get("execution_status") != "clarifying"
    visible = "\n".join(message_text(message) for message in output.get("messages") or [])
    assert "订单号是多少" not in visible
    assert "新的收货地址是什么" not in visible
    assert "新地址是什么" not in visible
