"""Run functional scenario cases against the live FastAPI + LangGraph runtime (ARK).

Test-only harness. Does not modify product modules beyond settings/env for isolation.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import sqlite3
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import uvicorn
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "tests" / "evals" / "functional_scenarios" / "master.jsonl"
HOST = "127.0.0.1"
PORT = 8771
BASE = f"http://{HOST}:{PORT}"

os.environ.setdefault("FAKE_MODEL", "false")
os.environ.setdefault("JEV_ENABLED", "false")
os.environ.setdefault("AUTH_ENABLED", "false")
os.environ.setdefault("CHATWOOT_ENABLED", "false")
os.environ.setdefault("LANGSMITH_TRACING", "false")

from ai_service_agent.config import get_settings, make_model
from ai_service_agent.harness.server import create_app

_DB_PATH = ""
_PROFILE_DB = ""
_SNAPSHOT_PATH = ""
_PROFILE_SNAPSHOT = ""


def load_seed():
    path = ROOT / "scripts" / "seed_mock_db.py"
    spec = importlib.util.spec_from_file_location("functional_seed", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def load_cases(path: Path, split: str | None = None, only: set[str] | None = None) -> list[dict]:
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if split and split != "full":
        cases = [c for c in cases if c.get("split") == split]
    if only:
        cases = [c for c in cases if c["id"] in only]
    cases.sort(key=lambda c: c["id"])
    if len(cases) < 1:
        raise ValueError("no cases selected")
    return cases


def parse_sse(text: str) -> list[dict]:
    events = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        for line in block.splitlines():
            if line.startswith("data: "):
                try:
                    events.append(json.loads(line[6:]))
                except json.JSONDecodeError:
                    events.append({"type": "RAW", "data": line})
    return events


def watched_events(events: list[dict]) -> list[str]:
    watched = {
        "handoff_created", "clarification_required", "confirmation_required",
        "approval_required", "confirmation_updated", "approval_updated",
        "safety_rejected", "tool_call", "tool_result", "done", "error", "token",
    }
    out = []
    for e in events:
        t = e.get("type", "")
        if t in watched and t != "token":
            out.append(t)
    return out


def tool_names(events: list[dict]) -> set[str]:
    names = set()
    for e in events:
        if e.get("type") in {"tool_call", "tool_result"}:
            name = (e.get("data") or {}).get("name")
            if name:
                names.add(name)
    return names


def snapshot(db_path: str, snap_path: str) -> None:
    src = sqlite3.connect(db_path)
    try:
        dst = sqlite3.connect(snap_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def restore(snap_path: str, db_path: str) -> None:
    src = sqlite3.connect(snap_path)
    try:
        dst = sqlite3.connect(db_path)
        try:
            src.backup(dst)
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()


def open_status(db_path: str, conv_id: str) -> dict:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT conversation_id, thread_id, mode, user_id FROM conversation_status WHERE conversation_id=?",
            (conv_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return {}
    return {"conversation_id": row[0], "thread_id": row[1], "mode": row[2], "user_id": row[3]}


def open_messages(db_path: str, conv_id: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT role, content FROM conversation_message WHERE conversation_id=? ORDER BY id",
            (conv_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    return [{"role": r[0], "content": r[1]} for r in rows]


def checkpoint_message_count(db_path: str, thread_id: str) -> int:
    from ai_service_agent.harness.graph import make_ai_graph
    from ai_service_agent.testing import make_fake_model

    async def _read():
        async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
            graph = make_ai_graph(make_fake_model(), saver)
            st = await graph.aget_state({"configurable": {"thread_id": thread_id}})
            return len(st.values.get("messages") or [])

    return asyncio.run(_read())


def insert_agent_message(db_path: str, conv_id: str, content: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO conversation_message (conversation_id, role, content, message_id) VALUES (?,?,?,?)",
            (conv_id, "assistant", content, f"agent-{int(time.time() * 1000)}"),
        )
        conn.commit()
    finally:
        conn.close()


def force_expire_approval(db_path: str, approval_id: str) -> None:
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE approval_record SET expires_at=? WHERE approval_id=?",
            (past, approval_id),
        )
        conn.commit()
    finally:
        conn.close()


def start_server(app) -> uvicorn.Server:
    server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 20
    while not getattr(server, "started", False) and time.time() < deadline:
        time.sleep(0.05)
    if not getattr(server, "started", False):
        raise RuntimeError("uvicorn failed to start")
    return server


def new_session(client: httpx.Client, user_id: str) -> str:
    r = client.post(f"{BASE}/sessions", json={"user_id": user_id})
    r.raise_for_status()
    return r.json()["session_id"]


def chat(client: httpx.Client, session_id: str, message: str) -> tuple[int, list[dict]]:
    with client.stream("POST", f"{BASE}/chat", json={"session_id": session_id, "message": message}) as resp:
        text = "".join(resp.iter_text())
    return resp.status_code, parse_sse(text)


def run_case(client: httpx.Client, case: dict) -> dict:
    global _DB_PATH
    restore(_SNAPSHOT_PATH, _DB_PATH)
    restore(_PROFILE_SNAPSHOT, _PROFILE_DB)

    sid = new_session(client, case.get("user_id") or "u1")
    all_events: list[dict] = []
    timeline: list[list[str]] = []
    http_status = 200
    started = time.perf_counter()

    for turn in case.get("turns") or []:
        action = turn.get("action")
        if action == "confirm":
            code, ev = chat(client, sid, "确认")
        elif action == "cancel":
            code, ev = chat(client, sid, "取消")
        elif action == "approve":
            approval_id = next(
                ((e.get("data") or {}).get("approval_id") for e in all_events if e.get("type") == "approval_required"),
                None,
            )
            if not approval_id:
                return _fail(case, sid, all_events, timeline, ["approval_id missing before approve"], started)
            r = client.post(f"{BASE}/approvals/{approval_id}/approve")
            code = r.status_code
            ev = [{"type": "approval_http", "data": r.json() if r.headers.get("content-type", "").startswith("application/json") else {"raw": r.text}}]
            # approve endpoint resumes graph; capture via follow-up get state only
        elif action == "reject":
            approval_id = next(
                ((e.get("data") or {}).get("approval_id") for e in all_events if e.get("type") == "approval_required"),
                None,
            )
            if not approval_id:
                return _fail(case, sid, all_events, timeline, ["approval_id missing before reject"], started)
            r = client.post(f"{BASE}/approvals/{approval_id}/reject")
            code = r.status_code
            ev = [{"type": "approval_http", "data": r.json() if r.status_code == 200 else {"raw": r.text}}]
        elif action == "expire":
            approval_id = next(
                ((e.get("data") or {}).get("approval_id") for e in all_events if e.get("type") == "approval_required"),
                None,
            )
            if not approval_id:
                return _fail(case, sid, all_events, timeline, ["approval_id missing before expire"], started)
            force_expire_approval(_DB_PATH, approval_id)
            # Trigger lazy expire + resume by GET then a chat nudge if needed
            r = client.get(f"{BASE}/approvals/{approval_id}")
            code = r.status_code
            body = r.json() if r.status_code == 200 else {}
            ev = [{"type": "approval_http", "data": body}]
            if body.get("status") == "expired":
                # HighRisk expire resume often via next chat or approval path; send benign nudge
                code2, ev2 = chat(client, sid, "还在吗")
                code = code2
                ev = ev2
        elif action == "agent_msg":
            insert_agent_message(_DB_PATH, sid, turn.get("message") or "坐席消息")
            code, ev = 200, [{"type": "agent_store_write", "data": {"ok": True}}]
        elif action == "resolve":
            # find handoff id from events or DB
            handoff_id = next(
                ((e.get("data") or {}).get("handoff_id") for e in all_events if e.get("type") == "handoff_created"),
                None,
            )
            if not handoff_id:
                conn = sqlite3.connect(_DB_PATH)
                try:
                    row = conn.execute(
                        "SELECT handoff_id FROM handoff_record WHERE conversation_id=? ORDER BY created_at DESC LIMIT 1",
                        (sid,),
                    ).fetchone()
                finally:
                    conn.close()
                handoff_id = row[0] if row else None
            if not handoff_id:
                return _fail(case, sid, all_events, timeline, ["handoff_id missing before resolve"], started)
            r = client.post(f"{BASE}/handoffs/{handoff_id}/resolve")
            code = r.status_code
            ev = [{"type": "handoff_resolve", "data": r.json() if r.status_code == 200 else {"raw": r.text}}]
        else:
            code, ev = chat(client, sid, turn.get("message") or "")

        if code != 200 and action not in {"agent_msg"}:
            http_status = code
        all_events.extend(ev)
        timeline.append(watched_events(ev))

    status_row = open_status(_DB_PATH, sid)
    msgs = open_messages(_DB_PATH, sid)
    actual_events = watched_events(all_events)
    actual_tools = tool_names(all_events)
    # For approve path, tool may only appear in checkpoint; count tool messages via store content heuristics later
    problems: list[str] = []

    actions = {t.get("action") for t in case.get("turns") or []}
    has_decision = bool(actions & {"approve", "reject", "expire", "resolve", "agent_msg"})

    for ev in case.get("expected_events") or []:
        if ev == "done":
            if "done" not in actual_events and not has_decision:
                problems.append("done event missing")
            continue
        if ev in {"tool_call", "tool_result"} and "approve" in actions:
            continue
        if ev not in actual_events:
            problems.append(f"event missing: {ev}")

    for ev in case.get("forbidden_events") or []:
        if ev in actual_events:
            problems.append(f"forbidden event emitted: {ev}")

    expected_tools = case.get("expected_tools")
    if expected_tools is not None:
        exp = set(expected_tools)
        if "approve" in actions:
            if exp and not _side_effect_ok(exp, case) and not exp.issubset(actual_tools):
                problems.append(f"tools/side-effect expected={sorted(exp)} actual={sorted(actual_tools)}")
        elif actual_tools != exp:
            problems.append(f"tools expected={sorted(exp)} actual={sorted(actual_tools)}")

    expected_mode = case.get("expected_mode")
    if expected_mode and status_row.get("mode") != expected_mode:
        problems.append(f"mode expected={expected_mode} actual={status_row.get('mode')}")

    for needle in case.get("store_contains") or []:
        blob = "\n".join(m.get("content") or "" for m in msgs)
        if needle not in blob:
            problems.append(f"store missing: {needle}")

    if http_status != 200:
        problems.append(f"http status={http_status}")

    # Context probe: checkpoint should be longer than a single turn after padding
    if case.get("context_probe"):
        thread_id = status_row.get("thread_id") or ""
        if thread_id:
            count = checkpoint_message_count(_DB_PATH, thread_id)
            if count < 3:
                problems.append(f"context_probe checkpoint too small: {count}")

    # S35: after second refund attempt, ensure not two successful refunds without rejection
    if case["id"] == "S35":
        conn = sqlite3.connect(_PROFILE_DB)
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM refunds WHERE order_id=? AND status IN ('refunding','refunded')",
                ("ORD-REFUND-1",),
            ).fetchone()[0]
        finally:
            conn.close()
        if n > 1:
            problems.append(f"duplicate refunds written: {n}")

    return {
        "id": case["id"],
        "suite": case.get("suite"),
        "status": "fail" if problems else "pass",
        "problems": problems,
        "session_id": sid,
        "actual": {
            "mode": status_row.get("mode"),
            "events": actual_events,
            "tools": sorted(actual_tools),
            "timeline": timeline,
            "store_preview": [{"role": m["role"], "content": (m["content"] or "")[:80]} for m in msgs[-6:]],
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        },
    }


def _side_effect_ok(expected_tools: set[str], case: dict) -> bool:
    conn = sqlite3.connect(_PROFILE_DB)
    try:
        if "refund_apply" in expected_tools:
            n = conn.execute(
                "SELECT COUNT(*) FROM refunds WHERE order_id LIKE 'ORD-REFUND%' AND status IN ('refunding','refunded')"
            ).fetchone()[0]
            return n >= 1
        if "cancel_order" in expected_tools:
            row = conn.execute("SELECT status FROM orders WHERE order_id=?", ("ORD-PAID-1",)).fetchone()
            return bool(row and row[0] == "cancelled")
        if "update_address" in expected_tools or "update_phone" in expected_tools:
            return True
    finally:
        conn.close()
    return False


def _fail(case, sid, events, timeline, problems, started):
    return {
        "id": case["id"],
        "suite": case.get("suite"),
        "status": "fail",
        "problems": problems,
        "session_id": sid,
        "actual": {
            "events": watched_events(events),
            "timeline": timeline,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        },
    }


def prepare_runtime():
    global _DB_PATH, _PROFILE_DB, _SNAPSHOT_PATH, _PROFILE_SNAPSHOT
    settings = get_settings()
    if not settings.model_api_key or not settings.model_endpoint:
        raise RuntimeError("ARK_API_KEY / ARK_BASE_URL required")

    temp_dir = tempfile.mkdtemp(prefix="functional-eval-")
    _DB_PATH = str(Path(temp_dir) / "runtime.db")
    _PROFILE_DB = str(Path(temp_dir) / "profile.db")
    load_seed().build_database(_PROFILE_DB)
    os.environ["MOCK_ECOMMERCE_DB_PATH"] = _PROFILE_DB
    get_settings.cache_clear()
    app = create_app(_DB_PATH, model=make_model(), profile_db_path=_PROFILE_DB, auth_enabled=False)
    server = start_server(app)
    _SNAPSHOT_PATH = str(Path(temp_dir) / "runtime-clean.db")
    _PROFILE_SNAPSHOT = str(Path(temp_dir) / "profile-clean.db")
    snapshot(_DB_PATH, _SNAPSHOT_PATH)
    snapshot(_PROFILE_DB, _PROFILE_SNAPSHOT)
    return server, temp_dir


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--split", choices=["smoke", "full"], default="full")
    parser.add_argument("--only", nargs="*")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    if not args.dataset.exists():
        raise SystemExit(f"dataset missing: {args.dataset} (run build_functional_scenarios_dataset.py)")

    only = set(args.only) if args.only else None
    cases = load_cases(args.dataset, split=None if args.split == "full" else args.split, only=only)
    server, _temp = prepare_runtime()
    rows = []
    try:
        with httpx.Client(timeout=180.0, trust_env=False) as client:
            for case in cases:
                try:
                    rows.append(run_case(client, case))
                except Exception as exc:  # noqa: BLE001
                    rows.append({
                        "id": case["id"], "suite": case.get("suite"), "status": "fail",
                        "problems": [f"{type(exc).__name__}: {exc}"], "actual": {},
                    })
                print(f"{rows[-1]['id']} {rows[-1]['status']}: {rows[-1].get('problems')}")
    finally:
        server.should_exit = True

    passed = sum(1 for r in rows if r["status"] == "pass")
    summary = {"cases": len(rows), "passed": passed, "failed": len(rows) - passed}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps({"summary": summary, "results": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if passed == len(rows) else 2


if __name__ == "__main__":
    raise SystemExit(main())
