from datetime import datetime, timedelta, timezone

from langchain_core.tools import tool


def _days_ago(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


# 确定性种子订单状态（D3，P4）：供离线 eval 与测试；signed_at 相对 now 生成，
# 保证「签收超 7 天窗口」（CTRL-0086）与「已取消」（CTRL-0090）恒成立。
ORDER_STORE: dict[str, dict] = {
    "ORD-10086": {"order_id": "ORD-10086", "user_id": "u1", "status": "signed", "signed_at": _days_ago(10)},
    "ORD-CANCEL-1": {
        "order_id": "ORD-CANCEL-1",
        "user_id": "u1",
        "status": "cancelled",
        "signed_at": _days_ago(20),
        "cancelled_at": _days_ago(3),
    },
}


def lookup_order_state(order_id: str) -> dict | None:
    """订单状态事实查询（D3，P4）：仅返回事实，不做授权/业务裁决；不存在 -> None。"""
    return ORDER_STORE.get(order_id)


@tool
def order_lookup(order_id: str) -> dict:
    """订单查询（MVP stub）。"""
    return {"order_id": order_id, "status": "已发货", "amount": 99.0}
