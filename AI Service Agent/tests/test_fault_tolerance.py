import httpx
import openai
import pytest
from langgraph.errors import NodeError, NodeTimeoutError
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from typing import TypedDict

from ai_service_agent.config import Settings
from ai_service_agent.graph.fault_tolerance import (
    RETRY_DEEPSEEK_ROUTER,
    RETRY_LLM,
    is_transient_error,
    make_timeout_policy,
    make_subgraph_error_handler,
    root_error_handler,
)
from ai_service_agent.graph.workflows.copy import RUNTIME_ERROR_TEXT


def test_only_transient_provider_errors_are_retried():
    request = httpx.Request("POST", "https://ark.example.invalid")
    assert is_transient_error(httpx.ConnectError("connect failed"))
    assert is_transient_error(httpx.ReadTimeout("read timed out"))
    assert is_transient_error(TimeoutError("timed out"))
    assert is_transient_error(openai.APIConnectionError(request=request))
    assert not is_transient_error(ValueError("bad input"))
    assert not is_transient_error(TypeError("bad type"))


def test_node_timeout_errors_are_transient():
    assert is_transient_error(
        NodeTimeoutError(node="router", elapsed=0.05, kind="run", run_timeout=0.05)
    )


def test_timeout_policy_uses_run_budget_without_idle_timeout():
    policy = make_timeout_policy(0.05)

    assert policy.run_timeout == 0.05
    assert policy.idle_timeout is None


@pytest.mark.parametrize("duration", [0, -0.1, float("inf"), float("-inf"), float("nan")])
def test_timeout_policy_rejects_non_positive_or_non_finite_durations(duration):
    with pytest.raises(ValueError, match="positive finite"):
        make_timeout_policy(duration)


def test_settings_expose_node_timeout_aliases(monkeypatch):
    monkeypatch.setenv("JEV_NODE_TIMEOUT_SECONDS", "3.1")
    monkeypatch.setenv("ROUTER_NODE_TIMEOUT_SECONDS", "6.1")
    monkeypatch.setenv("LLM_NODE_TIMEOUT_SECONDS", "12.1")

    settings = Settings(_env_file=None)

    assert settings.jev_node_timeout_seconds == 3.1
    assert settings.router_node_timeout_seconds == 6.1
    assert settings.llm_node_timeout_seconds == 12.1


def test_retry_policies_have_bounded_attempts():
    assert RETRY_LLM.max_attempts == 3
    assert RETRY_DEEPSEEK_ROUTER.max_attempts == 2


def test_root_error_handler_marks_failed():
    error = NodeError(node="router_jev", error=RuntimeError("provider unavailable"))
    output = root_error_handler({}, error)
    assert isinstance(output, Command)
    assert output.update == {"execution_status": "failed"}
    assert output.goto == "final"


@pytest.mark.asyncio
async def test_root_error_handler_recovers_failing_async_node(monkeypatch):
    import ai_service_agent.graph.fault_tolerance as fault_tolerance

    class GraphState(TypedDict):
        execution_status: str

    def final(state: GraphState):
        return None

    async def failing(state: GraphState):
        raise RuntimeError("async failure")

    builder = StateGraph(GraphState)
    builder.add_node("failing", failing)
    builder.add_node("final", final)
    builder.add_edge(START, "failing")
    builder.add_edge("failing", "final")
    builder.add_edge("final", END)
    builder.set_node_defaults(error_handler=root_error_handler)
    graph = builder.compile()

    events = []
    monkeypatch.setattr(fault_tolerance, "get_stream_writer", lambda: events.append)

    assert await graph.ainvoke({"execution_status": "running"}) == {
        "execution_status": "failed"
    }
    assert events == [{
        "type": "error",
        "data": {"code": "runtime_error", "message": RUNTIME_ERROR_TEXT},
    }]


@pytest.mark.asyncio
async def test_subgraph_error_handler_recovers_failing_async_node():
    class GraphState(TypedDict):
        execution_status: str

    async def failing(state: GraphState):
        raise RuntimeError("async failure")

    subgraph_builder = StateGraph(GraphState)
    subgraph_builder.add_node("failing", failing)
    subgraph_builder.add_node("final", lambda state: None)
    subgraph_builder.add_edge(START, "failing")
    subgraph_builder.add_edge("failing", "final")
    subgraph_builder.add_edge("final", END)
    subgraph_builder.set_node_defaults(error_handler=make_subgraph_error_handler("final"))
    subgraph = subgraph_builder.compile()

    root_builder = StateGraph(GraphState)
    root_builder.add_node("worker", subgraph)
    root_builder.add_edge(START, "worker")
    root_builder.add_edge("worker", END)
    root_graph = root_builder.compile()

    assert await root_graph.ainvoke({"execution_status": "running"}) == {
        "execution_status": "failed"
    }


def test_subgraph_error_handler_routes_to_local_finalizer():
    error = NodeError(node="tools", error=RuntimeError("database unavailable"))
    handler = make_subgraph_error_handler("finalize_failed")
    command = handler({}, error)
    assert command.goto == "finalize_failed"
    assert command.update == {"execution_status": "failed"}


def test_runtime_error_text_is_customer_safe():
    assert "Traceback" not in RUNTIME_ERROR_TEXT
    assert "转人工" in RUNTIME_ERROR_TEXT


def test_error_event_is_normalized_and_customer_safe(monkeypatch):
    import ai_service_agent.graph.fault_tolerance as fault_tolerance

    events = []
    monkeypatch.setattr(fault_tolerance, "get_stream_writer", lambda: events.append)
    error = NodeError(node="tools", error=RuntimeError("database unavailable"))

    fault_tolerance.root_error_handler({}, error)

    assert events == [{
        "type": "error",
        "data": {"code": "runtime_error", "message": RUNTIME_ERROR_TEXT},
    }]
