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


