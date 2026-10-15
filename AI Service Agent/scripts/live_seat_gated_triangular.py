"""Live triangular smoke for seat-gated handoff (#82).

Starts an ephemeral Runtime on AI_SERVICE_PORT (default 8766, matching Chatwoot
Account Webhook URL host.docker.internal:8766), then:

  A) customer request hang → mode stays ai + Chatwoot note/label
  B) FAQ still AI
  C) Chatwoot toggle_status(open) → webhook Takeover → Joined Notice + mode human
  D) further chat stays human tip
  E) Chatwoot toggle_status(resolved) → webhook Resolve → mode ai
  F) FAQ AI again

Prereq: scripts/check_seat_gated_webhooks.py PASS with matching RUNTIME_BASE.

Usage:
  $env:UV_CACHE_DIR="<repo>\\.uv-cache"; $env:UV_LINK_MODE="copy"
  $env:AI_SERVICE_PORT="8766"
  uv run --no-sync python scripts/live_seat_gated_triangular.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

import httpx
import uvicorn

from ai_service_agent.config import get_settings, make_model
from ai_service_agent.graph.workflows.copy import (
    handoff_joined_notice_text,
    handoff_request_confirm_text,
)
from ai_service_agent.harness.server import create_app

HOST = "0.0.0.0"
PORT = int(os.environ.get("AI_SERVICE_PORT", "8766"))
BASE = f"http://127.0.0.1:{PORT}"
REQUEST_TIMEOUT = 180.0
WEBHOOK_WAIT = 45.0


def parse_sse(text: str) -> list[dict]:
    events = []
    for block in text.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                try:
                    events.append(json.loads(line[len("data: "):]))
                except json.JSONDecodeError:
                    events.append({"type": "RAW", "data": line})
    return events


def event_types(events: list[dict]) -> list[str]:
    return [e.get("type", "?") for e in events]


def chat_sse(client: httpx.Client, session_id: str, message: str) -> tuple[int, list[dict]]:
    with client.stream(
        "POST", f"{BASE}/chat", json={"session_id": session_id, "message": message}
    ) as resp:
        text = "".join(resp.iter_text())
    return resp.status_code, parse_sse(text)


def get_mode(client: httpx.Client, sid: str) -> str | None:
    r = client.get(f"{BASE}/conversations/{sid}/messages")
    if r.status_code != 200:
        return None
    return r.json().get("mode")


def wait_mode(client: httpx.Client, sid: str, expected: str, timeout: float = WEBHOOK_WAIT) -> str | None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = get_mode(client, sid)
        if last == expected:
            return last
        time.sleep(0.5)
    return last


class ChatwootSeat:
    def __init__(self, settings):
        self.base = settings.chatwoot_base_url.rstrip("/")
        self.account_id = settings.chatwoot_account_id
        self.token = settings.chatwoot_api_token

    def _headers(self) -> dict:
        return {"api_access_token": self.token}

    def find_cw_id(self, db_path: str, conv_id: str) -> int | None:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT chatwoot_conversation_id FROM chatwoot_conversation "
                "WHERE conversation_id=?",
                (conv_id,),
            ).fetchone()
            return None if row is None else int(row[0])
        finally:
            conn.close()

    def labels(self, cw_id: int) -> list[str]:
        r = httpx.get(
            f"{self.base}/api/v1/accounts/{self.account_id}/conversations/{cw_id}/labels",
            headers=self._headers(),
            timeout=15,
            trust_env=False,
        )
        if r.status_code != 200:
            return []
        payload = r.json().get("payload")
        return [str(x) for x in payload] if isinstance(payload, list) else []

    def messages(self, cw_id: int) -> list[dict]:
        r = httpx.get(
            f"{self.base}/api/v1/accounts/{self.account_id}/conversations/{cw_id}/messages",
            headers=self._headers(),
            timeout=15,
            trust_env=False,
        )
        if r.status_code != 200:
            return []
        return r.json().get("payload") or []

    def toggle_status(self, cw_id: int, status: str) -> bool:
        r = httpx.post(
            f"{self.base}/api/v1/accounts/{self.account_id}/conversations/{cw_id}/toggle_status",
            headers=self._headers(),
            json={"status": status},
            timeout=15,
            trust_env=False,
        )
        return r.status_code == 200


def report(results: list[dict]) -> int:
    print("=" * 72)
    print("Live seat-gated triangular (#82)")
    print("=" * 72)
    for r in results:
        print(f"\n### {r['name']}: {'PASS' if r['pass'] else 'FAIL'}")
        for k, v in r.items():
            if k in ("name", "pass"):
                continue
            print(f"  {k}: {json.dumps(v, ensure_ascii=False, default=str)}")
    ok = all(r["pass"] for r in results)
    print("\n" + "=" * 72)
    print(f"结论: {'全部通过' if ok else '存在失败'}")
    return 0 if ok else 2


def main() -> int:
    settings = get_settings()
    if not settings.chatwoot_enabled:
        print("FAIL: CHATWOOT_ENABLED must be true"); return 1
    tmp = Path(tempfile.mkdtemp(prefix="seat-gated-tri-"))
    db_path = str(tmp / "runtime.db")
    model = "fake" if settings.fake_model else make_model()
    app = create_app(db_path, model=model, auth_enabled=False)
    server = uvicorn.Server(
        uvicorn.Config(app, host=HOST, port=PORT, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while not getattr(server, "started", False) and time.time() < deadline:
        time.sleep(0.05)
    if not getattr(server, "started", False):
        print(f"FAIL: uvicorn did not bind {HOST}:{PORT} (is the port free?)")
        return 1

    results: list[dict] = []
    seat = ChatwootSeat(settings)
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT, trust_env=False) as client:
            r = client.post(f"{BASE}/sessions", json={"user_id": "ops-82-tri"})
            sid = r.json().get("session_id")
            results.append({
                "name": "0.session",
                "pass": r.status_code == 200 and bool(sid),
                "http": r.status_code,
                "session_id": sid,
            })
            if not sid:
                return report(results)

            # A — request hang
            code, ev = chat_sse(client, sid, "我要转人工")
            types = event_types(ev)
            tip = handoff_request_confirm_text()
            mode = get_mode(client, sid)
            hang_ok = (
                code == 200
                and "handoff_requested" in types
                and "handoff_created" not in types
                and mode == "ai"
                and any(
                    e.get("type") == "token" and e.get("data", {}).get("text") == tip
                    for e in ev
                )
            )
            cw_id = seat.find_cw_id(db_path, sid)
            labels = seat.labels(cw_id) if cw_id else []
            private_note = False
            if cw_id:
                private_note = any(
                    m.get("private") is True and bool((m.get("content") or "").strip())
                    for m in seat.messages(cw_id)
                )
            results.append({
                "name": "A.request_hang_mode_ai",
                "pass": hang_ok and cw_id is not None and "handoff-request" in labels and private_note,
                "http": code,
                "types": types,
                "mode": mode,
                "cw_id": cw_id,
                "labels": labels,
                "private_note": private_note,
            })
            if cw_id is None:
                return report(results)

            # B — AI still replies after hang
            code, ev = chat_sse(client, sid, "介绍一下退货政策")
            types = event_types(ev)
            mode = get_mode(client, sid)
            ai_ok = (
                code == 200
                and "done" in types
                and any(e.get("type") == "token" for e in ev)
                and mode == "ai"
            )
            results.append({
                "name": "B.ai_still_replies",
                "pass": ai_ok,
                "http": code,
                "types": types,
                "mode": mode,
            })

            # Ensure Chatwoot status is pending before Takeover so pending→open
            # emits conversation_status_changed (API-created convs often start open).
            pending_ok = seat.toggle_status(cw_id, "pending")
            time.sleep(1.0)
            mode_after_pending = get_mode(client, sid)
            results.append({
                "name": "B2.chatwoot_pending_baseline",
                "pass": pending_ok and mode_after_pending == "ai",
                "toggle_ok": pending_ok,
                "mode": mode_after_pending,
            })

            # C — Seat Takeover via Chatwoot open (live Account Webhook)
            toggled = seat.toggle_status(cw_id, "open")
            mode = wait_mode(client, sid, "human")
            msgs = client.get(f"{BASE}/conversations/{sid}/messages").json().get("items") or []
            joined = handoff_joined_notice_text()
            has_joined = any(
                it.get("role") == "assistant" and it.get("content") == joined for it in msgs
            )
            results.append({
                "name": "C.takeover_joined_notice",
                "pass": toggled and mode == "human" and has_joined,
                "toggle_ok": toggled,
                "mode": mode,
                "joined_notice": has_joined,
            })

            # D — human lane tip on further chat (no Graph / no token stream)
            code, ev = chat_sse(client, sid, "人工还在吗")
            mode = get_mode(client, sid)
            types = event_types(ev)
            human_tip = next(
                (
                    e.get("data", {}).get("final")
                    for e in ev
                    if e.get("type") == "done"
                ),
                None,
            )
            human_ok = (
                code == 200
                and mode == "human"
                and "done" in types
                and "token" not in types
                and human_tip == "已转人工，坐席将尽快回复。"
            )
            results.append({
                "name": "D.human_lane_further_chat",
                "pass": human_ok,
                "http": code,
                "types": types,
                "mode": mode,
                "final": human_tip,
            })

            # E — Resolve via Chatwoot resolved
            toggled = seat.toggle_status(cw_id, "resolved")
            mode = wait_mode(client, sid, "ai")
            results.append({
                "name": "E.resolve_mode_ai",
                "pass": toggled and mode == "ai",
                "toggle_ok": toggled,
                "mode": mode,
            })

            # F — AI resumes
            code, ev = chat_sse(client, sid, "再介绍一下退货政策")
            types = event_types(ev)
            mode = get_mode(client, sid)
            resume_ok = (
                code == 200
                and mode == "ai"
                and "done" in types
                and any(e.get("type") == "token" for e in ev)
            )
            results.append({
                "name": "F.ai_resumes",
                "pass": resume_ok,
                "http": code,
                "types": types,
                "mode": mode,
            })

        return report(results)
    finally:
        server.should_exit = True
        thread.join(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())
