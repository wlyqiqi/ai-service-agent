"""真实模型 ReadGraph ReAct 端到端冒烟（3 场景：指定单号 / 零参数订单列表 / 零参数物流）。

- 前置：.env 已配置 ARK_MODEL / ARK_BASE_URL / ARK_API_KEY；
  data/mock_ecommerce.db 已生成（scripts/seed_mock_db.py）。
- 验证点：Agent 按 ReAct 范式正确选择工具（tool_calls -> ToolMessage -> 最终回答），
  最终回答只引用模拟库中登录用户本人的真实数据（防幻觉）。
- 只读：模拟库以 mode=ro 打开，全程不写。
- 用法：$env:UV_CACHE_DIR="<repo>\\.uv-cache"; $env:UV_LINK_MODE="copy";
  uv run --no-sync python scripts/real_readgraph_smoke.py
"""

import asyncio
import sqlite3
import sys
from pathlib import Path

from langchain_core.messages import HumanMessage

from ai_service_agent.config import get_settings, make_model
from ai_service_agent.graph.builder import build_graph
from ai_service_agent.tools import DEFAULT_TOOL_REGISTRY


def _ro_conn() -> sqlite3.Connection:
    db = get_settings().mock_ecommerce_db_path
    return sqlite3.connect(f"{Path(db).resolve().as_uri()}?mode=ro", uri=True, timeout=2)


def _pick_scenario_users() -> dict:
    """从模拟库动态挑选场景用户：u1（夹具属主）+ 有在途订单的用户。"""
    conn = _ro_conn()
    try:
        shipped_owner = conn.execute(
            "SELECT o.customer_id FROM orders o JOIN shipments s ON s.order_id = o.order_id"
            " WHERE o.status = 'shipped' AND s.status = 'in_transit' LIMIT 1"
        ).fetchone()
        return {
            "fixture": "u1",
            "shipped": shipped_owner[0] if shipped_owner else "u1",
        }
    finally:
        conn.close()


def _trace(result_state: dict) -> dict:
    """提取 ReAct 轨迹：模型轮次的 tool_calls、工具回执、最终文本回答。"""
    called_tools: list[str] = []
    tool_results: list[str] = []
    final_text = ""
    for m in result_state.get("messages", []):
        mtype = getattr(m, "type", "")
        if mtype == "ai":
            calls = getattr(m, "tool_calls", None) or []
            if calls:
                called_tools += [c.get("name", "") for c in calls]
            elif (m.content or "").strip():
                final_text = m.content
        elif mtype == "tool":
            tool_results.append(str(m.content))
    return {"called_tools": called_tools, "tool_results": tool_results, "final_text": final_text}


def _db_order_ids(user_id: str) -> set[str]:
    conn = _ro_conn()
    try:
        return {
            r[0] for r in conn.execute(
                "SELECT order_id FROM orders WHERE customer_id = ?", (user_id,)
            )
        }
    finally:
        conn.close()


SCENARIOS = [
    {
        "name": "S1 指定单号（夹具订单）",
        "user_key": "fixture",
        "input": "帮我查一下订单 ORD-10086 的状态",
        "expect_tools": {"order_lookup"},
        "check": lambda t, u: "ORD-10086" in t["final_text"],
        "desc": "最终回答引用订单号与真实状态",
    },
    {
        "name": "S2 零参数订单列表",
        "user_key": "shipped",
        "input": "我都有哪些订单？",
        "expect_tools": {"my_orders"},
        "check": lambda t, u: any(oid in t["final_text"] for oid in _db_order_ids(u)),
        "desc": "最终回答引用本人库中真实单号（防幻觉）",
    },
    {
        "name": "S3 零参数物流查询",
        "user_key": "shipped",
        "input": "我的快递到哪了？什么时候能到？",
        "expect_tools": {"shipment_query"},
        "check": lambda t, u: any(
            k in t["final_text"] for k in ("运输中", "已签收", "待发货", "预计", "运单")
        ),
        "desc": "最终回答引用物流要素（承运商/运单/时效）",
    },
]


def main() -> int:
    users = _pick_scenario_users()
    print(f"场景用户：{users}")
    model = make_model()
    tools = DEFAULT_TOOL_REGISTRY.for_risk_level("read")
    print(f"read 档工具：{[t.name for t in tools]}")
    graph = build_graph(model, tools=tools)

    failures = 0
    for scenario in SCENARIOS:
        user_id = users[scenario["user_key"]]
        print(f"\n===== {scenario['name']}（user_id={user_id}）=====")
        print(f"输入：{scenario['input']}")
        result = asyncio.run(graph.ainvoke({
            "messages": [HumanMessage(content=scenario["input"])],
            "user_id": user_id,
            "session_id": "readgraph-real-smoke",
            "context": {},
        }))
        trace = _trace(result)
        print(f"调用工具：{trace['called_tools'] or '（无）'}")
        print(f"工具回执数：{len(trace['tool_results'])}")
        print(f"最终回答：{trace['final_text'][:220]}{'…' if len(trace['final_text']) > 220 else ''}")

        problems = []
        if not trace["called_tools"]:
            problems.append("未发生任何工具调用（违反 ReAct 预期）")
        if not (scenario["expect_tools"] & set(trace["called_tools"])):
            problems.append(f"未调用预期工具 {sorted(scenario['expect_tools'])}")
        if not trace["tool_results"]:
            problems.append("工具回执缺失")
        if not scenario["check"](trace, user_id):
            problems.append(scenario["desc"] + "：未通过")
        if trace["final_text"].strip() == "":
            problems.append("最终回答为空")

        if problems:
            failures += 1
            print(f"结果：FAIL —— {'；'.join(problems)}")
        else:
            print("结果：PASS")

    print(f"\n===== 汇总：{len(SCENARIOS) - failures}/{len(SCENARIOS)} 通过 =====")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
