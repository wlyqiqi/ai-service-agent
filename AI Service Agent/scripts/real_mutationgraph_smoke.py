"""真实模型 MutationGraph 端到端冒烟（数据库临时副本）。"""

import asyncio
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from ai_service_agent.config import get_settings, make_model
from ai_service_agent.graph.builder import build_graph
from ai_service_agent.tools import DEFAULT_TOOL_REGISTRY


def _conn(path: str) -> sqlite3.Connection:
    return sqlite3.connect(path, timeout=2)


def _one(path: str, sql: str, params: tuple = ()) -> tuple | None:
    conn = _conn(path)
    try:
        return conn.execute(sql, params).fetchone()
    finally:
        conn.close()


def _snapshot(path: str) -> tuple[tuple, tuple]:
    return (
        _one(path, "SELECT * FROM orders WHERE order_id='ORD-2'"),
        _one(path, "SELECT COUNT(*) FROM idempotency"),
    )


def _tool_names(result: dict) -> list[str]:
    return [
        getattr(message, "name", "")
        for message in result.get("messages", [])
        if getattr(message, "type", "") == "tool"
    ]


def _invoke(graph, thread_id: str, user_id: str, text: str, resume=None) -> dict:
    config = {"configurable": {"thread_id": thread_id}}
    if resume is not None:
        return asyncio.run(graph.ainvoke(Command(resume=resume), config))
    return asyncio.run(graph.ainvoke({
        "messages": [HumanMessage(content=text)],
        "user_id": user_id,
        "session_id": thread_id,
        "context": {},
    }, config))


def main() -> int:
    source = Path(get_settings().mock_ecommerce_db_path).resolve()
    with tempfile.TemporaryDirectory(prefix="mutation-smoke-") as temp_dir:
        db_path = str(Path(temp_dir) / "mock_ecommerce.db")
        shutil.copy2(source, db_path)
        os.environ["MOCK_ECOMMERCE_DB_PATH"] = db_path
        get_settings.cache_clear()
        graph = build_graph(
            make_model(),
            tools=DEFAULT_TOOL_REGISTRY.for_risk_level("mutation"),
            checkpointer=MemorySaver(),
        )
        failures = []

        result = _invoke(graph, "mutation-smoke-address", "u1", "把我的收货地址改成北京市朝阳区测试路 99 号")
        if not result.get("__interrupt__"):
            failures.append("S1 address: missing confirmation interrupt")
        else:
            result = _invoke(graph, "mutation-smoke-address", "u1", "confirm", resume={"action": "confirm"})
            if "update_address" not in _tool_names(result):
                failures.append("S1 address: tool not executed")
            address = _one(db_path, "SELECT address FROM customers WHERE customer_id='u1'")
            if address != ("北京市朝阳区测试路 99 号",):
                failures.append(f"S1 address: db not updated ({address})")

        result = _invoke(graph, "mutation-smoke-boundary", "u1", "帮我把订单 ORD-2 的备注改成越权测试")
        if result.get("__interrupt__"):
            failures.append("S2 boundary: unexpected confirmation interrupt")
        if result.get("context", {}).get("precheck_reason") != "data_boundary":
            failures.append(f"S2 boundary: wrong reason ({result.get('context')})")

        before = _snapshot(db_path)
        result = _invoke(graph, "mutation-smoke-invoice", "u1", "给订单 ORD-1 开一张 999999 元的发票，抬头测试公司，税号 91110108MA01AB12CD")
        if result.get("__interrupt__"):
            failures.append("S3 invoice: unexpected confirmation interrupt")
        if result.get("context", {}).get("precheck_reason") != "invoice_amount_exceeds":
            failures.append(f"S3 invoice: wrong reason ({result.get('context')})")
        after = _snapshot(db_path)
        if before != after:
            failures.append("S3 invoice: rejected operation polluted db")

        print(f"temporary_db={db_path}")
        print(f"failures={len(failures)}")
        for failure in failures:
            print(f"- {failure}")
        return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
