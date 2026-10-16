"""工具参数占位哨兵。读路径与写路径共用这一处定义。"""

_ABSENT_SENTINELS = frozenset({"", "null", "none", "nil", "undefined", "n/a", "na"})


def is_absent_sentinel(value: object) -> bool:
    return isinstance(value, str) and value.strip().lower() in _ABSENT_SENTINELS


def normalize_absent_args(args: dict, *, skip: frozenset[str] = frozenset()) -> dict:
    """删除值为「缺省哨兵」的字段；skip 内的键原样保留。"""
    return {
        k: v for k, v in args.items()
        if k in skip or not is_absent_sentinel(v)
    }
