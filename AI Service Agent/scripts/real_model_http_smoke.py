"""真实模型端到端 HTTP+SSE 冒烟（4 场景：FAQ/Read · Mutation · HighRisk · Handoff）。

- 前置：.env 已配置 ARK_MODEL / ARK_BASE_URL / ARK_API_KEY；离线全量测试全绿。
- 每次运行使用全新临时 SQLite DB，可重复执行。
- 用法：$env:UV_CACHE_DIR="<repo>\\.uv-cache"; $env:UV_LINK_MODE="copy"; uv run --no-sync python scripts/real_model_http_smoke.py
"""

import asyncio
import json
import os
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

import httpx
import uvicorn
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from ai_service_agent.testing import ToolCallingFakeModel

from ai_service_agent.config import make_model
from ai_service_agent.harness.server import create_app

class SmokeFakeModel(ToolCallingFakeModel):
    """离线冒烟用（SMOKE_MODEL=fake）：按用户输入路由 + select 固定输出。"""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        system = next((m.content for m in messages if getattr(m, "type", "") == "system"), "")
        user = ""
        for m in reversed(messages):
            if getattr(m, "type", "") == "human":
                user = m.content or ""
                break
        if "路由器" in system:
            if "转人工" in user:
                payload = '{"capability": "unknown", "risk_level": "read", "confidence": 0.2}'
            elif "退款" in user:
                payload = '{"capability": "order", "risk_level": "high_risk", "confidence": 0.9}'
            elif "收货地址" in user:
                payload = '{"capability": "account", "risk_level": "mutation", "confidence": 0.9}'
            else:
                payload = '{"capability": "faq", "risk_level": "read", "confidence": 0.9}'
        elif "选择器" in system:
            if "退款" in user:
                payload = '{"tool": "refund_apply", "args": {"order_id": "10086"}}'
            else:
                payload = '{"tool": "account_update", "args": {"address": "北京市朝阳区"}}'
        else:
            payload = "离线冒烟回复"
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=payload))])


def _make_model():
    if os.environ.get("SMOKE_MODEL") == "fake":
        return SmokeFakeModel(responses=[])
    return make_model()


PORT = 8321
BASE = f"http://127.0.0.1:{PORT}"
REQUEST_TIMEOUT = 180.0  # 真实模型单次请求超时上限


def parse_sse(text: str) -> list[dict]:
    events = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        for line in block.splitlines():
            if line.startswith("data: "):
                try:
                    events.append(json.loads(line[len("data: "):]))
                except json.JSONDecodeError:
                    events.append({"type": "RAW", "data": line})
    return events


def event_types(events: list[dict]) -> list[str]:
    return [e.get("type", "?") for e in events]


def open_status(db_path: str, conv_id: str) -> dict:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT conversation_id, thread_id, mode, user_id FROM conversation_status WHERE conversation_id=?",
            (conv_id,),
        ).fetchone()
        return None if row is None else {
            "conversation_id": row[0], "thread_id": row[1], "mode": row[2], "user_id": row[3],
        }
    finally:
        conn.close()


def open_handoffs(db_path: str, conv_id: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT handoff_id, conversation_id, reason, status, created_at, resolved_at FROM handoff_record WHERE conversation_id=?",
            (conv_id,),
        ).fetchall()
        return [
            {"handoff_id": r[0], "conversation_id": r[1], "reason": r[2], "status": r[3], "created_at": r[4], "resolved_at": r[5]}
            for r in rows
        ]
    finally:
        conn.close()


def open_approvals(db_path: str, conv_id: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT approval_id, thread_id, summary, status, decision, decided_by, expires_at FROM approval_record WHERE conversation_id=?",
            (conv_id,),
        ).fetchall()
        return [
            {"approval_id": r[0], "thread_id": r[1], "summary": r[2], "status": r[3], "decision": r[4], "decided_by": r[5], "expires_at": r[6]}
            for r in rows
        ]
    finally:
        conn.close()


def open_messages(db_path: str, conv_id: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT role, content FROM conversation_message WHERE conversation_id=? ORDER BY id", (conv_id,)
        ).fetchall()
        return [{"role": r[0], "content": r[1]} for r in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def count_tool_events(events: list[dict], tool: str) -> tuple[int, int]:
    calls = [e for e in events if e.get("type") == "tool_call" and e.get("data", {}).get("name") == tool]
    results = [e for e in events if e.get("type") == "tool_result" and e.get("data", {}).get("name") == tool]
    return len(calls), len(results)


def graph_state(db_path: str, thread_id: str) -> dict:
    """用与 tests 相同的方式读 checkpoint：interrupts + execution_status + 工具执行次数。"""
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from ai_service_agent.harness.graph import make_ai_graph
    from ai_service_agent.testing import make_fake_model

    async def _read():
        async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
            graph = make_ai_graph(make_fake_model(), saver)
            st = await graph.aget_state({"configurable": {"thread_id": thread_id}})
            msgs = st.values.get("messages", [])
            tool_msgs = [
                m for m in msgs
                if getattr(m, "type", "") in ("tool", "ToolMessage")
                or (getattr(m, "tool_calls", None) and any(tc.get("name") for tc in m.tool_calls))
            ]
            assistant_tail = [
                (m.content if isinstance(m.content, str) else str(m.content))
                for m in msgs if getattr(m, "type", "") == "ai" and m.content
            ]
            return {
                "interrupts": [getattr(it, "value", None) for it in st.interrupts],
                "execution_status": st.values.get("execution_status"),
                "handoff": st.values.get("handoff"),
                "last_assistant": assistant_tail[-1] if assistant_tail else None,
                "messages": [
                    {"type": getattr(m, "type", "?"), "name": getattr(m, "name", None),
                     "tool_calls": [tc.get("name") for tc in getattr(m, "tool_calls", None) or []]}
                    for m in msgs
                ],
            }

    return asyncio.run(_read())


def new_session(client: httpx.Client, user_id: str = "u-smoke") -> tuple[str, dict]:
    r = client.post(f"{BASE}/sessions", json={"user_id": user_id})
    body = r.json()
    return body["session_id"], {"status": r.status_code, "body": body}


def chat_sse(client: httpx.Client, session_id: str, message: str) -> tuple[int, list[dict]]:
    with client.stream(
        "POST", f"{BASE}/chat", json={"session_id": session_id, "message": message}
    ) as resp:
        text = "".join(resp.iter_text())
    return resp.status_code, parse_sse(text)


def check_thread_id_consistent(db_path: str, conv_id: str, label: str) -> bool:
    """本会话 thread_id 全程一致：conversation_id==session_id，且 graph 全部落在映射的 thread_id。"""
    st = open_status(db_path, conv_id)
    assert st is not None, f"{label}: conversation_status row missing"
    assert st["conversation_id"] == conv_id, f"{label}: conversation_id != session_id"
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT DISTINCT thread_id FROM checkpoints").fetchall()
        # 该 thread_id 下至少 1 个 checkpoint（图确实写在了映射的 thread_id）
        count = conn.execute("SELECT COUNT(*) FROM checkpoints WHERE thread_id=?", (st["thread_id"],)).fetchone()[0]
    finally:
        conn.close()
    return st["thread_id"] in {r[0] for r in rows} and count >= 1


def scenario_faq(client: httpx.Client, db_path: str) -> dict:
    sid, sess = new_session(client)
    code, events = chat_sse(client, sid, "你好，有什么商品推荐吗？")
    types = event_types(events)
    done = next((e for e in reversed(events) if e.get("type") == "done"), None)
    tokens = [e for e in events if e.get("type") == "token"]
    pass_ = (
        code == 200
        and done is not None
        and "error" not in types
        and len(tokens) >= 1
        and done.get("data", {}).get("final")
    )
    return {
        "name": "FAQ/Read", "pass": pass_, "session_id": sid, "http_status": code,
        "timeline": types, "token_count": len(tokens), "final": (done or {}).get("data", {}),
        "thread_id": open_status(db_path, sid)["thread_id"],
        "mode": open_status(db_path, sid)["mode"],
        "thread_id_consistent": check_thread_id_consistent(db_path, sid, "FAQ"),
    }


def scenario_mutation(client: httpx.Client, db_path: str) -> dict:
    sid, sess = new_session(client)
    code1, ev1 = chat_sse(client, sid, "把收货地址改成北京市朝阳区")
    types1 = event_types(ev1)
    confirm = any(e.get("type") == "confirmation_required" for e in ev1)
    done1 = next((e for e in reversed(ev1) if e.get("type") == "done"), None)

    code2, ev2 = chat_sse(client, sid, "确认")
    types2 = event_types(ev2)
    calls, results = count_tool_events(ev2, "account_update")
    done2 = next((e for e in reversed(ev2) if e.get("type") == "done"), None)

    st = graph_state(db_path, open_status(db_path, sid)["thread_id"])
    tool_msgs = [m for m in st["messages"] if m["name"] == "account_update"]
    pass_ = (
        code1 == 200 and confirm and (done1 is not None)
        and code2 == 200 and calls == 1 and results == 1
        and st["execution_status"] == "completed" and len(tool_msgs) == 1
    )
    return {
        "name": "Mutation", "pass": pass_, "session_id": sid,
        "turn1": {"http": code1, "timeline": types1, "confirm_required": confirm, "done_final": (done1 or {}).get("data")},
        "turn2": {"http": code2, "timeline": types2, "tool_call": calls, "tool_result": results, "done_final": (done2 or {}).get("data")},
        "thread_id": open_status(db_path, sid)["thread_id"],
        "final_state": {"execution_status": st["execution_status"], "tool_messages": len(tool_msgs), "interrupts": st["interrupts"], "last_assistant": st["last_assistant"]},
        "mode": open_status(db_path, sid)["mode"],
        "thread_id_consistent": check_thread_id_consistent(db_path, sid, "Mutation"),
    }


def scenario_highrisk(client: httpx.Client, db_path: str) -> dict:
    sid, sess = new_session(client)
    code1, ev1 = chat_sse(client, sid, "订单10086申请退款")
    types1 = event_types(ev1)
    approval_ev = next((e for e in ev1 if e.get("type") == "approval_required"), None)
    approval_id = (approval_ev or {}).get("data", {}).get("approval_id")
    done1 = next((e for e in reversed(ev1) if e.get("type") == "done"), None)

    pending = None
    if approval_id:
        r = client.get(f"{BASE}/approvals/{approval_id}")
        pending = {"http": r.status_code, "body": r.json()} if r.status_code == 200 else {"http": r.status_code, "body": r.text}

    approve = None
    if approval_id:
        r = client.post(f"{BASE}/approvals/{approval_id}/approve")
        approve = {"http": r.status_code, "body": r.json()} if r.status_code == 200 else {"http": r.status_code, "body": r.text}

    after = None
    if approval_id:
        r = client.get(f"{BASE}/approvals/{approval_id}")
        after = {"http": r.status_code, "body": r.json()} if r.status_code == 200 else {"http": r.status_code, "body": r.text}

    st = graph_state(db_path, open_status(db_path, sid)["thread_id"])
    tool_msgs = [m for m in st["messages"] if m["name"] == "refund_apply"]
    pass_ = (
        code1 == 200 and approval_ev is not None
        and (approve or {}).get("http") == 200 and (approve or {}).get("body", {}).get("status") == "approved"
        and st["execution_status"] == "completed" and len(tool_msgs) == 1
    )
    return {
        "name": "HighRisk", "pass": pass_, "session_id": sid, "approval_id": approval_id,
        "chat": {"http": code1, "timeline": types1, "done_final": (done1 or {}).get("data")},
        "get_pending": pending, "approve": approve, "get_after": after,
        "thread_id": open_status(db_path, sid)["thread_id"],
        "final_state": {"execution_status": st["execution_status"], "tool_messages": len(tool_msgs), "interrupts": st["interrupts"], "last_assistant": st["last_assistant"]},
        "mode": open_status(db_path, sid)["mode"],
        "thread_id_consistent": check_thread_id_consistent(db_path, sid, "HighRisk"),
    }


def scenario_handoff(client: httpx.Client, db_path: str) -> dict:
    sid, sess = new_session(client)
    code1, ev1 = chat_sse(client, sid, "我要转人工")
    types1 = event_types(ev1)
    created = next((e for e in ev1 if e.get("type") == "handoff_created"), None)
    handoff_id = (created or {}).get("data", {}).get("handoff_id")
    mode_after_handoff = open_status(db_path, sid)["mode"]

    resolve = None
    if handoff_id:
        r = client.post(f"{BASE}/handoffs/{handoff_id}/resolve")
        resolve = {"http": r.status_code, "body": r.json()} if r.status_code == 200 else {"http": r.status_code, "body": r.text}

    mode_after_resolve = open_status(db_path, sid)["mode"]
    handoffs = open_handoffs(db_path, sid)
    st = graph_state(db_path, open_status(db_path, sid)["thread_id"])

    code2, ev2 = chat_sse(client, sid, "你好，还在吗？")
    types2 = event_types(ev2)
    done2 = next((e for e in reversed(ev2) if e.get("type") == "done"), None)
    tokens2 = [e for e in ev2 if e.get("type") == "token"]
    msgs = open_messages(db_path, sid)

    pass_ = (
        code1 == 200 and created is not None and mode_after_handoff == "human"
        and (resolve or {}).get("http") == 200 and (resolve or {}).get("body", {}).get("mode") == "ai"
        and mode_after_resolve == "ai"
        and (handoffs and handoffs[0]["status"] == "resolved")
        and not st["interrupts"] and st["execution_status"] == "completed"
        and code2 == 200 and "error" not in types2 and done2 is not None and len(tokens2) >= 1
    )
    return {
        "name": "Handoff", "pass": pass_, "session_id": sid, "handoff_id": handoff_id,
        "chat1": {"http": code1, "timeline": types1},
        "mode_after_handoff": mode_after_handoff, "resolve": resolve, "mode_after_resolve": mode_after_resolve,
        "handoff_records": handoffs,
        "thread_id": open_status(db_path, sid)["thread_id"],
        "final_state": {"execution_status": st["execution_status"], "interrupts": st["interrupts"], "handoff": st["handoff"], "last_assistant": st["last_assistant"]},
        "chat2": {"http": code2, "timeline": types2, "token_count": len(tokens2), "done_final": (done2 or {}).get("data")},
        "message_store": [{"role": m["role"], "content": m["content"][:40]} for m in msgs],
        "thread_id_consistent": check_thread_id_consistent(db_path, sid, "Handoff"),
    }


def report(results: list[dict], db_path: str) -> None:
    print("=" * 72)
    print("SSE 真机冒烟报告（ARK 真实模型）")
    print("=" * 72)
    for r in results:
        print(f"\n### {r['name']}: {'PASS' if r['pass'] else 'FAIL'}")
        for k, v in r.items():
            if k in ("name", "pass"):
                continue
            print(f"  {k}: {json.dumps(v, ensure_ascii=False, default=str)}")
    ok = all(r["pass"] for r in results)
    print("\n" + "=" * 72)
    print(f"结论: {'全部通过' if ok else '存在失败'}  | DB: {db_path}")


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="smoke-http-")
    db_path = os.path.join(tmp, "smoke.db")
    app = create_app(db_path, model=_make_model())
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while not getattr(server, "started", False) and time.time() < deadline:
        time.sleep(0.05)
    if not getattr(server, "started", False):
        print("FAIL: uvicorn server did not start"); return 1

    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            results = [
                scenario_faq(client, db_path),
                scenario_mutation(client, db_path),
                scenario_highrisk(client, db_path),
                scenario_handoff(client, db_path),
            ]
        report(results, db_path)
        return 0 if all(r["pass"] for r in results) else 2
    finally:
        server.should_exit = True
        thread.join(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())
