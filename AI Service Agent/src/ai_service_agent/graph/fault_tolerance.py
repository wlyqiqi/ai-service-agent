from collections.abc import Callable

import httpx
import openai
from langgraph.config import get_stream_writer
from langgraph.errors import NodeError, NodeTimeoutError
from langgraph.types import Command, RetryPolicy, TimeoutPolicy

from ai_service_agent.graph.workflows.copy import RUNTIME_ERROR_TEXT


TRANSIENT_HTTP_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


def _http_status(error: Exception) -> int | None:
    if isinstance(error, openai.APIStatusError):
        return getattr(error, "status_code", None)
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code
    return None


def is_transient_error(error: BaseException) -> bool:
    """Return True only for errors that are safe to retry at the LLM boundary."""
    if isinstance(
        error,
        (
            NodeTimeoutError,
            TimeoutError,
            ConnectionError,
            httpx.TransportError,
        ),
    ):
        return True
    if isinstance(error, (openai.APIConnectionError, openai.RateLimitError)):
        return True
    status = _http_status(error)
    return status is not None and status in TRANSIENT_HTTP_STATUSES


RETRY_LLM = RetryPolicy(
    max_attempts=3,
    initial_interval=0.5,
    backoff_factor=2.0,
    max_interval=4.0,
    jitter=True,
    retry_on=is_transient_error,
)

RETRY_DEEPSEEK_ROUTER = RetryPolicy(
    max_attempts=2,
    initial_interval=0.5,
    backoff_factor=2.0,
    max_interval=2.0,
    jitter=True,
    retry_on=is_transient_error,
)


def make_timeout_policy(run_timeout: float) -> TimeoutPolicy:
    return TimeoutPolicy(run_timeout=run_timeout)


def _emit_node_error(error: NodeError) -> None:
    try:
        get_stream_writer()({
            "type": "error",
            "data": {
                "code": "runtime_error",
                "message": RUNTIME_ERROR_TEXT,
            },
        })
    except RuntimeError:
        pass


def root_error_handler(state: dict, error: NodeError) -> Command:
    _emit_node_error(error)
    return Command(
        update={"execution_status": "failed"},
        goto="final",
    )


def make_subgraph_error_handler(final_node: str) -> Callable[[dict, NodeError], Command]:
    def handler(state: dict, error: NodeError) -> Command:
        _emit_node_error(error)
        return Command(
            update={"execution_status": "failed"},
            goto=final_node,
        )

    return handler
