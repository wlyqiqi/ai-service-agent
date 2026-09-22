"""Harness tool hooks（architecture.md §8.1：挂在循环上，不写进循环里）。

MVP 只做日志/观测：State 只读、不发 SSE 事件、hook 异常 fail-open。
通过 ToolNode 原生 awrap_tool_call 挂载（LangGraph 1.2.10 实测签名：
async def wrapper(request, execute) -> ToolMessage | Command）。
"""

import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol

from langchain_core.messages import ToolMessage
from langgraph.types import Command

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolHookContext:
    tool_name: str
    tool_call_id: str
    args: dict[str, Any]
    user_id: str
    thread_id: str


@dataclass(frozen=True)
class ToolHookOutcome:
    ok: bool
    result_summary: str | None
    error: str | None
    duration_ms: float


class ToolHook(Protocol):
    def on_before(self, context: ToolHookContext) -> None: ...

    def on_after(self, context: ToolHookContext, outcome: ToolHookOutcome) -> None: ...


class LoggingToolHook:
    """默认观测 hook：结构化日志（stdlib logging，观测体系后续升级 structlog）。"""

    def on_before(self, context: ToolHookContext) -> None:
        logger.info(
            "tool.call",
            extra={
                "tool_name": context.tool_name,
                "tool_call_id": context.tool_call_id,
                "user_id": context.user_id,
                "thread_id": context.thread_id,
            },
        )

    def on_after(self, context: ToolHookContext, outcome: ToolHookOutcome) -> None:
        payload = {
            "tool_name": context.tool_name,
            "tool_call_id": context.tool_call_id,
            "user_id": context.user_id,
            "thread_id": context.thread_id,
            "duration_ms": round(outcome.duration_ms, 3),
        }
        if outcome.ok:
            logger.info("tool.result", extra={**payload, "summary": (outcome.result_summary or "")[:120]})
        else:
            logger.warning("tool.result.error", extra={**payload, "error": outcome.error})


def build_hook_context(request: Any) -> ToolHookContext:
    """从 ToolCallRequest 提取只读上下文（鸭子类型，不 import 私有类）。"""
    tool_call = request.tool_call
    state = request.state if isinstance(request.state, dict) else {}
    runtime = getattr(request, "runtime", None)
    config = getattr(runtime, "config", None) or {}
    configurable = config.get("configurable") or {}
    return ToolHookContext(
        tool_name=tool_call["name"],
        tool_call_id=tool_call["id"],
        args=dict(tool_call.get("args") or {}),
        user_id=str(state.get("user_id") or ""),
        thread_id=str(configurable.get("thread_id") or ""),
    )


def _is_ok(result: Any) -> bool:
    if isinstance(result, ToolMessage):
        return result.status != "error"
    return True


def _summarize(result: Any) -> str | None:
    if isinstance(result, ToolMessage):
        text = result.content if isinstance(result.content, str) else str(result.content)
        return text[:200]
    if isinstance(result, Command):
        return "command"
    if result is None:
        return None
    return str(result)[:200]


def make_tool_hook_wrapper(hooks):
    """生成 ToolNode awrap_tool_call 兼容包装器；hooks 为空返回 None（零开销直通）。"""
    hook_list = list(hooks)
    if not hook_list:
        return None

    async def wrapper(request: Any, execute) -> Any:
        context = build_hook_context(request)
        for hook in hook_list:
            try:
                hook.on_before(context)
            except Exception:
                logger.exception("tool hook on_before failed; fail-open")
        start = time.perf_counter()
        try:
            result = await execute(request)
        except Exception as exc:
            duration_ms = (time.perf_counter() - start) * 1000
            outcome = ToolHookOutcome(ok=False, result_summary=None, error=str(exc), duration_ms=duration_ms)
            for hook in hook_list:
                try:
                    hook.on_after(context, outcome)
                except Exception:
                    logger.exception("tool hook on_after failed; fail-open")
            raise
        duration_ms = (time.perf_counter() - start) * 1000
        outcome = ToolHookOutcome(
            ok=_is_ok(result), result_summary=_summarize(result), error=None, duration_ms=duration_ms
        )
        for hook in hook_list:
            try:
                hook.on_after(context, outcome)
            except Exception:
                logger.exception("tool hook on_after failed; fail-open")
        return result

    return wrapper
