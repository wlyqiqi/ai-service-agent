"""Guard 确定性 v1（D2 过渡声明，非终态）。

v1 是过渡方案，不是 Guard 终态能力：确定性关键词/正则无法有效覆盖语义层面的恶意输入/
越权诱导（同义替换、间接暗示等绕过关键词匹配的攻击）。后续必须评估升级为 LLM Guard 或
其它语义层判断方式（升级评估登记：docs/decisions/2026-09-15-p3-design-decisions.md §2）。
"""

import re
from typing import Callable

from ai_service_agent.config import get_settings

GUARD_REJECT_TEXT = "该请求存在安全风险，已停止处理。如有疑问请联系人工客服。"


def make_guard_node(patterns: list[str] | None = None) -> Callable[[dict], dict]:
    """构造 Guard 节点：只输出 guard_status（pass/fail），不执行 tools、不改业务状态（graph-spec §2）。"""
    compiled = [re.compile(p) for p in (patterns if patterns is not None else get_settings().guard_patterns)]

    def guard(state: dict) -> dict:
        last = ""
        for m in reversed(state.get("messages", [])):
            if getattr(m, "type", "") == "human":
                last = m.content if isinstance(m.content, str) else str(m.content)
                break
        if any(p.search(last) for p in compiled):
            return {"guard_status": "fail"}
        return {"guard_status": "pass"}

    return guard
