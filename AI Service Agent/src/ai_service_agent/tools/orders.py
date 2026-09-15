from datetime import datetime, timedelta, timezone
from typing import Annotated

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState


def _days_ago(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


# 确定性种子订单状态（D3，P4）：供离线 eval 与测试；signed_at 相对 now 生成，
# 保证「签收超 7 天窗口」（CTRL-0086）与「已取消」（CTRL-0090）恒成立。
ORDER_STORE: dict[str, dict] = {
    "ORD-1": {"order_id": "ORD-1", "user_id": "u1", "status": "signed", "signed_at": _days_ago(2)},
    "ORD-2": {"order_id": "ORD-2", "user_id": "u2", "status": "signed", "signed_at": _days_ago(2)},  # 同事订单（跨用户边界，D4）
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
def order_lookup(order_id: str, user_id: Annotated[str, InjectedState("user_id")]) -> dict:
    """订单查询（MVP stub + D4 属主校验）：仅返回本人订单；越权返回 data_boundary（safety-spec §1.4，
    数据边界由工具自身保证，ReadGraph 不增加 precheck）。属主事实来自 ORDER_STORE。"""
    rec = ORDER_STORE.get(order_id)
    if rec is None:
        return {"error": "not_found", "order_id": order_id}
    if rec["user_id"] != user_id:
        return {"error": "data_boundary", "order_id": order_id}
    return {"order_id": order_id, "status": "已发货", "amount": 99.0, "user_id": user_id}
