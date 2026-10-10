"""P0 入口显式转人工：不进 Router/Guard/Graph、事件顺序、幂等、pending interrupt 优先。"""

import sqlite3
import pytest

from fastapi.testclient import TestClient
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.messages import AIMessage

from ai_service_agent.harness.server import create_app
from ai_service_agent.harness import server as server_module
from ai_service_agent.harness.handoff import HANDOFF_NOTICE
from ai_service_agent.harness.session import ConversationStatusStore
from ai_service_agent.testing import ToolCallingFakeModel


@pytest.fixture(autouse=True)
def isolated_business_db(monkeypatch, tmp_path):
    import importlib.util
    from pathlib import Path
    from ai_service_agent.graph import builder as builder_mod

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("seed_mock_db_handoff", root / "scripts" / "seed_mock_db.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    db_path = tmp_path / "mock_ecommerce.db"
    module.build_database(str(db_path))
    class _Settings:
        mock_ecommerce_db_path = str(db_path)
        def __init__(self, real):
            self.__dict__.update(real.__dict__)
            self.mock_ecommerce_db_path = str(db_path)
    settings = _Settings(builder_mod.get_settings())
    settings.mock_ecommerce_db_path = str(db_path)
    monkeypatch.setattr(builder_mod, "get_settings", lambda: settings)
    real_server_settings = server_module.get_settings()
    monkeypatch.setattr(server_module, "get_settings", lambda: _Settings(real_server_settings))


def _parse_sse(text: str) -> list[dict]:
    import json
    events = []
    for block in text.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


class ExplodingModel(ToolCallingFakeModel):
    """若显式转人工仍进入图内，则 _generate 被调用并抛错（证明不进 Router/Guard/Graph）。"""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise AssertionError("graph must not run for explicit handoff")


class ApprovalFlowFakeModel(ToolCallingFakeModel):
    """Router -> order/high_risk；select -> refund_apply；用于构造 approval_required 挂起中断。"""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        system = next((m.content for m in messages if getattr(m, "type", "") == "system"), "")
        if "选择器" in system:
            return ChatResult(generations=[ChatGeneration(
                message=AIMessage(content='{"tool": "refund_apply", "args": {"order_id": "ORD-PAID-1"}}'))])
        if "客服路由意图分类器" in system:
            return ChatResult(generations=[ChatGeneration(
                message=AIMessage(content='{"route_intent": "refund_apply_execute", "confidence": 0.9}'))])
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="已受理"))])


def test_explicit_handoff_skips_graph(tmp_path):
    """D1：显式转人工不进 Router/Guard/Graph（ExplodingModel 若被调用即失败）。"""
    client = TestClient(create_app(str(tmp_path / "s.db"), model=ExplodingModel(responses=[])))
    r = client.post("/sessions", json={"user_id": "u1"})
    sid = r.json()["session_id"]
    with client.stream("POST", "/chat", json={"session_id": sid, "message": "我要转人工"}) as resp:
        events = _parse_sse("".join(resp.iter_text()))
    types = [e["type"] for e in events]
    assert types[0] == "token"
    assert "handoff_created" in types
    assert types[-1] == "done"
    assert ConversationStatusStore(sqlite3.connect(str(tmp_path / "s.db"))).get_mode(sid) == "human"


def test_explicit_handoff_writes_user_and_notice_to_store(tmp_path):
    """D5：Message Store 顺序 = [user, HANDOFF_NOTICE]（唯一真相源）。"""
    from ai_service_agent.harness.messages import MessageStore
    client = TestClient(create_app(str(tmp_path / "s.db"), model="fake"))
    r = client.post("/sessions", json={"user_id": "u1"})
    sid = r.json()["session_id"]
    with client.stream("POST", "/chat", json={"session_id": sid, "message": "我要转人工"}) as resp:
        "".join(resp.iter_text())
    rows = MessageStore(sqlite3.connect(str(tmp_path / "s.db"))).list(sid)
    assert [m["content"] for m in rows] == ["我要转人工", HANDOFF_NOTICE]


def test_non_handoff_message_still_uses_ai_lane(tmp_path):
    """D1：detect False -> 保持 AI Lane 完整流程（有 token + done）。"""
    client = TestClient(create_app(str(tmp_path / "s.db"), model="fake"))
    r = client.post("/sessions", json={"user_id": "u1"})
    sid = r.json()["session_id"]
    with client.stream("POST", "/chat", json={"session_id": sid, "message": "查一下订单"}) as resp:
        types = [e["type"] for e in _parse_sse("".join(resp.iter_text()))]
    assert "token" in types and "done" in types
    assert ConversationStatusStore(sqlite3.connect(str(tmp_path / "s.db"))).get_mode(sid) == "ai"


def test_consecutive_explicit_handoffs_single_active(tmp_path):
    """D3 duplicate protection：连续两次显式转人工只产生 1 个 active handoff。"""
    client = TestClient(create_app(str(tmp_path / "s.db"), model="fake"))
    r = client.post("/sessions", json={"user_id": "u1"})
    sid = r.json()["session_id"]
    for msg in ("我要转人工", "再转一次人工"):
        with client.stream("POST", "/chat", json={"session_id": sid, "message": msg}) as resp:
            "".join(resp.iter_text())
    conn = sqlite3.connect(str(tmp_path / "s.db"))
    n = conn.execute("SELECT COUNT(*) FROM handoff_record WHERE conversation_id=? AND status='active'", (sid,)).fetchone()[0]
    assert n == 1


def test_explicit_handoff_priority_over_pending_interrupt(tmp_path):
    """D6（C4）：approval 挂起时显式转人工优先——直接转人工，approval 保持 pending。"""
    client = TestClient(create_app(str(tmp_path / "s.db"), model=ApprovalFlowFakeModel(responses=[])))
    r = client.post("/sessions", json={"user_id": "u1"})
    sid = r.json()["session_id"]
    # 触发 approval_required 挂起
    with client.stream("POST", "/chat", json={"session_id": sid, "message": "订单ORD-PAID-1申请退款"}) as resp:
        types = [e["type"] for e in _parse_sse("".join(resp.iter_text()))]
    assert "approval_required" in types
    # 显式转人工 -> 不进 resume，直接 human
    with client.stream("POST", "/chat", json={"session_id": sid, "message": "我要转人工"}) as resp:
        types2 = [e["type"] for e in _parse_sse("".join(resp.iter_text()))]
    assert "handoff_created" in types2
    assert ConversationStatusStore(sqlite3.connect(str(tmp_path / "s.db"))).get_mode(sid) == "human"
    pending = client.get("/approvals?status=pending").json()["items"]
    assert any(a["conversation_id"] == sid and a["status"] == "pending" for a in pending)
