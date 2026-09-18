"""真实模型 + 真实 Chatwoot 人工车道 + 页面契约冒烟（阶段 2/3 验证）。

- 前置：.env 配置 ARK_MODEL/ARK_BASE_URL/ARK_API_KEY + CHATWOOT_*（enabled=true）；
  Chatwoot inbox webhook 指向 http://<宿主机LAN IP>:8000/webhooks/chatwoot（本机 172.99.46.165:8000）。
- 每次运行使用全新临时 SQLite DB；Runtime 绑定 0.0.0.0:8000，供 Chatwoot webhook 回推。
- 覆盖页面 createApi 实际消费的契约：/auth/login、/sessions{user_id}、/chat{session_id,message}(SSE)、
  GET /conversations/{id}/messages -> {items, mode}、/handoffs/{id}/resolve。
- 人工车道链路：转人工(handoff_created) -> 人工消息(Message Store+Chatwoot) -> 坐席 outgoing 消息
  (webhook 回写 Message Store) -> resolve(mode=ai) -> AI 恢复(done)。
- 用法：$env:UV_CACHE_DIR="<repo>\\.uv-cache"; $env:UV_LINK_MODE="copy";
        uv run --no-sync python scripts/real_human_lane_smoke.py
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

from ai_service_agent.config import make_model, get_settings
from ai_service_agent.harness.server import create_app

HOST = "0.0.0.0"
PORT = 8000
BASE = f"http://127.0.0.1:{PORT}"
REQUEST_TIMEOUT = 180.0
HANDOFF_PHRASES = ["我要转人工", "帮我转人工", "转人工", "我不想跟机器人聊，叫个真人来"]
MAX_HANDOFF_ATTEMPTS = 3


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


def error_details(events: list[dict]) -> list[dict]:
    return [e.get("data", {}) for e in events if e.get("type") == "error"]


def ai_turn_ok(code: int, ev: list[dict]) -> bool:
    """AI 车道干净通过：http 200 + done + 有 token 内容 + 无 error 事件。"""
    return (
        code == 200
        and "done" in event_types(ev)
        and any(e.get("type") == "token" for e in ev)
        and not error_details(ev)
    )


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


def open_messages(db_path: str, conv_id: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    try:
        return [
            {"role": r[0], "content": r[1], "ts": r[2]}
            for r in conn.execute(
                "SELECT role, content, created_at, message_id FROM conversation_message WHERE conversation_id=? ORDER BY id",
                (conv_id,),
            )
        ]
    finally:
        conn.close()


def open_handoffs(db_path: str, conv_id: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    try:
        return [
            {"handoff_id": r[0], "status": r[2], "reason": r[1]}
            for r in conn.execute(
                "SELECT handoff_id, reason, status FROM handoff_record WHERE conversation_id=?",
                (conv_id,),
            )
        ]
    finally:
        conn.close()


def chat_sse(client: httpx.Client, session_id: str, message: str) -> tuple[int, list[dict]]:
    with client.stream(
        "POST", f"{BASE}/chat", json={"session_id": session_id, "message": message}
    ) as resp:
        text = "".join(resp.iter_text())
    return resp.status_code, parse_sse(text)


def report(results: list[dict]) -> int:
    print("=" * 76)
    print("真机人工车道 + 页面契约冒烟报告（ARK 真实模型 + Chatwoot 真实坐席端）")
    print("=" * 76)
    for r in results:
        print(f"\n### {r['name']}: {'PASS' if r['pass'] else 'FAIL'}")
        for k, v in r.items():
            if k in ("name", "pass"):
                continue
            print(f"  {k}: {json.dumps(v, ensure_ascii=False, default=str)}")
    ok = all(r["pass"] for r in results)
    print("\n" + "=" * 76)
    print(f"结论: {'全部通过' if ok else '存在失败'}")
    return 0 if ok else 2


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="human-lane-"))
    db_path = str(tmp / "runtime.db")
    s = get_settings()
    app = create_app(db_path, model=make_model())
    server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=PORT, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while not getattr(server, "started", False) and time.time() < deadline:
        time.sleep(0.05)
    if not getattr(server, "started", False):
        print("FAIL: uvicorn server did not start"); return 1

    results = []
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT, trust_env=False) as client:
            # ---- 1. login（页面 createApi.login）----
            r = client.post(f"{BASE}/auth/login", json={"account": "demo", "password": "demo"})
            body = r.json()
            login_ok = (
                r.status_code == 200
                and isinstance(body.get("token"), str) and body.get("token")
                and isinstance(body.get("user", {}).get("id"), str)
            )
            results.append({"name": "1.login(页面契约)", "pass": login_ok, "http": r.status_code,
                            "body_keys": sorted(body.keys()), "user": body.get("user")})

            user_id = (body.get("user") or {}).get("id") or "anonymous"

            # ---- 2. createSession({user_id}) ----
            r = client.post(f"{BASE}/sessions", json={"user_id": user_id})
            body = r.json()
            session_ok = r.status_code == 200 and body.get("session_id") and body.get("mode") == "ai"
            results.append({"name": "2.createSession(user_id)", "pass": session_ok, "http": r.status_code,
                            "session_id": body.get("session_id"), "mode": body.get("mode")})
            sid = body["session_id"]

            # ---- 3. GET messages 页面形状 {items, mode} ----
            r = client.get(f"{BASE}/conversations/{sid}/messages")
            body = r.json()
            shape_ok = r.status_code == 200 and isinstance(body.get("items"), list) and body.get("mode") == "ai"
            results.append({"name": "3.getMessages(页面形状)", "pass": shape_ok, "http": r.status_code,
                            "items_len": len(body.get("items", [])), "mode": body.get("mode")})

            # ---- 4. AI 车道对话 ----
            code, ev = chat_sse(client, sid, "你好")
            types = event_types(ev)
            ai_ok = code == 200 and "done" in types and any(
                e.get("type") == "token" for e in ev
            )
            results.append({"name": "4.AI车道(SSE->done)", "pass": ai_ok, "http": code,
                            "types": types, "errors": error_details(ev),
                            "thread_id": (open_status(db_path, sid) or {}).get("thread_id")})

            # ---- 5. 转人工（P0：入口层 detector 前置拦截 -> handoff_created + mode=human）
            # 关键断言不是 Router 分类，而是 handoff_created + mode=human——即使 Router 原本
            # 可能误判，入口 detector 阶段已直接进入 Human Lane。多短语按序重试兜底。----
            handoff_created = None
            attempt = 0
            handoff_types = []
            for phrase in HANDOFF_PHRASES:
                attempt += 1
                code, ev = chat_sse(client, sid, phrase)
                handoff_types = event_types(ev)
                handoff_created = next((e for e in ev if e.get("type") == "handoff_created"), None)
                if handoff_created is not None:
                    break
            st = open_status(db_path, sid) or {}
            handoff_ok = (
                handoff_created is not None
                and st.get("mode") == "human"
            )
            results.append({"name": "5.转人工(handoff_created)", "pass": handoff_ok,
                            "attempts": attempt, "handoff_id": (handoff_created or {}).get("data", {}).get("handoff_id"),
                            "mode": st.get("mode"), "last_handoff_types": handoff_types,
                            "errors": error_details(ev)})
            handoff_id = (handoff_created or {}).get("data", {}).get("handoff_id")

            # ---- 6. 人工车道：POST /chat 落 Message Store + 推 Chatwoot ----
            code, ev = chat_sse(client, sid, "在吗")
            msgs = open_messages(db_path, sid)
            user_ok = code == 200 and any(m["role"] == "user" and m["content"] == "在吗" for m in msgs)
            results.append({"name": "6.人工消息落库(Message Store)", "pass": user_ok, "http": code,
                            "messages": msgs[-4:]})

            # 验证 Chatwoot 侧收到 contact 消息
            cw = ChatwootApiProbe(s)
            cw_find = cw.find_conversation(db_path, sid)
            cw_contact_ok = False
            if cw_find:
                cw_msgs = cw.messages(cw_find)
                cw_contact_ok = any(
                    m.get("message_type") == 0 and "在吗" in (m.get("content") or "")
                    and (m.get("sender") or {}).get("type") == "contact"
                    for m in cw_msgs
                )
            results.append({"name": "6b.Chatwoot收到contact消息", "pass": cw_contact_ok,
                            "cw_conversation": cw_find, "cw_message_count": len(cw.msgs_last if hasattr(cw, "msgs_last") else [])})

            # ---- 7. 坐席 outgoing 消息 -> webhook 回写 Message Store ----
            agent_ok = False
            if cw_find:
                sent = cw.send_agent_message(cw_find, "好的，已为您登记，人工客服正在跟进。")
                poll_deadline = time.time() + 30
                assistant_msg = None
                while time.time() < poll_deadline:
                    msgs = open_messages(db_path, sid)
                    assistant_msg = next(
                        (m for m in msgs if m["role"] == "assistant" and "正在跟进" in (m["content"] or "")),
                        None,
                    )
                    if assistant_msg is not None:
                        break
                    time.sleep(1.0)
                # 去重：只应落 1 条
                dup = [m for m in open_messages(db_path, sid)
                       if m["role"] == "assistant" and "正在跟进" in (m["content"] or "")]
                agent_ok = sent and assistant_msg is not None and len(dup) == 1
                results.append({"name": "7.坐席回复(webhook回写)", "pass": agent_ok,
                                "agent_api_sent": sent, "assistant_msg": assistant_msg,
                                "duplicate_count": len(dup)})
            else:
                results.append({"name": "7.坐席回复(webhook回写)", "pass": False, "agent_api_sent": False,
                                "reason": "无 Chatwoot conversation 映射"})

            # ---- 8. GET messages 应含坐席回复 + mode=human ----
            r = client.get(f"{BASE}/conversations/{sid}/messages")
            body = r.json()
            poll_ok = (
                body.get("mode") == "human"
                and any("正在跟进" in (it.get("content") or "") for it in body.get("items", []) if it.get("role") == "assistant")
            )
            results.append({"name": "8.getMessages含坐席回复+mode=human", "pass": poll_ok,
                            "http": r.status_code, "mode": body.get("mode"),
                            "last_items": body.get("items", [])[-3:]})

            # ---- 9. resolve -> mode=ai ----
            resolve_ok = False
            if handoff_id:
                r = client.post(f"{BASE}/handoffs/{handoff_id}/resolve")
                st = open_status(db_path, sid) or {}
                hds = open_handoffs(db_path, sid)
                resolve_ok = r.status_code == 200 and st.get("mode") == "ai" and (hds or [{}])[0].get("status") == "resolved"
            results.append({"name": "9.resolve(mode->ai)", "pass": resolve_ok,
                            "http": r.status_code if handoff_id else None,
                            "mode": (open_status(db_path, sid) or {}).get("mode"),
                            "handoff_records": open_handoffs(db_path, sid)})

            # ---- 10. AI 恢复（真实模型偶发失败：记录 error 详情，换问重试一次）----
            code1, ev1 = chat_sse(client, sid, "那我的收货地址能帮我看看吗")
            ok1 = ai_turn_ok(code1, ev1)
            retried = False
            code2, ev2 = None, []
            if not ok1:
                retried = True
                code2, ev2 = chat_sse(client, sid, "介绍一下你们的退货政策")
            st = open_status(db_path, sid) or {}
            resume_ok = (ok1 or ai_turn_ok(code2, ev2)) and st.get("mode") == "ai"
            results.append({"name": "10.AI恢复(mode=ai, done)", "pass": resume_ok,
                            "http": code1, "retried": retried,
                            "attempt1": {"http": code1, "types": event_types(ev1), "errors": error_details(ev1)},
                            "attempt2": {"http": code2, "types": event_types(ev2), "errors": error_details(ev2)},
                            "mode": st.get("mode"), "thread_id": st.get("thread_id")})

        return report(results)
    finally:
        server.should_exit = True
        thread.join(timeout=10)


class ChatwootApiProbe:
    """直接调 Chatwoot API 验证坐席侧（只读 + 模拟坐席 outgoing）。"""

    def __init__(self, settings):
        self.base = settings.chatwoot_base_url.rstrip("/")
        self.account_id = settings.chatwoot_account_id
        self.api_token = settings.chatwoot_api_token
        self.msgs_last = []

    def _headers(self) -> dict:
        return {"api_access_token": self.api_token}

    def find_conversation(self, db_path: str, conv_id: str) -> int | None:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT chatwoot_conversation_id FROM chatwoot_conversation WHERE conversation_id=?",
                (conv_id,),
            ).fetchone()
            return None if row is None else row[0]
        finally:
            conn.close()

    def messages(self, cw_id: int) -> list[dict]:
        r = httpx.get(
            f"{self.base}/api/v1/accounts/{self.account_id}/conversations/{cw_id}/messages",
            headers=self._headers(), timeout=15, trust_env=False,
        )
        if r.status_code != 200:
            return []
        payload = r.json().get("payload", [])
        self.msgs_last = payload
        return payload

    def send_agent_message(self, cw_id: int, content: str) -> bool:
        r = httpx.post(
            f"{self.base}/api/v1/accounts/{self.account_id}/conversations/{cw_id}/messages",
            headers=self._headers(), json={"content": content, "message_type": "outgoing"}, timeout=15, trust_env=False,
        )
        return r.status_code == 200


if __name__ == "__main__":
    raise SystemExit(main())



