"""T6：签名决策链接的 HTTP 面（GET 渲染 / POST 决策）。

坐席在 Chatwoot 里点卡片按钮 → 确认页 → 提交。链路要保证：
token 不可伪造、不可跨审批复用、过期有明确交代、重复提交不产生第二次执行。
"""

from __future__ import annotations

import importlib.util
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from ai_service_agent.harness import server as server_module
from ai_service_agent.harness.approval_card import derive_decision_secret, sign_decision_token
from ai_service_agent.harness.server import create_app
from ai_service_agent.harness.chatwoot import ChatwootMappingStore
from ai_service_agent.testing import ApprovalFlowFakeModel

SEAT_SECRET = "test-seat-approval-secret"
_ROOT = Path(__file__).resolve().parents[1]


def _load_seed_module():
    spec = importlib.util.spec_from_file_location(
        "seed_mock_db_decide_link", _ROOT / "scripts" / "seed_mock_db.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def isolated_mock_db(monkeypatch, tmp_path):
    module = _load_seed_module()
    db_path = tmp_path / "mock_ecommerce.db"
    module.build_database(str(db_path))
    from ai_service_agent.graph import builder as _builder
    from ai_service_agent.tools import orders as _orders
    from ai_service_agent.tools import write as _write

    class _S:
        def __init__(self, real):
            self.__dict__.update(real.__dict__)
            self.mock_ecommerce_db_path = str(db_path)
            self.approval_link_secret = None
            self.approval_link_base_url = None

    real_settings = _builder.get_settings()
    real_server_settings = server_module.get_settings()
    monkeypatch.setattr(_write, "get_settings", lambda: _S(real_settings))
    monkeypatch.setattr(_orders, "get_settings", lambda: _S(real_settings))
    monkeypatch.setattr(_builder, "get_settings", lambda: _S(real_settings))
    monkeypatch.setattr(server_module, "get_settings", lambda: _S(real_server_settings))


def _pending(tmp_path) -> tuple[TestClient, str, str, object]:
    """跑一次真实链路触发 HighRisk 审批，返回 (client, approval_id, conversation_id, chatwoot)。"""
    cw = SimpleNamespace(send_bot_message=AsyncMock(), send_interactive_message=AsyncMock())
    db = str(tmp_path / "decide-link.db")
    client = TestClient(
        create_app(
            db,
            model=ApprovalFlowFakeModel(responses=[]),
            chatwoot=cw,
            chatwoot_webhook_secret="cw-webhook-sec",
            seat_approval_secret=SEAT_SECRET,
        )
    )
    session_id = client.post("/sessions", json={"user_id": "u1"}).json()["session_id"]
    conn = sqlite3.connect(db)
    ChatwootMappingStore(conn).put(session_id, 777)
    conn.close()
    with client.stream(
        "POST", "/chat", json={"session_id": session_id, "message": "给 ORD-PAID-1 申请退款"}
    ) as r:
        list(r.iter_bytes())

    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT approval_id, conversation_id FROM approval_record ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    conn.close()
    assert row is not None, "expected a pending approval"
    return client, row[0], row[1], cw


def _token(approval_id: str, conversation_id: str, **kwargs) -> str:
    return sign_decision_token(
        approval_id, conversation_id, "link", secret=derive_decision_secret(SEAT_SECRET), **kwargs
    )


# --- GET 确认页 ---------------------------------------------------------------


def test_decide_page_renders_facts_and_actions(tmp_path):
    client, approval_id, conversation_id, _cw = _pending(tmp_path)

    r = client.get(f"/approvals/{approval_id}/decide", params={"t": _token(approval_id, conversation_id)})

    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    body = r.text
    assert "高风险审批" in body
    assert "退款申请" in body
    assert "ORD-PAID-1" in body
    assert 'value="approve"' in body and 'value="reject"' in body
    assert "拒绝理由" in body


def test_decide_page_rejects_tampered_token(tmp_path):
    client, approval_id, conversation_id, _cw = _pending(tmp_path)
    token = _token(approval_id, conversation_id)
    body, _, signature = token.partition(".")
    tampered = ("A" if body[0] != "A" else "B") + body[1:] + "." + signature

    r = client.get(f"/approvals/{approval_id}/decide", params={"t": tampered})

    assert r.status_code == 400
    assert "链接无效" in r.text


def test_decide_page_reports_expired_token(tmp_path):
    client, approval_id, conversation_id, _cw = _pending(tmp_path)
    stale = _token(
        approval_id,
        conversation_id,
        now=datetime.now(timezone.utc) - timedelta(hours=1),
        ttl_seconds=60,
    )

    r = client.get(f"/approvals/{approval_id}/decide", params={"t": stale})

    assert r.status_code == 410
    assert "链接已过期" in r.text


def test_decide_page_rejects_token_for_other_approval(tmp_path):
    client, approval_id, conversation_id, _cw = _pending(tmp_path)
    other = _token("another-approval", conversation_id)

    r = client.get(f"/approvals/{approval_id}/decide", params={"t": other})

    assert r.status_code == 400


# --- POST 决策 ----------------------------------------------------------------


def test_approve_via_link_executes_and_marks_seat(tmp_path):
    client, approval_id, conversation_id, cw = _pending(tmp_path)
    token = _token(approval_id, conversation_id)

    r = client.post(f"/approvals/{approval_id}/decide", json={"t": token, "action": "approve"})

    assert r.status_code == 200
    assert r.json() == {"approval_id": approval_id, "status": "approved"}
    # 客户收到结果，坐席收到留痕私备注
    private_notes = [
        call.args[1] for call in cw.send_bot_message.await_args_list if call.kwargs.get("private")
    ]
    assert any("已批准" in note for note in private_notes)


def test_reject_via_link_requires_reason(tmp_path):
    client, approval_id, conversation_id, _cw = _pending(tmp_path)
    token = _token(approval_id, conversation_id)

    r = client.post(f"/approvals/{approval_id}/decide", json={"t": token, "action": "reject"})

    assert r.status_code == 422
    assert "理由" in r.json()["detail"]


def test_reject_via_link_carries_reason(tmp_path):
    client, approval_id, conversation_id, cw = _pending(tmp_path)
    token = _token(approval_id, conversation_id)

    r = client.post(
        f"/approvals/{approval_id}/decide",
        json={"t": token, "action": "reject", "note": "商品已发货，需走退货流程"},
    )

    assert r.status_code == 200
    assert r.json()["status"] == "rejected"
    private_notes = [
        call.args[1] for call in cw.send_bot_message.await_args_list if call.kwargs.get("private")
    ]
    assert any("已拒绝" in note and "商品已发货" in note for note in private_notes)


def test_second_post_is_conflict(tmp_path):
    client, approval_id, conversation_id, _cw = _pending(tmp_path)
    token = _token(approval_id, conversation_id)

    first = client.post(f"/approvals/{approval_id}/decide", json={"t": token, "action": "approve"})
    second = client.post(f"/approvals/{approval_id}/decide", json={"t": token, "action": "approve"})

    assert first.status_code == 200
    assert second.status_code == 409
    assert "已被批准" in second.json()["detail"]


def test_decided_page_reports_already_processed(tmp_path):
    client, approval_id, conversation_id, _cw = _pending(tmp_path)
    token = _token(approval_id, conversation_id)
    client.post(f"/approvals/{approval_id}/decide", json={"t": token, "action": "approve"})

    r = client.get(f"/approvals/{approval_id}/decide", params={"t": token})

    assert r.status_code == 409
    assert "已被批准" in r.text


def test_post_rejects_tampered_token(tmp_path):
    client, approval_id, conversation_id, _cw = _pending(tmp_path)

    r = client.post(
        f"/approvals/{approval_id}/decide", json={"t": "forged.token", "action": "approve"}
    )

    assert r.status_code == 400


def test_post_rejects_unknown_action(tmp_path):
    client, approval_id, conversation_id, _cw = _pending(tmp_path)
    token = _token(approval_id, conversation_id)

    r = client.post(f"/approvals/{approval_id}/decide", json={"t": token, "action": "maybe"})

    assert r.status_code == 400


def test_native_form_post_returns_html_result(tmp_path):
    """JS 挂掉时原生 form 提交也要能完成决策，且给坐席看的是页面不是 JSON。"""
    client, approval_id, conversation_id, _cw = _pending(tmp_path)
    token = _token(approval_id, conversation_id)

    r = client.post(
        f"/approvals/{approval_id}/decide",
        data={"t": token, "action": "approve"},
        headers={"accept": "text/html"},
    )

    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "已批准" in r.text


def test_link_disabled_when_no_secret(tmp_path):
    """没配座位口令/链接密钥时 fail closed：链接一律判无效，卡片仍可读。"""
    client = TestClient(
        create_app(
            str(tmp_path / "no-secret.db"),
            model=ApprovalFlowFakeModel(responses=[]),
            chatwoot=SimpleNamespace(send_bot_message=AsyncMock(), send_interactive_message=AsyncMock()),
            chatwoot_webhook_secret="cw-webhook-sec",
            seat_approval_secret="",
        )
    )

    r = client.get("/approvals/whatever/decide", params={"t": "whatever"})

    assert r.status_code == 400
