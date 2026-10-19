"""Seat-gated 入口策略：一条入站消息该进哪条车道（handoff-spec.md §2）。

单一策略，两个 Transport 共用（`/chat` 与 AgentBot）。本模块只回答「该做什么」：

- 不落库、不发 Chatwoot、不构造 SSE 事件 —— 那是 Transport 的职责；
- 不判 form —— `form_gate.py` 的职责，适配器先确认这是文本入站；
- 不查库 —— `mode` / `awaiting` 由调用方读取后传入，判断保持轻量（§2）。

`_Avoid_`：把 `submitted_values`、渠道差异或 per-entry 开关塞进接口。那会把
「按入口分叉」重新合法化，而本模块存在的理由正是消除它。
"""

from __future__ import annotations

from enum import Enum

from ai_service_agent.graph.handoff_rules import detect_handoff_cancel, detect_handoff_intent

HUMAN_MODE = "human"


class EntryDecision(str, Enum):
    """一条文本入站的处理去向。"""

    HUMAN_LANE = "human_lane"  # mode=human：只写 Message Store，不跑图
    CANCEL = "cancel"          # 挂起期间取消转人工：清挂起，不切 mode，不进 Graph
    REQUEST = "request"        # 显式转人工：挂起申请（mode 仍 ai），本 turn 只回确认
    AI = "ai"                  # 正常 AI 车道


def is_human_lane(mode: str) -> bool:
    """只有 `human` 是人工车道；缺失 / 意外值按 ai 放行（handoff-spec.md §1.1）。"""
    return mode == HUMAN_MODE


def decide_entry(*, mode: str, awaiting: bool, text: str) -> EntryDecision:
    """决定一条文本入站的去向。确定性、无 I/O，输入相同结果恒定。

    顺序即策略：mode 门控先于任何检测；`CANCEL` 只在有挂起申请时成立（§2）；
    其余命中显式转人工措辞的走 `REQUEST`。取消措辞先于请求措辞判定，因为
    「取消转人工」同时命两者，语义上必须是取消。
    """
    if is_human_lane(mode):
        return EntryDecision.HUMAN_LANE
    if awaiting and detect_handoff_cancel(text):
        return EntryDecision.CANCEL
    if detect_handoff_intent(text):
        return EntryDecision.REQUEST
    return EntryDecision.AI
