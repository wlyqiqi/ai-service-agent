"""Operation Clarify + Hybrid Tool Resolution (entry layer, before MutationGraph).

Vague mutation-like intents are clarified in chat here. MutationGraph is only
entered once a concrete mutation tool is known (CONTEXT.md / mutation-form-interrupt-spec).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence

from langchain_core.messages import AIMessage
from langgraph.config import get_stream_writer

from ai_service_agent.graph.workflows.copy import operation_clarify_text

# Mutation tools only (HighRisk cancel/refund stay on approval flows).
MUTATION_TOOLS: tuple[str, ...] = (
    "update_address",
    "update_phone",
    "update_order_note",
    "create_invoice",
    "create_service_ticket",
)

# High-confidence keyword → tool. Unique match wins Hybrid Tool Resolution.
_TOOL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "update_address",
        re.compile(r"收货地址|(改|修改|换|更新).{0,8}地址|地址.{0,8}(改|修改|换|更新)"),
    ),
    (
        "update_phone",
        re.compile(
            r"联系电话|手机号|换绑.{0,6}手机|"
            r"(改|修改|换|更新).{0,8}(电话|手机)|"
            r"(电话|手机).{0,8}(改|修改|换|更新|换绑)"
        ),
    ),
    ("update_order_note", re.compile(r"订单备注|加个备注|(改|修改).{0,6}备注")),
    ("create_invoice", re.compile(r"发票|开票")),
    ("create_service_ticket", re.compile(r"售后|工单")),
)

# Vague mutation-like umbrella phrases → Operation Clarify (chat, no form).
# Keep tight: must not fire on concrete tool phrasing (地址/发票/…).
_VAGUE_MUTATION_PATTERN = re.compile(
    r"(改一下|修改一下|更改一下|修改|更改|改)\s*(一下\s*)?(我的\s*)?订单|"
    r"订单\s*(要\s*)?(改一下|修改一下|改|修改)|"
    r"modify\s+(my\s+)?order|"
    r"change\s+(my\s+)?order",
    re.IGNORECASE,
)


def resolve_mutation_tool(text: str, allowed_tools: Sequence[str] | None = None) -> str | None:
    """Deterministic Hybrid Tool Resolution: unique high-confidence tool, else None.

    When ``allowed_tools`` is set, only those tools may match (Policy allowlist).
    """
    allow = set(allowed_tools) if allowed_tools is not None else set(MUTATION_TOOLS)
    matches: list[str] = []
    for tool_name, pattern in _TOOL_PATTERNS:
        if tool_name not in allow:
            continue
        if pattern.search(text or ""):
            matches.append(tool_name)
    if len(matches) == 1:
        return matches[0]
    return None


def needs_operation_clarify(text: str, allowed_tools: Sequence[str] | None = None) -> bool:
    """True when mutation intent is vague and no unique tool maps (Operation Clarify)."""
    if resolve_mutation_tool(text, allowed_tools) is not None:
        return False
    return bool(_VAGUE_MUTATION_PATTERN.search(text or ""))


def make_hybrid_tool_resolve_node() -> Callable[[dict], dict]:
    """Narrow ``allowed_tools`` when unique; vague-only → Operation Clarify.

    Non-vague, non-matching text still enters MutationGraph so select can
    reject unknown tools (Closed MutationGraph; no internal business handoff).
    """

    def hybrid_tool_resolve(state: dict) -> dict:
        if state.get("target_graph") != "mutation_graph":
            return {}
        if state.get("guard_status") == "fail":
            return {}
        text = _latest_user_text(state)
        allowed = list(state.get("allowed_tools") or MUTATION_TOOLS)
        resolved = resolve_mutation_tool(text, allowed)
        ctx = {**state.get("context", {})}
        ctx.pop("operation_clarify", None)
        if resolved is not None:
            return {"allowed_tools": [resolved], "context": ctx}
        if len(allowed) == 1 and allowed[0] in MUTATION_TOOLS:
            return {"allowed_tools": allowed, "context": ctx}
        if needs_operation_clarify(text, allowed):
            ctx["operation_clarify"] = True
            return {"context": ctx}
        return {"context": ctx}

    return hybrid_tool_resolve


def make_operation_clarify_node() -> Callable[[dict], dict]:
    """Chat Operation Clarify outside MutationGraph (no Mutation Form / form_required)."""

    def operation_clarify(state: dict) -> dict:
        text = operation_clarify_text()
        data = {"missing": [], "reason": "operation_unclear"}
        try:
            get_stream_writer()({"type": "clarification_required", "data": data})
        except RuntimeError:
            pass
        ctx = {**state.get("context", {})}
        ctx.pop("operation_clarify", None)
        return {
            "messages": [AIMessage(content=text)],
            "context": ctx,
            "execution_status": "clarifying",
        }

    return operation_clarify


def _latest_user_text(state: dict) -> str:
    messages = state.get("messages") or []
    for message in reversed(messages):
        if isinstance(message, dict):
            role = message.get("type", message.get("role", ""))
            content = message.get("content", "")
        else:
            role = getattr(message, "type", "")
            content = getattr(message, "content", "")
        if role in ("human", "user"):
            return str(content)
    return ""
