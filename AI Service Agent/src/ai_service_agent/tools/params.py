"""工具参数占位哨兵。读路径与写路径共用这一处定义。"""

import logging
from uuid import uuid4

logger = logging.getLogger("ai_service_agent")

_ABSENT_SENTINELS = frozenset({"", "null", "none", "nil", "undefined", "n/a", "na"})


def is_absent_sentinel(value: object) -> bool:
    return isinstance(value, str) and value.strip().lower() in _ABSENT_SENTINELS


def normalize_absent_args(args: dict, *, skip: frozenset[str] = frozenset()) -> dict:
    """删除值为「缺省哨兵」的字段；skip 内的键原样保留。"""
    return {
        k: v for k, v in args.items()
        if k in skip or not is_absent_sentinel(v)
    }


def ensure_request_id(value: object, *, tool_name: str = "") -> str:
    """写工具幂等键：优先使用调用方传入的 ``request_id``。

    兜底生成只是防御。图层必须在 execute 之前给出 ``request_id``
    （select → Form Resume → recheck）；这里命中说明上游漏传，故记 warning
    而不是静默补号——静默补出来的号换个请求就作废，救不了重试，
    只会掩盖上游退化。

    刻意不复用 ``is_absent_sentinel``：它把 ``"null"`` / ``"none"`` 也算缺失，
    用在幂等键上会把合法旧值替换掉。``request_id`` 由系统生成，不是用户输入。
    """
    if isinstance(value, str) and value.strip():
        return value
    logger.warning(
        "request_id missing at write entry (tool=%s); generated fallback id",
        tool_name or "?",
    )
    return str(uuid4())
