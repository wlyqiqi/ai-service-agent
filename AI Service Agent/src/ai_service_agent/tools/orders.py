from langchain_core.tools import tool


@tool
def order_lookup(order_id: str) -> dict:
    """订单查询（MVP stub）。"""
    return {"order_id": order_id, "status": "已发货", "amount": 99.0}
