"""注入给 ``ThreadLifecycleService`` 的 SQLite 探测实现。

只在**轮换阈值命中**时才被调用（30 轮 / 24 小时），因此允许每次新建 saver 与
编译图；常规 AI turn 不付这个成本。

探测能力缺失即视为「无法证明安全」——``collect_blockers`` 会 fail closed，
轮换被跳过而不是带着未知挂起状态切换 thread。
"""

from __future__ import annotations

import logging

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from ai_service_agent.harness.graph import make_ai_graph
from ai_service_agent.testing import make_fake_model

logger = logging.getLogger("ai_service_agent")


async def load_thread_interrupts(thread_id: str, db_path: str) -> list:
    """官方口径：``graph.aget_state(config).interrupts`` 是挂起状态的权威。"""
    async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
        await saver.setup()
        graph = make_ai_graph(make_fake_model(), saver)
        state = await graph.aget_state({"configurable": {"thread_id": thread_id}})
        return [interrupt.value for interrupt in state.interrupts]


async def load_thread_context(thread_id: str, db_path: str) -> dict | None:
    """直接读 checkpoint 的 ``context`` channel，不编译图。"""
    async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
        tup = await saver.aget_tuple({"configurable": {"thread_id": thread_id}})
    if tup is None:
        return None
    values = (tup.checkpoint or {}).get("channel_values") or {}
    context = values.get("context")
    return context if isinstance(context, dict) else None


async def delete_thread(thread_id: str, db_path: str) -> None:
    """官方整 thread 删除；只删 checkpoints / writes。"""
    async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
        await saver.setup()
        await saver.adelete_thread(thread_id)


def sqlite_lifecycle_probes(db_path: str) -> dict:
    return {
        "interrupt_loader": lambda thread_id: load_thread_interrupts(thread_id, db_path),
        "context_reader": lambda thread_id: load_thread_context(thread_id, db_path),
        "delete_thread": lambda thread_id: delete_thread(thread_id, db_path),
    }
