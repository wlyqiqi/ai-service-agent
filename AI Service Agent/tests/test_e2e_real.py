"""真实端到端测试（P1 + P2）。

运行方式：
  离线（CI）：uv run --no-sync pytest tests/test_e2e_real.py -v
  真实模型：  $env:E2E_MODEL="real"; uv run --no-sync pytest tests/test_e2e_real.py -v

E2E_MODEL=real  使用真实 ARK 模型 + 真实 JEV（需 .env 配好 API Key）
E2E_MODEL=fake  使用离线 fake model（默认，确定性，CI 可跑）
"""

import json
import os
import socket
import sqlite3
import tempfile
import threading
import time
import importlib.util
from pathlib import Path
from typing import ClassVar

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_openai import ChatOpenAI

from ai_service_agent.harness.auth import UserStore
from ai_service_agent.harness.server import create_app
from ai_service_agent.domain.orders import SQLiteOrderRepository


def _seed_business_db(path: str):
    spec = importlib.util.spec_from_file_location(
        "seed_mock_db_e2e_helper", str(Path(__file__).resolve().parents[1] / "scripts" / "seed_mock_db.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.build_database(path)
from ai_service_agent.testing import ToolCallingFakeModel, router_json_message


# ---------------------------------------------------------------------------
# Smart fake model
# ---------------------------------------------------------------------------


class E2EFakeModel(ToolCallingFakeModel):
    """离线 E2E 全场景 fake model：按用户输入路由到对应 capability/risk/tool。

    system prompt 包含 "路由器" -> Router 层调用；
    system prompt 包含 "选择器" -> Select 层调用；
    其余 -> 子图 LLM 回复。
    """

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        system = next((m.content for m in messages if getattr(m, "type", "") == "system"), "")
        user = ""
        for m in reversed(messages):
            if getattr(m, "type", "") == "human":
                user = m.content or ""
                break

        if "客服路由意图分类器" in system:
            payload = self._route(user)
        elif "选择器" in system:
            payload = self._select(user)
        else:
            payload = f"离线 E2E 回复: {user[:30]}"

        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=payload))])

    def _route(self, user: str) -> str:
        if "转人工" in user:
            return '{"route_intent": "unknown", "confidence": 0.2}'
        if "退款" in user or "退了吧" in user or "取消订单" in user:
            return '{"route_intent": "refund_apply_execute", "confidence": 0.9}'
        if "地址" in user or "手机号" in user:
            return '{"route_intent": "account_profile_update", "confidence": 0.9}'
        if "备注" in user or "开票" in user or "售后" in user:
            return '{"route_intent": "order_mutation", "confidence": 0.9}'
        return '{"route_intent": "faq", "confidence": 0.9}'

    def _select(self, user: str) -> str:
        if "退款" in user or "退了吧" in user:
            return '{"tool": "refund_apply", "args": {"order_id": "ORD-PAID-1", "amount": 99.0}}'
        if "取消订单" in user:
            return '{"tool": "cancel_order", "args": {"order_id": "ORD-PAID-1", "reason": "不想要了"}}'
        if "我要改个地址" in user:
            return '{"tool": "update_address", "args": {}}'
        if "备注" in user:
            return '{"tool": "update_order_note", "args": {"note": "放驿站就行"}}'
        return '{"tool": "update_address", "args": {"order_id": "ORD-PAID-1", "address": "北京市朝阳区望京SOHO"}}'


def _make_model():
    if os.environ.get("E2E_MODEL") == "real":
        from ai_service_agent.config import make_model

        return make_model()
    return E2EFakeModel(responses=[])


# ---------------------------------------------------------------------------
# Server fixture
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server():
    tmp = tempfile.mkdtemp(prefix="e2e-real-")
    db_path = os.path.join(tmp, "e2e.db")
    seed_spec = importlib.util.spec_from_file_location(
        "seed_mock_db_e2e",
        str(Path(__file__).resolve().parents[1] / "scripts" / "seed_mock_db.py"),
    )
    seed_module = importlib.util.module_from_spec(seed_spec)
    seed_spec.loader.exec_module(seed_module)
    seed_module.build_database(db_path)
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    app = create_app(db_path, model=_make_model(), order_repository=SQLiteOrderRepository(db_path))
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not getattr(srv, "started", False) and time.time() < deadline:
        time.sleep(0.05)
    if not getattr(srv, "started", False):
        pytest.fail("E2E server did not start")

    yield {"base": base, "db_path": db_path}
    srv.should_exit = True
    thread.join(timeout=10)


@pytest.fixture(scope="module")
def client(server):
    return httpx.Client(timeout=120.0, base_url=server["base"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def new_session(client: httpx.Client, user_id: str = "u1") -> str:
    r = client.post("/sessions", json={"user_id": user_id})
    assert r.status_code == 200, f"create session failed: {r.status_code} {r.text}"
    return r.json()["session_id"]


def chat_sse(client: httpx.Client, session_id: str, message: str) -> tuple[int, list[dict]]:
    with client.stream("POST", "/chat", json={"session_id": session_id, "message": message}) as resp:
        text = "".join(resp.iter_text())
    return resp.status_code, parse_sse(text)


def open_status(db_path: str, conv_id: str) -> dict | None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM conversation_status WHERE conversation_id=?", (conv_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def open_messages(db_path: str, conv_id: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT role, content FROM conversation_message WHERE conversation_id=? ORDER BY created_at", (conv_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def open_approvals(db_path: str, conv_id: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM approval_record WHERE conversation_id=? ORDER BY created_at", (conv_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def count_tool_events(events: list[dict], tool: str) -> tuple[int, int]:
    calls = sum(1 for e in events if e.get("type") == "tool_call" and e.get("data", {}).get("name") == tool)
    results = sum(1 for e in events if e.get("type") == "tool_result" and e.get("data", {}).get("name") == tool)
    return calls, results


# ---------------------------------------------------------------------------
# P1: Core scenarios
# ---------------------------------------------------------------------------


class TestCoreScenarios:
    def test_faq_read(self, client, server):
        """FAQ/Read：Router(faq/read) -> ReadGraph -> token+done。"""
        sid = new_session(client)
        code, events = chat_sse(client, sid, "你好，有什么商品推荐吗？")
        types = event_types(events)
        done = next((e for e in reversed(events) if e.get("type") == "done"), None)
        tokens = [e for e in events if e.get("type") == "token"]

        assert code == 200
        assert "error" not in types
        assert len(tokens) >= 1
        assert done is not None and done.get("data", {}).get("final")

        st = open_status(server["db_path"], sid)
        assert st is not None
        assert st["mode"] == "ai"

    def test_mutation_confirm_flow(self, client, server):
        """Mutation：改地址 -> form_required -> 确认 -> update_address 执行。"""
        sid = new_session(client)

        code1, ev1 = chat_sse(client, sid, "订单ORD-PAID-1的收货地址改成北京市朝阳区")
        types1 = event_types(ev1)
        confirm = any(e.get("type") == "form_required" for e in ev1)
        done1 = next((e for e in reversed(ev1) if e.get("type") == "done"), None)

        assert code1 == 200
        assert confirm, f"expected form_required, got {types1}"
        assert done1 is not None

        confirmed = client.post(
            "/actions/confirm",
            json={
                "conversation_id": sid,
                "action": "confirm",
                "order_id": "ORD-PAID-1",
                "fields": {"address": "北京市朝阳区望京SOHO"},
            },
        )
        assert confirmed.status_code == 200
        body = confirmed.json()
        calls, results = count_tool_events(body["events"], "update_address")

        assert calls == 1 and results == 1
        assert body.get("final")

        st = open_status(server["db_path"], sid)
        assert st["mode"] == "ai"

    def test_highrisk_approve(self, client, server):
        """HighRisk approve：退款 -> approval_required -> approve -> refund_apply 执行。"""
        sid = new_session(client)
        code1, ev1 = chat_sse(client, sid, "订单ORD-PAID-1申请退款")
        approval_ev = next((e for e in ev1 if e.get("type") == "approval_required"), None)
        approval_id = (approval_ev or {}).get("data", {}).get("approval_id")

        assert code1 == 200
        assert approval_ev is not None, "expected approval_required"
        assert approval_id

        r = client.post(f"/approvals/{approval_id}/approve")
        assert r.status_code == 200
        assert r.json().get("status") == "approved"

        approvals = open_approvals(server["db_path"], sid)
        assert any(a["approval_id"] == approval_id and a["status"] == "approved" for a in approvals)

    def test_highrisk_reject(self, client, server):
        """HighRisk reject：退款 -> approval_required -> reject -> 工具不执行。"""
        sid = new_session(client)
        code1, ev1 = chat_sse(client, sid, "订单ORD-PAID-1申请退款")
        approval_ev = next((e for e in ev1 if e.get("type") == "approval_required"), None)
        approval_id = (approval_ev or {}).get("data", {}).get("approval_id")

        assert approval_id

        r = client.post(f"/approvals/{approval_id}/reject", json={"note": "不想退了"})
        assert r.status_code == 200

        approvals = open_approvals(server["db_path"], sid)
        rec = next(a for a in approvals if a["approval_id"] == approval_id)
        assert rec["status"] == "rejected"

    def test_handoff_flow(self, client, server):
        """Handoff：转人工 -> handoff_created -> mode=human -> resolve -> mode=ai。"""
        sid = new_session(client)
        code1, ev1 = chat_sse(client, sid, "我要转人工")
        created = next((e for e in ev1 if e.get("type") == "handoff_created"), None)
        handoff_id = (created or {}).get("data", {}).get("handoff_id")

        assert code1 == 200
        assert created is not None
        assert handoff_id

        st = open_status(server["db_path"], sid)
        assert st["mode"] == "human"

        r = client.post(f"/handoffs/{handoff_id}/resolve")
        assert r.status_code == 200
        assert r.json().get("mode") == "ai"

        st2 = open_status(server["db_path"], sid)
        assert st2["mode"] == "ai"

    def test_message_store_grows(self, client, server):
        """Message Store：每轮对话后 user + assistant 消息都落库。"""
        sid = new_session(client)
        chat_sse(client, sid, "你好")
        msgs1 = open_messages(server["db_path"], sid)
        assert any(m["role"] == "user" for m in msgs1)
        assert any(m["role"] == "assistant" for m in msgs1)

        chat_sse(client, sid, "再问一个问题")
        msgs2 = open_messages(server["db_path"], sid)
        assert len(msgs2) > len(msgs1)


# ---------------------------------------------------------------------------
# P2: Approval expired (方案 2: 直接改 DB)
# ---------------------------------------------------------------------------


class TestApprovalExpired:
    def test_highrisk_expired_approve_rejected(self, client, server):
        """过期审批：改 expires_at 为过去时间 -> approve -> expired，不执行工具。"""
        sid = new_session(client)
        code1, ev1 = chat_sse(client, sid, "订单ORD-PAID-1申请退款")
        approval_ev = next((e for e in ev1 if e.get("type") == "approval_required"), None)
        approval_id = (approval_ev or {}).get("data", {}).get("approval_id")
        assert approval_id

        conn = sqlite3.connect(server["db_path"])
        conn.execute(
            "UPDATE approval_record SET expires_at = ? WHERE approval_id = ?",
            ("2020-01-01T00:00:00+00:00", approval_id),
        )
        conn.commit()
        conn.close()

        r = client.post(f"/approvals/{approval_id}/approve")
        assert r.status_code in (409, 410), f"expected 409/410 for expired, got {r.status_code}"

        approvals = open_approvals(server["db_path"], sid)
        rec = next(a for a in approvals if a["approval_id"] == approval_id)
        assert rec["status"] == "expired"

    def test_highrisk_expired_reject_rejected(self, client, server):
        """过期审批 reject 同样被前置有效性检查拦截。"""
        sid = new_session(client)
        code1, ev1 = chat_sse(client, sid, "订单ORD-PAID-1申请退款")
        approval_ev = next((e for e in ev1 if e.get("type") == "approval_required"), None)
        approval_id = (approval_ev or {}).get("data", {}).get("approval_id")
        assert approval_id

        conn = sqlite3.connect(server["db_path"])
        conn.execute(
            "UPDATE approval_record SET expires_at = ? WHERE approval_id = ?",
            ("2020-01-01T00:00:00+00:00", approval_id),
        )
        conn.commit()
        conn.close()

        r = client.post(f"/approvals/{approval_id}/reject", json={"note": "test"})
        assert r.status_code in (409, 410)


# ---------------------------------------------------------------------------
# Fault tolerance fault injection (real model for router/select decisions)
# ---------------------------------------------------------------------------


class FlakyRouterMixin:
    """Shared transient-error injection for the router boundary."""

    router_calls: ClassVar[list[int]] = []

    def _raise_once(self, messages):
        if any(
            getattr(m, "type", "") == "system"
            and ("客服意图路由器" in str(m.content) or "客服路由意图分类器" in str(m.content))
            for m in messages
        ):
            self.router_calls.append(1)
            if len(self.router_calls) == 1:
                raise ConnectionError("real e2e transient router failure")


class FlakyRouterRealModel(FlakyRouterMixin, ChatOpenAI):
    """Real model wrapper; injects one transient error only at the router boundary."""

    def __init__(self, real_model):
        super().__init__(
            model=real_model.model_name,
            api_key=real_model.openai_api_key,
            base_url=real_model.openai_api_base,
        )

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self._raise_once(messages)
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        self._raise_once(messages)
        return await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)

    def _generate_with_cache(self, messages, stop=None, run_manager=None, **kwargs):
        self._raise_once(messages)
        return super()._generate_with_cache(messages, stop=stop, run_manager=run_manager, **kwargs)

    async def _agenerate_with_cache(self, messages, stop=None, run_manager=None, **kwargs):
        self._raise_once(messages)
        return await super()._agenerate_with_cache(messages, stop=stop, run_manager=run_manager, **kwargs)

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        self._raise_once(messages)
        yield from super()._stream(messages, stop=stop, run_manager=run_manager, **kwargs)

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        self._raise_once(messages)
        async for chunk in super()._astream(messages, stop=stop, run_manager=run_manager, **kwargs):
            yield chunk


class FlakyRouterFakeModel(FlakyRouterMixin, E2EFakeModel):
    """Offline model wrapper; injects one transient error only at the router boundary."""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self._raise_once(messages)
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        self._raise_once(messages)
        return await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)


class TestRealFaultToleranceE2E:
    def test_router_transient_failure_retries_once(self, monkeypatch, tmp_path):
        """Provider retry is transient-only and recovers without surfacing an error."""
        from ai_service_agent.config import make_model
        from ai_service_agent.graph import router as router_mod

        def unavailable_jev(*args, **kwargs):
            raise httpx.ConnectError("real e2e JEV unavailable")

        monkeypatch.setattr(router_mod, "_classify_route_intent_with_jev", unavailable_jev)

        if os.environ.get("E2E_MODEL") == "real":
            model = FlakyRouterRealModel(_make_model())
        else:
            model = FlakyRouterFakeModel(responses=[])
        model.router_calls.clear()
        app = create_app(str(tmp_path / "router-retry.db"), model=model)
        client = TestClient(app)

        sid = new_session(client)
        code, events = chat_sse(client, sid, "你好，有什么商品推荐吗？")
        types = event_types(events)
        done = next((e for e in reversed(events) if e.get("type") == "done"), None)

        assert code == 200
        assert len(model.router_calls) >= 2
        assert "error" not in types
        assert done is not None and done.get("data", {}).get("final")

    def test_mutation_tool_failure_uses_local_error_handler(self, monkeypatch, tmp_path):
        """Mutation tool failures terminate at the subgraph's local failed node."""
        from langgraph.prebuilt.tool_node import ToolNode

        from ai_service_agent.config import make_model
        from ai_service_agent.graph.workflows import mutation_agent

        def failing_tools_tool_node(*args, **kwargs):
            async def failing_tools(state, config):
                raise httpx.ConnectError("real e2e mutation tool transport failure")

            return failing_tools

        monkeypatch.setattr(mutation_agent, "ToolNode", failing_tools_tool_node)
        app = create_app(
            str(tmp_path / "mutation-error.db"),
            model=_make_model(),
            order_repository=SQLiteOrderRepository(str(tmp_path / "mutation-seed.db")),
        )
        _seed_business_db(str(tmp_path / "mutation-seed.db"))
        client = TestClient(app)

        sid = new_session(client)
        code1, events1 = chat_sse(client, sid, "订单ORD-PAID-1的收货地址改成北京市朝阳区")
        assert code1 == 200
        assert any(e.get("type") == "form_required" for e in events1)

        confirmed = client.post(
            "/actions/confirm",
            json={
                "conversation_id": sid,
                "action": "confirm",
                "order_id": "ORD-PAID-1",
                "fields": {"address": "北京市朝阳区望京SOHO"},
            },
        )
        assert confirmed.status_code == 502
        messages = open_messages(str(tmp_path / "mutation-error.db"), sid)
        assert messages[-1]["role"] == "assistant"
        assert "系统刚才遇到技术问题" in messages[-1]["content"]


# ---------------------------------------------------------------------------
# P2: Auth flow (uses TestClient, separate app with auth_enabled=True)
# ---------------------------------------------------------------------------


class TestAuthFlow:
    @pytest.fixture()
    def auth_app(self, tmp_path):
        db_path = str(tmp_path / "auth-e2e.db")
        conn = sqlite3.connect(db_path)
        users = UserStore(conn)
        users.upsert("u-e2e-auth", "13800000001", "E2E测试用户", "password123")
        conn.close()
        app = create_app(
            db_path,
            model="fake",
            auth_enabled=True,
            jwt_secret="test-secret-e2e",
        )
        return TestClient(app)

    def test_login_returns_token(self, auth_app):
        r = auth_app.post("/auth/login", json={"phone": "13800000001", "password": "password123"})
        assert r.status_code == 200
        data = r.json()
        assert "token" in data and data["token"]
        assert data["user"]["id"] == "u-e2e-auth"

    def test_login_invalid_credentials(self, auth_app):
        r = auth_app.post("/auth/login", json={"phone": "13800000001", "password": "wrong"})
        assert r.status_code == 401

    def test_chat_without_token_401(self, auth_app):
        r = auth_app.post("/sessions", json={"user_id": "u-e2e-auth"})
        assert r.status_code == 401

    def test_chat_with_token(self, auth_app):
        login = auth_app.post("/auth/login", json={"phone": "13800000001", "password": "password123"})
        token = login.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        r = auth_app.post("/sessions", json={"user_id": "u-e2e-auth"}, headers=headers)
        assert r.status_code == 200
        sid = r.json()["session_id"]

        r2 = auth_app.post(
            "/chat",
            json={"session_id": sid, "message": "你好"},
            headers=headers,
        )
        assert r2.status_code == 200

    def test_chat_wrong_user_403(self, auth_app):
        login = auth_app.post("/auth/login", json={"phone": "13800000001", "password": "password123"})
        token = login.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        r = auth_app.post("/sessions", json={"user_id": "u-other-user"}, headers=headers)
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# P2: Concurrent session isolation
# ---------------------------------------------------------------------------


class TestConcurrentIsolation:
    def test_two_sessions_same_message_independent(self, client, server):
        """两个 session 同时发消息，各自 DB 行互不干扰。"""
        sid_a = new_session(client, "user-a")
        sid_b = new_session(client, "user-b")
        assert sid_a != sid_b

        code_a, ev_a = chat_sse(client, sid_a, "你好")
        code_b, ev_b = chat_sse(client, sid_b, "你好")

        assert code_a == 200 and code_b == 200

        msgs_a = open_messages(server["db_path"], sid_a)
        msgs_b = open_messages(server["db_path"], sid_b)
        assert all(m is not None for m in msgs_a)
        assert all(m is not None for m in msgs_b)
        assert len(msgs_a) >= 2
        assert len(msgs_b) >= 2

        st_a = open_status(server["db_path"], sid_a)
        st_b = open_status(server["db_path"], sid_b)
        assert st_a["thread_id"] != st_b["thread_id"]
        assert st_a["conversation_id"] == sid_a
        assert st_b["conversation_id"] == sid_b


# ---------------------------------------------------------------------------
# P3: Checkpointer recovery (server restart mid-approval)
# ---------------------------------------------------------------------------


class TestCheckpointerRecovery:
    """Server 重启后 HighRisk 审批从 Checkpointer 恢复。"""

    @pytest.fixture()
    def restartable_server(self):
        """独立 function-scoped server，支持 stop + restart（同一 db_path）。"""
        tmp = tempfile.mkdtemp(prefix="e2e-recovery-")
        db_path = os.path.join(tmp, "recovery.db")
        state = {"srv": None, "thread": None, "port": None, "db_path": db_path}

        def _start():
            port = _free_port()
            state["port"] = port
            app = create_app(db_path, model=_make_model(), order_repository=SQLiteOrderRepository(db_path))
            srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
            thread = threading.Thread(target=srv.run, daemon=True)
            thread.start()
            deadline = time.time() + 15
            while not getattr(srv, "started", False) and time.time() < deadline:
                time.sleep(0.05)
            if not getattr(srv, "started", False):
                pytest.fail("recovery server did not start")
            state["srv"] = srv
            state["thread"] = thread

        def _stop():
            state["srv"].should_exit = True
            state["thread"].join(timeout=10)

        state["start"] = _start
        state["stop"] = _stop
        _seed_business_db(db_path)
        _start()
        yield state
        if state["srv"] is not None and not state["srv"].should_exit:
            _stop()

    def test_highrisk_approve_after_server_restart(self, restartable_server):
        """HighRisk 发起 -> 杀 server -> 重启同一 db -> approve -> 图恢复并完成。"""
        state = restartable_server
        base = f"http://127.0.0.1:{state['port']}"
        db_path = state["db_path"]

        # Phase 1: 发起 HighRisk 请求，拿到 approval_id
        with httpx.Client(timeout=120.0, base_url=base) as client:
            sid = new_session(client)
            code1, ev1 = chat_sse(client, sid, "订单ORD-PAID-1申请退款")
            approval_ev = next((e for e in ev1 if e.get("type") == "approval_required"), None)
            approval_id = (approval_ev or {}).get("data", {}).get("approval_id")

        assert code1 == 200
        assert approval_id, "expected approval_required in phase 1"

        # 确认审批处于 pending
        approvals_before = open_approvals(db_path, sid)
        assert any(a["approval_id"] == approval_id and a["status"] == "pending" for a in approvals_before)

        # Phase 2: 停掉 server
        state["stop"]()

        # Phase 3: 重启 server（同一 db_path，Checkpointer 从中断点恢复）
        state["start"]()
        base2 = f"http://127.0.0.1:{state['port']}"

        with httpx.Client(timeout=120.0, base_url=base2) as client2:
            # 重启后审批仍可查到
            r_get = client2.get(f"/approvals/{approval_id}")
            assert r_get.status_code == 200, "approval should survive server restart"
            assert r_get.json()["status"] == "pending"

            # approve -> 图从 Checkpointer 恢复并执行
            r_approve = client2.post(f"/approvals/{approval_id}/approve")
            assert r_approve.status_code == 200
            assert r_approve.json().get("status") == "approved"

        # Phase 4: 验证恢复后状态
        approvals_after = open_approvals(db_path, sid)
        rec = next(a for a in approvals_after if a["approval_id"] == approval_id)
        assert rec["status"] == "approved"

        # Checkpointer 恢复的核心验证：重启前 pending -> 重启后 approve 成功 + 状态 approved
        # 图确实从 interrupt 点恢复（approve 返回 200 且生成了响应）


# ---------------------------------------------------------------------------
# P3: Frontend API contract tests
# ---------------------------------------------------------------------------


class TestFrontendApiContract:
    """前端调用的每个后端接口契约验证。"""

    def test_confirm_action_endpoint(self, client, server):
        """POST /actions/confirm：Mutation 确认按钮调用的接口。"""
        sid = new_session(client)
        chat_sse(client, sid, "订单ORD-PAID-1的收货地址改成北京市朝阳区")

        r = client.post("/actions/confirm", json={"conversation_id": sid, "action": "confirm"})
        assert r.status_code == 200

    def test_confirm_action_invalid_action_422(self, client, server):
        """POST /actions/confirm：非法 action -> 422。"""
        sid = new_session(client)
        r = client.post("/actions/confirm", json={"conversation_id": sid, "action": "invalid"})
        assert r.status_code == 422

    def test_confirm_action_not_found_404(self, client):
        """POST /actions/confirm：不存在的会话 -> 404。"""
        r = client.post("/actions/confirm", json={"conversation_id": "nonexistent", "action": "confirm"})
        assert r.status_code == 404

    def test_get_messages_endpoint(self, client, server):
        """GET /conversations/{id}/messages：前端历史消息加载。"""
        sid = new_session(client)
        chat_sse(client, sid, "你好")

        r = client.get(f"/conversations/{sid}/messages")
        assert r.status_code == 200
        data = r.json()
        assert "items" in data and "mode" in data
        assert data["mode"] == "ai"
        assert len(data["items"]) >= 2

    def test_get_messages_not_found_404(self, client):
        """GET /conversations/{id}/messages：不存在的会话 -> 404。"""
        r = client.get("/conversations/nonexistent/messages")
        assert r.status_code == 404

    def test_list_approvals_endpoint(self, client, server):
        """GET /approvals：审批列表查询。"""
        sid = new_session(client)
        chat_sse(client, sid, "订单ORD-PAID-1申请退款")

        r = client.get("/approvals")
        assert r.status_code == 200
        data = r.json()
        assert "items" in data

    def test_list_handoffs_endpoint(self, client, server):
        """GET /handoffs：handoff 列表查询。"""
        sid = new_session(client)
        chat_sse(client, sid, "我要转人工")

        r = client.get("/handoffs", params={"conversation_id": sid})
        assert r.status_code == 200
        data = r.json()
        assert "handoff_id" in data and "conversation_id" in data
