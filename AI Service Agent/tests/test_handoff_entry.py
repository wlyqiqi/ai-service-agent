"""Seat-gated 入口策略真值表（handoff-spec.md §2）。

`decide_entry` 是 `/chat` 与 AgentBot 的唯一入口策略，纯函数，不需要 FastAPI / DB。
"""

import pytest

from ai_service_agent.harness.handoff_entry import (
    EntryDecision,
    decide_entry,
    is_human_lane,
)


@pytest.mark.parametrize(
    "mode, awaiting, text, expected",
    [
        # mode 门控先于任何检测
        ("human", True, "我要转人工", EntryDecision.HUMAN_LANE),
        ("human", True, "取消转人工", EntryDecision.HUMAN_LANE),
        ("human", False, "查订单", EntryDecision.HUMAN_LANE),
        # 取消：只在有挂起申请时成立
        ("ai", True, "取消转人工", EntryDecision.CANCEL),
        ("ai", True, "不用转人工了", EntryDecision.CANCEL),
        # 无挂起时「取消转人工」命中转人工措辞 → 按显式申请处理（既有行为，保持不变）
        ("ai", False, "取消转人工", EntryDecision.REQUEST),
        # 取消措辞不同时不误判为申请
        ("ai", False, "取消人工", EntryDecision.AI),
        # 申请：挂起期间重复申请仍是 REQUEST，幂等由 request_handoff.created 决定
        ("ai", False, "我要转人工", EntryDecision.REQUEST),
        ("ai", True, "我要转人工", EntryDecision.REQUEST),
        # 正常 AI 车道
        ("ai", False, "查一下订单", EntryDecision.AI),
        ("ai", True, "查一下订单", EntryDecision.AI),
        # 缺失 / 意外 mode 值 fail-open 到 ai（handoff-spec §1.1）
        ("", False, "我要转人工", EntryDecision.REQUEST),
        ("invalid", False, "查订单", EntryDecision.AI),
    ],
)
def test_decide_entry_truth_table(mode, awaiting, text, expected):
    assert decide_entry(mode=mode, awaiting=awaiting, text=text) is expected


def test_decide_entry_is_deterministic():
    kwargs = {"mode": "ai", "awaiting": True, "text": "我要转人工"}
    assert decide_entry(**kwargs) is decide_entry(**kwargs)


def test_is_human_lane_is_fail_open():
    assert is_human_lane("human") is True
    assert is_human_lane("ai") is False
    assert is_human_lane("") is False
    assert is_human_lane("HUMAN") is False
