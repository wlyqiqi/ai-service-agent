"""AI 回复 / 审批结果的「落库 + 出站」门控（唯一实现）。

此前三处各写一份：``server.py`` 的 ``/chat``、``agent_bot.py`` 的 AgentBot 出口、
``server.py`` 的 ``_approval_resume``。三份副本已经产生可观测分歧：同一笔审批，
``human`` 期间落库的是长 ``done.final``，``ai`` 期间落库的是短 followup。

差异的真实来源不是「policy」，而是**谁拥有这一轮**：

- ``AI_TURN``：客户侧 AI 回合（``/chat``、AgentBot）。会话被坐席接管时整轮作废——
  既不落 Message Store 也不出站，否则会把 AI 气泡写进人工接管后的客户历史。
- ``SEAT_DECISION``：坐席审批后的图恢复。执行结果**必须**落 Message Store
  （客户历史要能看到结果），出站仍受 ``conversation_mode`` 门控
  （``handoff-spec`` §1.4 / ADR-0002）。

出站文案与落库文案同源：有 ``success_spec`` 用短 followup，否则用 ``done.final``。
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any

# SSE 对外提示码（event-spec）：AI 回合被切走时 /chat 用它告知客户端。
MODE_CHANGED_NOTICE = "ModeChanged"


class TurnOrigin(str, Enum):
    """这一轮由谁拥有——决定 mode 门控是否连落库一起作废。"""

    AI_TURN = "ai_turn"              # /chat、AgentBot
    SEAT_DECISION = "seat_decision"  # 审批 resume


@dataclass(frozen=True)
class DeliveryDecision:
    persist: str | None = None   # 落 Message Store 的 assistant 文案（None = 不落）
    outbound: bool = False       # 是否发 Chatwoot
    card: bool = False           # True → 调用方用成功卡片出站（需持 success_spec）
    notice: str | None = None    # 对外提示码；None = 静默


def decide_delivery(
    *,
    mode: str,
    origin: TurnOrigin,
    success_spec: Any = None,
    final: str | None = None,
    can_send: bool = True,
) -> DeliveryDecision:
    """唯一门控。空文案视为无内容（不落库、不出站）。"""
    gated = mode != "ai"

    if gated and origin is TurnOrigin.AI_TURN:
        # 整轮作废：坐席已接管，AI 气泡不得进入客户历史。
        return DeliveryDecision(notice=MODE_CHANGED_NOTICE)

    text = success_spec.followup if success_spec is not None else final
    if not text:
        return DeliveryDecision()

    return DeliveryDecision(
        persist=text,
        outbound=(not gated) and can_send,
        card=success_spec is not None,
    )
