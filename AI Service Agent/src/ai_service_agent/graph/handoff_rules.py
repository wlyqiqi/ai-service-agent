"""显式转人工意图检测（safety-spec.md §2.1 第一类：显式请求）。

detector 只判定「用户明确要求转人工」：确定性、无 LLM、无外部调用，可纯单测。
流程：normalize（去空白/小写）→ explicit phrase（完整短语）→ intent pattern
（动作前缀 + 近邻目标词，排除话题性后缀）。

不处理动态信号（工具连续失败 / 重复未解决 / 情绪升级 / 审批被拒后坚持等），
那些依赖 context / 运行状态，由 graph 内 check_handoff 负责（graph-spec.md §6.4）。
"""

import re

from ai_service_agent.config import get_settings

# 动作前缀：用户「要求/请求转接」的动词短语。刻意不含裸「人工」子串，防话题性误伤。
_ACTION_PREFIX = r"(?:转接|转|接|找|叫|让|请|联系|安排|派|呼叫|帮我|给我|我要|想找)"

# 目标词（不含裸「人」，防「找个人帮我看看」等泛化表达误伤）。
_TARGET = r"(?:人工|真人|客服)"


def detect_handoff_intent(text: str) -> bool:
    """显式转人工意图判定（P0 D2）。输入相同时结果恒定。

    False 不代表没有转人工意图：动态信号类（依赖 context）由图内 check_handoff 判定。
    """
    if not text:
        return False
    norm = re.sub(r"\s+", "", text).lower()
    if norm in set(get_settings().handoff_phrases):
        return True
    suffix = "|".join(re.escape(s) for s in get_settings().handoff_negative_suffix)
    pattern = re.compile(
        _ACTION_PREFIX + r".{0,4}?" + _TARGET + r"(?!.{0,2}(?:" + suffix + r"))"
    )
    return bool(pattern.search(norm))


def detect_handoff_cancel(text: str) -> bool:
    """Handoff Cancel 入口层判定（ADR-0003）：确定性、无 LLM。

    仅在已有 awaiting hang 时由入口调用；本函数本身只看文案。
    """
    if not text:
        return False
    norm = re.sub(r"\s+", "", text).lower()
    phrases = set(get_settings().handoff_cancel_phrases)
    if norm in phrases:
        return True
    return any(p and p in norm for p in phrases)
