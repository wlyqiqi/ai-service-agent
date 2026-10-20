"""出站门控：一张表锁死落库 / 出站 / 卡片 / 提示码。

取代三份副本（``/chat``、AgentBot、审批 resume）。
"""

import pytest

from ai_service_agent.harness.outbound_gate import (
    MODE_CHANGED_NOTICE,
    DeliveryDecision,
    TurnOrigin,
    decide_delivery,
)


class _Spec:
    """``MutationSuccessCardSpec`` 的最小替身：门控只读 ``followup``。"""

    def __init__(self, followup: str) -> None:
        self.followup = followup


AI = TurnOrigin.AI_TURN
SEAT = TurnOrigin.SEAT_DECISION


@pytest.mark.parametrize(
    "mode,origin,spec,final,expected",
    [
        # ai：卡片优先，落库与出站都用短 followup
        ("ai", AI, _Spec("已提交"), "长文案", DeliveryDecision("已提交", True, True, None)),
        ("ai", SEAT, _Spec("已提交"), "长文案", DeliveryDecision("已提交", True, True, None)),
        # ai：无卡片 → 落 done.final 并出站
        ("ai", AI, None, "普通回复", DeliveryDecision("普通回复", True, False, None)),
        ("ai", SEAT, None, "普通回复", DeliveryDecision("普通回复", True, False, None)),
        # human + 坐席决策：结果必须落库（客户历史），但不出站
        ("human", SEAT, _Spec("已提交"), "长文案", DeliveryDecision("已提交", False, True, None)),
        ("human", SEAT, None, "普通回复", DeliveryDecision("普通回复", False, False, None)),
        # human + AI 回合：整轮作废 + 提示码
        ("human", AI, _Spec("已提交"), "长文案", DeliveryDecision(None, False, False, MODE_CHANGED_NOTICE)),
        ("human", AI, None, "普通回复", DeliveryDecision(None, False, False, MODE_CHANGED_NOTICE)),
    ],
)
def test_delivery_table(mode, origin, spec, final, expected):
    assert decide_delivery(mode=mode, origin=origin, success_spec=spec, final=final) == expected


def test_human_seat_decision_persists_the_same_text_as_ai_mode():
    """D1=A：同一审批在 human / ai 下落同一文案（此前分叉为长 done.final）。"""
    spec = _Spec("退款申请已提交")
    ai = decide_delivery(mode="ai", origin=SEAT, success_spec=spec, final="通过审核")
    human = decide_delivery(mode="human", origin=SEAT, success_spec=spec, final="通过审核")
    assert ai.persist == human.persist == "退款申请已提交"
    assert ai.outbound is True
    assert human.outbound is False


@pytest.mark.parametrize("final", [None, ""], ids=["none", "empty"])
def test_empty_final_is_a_noop(final):
    assert decide_delivery(mode="ai", origin=AI, final=final) == DeliveryDecision()


def test_cannot_send_still_persists_seat_decision():
    """没有 Chatwoot 客户端（单测 / 无出站渠道）时仍要落库。"""
    decision = decide_delivery(
        mode="ai", origin=SEAT, success_spec=_Spec("已提交"), can_send=False
    )
    assert decision.persist == "已提交"
    assert decision.outbound is False
    assert decision.card is True


def test_cannot_send_does_not_resurrect_dropped_ai_turn():
    decision = decide_delivery(mode="human", origin=AI, final="x", can_send=False)
    assert decision.persist is None
    assert decision.outbound is False
