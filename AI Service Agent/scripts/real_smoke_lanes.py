"""核心运行时真机冒烟（架构车道 L1–L6）。

按 capability × risk_level × 闸门生命周期验收，不沿用历史 bug 编号场景堆叠。
前置：.env 配置 ARK_MODEL / ARK_BASE_URL / ARK_API_KEY。
用法：
  $env:UV_CACHE_DIR="<repo>\\.uv-cache"; $env:UV_LINK_MODE="copy"
  uv run --no-sync python scripts/real_smoke_lanes.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

import httpx
import uvicorn

# Force real model before config/settings load side effects in create_app.
os.environ["FAKE_MODEL"] = "false"
os.environ.setdefault("JEV_ENABLED", "false")
os.environ.setdefault("AUTH_ENABLED", "false")
os.environ.setdefault("CHATWOOT_ENABLED", "false")
os.environ.setdefault("LANGSMITH_TRACING", "false")

from ai_service_agent.config import make_model
from ai_service_agent.evals.lane_criteria import mutation_form_incomplete_pass
from ai_service_agent.graph.workflows.copy import (
    approval_approved_text,
    completion_text,
)
from ai_service_agent.harness.approvals import APPROVER_USER_HEADER
from ai_service_agent.harness.server import create_app

# scripts/ is not a package; load seed helper by path.
import importlib.util

_seed_spec = importlib.util.spec_from_file_location(
    "seed_mock_db",
    Path(__file__).resolve().parents[0] / "seed_mock_db.py",
)
_seed_mod = importlib.util.module_from_spec(_seed_spec)
assert _seed_spec.loader is not None
_seed_spec.loader.exec_module(_seed_mod)
build_database = _seed_mod.build_database

ROOT = Path(__file__).resolve().parents[1]
REPORTS_DIR = ROOT / "docs" / "reports"
PORT = 8327
BASE = f"http://127.0.0.1:{PORT}"
REQUEST_TIMEOUT = 180.0
USER_ID = "u1"


def preflight_ark() -> None:
    missing = [k for k in ("ARK_MODEL", "ARK_BASE_URL", "ARK_API_KEY") if not os.environ.get(k)]
    if missing:
        # Load .env manually if present (uvicorn path may not have pydantic-settings yet).
        env_path = ROOT / ".env"
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key, value = key.strip(), value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
        missing = [k for k in ("ARK_MODEL", "ARK_BASE_URL", "ARK_API_KEY") if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"FAIL preflight: missing {', '.join(missing)}")
    if os.environ.get("FAKE_MODEL", "").lower() in {"1", "true", "yes"}:
        raise SystemExit("FAIL preflight: FAKE_MODEL must be false for real smoke")


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
        if row is None:
            raise AssertionError(f"conversation_status missing for {conv_id}")
        return {
            "conversation_id": row[0],
            "thread_id": row[1],
            "mode": row[2],
            "user_id": row[3],
        }
    finally:
        conn.close()


def open_handoffs(db_path: str, conv_id: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT handoff_id, conversation_id, reason, status, created_at, resolved_at "
            "FROM handoff_record WHERE conversation_id=?",
            (conv_id,),
        ).fetchall()
        return [
            {
                "handoff_id": r[0],
                "conversation_id": r[1],
                "reason": r[2],
                "status": r[3],
                "created_at": r[4],
                "resolved_at": r[5],
            }
            for r in rows
        ]
    finally:
        conn.close()


def open_messages(db_path: str, conv_id: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT role, content FROM conversation_message WHERE conversation_id=? ORDER BY id",
            (conv_id,),
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
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    from ai_service_agent.harness.graph import make_ai_graph
    from ai_service_agent.testing import make_fake_model

    async def _read():
        async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
            graph = make_ai_graph(make_fake_model(), saver)
            st = await graph.aget_state({"configurable": {"thread_id": thread_id}})
            msgs = st.values.get("messages", [])
            assistant_tail = [
                (m.content if isinstance(m.content, str) else str(m.content))
                for m in msgs
                if getattr(m, "type", "") == "ai" and m.content
            ]
            return {
                "interrupts": [getattr(it, "value", None) for it in st.interrupts],
                "execution_status": st.values.get("execution_status"),
                "router": {
                    "capability": st.values.get("capability"),
                    "risk_level": st.values.get("risk_level"),
                },
                "clarify_count": (st.values.get("context") or {}).get("clarify_count", 0),
                "handoff": st.values.get("handoff"),
                "last_assistant": assistant_tail[-1] if assistant_tail else None,
                "messages": [
                    {
                        "type": getattr(m, "type", "?"),
                        "name": getattr(m, "name", None),
                        "tool_calls": [tc.get("name") for tc in getattr(m, "tool_calls", None) or []],
                    }
                    for m in msgs
                ],
            }

    return asyncio.run(_read())


def thread_id_consistent(db_path: str, conv_id: str) -> bool:
    st = open_status(db_path, conv_id)
    if st["conversation_id"] != conv_id:
        return False
    conn = sqlite3.connect(db_path)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM checkpoints WHERE thread_id=?",
            (st["thread_id"],),
        ).fetchone()[0]
    finally:
        conn.close()
    return count >= 1


def new_session(client: httpx.Client, user_id: str = USER_ID) -> str:
    r = client.post(f"{BASE}/sessions", json={"user_id": user_id})
    r.raise_for_status()
    return r.json()["session_id"]


def chat_sse(client: httpx.Client, session_id: str, message: str) -> tuple[int, list[dict]]:
    with client.stream(
        "POST",
        f"{BASE}/chat",
        json={"session_id": session_id, "message": message},
    ) as resp:
        text = "".join(resp.iter_text())
    return resp.status_code, parse_sse(text)


def lane_l1_read(client: httpx.Client, db_path: str) -> dict:
    sid = new_session(client)
    code, events = chat_sse(client, sid, "你们有什么空气炸锅推荐吗？")
    types = event_types(events)
    done = next((e for e in reversed(events) if e.get("type") == "done"), None)
    final = (done or {}).get("data", {}).get("final")
    status = open_status(db_path, sid)
    hard_error = "error" in types and not final
    passed = (
        code == 200
        and done is not None
        and bool(final)
        and not hard_error
        and status["mode"] == "ai"
        and thread_id_consistent(db_path, sid)
    )
    return {
        "id": "L1",
        "name": "Read",
        "pass": passed,
        "session_id": sid,
        "http_status": code,
        "timeline": types,
        "done_final": final,
        "mode": status["mode"],
        "thread_id": status["thread_id"],
        "thread_id_consistent": thread_id_consistent(db_path, sid),
    }


def lane_l2_mutation_confirm(client: httpx.Client, db_path: str) -> dict:
    """Mutation happy path: form_required → Form Resume Confirm → write once."""
    sid = new_session(client)
    code1, ev1 = chat_sse(client, sid, "订单 ORD-PAID-1 的收货地址改成北京市朝阳区")
    types1 = event_types(ev1)
    form_required = any(e.get("type") == "form_required" for e in ev1)
    done1 = next((e for e in reversed(ev1) if e.get("type") == "done"), None)

    address = "北京市朝阳区"
    resume = client.post(
        f"{BASE}/actions/confirm",
        json={
            "conversation_id": sid,
            "action": "confirm",
            "order_id": "ORD-PAID-1",
            "fields": {"address": address},
        },
    )
    code2 = resume.status_code
    body2 = resume.json() if resume.headers.get("content-type", "").startswith("application/json") else {}
    ev2 = list(body2.get("events") or [])
    types2 = event_types(ev2)
    calls, results = count_tool_events(ev2, "update_address")
    tool_result_ev = next(
        (
            e
            for e in ev2
            if e.get("type") == "tool_result" and e.get("data", {}).get("name") == "update_address"
        ),
        None,
    )
    done2 = next((e for e in reversed(ev2) if e.get("type") == "done"), None)
    final2 = body2.get("final") or (done2 or {}).get("data", {}).get("final")
    expected_final = completion_text(
        "update_address",
        {"order_id": "ORD-PAID-1", "address": address},
    )
    st = graph_state(db_path, open_status(db_path, sid)["thread_id"])
    tool_msgs = [m for m in st["messages"] if m.get("name") == "update_address"]
    msgs = open_messages(db_path, sid)
    store_ok = any(
        m["role"] == "assistant" and m["content"] == expected_final for m in msgs
    ) or any(
        m["role"] == "assistant" and "收货地址" in (m["content"] or "") and "完成" in (m["content"] or "")
        for m in msgs
    )
    final_ok = final2 == expected_final or (
        isinstance(final2, str) and "收货地址" in final2 and ("完成" in final2 or "成功" in final2)
    )
    passed = (
        code1 == 200
        and form_required
        and done1 is not None
        and code2 == 200
        and calls == 1
        and results == 1
        and st["execution_status"] == "completed"
        and len(tool_msgs) == 1
        and final_ok
        and store_ok
        and thread_id_consistent(db_path, sid)
    )
    return {
        "id": "L2",
        "name": "MutationConfirm",
        "pass": passed,
        "session_id": sid,
        "turn1": {
            "http": code1,
            "timeline": types1,
            "form_required": form_required,
            "done_final": (done1 or {}).get("data", {}).get("final"),
        },
        "turn2": {
            "http": code2,
            "timeline": types2,
            "tool_call": calls,
            "tool_result": results,
            "tool_result_data": (tool_result_ev or {}).get("data"),
            "done_final": final2,
            "expected_final": expected_final,
        },
        "execution_status": st["execution_status"],
        "tool_messages": len(tool_msgs),
        "message_store_ok": store_ok,
        "thread_id": open_status(db_path, sid)["thread_id"],
        "thread_id_consistent": thread_id_consistent(db_path, sid),
        "mode": open_status(db_path, sid)["mode"],
    }


def lane_l3_mutation_form_incomplete(client: httpx.Client, db_path: str) -> dict:
    """Tool-known missing fields → Form Interrupt; Operation Clarify stays chat-only."""
    sid = new_session(client)
    code, events = chat_sse(client, sid, "我要改个地址")
    types = event_types(events)
    form_required = any(e.get("type") == "form_required" for e in events)
    clarify = any(e.get("type") == "clarification_required" for e in events)
    confirm = any(e.get("type") == "confirmation_required" for e in events)
    write_tools = [
        e
        for e in events
        if e.get("type") == "tool_call"
        and e.get("data", {}).get("name")
        in {
            "update_address",
            "update_phone",
            "update_order_note",
            "create_invoice",
            "create_service_ticket",
        }
    ]
    done = next((e for e in reversed(events) if e.get("type") == "done"), None)
    st = graph_state(db_path, open_status(db_path, sid)["thread_id"])
    passed = mutation_form_incomplete_pass(
        http_ok=code == 200,
        form_required=form_required,
        clarification_required=clarify,
        confirmation_required=confirm,
        write_tool_count=len(write_tools),
        done=done is not None,
        interrupts=st["interrupts"],
        thread_id_ok=thread_id_consistent(db_path, sid),
    )
    return {
        "id": "L3",
        "name": "MutationFormIncomplete",
        "pass": passed,
        "session_id": sid,
        "http_status": code,
        "timeline": types,
        "form_required": form_required,
        "clarification_required": clarify,
        "confirmation_required": confirm,
        "write_tool_calls": len(write_tools),
        "done_final": (done or {}).get("data", {}).get("final"),
        "execution_status": st["execution_status"],
        "thread_id": open_status(db_path, sid)["thread_id"],
        "thread_id_consistent": thread_id_consistent(db_path, sid),
        "mode": open_status(db_path, sid)["mode"],
    }


def lane_l4_highrisk_approve(client: httpx.Client, db_path: str) -> dict:
    sid = new_session(client)
    code1, ev1 = chat_sse(client, sid, "订单 ORD-REFUND-1 申请退款")
    types1 = event_types(ev1)
    approval_ev = next((e for e in ev1 if e.get("type") == "approval_required"), None)
    approval_id = (approval_ev or {}).get("data", {}).get("approval_id")
    done1 = next((e for e in reversed(ev1) if e.get("type") == "done"), None)

    approve = None
    if approval_id:
        r = client.post(
            f"{BASE}/approvals/{approval_id}/approve",
            headers={APPROVER_USER_HEADER: "approver"},
        )
        approve = {"http": r.status_code, "body": r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text}

    st = graph_state(db_path, open_status(db_path, sid)["thread_id"])
    tool_msgs = [m for m in st["messages"] if m.get("name") == "refund_apply"]
    msgs = open_messages(db_path, sid)
    expected_store = approval_approved_text("refund_apply")
    store_ok = any(m["role"] == "assistant" and "已通过审核并执行完成" in (m["content"] or "") for m in msgs)
    passed = (
        code1 == 200
        and approval_ev is not None
        and bool(approval_id)
        and done1 is not None
        and (approve or {}).get("http") == 200
        and (approve or {}).get("body", {}).get("status") == "approved"
        and st["execution_status"] == "completed"
        and len(tool_msgs) == 1
        and not st["interrupts"]
        and store_ok
        and thread_id_consistent(db_path, sid)
    )
    return {
        "id": "L4",
        "name": "HighRiskApprove",
        "pass": passed,
        "session_id": sid,
        "approval_id": approval_id,
        "chat": {
            "http": code1,
            "timeline": types1,
            "done_final": (done1 or {}).get("data", {}).get("final"),
            "approval_summary": (approval_ev or {}).get("data", {}).get("summary"),
        },
        "approve": approve,
        "last_assistant": st.get("last_assistant"),
        "execution_status": st["execution_status"],
        "tool_messages": len(tool_msgs),
        "interrupts": st["interrupts"],
        "message_store_ok": store_ok,
        "expected_store_snippet": expected_store[:40],
        "thread_id": open_status(db_path, sid)["thread_id"],
        "thread_id_consistent": thread_id_consistent(db_path, sid),
        "mode": open_status(db_path, sid)["mode"],
    }


def lane_l5_highrisk_clarify(client: httpx.Client, db_path: str) -> dict:
    sid = new_session(client)
    code, events = chat_sse(client, sid, "我要申请退款")
    types = event_types(events)
    clarify = any(e.get("type") == "clarification_required" for e in events)
    approval = any(e.get("type") == "approval_required" for e in events)
    done = next((e for e in reversed(events) if e.get("type") == "done"), None)
    st = graph_state(db_path, open_status(db_path, sid)["thread_id"])
    passed = (
        code == 200
        and clarify
        and not approval
        and done is not None
        and st["execution_status"] == "clarifying"
        and thread_id_consistent(db_path, sid)
    )
    return {
        "id": "L5",
        "name": "HighRiskClarify",
        "pass": passed,
        "session_id": sid,
        "http_status": code,
        "timeline": types,
        "clarification_required": clarify,
        "approval_required": approval,
        "done_final": (done or {}).get("data", {}).get("final"),
        "execution_status": st["execution_status"],
        "thread_id": open_status(db_path, sid)["thread_id"],
        "thread_id_consistent": thread_id_consistent(db_path, sid),
        "mode": open_status(db_path, sid)["mode"],
    }


def lane_l6_handoff(client: httpx.Client, db_path: str) -> dict:
    sid = new_session(client)
    code1, ev1 = chat_sse(client, sid, "我要转人工")
    types1 = event_types(ev1)
    created = next((e for e in ev1 if e.get("type") == "handoff_created"), None)
    handoff_id = (created or {}).get("data", {}).get("handoff_id")
    mode_after_handoff = open_status(db_path, sid)["mode"]

    resolve = None
    if handoff_id:
        r = client.post(f"{BASE}/handoffs/{handoff_id}/resolve")
        resolve = {
            "http": r.status_code,
            "body": r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text,
        }
    mode_after_resolve = open_status(db_path, sid)["mode"]
    handoffs = open_handoffs(db_path, sid)
    st = graph_state(db_path, open_status(db_path, sid)["thread_id"])

    code2, ev2 = chat_sse(client, sid, "你好，还在吗？")
    types2 = event_types(ev2)
    done2 = next((e for e in reversed(ev2) if e.get("type") == "done"), None)
    final2 = (done2 or {}).get("data", {}).get("final")

    passed = (
        code1 == 200
        and created is not None
        and mode_after_handoff == "human"
        and (resolve or {}).get("http") == 200
        and (resolve or {}).get("body", {}).get("mode") == "ai"
        and mode_after_resolve == "ai"
        and bool(handoffs)
        and handoffs[0]["status"] == "resolved"
        and not st["interrupts"]
        and code2 == 200
        and done2 is not None
        and bool(final2)
        and thread_id_consistent(db_path, sid)
    )
    return {
        "id": "L6",
        "name": "HandoffResolve",
        "pass": passed,
        "session_id": sid,
        "handoff_id": handoff_id,
        "chat1": {"http": code1, "timeline": types1},
        "mode_after_handoff": mode_after_handoff,
        "resolve": resolve,
        "mode_after_resolve": mode_after_resolve,
        "handoff_records": handoffs,
        "chat2": {
            "http": code2,
            "timeline": types2,
            "done_final": final2,
        },
        "interrupts": st["interrupts"],
        "thread_id": open_status(db_path, sid)["thread_id"],
        "thread_id_consistent": thread_id_consistent(db_path, sid),
    }


def write_reports(results: list[dict], db_path: str, started_at: str) -> tuple[Path, Path]:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = REPORTS_DIR / f"real_smoke_lanes_{stamp}"
    json_path = base.with_suffix(".json")
    md_path = base.with_suffix(".md")

    payload = {
        "started_at": started_at,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "db_path": db_path,
        "ark_model": os.environ.get("ARK_MODEL"),
        "results": results,
        "summary": {
            "passed": sum(1 for r in results if r["pass"]),
            "total": len(results),
            "all_pass": all(r["pass"] for r in results),
        },
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    lines = [
        "# Real Smoke Lanes Report",
        "",
        f"- Started: `{payload['started_at']}`",
        f"- Finished: `{payload['finished_at']}`",
        f"- Model: `{payload['ark_model']}`",
        f"- Result: **{payload['summary']['passed']}/{payload['summary']['total']}** "
        f"({'ALL PASS' if payload['summary']['all_pass'] else 'HAS FAILURES'})",
        f"- DB: `{db_path}`",
        "",
        "## Lanes",
        "",
        "| ID | Lane | Result | Notes |",
        "| --- | --- | --- | --- |",
    ]
    for r in results:
        notes = ""
        if r["id"] == "L1":
            notes = f"final={'yes' if r.get('done_final') else 'no'}"
        elif r["id"] == "L2":
            notes = f"tools={r.get('turn2', {}).get('tool_call')}/{r.get('turn2', {}).get('tool_result')}"
        elif r["id"] == "L3":
            notes = (
                f"form={r.get('form_required')} clarify={r.get('clarification_required')} "
                f"confirm={r.get('confirmation_required')}"
            )
        elif r["id"] == "L4":
            notes = f"approval_id={r.get('approval_id')} tools={r.get('tool_messages')}"
        elif r["id"] == "L5":
            notes = f"clarify={r.get('clarification_required')} approval={r.get('approval_required')}"
        elif r["id"] == "L6":
            notes = f"mode={r.get('mode_after_handoff')}->{r.get('mode_after_resolve')}"
        lines.append(
            f"| {r['id']} | {r['name']} | {'PASS' if r['pass'] else 'FAIL'} | {notes} |"
        )

    lines.extend(["", "## Evidence", ""])
    for r in results:
        lines.append(f"### {r['id']} {r['name']}: {'PASS' if r['pass'] else 'FAIL'}")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(r, ensure_ascii=False, indent=2, default=str))
        lines.append("```")
        lines.append("")

    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path, json_path


def print_console(results: list[dict], md_path: Path, json_path: Path, db_path: str) -> None:
    print("=" * 72)
    print("Real Smoke Lanes (ARK)")
    print("=" * 72)
    for r in results:
        mark = "PASS" if r["pass"] else "FAIL"
        print(f"{r['id']} {r['name']}: {mark}")
        print(f"  timeline/evidence: {json.dumps({k: v for k, v in r.items() if k not in ('pass',)}, ensure_ascii=False, default=str)[:500]}")
    print("=" * 72)
    print(f"report_md: {md_path}")
    print(f"report_json: {json_path}")
    print(f"db: {db_path}")
    print(f"conclusion: {'ALL PASS' if all(r['pass'] for r in results) else 'HAS FAILURES'}")


def main() -> int:
    preflight_ark()
    started_at = datetime.now().isoformat(timespec="seconds")
    tmp = tempfile.mkdtemp(prefix="smoke-lanes-")
    db_path = os.path.join(tmp, "smoke.db")
    # Isolate mock ecommerce fixtures so prior smoke runs cannot leave ORD-* dirty.
    # Mutation write tools (_write_connect) always read Settings.mock_ecommerce_db_path,
    # so point env + clear settings cache before create_app.
    profile_db_path = os.path.join(tmp, "mock_ecommerce.db")
    build_database(profile_db_path)
    os.environ["MOCK_ECOMMERCE_DB_PATH"] = profile_db_path
    from ai_service_agent.config import get_settings

    get_settings.cache_clear()
    app = create_app(db_path, model=make_model(), profile_db_path=profile_db_path)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while not getattr(server, "started", False) and time.time() < deadline:
        time.sleep(0.05)
    if not getattr(server, "started", False):
        print("FAIL: uvicorn did not start")
        return 1

    try:
        # Local uvicorn must not go through corporate HTTP(S)_PROXY.
        with httpx.Client(timeout=REQUEST_TIMEOUT, trust_env=False) as client:
            runners = [
                ("L1", "Read", lane_l1_read),
                ("L2", "MutationConfirm", lane_l2_mutation_confirm),
                ("L3", "MutationFormIncomplete", lane_l3_mutation_form_incomplete),
                ("L4", "HighRiskApprove", lane_l4_highrisk_approve),
                ("L5", "HighRiskClarify", lane_l5_highrisk_clarify),
                ("L6", "HandoffResolve", lane_l6_handoff),
            ]
            results = []
            for lane_id, lane_name, fn in runners:
                try:
                    results.append(fn(client, db_path))
                except Exception as exc:  # noqa: BLE001 — smoke must continue remaining lanes
                    results.append(
                        {
                            "id": lane_id,
                            "name": lane_name,
                            "pass": False,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
        md_path, json_path = write_reports(results, db_path, started_at)
        print_console(results, md_path, json_path, db_path)
        return 0 if all(r["pass"] for r in results) else 2
    finally:
        server.should_exit = True
        thread.join(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())
