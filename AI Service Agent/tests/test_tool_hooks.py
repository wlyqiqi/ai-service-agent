import asyncio

import pytest
from langchain_core.messages import ToolMessage

from ai_service_agent.harness.tool_hooks import (
    ToolHookContext,
    build_hook_context,
    make_tool_hook_wrapper,
)


class FakeRuntime:
    def __init__(self, config):
        self.config = config


class FakeRequest:
    def __init__(self, tool_call, state, runtime):
        self.tool_call = tool_call
        self.state = state
        self.runtime = runtime


def _request(**overrides):
    tool_call = {
        "name": overrides.get("tool_name", "kb_search"),
        "args": {"text": "hi"},
        "id": "call-1",
        "type": "tool_call",
    }
    state = overrides.get("state", {"user_id": "u1"})
    runtime = FakeRuntime(config={"configurable": {"thread_id": "t1"}})
    return FakeRequest(tool_call=tool_call, state=state, runtime=runtime)


class Recorder:
    def __init__(self):
        self.events = []

    def on_before(self, context):
        self.events.append(("before", context))

    def on_after(self, context, outcome):
        self.events.append(("after", context, outcome))


class BoomHook:
    """所有回调都抛异常，用于验证 fail-open。"""

    def on_before(self, context):
        raise RuntimeError("hook boom")

    def on_after(self, context, outcome):
        raise RuntimeError("hook boom")


async def _execute_ok(request):
    return ToolMessage(content="ok result", name=request.tool_call["name"], tool_call_id=request.tool_call["id"])


def test_build_context_extracts_fields():
    ctx = build_hook_context(_request())
    assert ctx == ToolHookContext(
        tool_name="kb_search",
        tool_call_id="call-1",
        args={"text": "hi"},
        user_id="u1",
        thread_id="t1",
    )


def test_build_context_tolerates_non_dict_state_and_missing_config():
    ctx = build_hook_context(_request(state=None))
    assert ctx.user_id == ""
    assert ctx.thread_id == "t1"

    runtime_no_config = FakeRuntime(config=None)
    ctx2 = build_hook_context(FakeRequest(
        tool_call={"name": "kb_search", "args": {}, "id": "call-1"},
        state={"user_id": "u1"},
        runtime=runtime_no_config,
    ))
    assert ctx2.thread_id == ""


def test_wrapper_reports_success_outcome():
    recorder = Recorder()
    wrapper = make_tool_hook_wrapper([recorder])
    result = asyncio.run(wrapper(_request(), _execute_ok))
    assert isinstance(result, ToolMessage)
    assert recorder.events[0][0] == "before"
    assert recorder.events[0][1].tool_name == "kb_search"
    _, _, outcome = recorder.events[1]
    assert outcome.ok is True
    assert outcome.result_summary == "ok result"
    assert outcome.error is None
    assert outcome.duration_ms >= 0


def test_wrapper_reports_error_and_reraises():
    recorder = Recorder()
    wrapper = make_tool_hook_wrapper([recorder])

    async def execute(request):
        raise RuntimeError("tool boom")

    with pytest.raises(RuntimeError, match="tool boom"):
        asyncio.run(wrapper(_request(), execute))
    _, _, outcome = recorder.events[-1]
    assert outcome.ok is False
    assert outcome.error == "tool boom"
    assert outcome.duration_ms >= 0


def test_wrapper_fail_open_on_hook_errors():
    recorder = Recorder()
    wrapper = make_tool_hook_wrapper([BoomHook(), recorder])
    result = asyncio.run(wrapper(_request(), _execute_ok))
    assert result.content == "ok result"
    assert recorder.events[0][0] == "before"
    assert recorder.events[1][0] == "after"


def test_empty_hooks_return_none():
    assert make_tool_hook_wrapper([]) is None




import importlib.util
import sqlite3
import tempfile
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from ai_service_agent.graph.agents.read_agent import make_read_graph
from ai_service_agent.graph.builder import build_graph
from ai_service_agent.testing import ToolCallingFakeModel, router_json_message, select_json_message
from ai_service_agent.tools.kb import kb_search


ROOT = Path(__file__).resolve().parents[1]


@tool
def hook_echo_tool(text: str) -> str:
    """Test echo tool (offline deterministic, no external resources)."""
    return f"echo:{text}"


def test_read_graph_runs_tool_hooks():
    recorder = Recorder()
    model = ToolCallingFakeModel(responses=[
        AIMessage(content="", tool_calls=[
            {"name": "hook_echo_tool", "args": {"text": "hello"}, "id": "call-1", "type": "tool_call"},
        ]),
        AIMessage(content="Found it."),
    ])
    graph = make_read_graph(model, tools=[hook_echo_tool], tool_hooks=[recorder])
    out = graph.invoke(
        {"messages": [], "user_id": "u1"},
        config={"configurable": {"thread_id": "t-hook-read"}},
    )
    assert out["execution_status"] == "completed"
    assert recorder.events[0][0] == "before"
    ctx = recorder.events[0][1]
    assert ctx.tool_name == "hook_echo_tool"
    assert ctx.args == {"text": "hello"}
    assert ctx.user_id == "u1"
    assert ctx.thread_id == "t-hook-read"
    assert recorder.events[1][0] == "after"
    assert "echo:hello" in recorder.events[1][2].result_summary
    assert recorder.events[1][2].ok is True


def _load_seed_module():
    spec = importlib.util.spec_from_file_location(
        "seed_mock_db_hook", ROOT / "scripts" / "seed_mock_db.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def hook_seed():
    module = _load_seed_module()
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "hook.db"
        module.build_database(str(db_path))
        yield module, db_path


def _patch_hook_db(monkeypatch, db_path):
    from ai_service_agent.tools import orders as orders_mod
    from ai_service_agent.tools import write as write_mod

    class _Settings:
        mock_ecommerce_db_path = str(db_path)

    monkeypatch.setattr(write_mod, "get_settings", lambda: _Settings())
    monkeypatch.setattr(orders_mod, "get_settings", lambda: _Settings())


def test_mutation_graph_runs_tool_hooks(hook_seed, monkeypatch):
    _, db_path = hook_seed
    _patch_hook_db(monkeypatch, db_path)
    recorder = Recorder()
    model = ToolCallingFakeModel(responses=[
        router_json_message("order", "mutation", 0.9),
        select_json_message("update_address", {"address": "Beijing Chaoyang"}),
    ])
    graph = build_graph(model, tools=[kb_search], checkpointer=MemorySaver(), tool_hooks=[recorder])
    config = {"configurable": {"thread_id": "t-hook-m"}}
    graph.invoke(
        {"messages": [HumanMessage(content="Update delivery address")], "user_id": "u1", "session_id": "t-hook-m", "context": {}},
        config,
    )
    out = graph.invoke(Command(resume={"action": "confirm"}), config)
    assert out["execution_status"] == "completed"
    afters = [event for event in recorder.events if event[0] == "after"]
    assert afters
    assert afters[-1][1].tool_name == "update_address"
    assert afters[-1][1].user_id == "u1"
    assert afters[-1][2].ok is True


def _load_refund_seed(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "seed_mock_db_hook_refund", ROOT / "scripts" / "seed_mock_db.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    db_path = tmp_path / "hook-refund.db"
    module.build_database(str(db_path))
    return module, db_path


def test_high_risk_graph_runs_tool_hooks(monkeypatch, tmp_path):
    _, db_path = _load_refund_seed(tmp_path)
    _patch_hook_db(monkeypatch, db_path)
    conn = sqlite3.connect(db_path)
    try:
        owner, order_id = conn.execute(
            "SELECT customer_id, order_id FROM orders WHERE status='paid' LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    recorder = Recorder()
    model = ToolCallingFakeModel(responses=[
        router_json_message("order", "high_risk", 0.9),
        select_json_message("refund_apply", {"order_id": order_id, "amount": 1.0}),
    ])
    graph = build_graph(model, tools=[kb_search], checkpointer=MemorySaver(), tool_hooks=[recorder])
    config = {"configurable": {"thread_id": "t-hook-hr"}}
    graph.invoke(
        {"messages": [HumanMessage(content="Apply for refund")], "user_id": owner, "session_id": "t-hook-hr", "context": {}},
        config,
    )
    out = graph.invoke(Command(resume={"action": "approve"}), config)
    assert out["execution_status"] == "completed"
    afters = [event for event in recorder.events if event[0] == "after"]
    assert afters
    assert afters[-1][1].tool_name == "refund_apply"
    assert afters[-1][2].ok is True
