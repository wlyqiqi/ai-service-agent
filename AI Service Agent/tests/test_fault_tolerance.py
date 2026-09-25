import httpx
import openai
from langgraph.errors import NodeError

from ai_service_agent.graph.fault_tolerance import (
    RETRY_DEEPSEEK_ROUTER,
    RETRY_LLM,
    is_transient_error,
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


def test_retry_policies_have_bounded_attempts():
    assert RETRY_LLM.max_attempts == 3
    assert RETRY_DEEPSEEK_ROUTER.max_attempts == 2


def test_root_error_handler_marks_failed():
    error = NodeError(node="router_jev", error=RuntimeError("provider unavailable"))
    output = root_error_handler({}, error)
    assert output == {"execution_status": "failed"}


def test_subgraph_error_handler_routes_to_local_finalizer():
    error = NodeError(node="tools", error=RuntimeError("database unavailable"))
    handler = make_subgraph_error_handler("finalize_failed")
    command = handler({}, error)
    assert command.goto == "finalize_failed"
    assert command.update == {"execution_status": "failed"}


def test_runtime_error_text_is_customer_safe():
    assert "Traceback" not in RUNTIME_ERROR_TEXT
    assert "转人工" in RUNTIME_ERROR_TEXT
