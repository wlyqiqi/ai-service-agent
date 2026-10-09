
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Interrupt
from langchain_core.messages import HumanMessage

from ai_service_agent.harness.graph import make_ai_graph
from ai_service_agent.harness.resume import resolve_resume_payload
from ai_service_agent.testing import make_fake_model
from conftest import ainvoke_graph


def test_make_ai_graph_compiles_and_runs():
    graph = make_ai_graph(make_fake_model(), MemorySaver())
    out = ainvoke_graph(graph, {"messages": [HumanMessage(content="hi")], "user_id": "u1", "session_id": "s1", "context": {}}, {"configurable": {"thread_id": "t1"}})
    assert out["execution_status"] == "completed"


def test_resolve_resume_payload_handoff_request():
    it = Interrupt(value={"type": "handoff_request", "reason": "r", "summary": "s"}, id="i1")
    payload = resolve_resume_payload(it.value, latest_user_text="我要转人工")
    assert payload == {"action": "handoff_resumed", "note": "我要转人工"}


def test_resolve_resume_payload_no_interrupt_returns_none():
    assert resolve_resume_payload(None) is None
    assert resolve_resume_payload({"type": "unknown"}) is None


from langchain_core.messages import AIMessage, HumanMessage

from ai_service_agent.tools import ToolMeta, ToolRegistry
from ai_service_agent.tools.kb import kb_search
from ai_service_agent.testing import ToolCallingFakeModel


def _invoke(graph, text, thread_id):
    return ainvoke_graph(graph,
        {"messages": [HumanMessage(content=text)], "user_id": "u1", "session_id": "s1", "context": {}},
        {"configurable": {"thread_id": thread_id}},
    )


def test_make_ai_graph_injects_read_tools_by_default(monkeypatch):
    # Task 2：kb_search 接真实 LightRAG——注入 fake client，离线确定性（命中带参考来源）
    from ai_service_agent.tools import kb as kb_module

    class _FakeLightRag:
        def query_context(self, query):
            return {"context": "炸篮 4.5L", "references": [{"file_path": "JoyAir_A1智能空气炸锅.md", "reference_id": "1"}]}

    monkeypatch.setattr(kb_module._lightrag, "get_lightrag_client", lambda: _FakeLightRag())
    model = ToolCallingFakeModel(responses=[
        AIMessage(content='{"route_intent":"faq","confidence":0.9}'),
        AIMessage(content="", tool_calls=[{"name": "kb_search", "args": {"text": "退款"}, "id": "call_1", "type": "tool_call"}]),
        AIMessage(content="查到了。"),
    ])
    out = _invoke(make_ai_graph(model, MemorySaver()), "保修多久", "t1")
    assert out["execution_status"] == "completed"
    tool_msgs = [m for m in out["messages"] if getattr(m, "type", "") == "tool"]
    assert tool_msgs and getattr(tool_msgs[0], "name", "") == "kb_search"
    assert "检索结果" in str(tool_msgs[0].content)
    assert "JoyAir_A1智能空气炸锅.md" in str(tool_msgs[0].content)


def test_default_registry_order_lookup_injected():
    model = ToolCallingFakeModel(responses=[
        AIMessage(content='{"route_intent":"order_query","confidence":0.9}'),
        AIMessage(content="", tool_calls=[{"name": "order_lookup", "args": {"order_id": "ORD-1"}, "id": "call_2", "type": "tool_call"}]),
        AIMessage(content="完成。"),
    ])
    out = _invoke(make_ai_graph(model, MemorySaver()), "查订单", "t2")
    tool_msgs = [m for m in out["messages"] if getattr(m, "type", "") == "tool"]
    assert tool_msgs and "已送达" in str(tool_msgs[0].content)


def test_make_ai_graph_respects_registry_read_tier():
    """注入的注册表决定工具集：只注册 kb_search 时，order_lookup 不在 read 档 -> ToolNode 未知工具。"""
    reg = ToolRegistry()
    reg.register(ToolMeta("kb_search", "检索", "read"), kb_search)
    model = ToolCallingFakeModel(responses=[
        AIMessage(content='{"route_intent":"order_query","confidence":0.9}'),
        AIMessage(content="", tool_calls=[{"name": "order_lookup", "args": {"order_id": "ORD-1"}, "id": "call_3", "type": "tool_call"}]),
        AIMessage(content="完成。"),
    ])
    out = _invoke(make_ai_graph(model, MemorySaver(), registry=reg), "查订单", "t3")
    tool_msgs = [m for m in out["messages"] if getattr(m, "type", "") == "tool"]
    assert tool_msgs and getattr(tool_msgs[0], "name", "") == "order_lookup"
    assert str(tool_msgs[0].content).startswith("Error:")  # 未注入 -> 未知工具错误 ToolMessage


def test_resolve_resume_payload_confirmation_keywords():
    it = Interrupt(value={"type": "confirmation_required", "summary": "s"}, id="i2")
    assert resolve_resume_payload(it.value, latest_user_text="好的，确认") == {"action": "confirm"}
    assert resolve_resume_payload(it.value, latest_user_text="取消吧") == {"action": "cancel"}
    assert resolve_resume_payload(it.value, latest_user_text="随便") == {"action": "cancel"}  # 未命中 fail-closed

def test_approval_dispatch_pending_and_expired():
    from ai_service_agent.harness.resume import approval_dispatch
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    pending = {"type": "approval_required", "approval_id": "A", "expires_at": (now + timedelta(minutes=30)).isoformat()}
    expired = {"type": "approval_required", "approval_id": "A", "expires_at": (now - timedelta(minutes=1)).isoformat()}
    assert approval_dispatch(pending, now=now) == "pending"
    assert approval_dispatch(expired, now=now) == "expired"
    assert approval_dispatch({"type": "confirmation_required"}, now=now) is None
