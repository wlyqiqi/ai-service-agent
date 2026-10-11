"""Guard 确定性 v2（低延迟，无 LLM）。

与 Router 并行的图节点：命名规则包匹配最后一条 human 消息，只输出
guard_status / guard_reason。不做语义模型判断；情绪类威胁不在此 reject。
"""

from __future__ import annotations

import re
from typing import Callable

from ai_service_agent.config import get_settings
from ai_service_agent.graph.workflows.copy import guard_reject_text

GUARD_REJECT_TEXT = guard_reject_text()

# 默认规则包顺序即优先级（先命中先生效）。
DEFAULT_GUARD_RULE_PATTERNS: dict[str, list[str]] = {
    "prompt_injection": [r"忽略.*规则"],
    "approval_bypass": [r"绕过审批", r"不需要审批", r"不用审批"],
    "privilege_spoof": [r"系统管理员", r"admin"],
    "state_forgery": [r"查询.*标记为已退款"],
}

KNOWN_GUARD_REASONS = frozenset(DEFAULT_GUARD_RULE_PATTERNS)


def _last_human_text(state: dict) -> str:
    for message in reversed(state.get("messages", [])):
        if getattr(message, "type", "") == "human":
            content = message.content
            return content if isinstance(content, str) else str(content)
    return ""


def _compile_rule_packs(
    rule_patterns: dict[str, list[str]],
) -> list[tuple[str, list[re.Pattern[str]]]]:
    compiled: list[tuple[str, list[re.Pattern[str]]]] = []
    for reason, patterns in rule_patterns.items():
        if not patterns:
            continue
        compiled.append((reason, [re.compile(pattern) for pattern in patterns]))
    return compiled


def resolve_guard_rule_patterns(
    rule_patterns: dict[str, list[str]] | None = None,
    patterns: list[str] | None = None,
) -> dict[str, list[str]]:
    """解析生效规则包。

    - ``patterns`` 非空：兼容旧 API，整表替换为单一 ``custom`` 包。
    - 否则使用 ``rule_patterns``，缺省读 Settings.guard_rule_patterns。
    """
    if patterns is not None:
        return {"custom": list(patterns)}
    if rule_patterns is not None:
        return dict(rule_patterns)
    return dict(get_settings().guard_rule_patterns)


def make_guard_node(
    patterns: list[str] | None = None,
    rule_patterns: dict[str, list[str]] | None = None,
) -> Callable[[dict], dict]:
    """构造 Guard 节点：只输出 guard_status / guard_reason（graph-spec §2）。"""
    packs = _compile_rule_packs(resolve_guard_rule_patterns(rule_patterns, patterns))

    def guard(state: dict) -> dict:
        last = _last_human_text(state)
        for reason, compiled_patterns in packs:
            if any(pattern.search(last) for pattern in compiled_patterns):
                return {"guard_status": "fail", "guard_reason": reason}
        return {"guard_status": "pass", "guard_reason": None}

    return guard
