"""投诉硬闸与投诉询问回复解析（确定性、无 LLM）。

与 ``detect_handoff_intent`` 并列，但**不**走入口人工车道：命中只供 Policy Gate
强制图内 ``route_intent=complaint``。同句显式转人工优先于本闸。
"""

from __future__ import annotations

import re

from ai_service_agent.config import get_settings
from ai_service_agent.graph.handoff_rules import detect_handoff_intent

# 硬闸覆盖 LLM 时写入的置信度，高于投诉档阈值（0.80）。
COMPLAINT_GATE_CONFIDENCE = 1.0


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text or "").lower()


def _exact_reply(text: str) -> str:
    return _norm(text).strip("。！？!?，,、~～.…")


def detect_complaint_intent(text: str) -> bool:
    """明显投诉短语。弱情绪（如「有点不满意」）不在默认短语里，交给 Router。"""
    if not text or not str(text).strip():
        return False
    norm = _norm(text)
    phrases = [phrase for phrase in get_settings().complaint_phrases if phrase]
    return any(_norm(phrase) in norm for phrase in phrases)


def complaint_gate_applies(text: str) -> bool:
    """同句：``detect_handoff_intent`` 优先，投诉闸不覆盖转人工。"""
    if detect_handoff_intent(text):
        return False
    return detect_complaint_intent(text)


def classify_complaint_offer_reply(text: str) -> str:
    """投诉询问的下一句：``agree`` / ``reconfirm`` / ``clear``。

    肯定集收窄（转人工 / 要人工 / 帮我转 等）。裸「要」再确认。
    其余（拒绝或改提业务）视为 clear，交回 Router。
    """
    norm = _norm(text)
    if not norm:
        return "clear"
    agree = [phrase for phrase in get_settings().complaint_offer_agree_phrases if phrase]
    if any(_norm(phrase) in norm for phrase in agree):
        return "agree"
    exact = {_exact_reply(phrase) for phrase in get_settings().complaint_offer_reconfirm_exact if phrase}
    if _exact_reply(text) in exact:
        return "reconfirm"
    return "clear"


def emotion_auto_handoff_allowed(context: dict | None) -> bool:
    """投诉 offer 待答期间，禁止情绪规则自动 ``handoff_request``。"""
    if not context:
        return True
    return not bool(context.get("complaint_offer_pending"))
