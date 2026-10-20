"""Mutation / HighRisk 写成功的出站投影（Transport 无关）。

从 ``agent_bot.py`` 抽出：``WriteSuccessCapture`` 与 Chatwoot 卡片投影同时被
AgentBot 与 Approval resume 使用；放在任一 Transport 适配器里都会让另一个
反向依赖它（``server`` 已经需要 ``from agent_bot import ...``）。

本模块只做「事件流 → 卡片 spec → Chatwoot items」，不含任何业务决策。
"""

from typing import Any

from ai_service_agent.graph.workflows.copy import (
    MutationSuccessCardSpec,
    build_mutation_success_card,
)
from ai_service_agent.harness.chatwoot import ChatwootClient

_WRITE_SUCCESS_TOOLS = frozenset({
    "update_address",
    "update_phone",
    "update_order_note",
    "create_invoice",
    "create_service_ticket",
    "cancel_order",
    "refund_apply",
})


class WriteSuccessCapture:
    """Remember the latest successful write in one event stream (args + tool payload)."""

    def __init__(self) -> None:
        self._args: dict[str, dict[str, Any]] = {}
        self.tool: str | None = None
        self.args: dict[str, Any] = {}
        self.result: dict | None = None

    def observe(self, event: dict) -> None:
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        name = data.get("name")
        if not isinstance(name, str) or name not in _WRITE_SUCCESS_TOOLS:
            return
        if event.get("type") == "tool_call":
            args = data.get("args") if isinstance(data.get("args"), dict) else {}
            self._args[name] = args
        elif event.get("type") == "tool_result" and data.get("ok"):
            result = data.get("result") if isinstance(data.get("result"), dict) else None
            self.tool = name
            self.args = self._args.get(name, {})
            self.result = result

    def spec(self) -> MutationSuccessCardSpec | None:
        if self.tool is None:
            return None
        return build_mutation_success_card(self.tool, self.args, self.result)


def mutation_success_card_items(spec: MutationSuccessCardSpec) -> list[dict[str, Any]]:
    """Chatwoot `cards` items. Status is the first description bullet (no native badge).

    `actions` is required by Chatwoot card validation (informational ack).
    """
    return [
        {
            "title": spec.title,
            "description": spec.description,
            "actions": [
                {
                    "type": "postback",
                    "text": spec.button_text,
                    "payload": spec.ack_payload,
                }
            ],
        }
    ]


def update_address_success_card_items(*, order_id: str, address: str) -> list[dict[str, Any]]:
    """Backward-compatible address card items (#73), now via the shared spec."""
    spec = build_mutation_success_card(
        "update_address",
        {"order_id": order_id, "address": address},
        None,
    )
    assert spec is not None
    return mutation_success_card_items(spec)


async def send_mutation_success_card(
    chatwoot: ChatwootClient,
    conversation_id: str,
    spec: MutationSuccessCardSpec,
    *,
    followup: bool = True,
) -> str | None:
    """Project a write-tool confirm success to Chatwoot `cards` (+ optional short text).

    Prefer cards over the long Graph `done.final`; return the follow-up bubble
    (or `None`) so the Adapter can store/return it instead of that long done.
    """
    items = mutation_success_card_items(spec)
    await chatwoot.send_interactive_message(
        conversation_id,
        spec.title,
        "cards",
        {"items": items},
    )
    if not followup:
        return None
    await chatwoot.send_bot_message(conversation_id, spec.followup)
    return spec.followup
