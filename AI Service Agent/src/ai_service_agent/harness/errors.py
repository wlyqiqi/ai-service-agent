def normalize_error(exc: Exception) -> dict:
    """入口异常归一化（event-spec.md §2：error 事件）。"""
    return {"type": "error", "data": {"code": type(exc).__name__, "message": str(exc)}}
