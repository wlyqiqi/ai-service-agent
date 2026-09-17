import sqlite3

from ai_service_agent.harness.chatwoot import ChatwootMappingStore


def test_mapping_put_get(tmp_path):
    conn = sqlite3.connect(tmp_path / "m.db")
    m = ChatwootMappingStore(conn)
    m.put("c1", 101, contact_id=7)
    rec = m.get("c1")
    assert rec is not None
    assert rec["chatwoot_conversation_id"] == 101
    assert rec["contact_id"] == 7
    assert m.get("nope") is None


def test_mapping_find_reverse(tmp_path):
    conn = sqlite3.connect(tmp_path / "m.db")
    m = ChatwootMappingStore(conn)
    m.put("c1", 101)
    assert m.find(101)["conversation_id"] == "c1"
    assert m.find(999) is None


import hashlib
import hmac

from ai_service_agent.harness.chatwoot import verify_chatwoot_signature


def _sig(payload: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def test_signature_valid():
    payload = b'{"event":"message_created"}'
    assert verify_chatwoot_signature(payload, _sig(payload, "sec"), "sec") is True


def test_signature_tampered_payload():
    payload = b'{"event":"message_created"}'
    assert verify_chatwoot_signature(b'{"event":"evil"}', _sig(payload, "sec"), "sec") is False


def test_signature_missing_or_bad_format():
    assert verify_chatwoot_signature(b"{}", None, "sec") is False
    assert verify_chatwoot_signature(b"{}", "deadbeef", "sec") is False


import pytest
from httpx import AsyncClient, MockTransport, Response

from ai_service_agent.harness.chatwoot import ChatwootClient, ChatwootMappingStore


def _client_with(handler, tmp_path) -> tuple[ChatwootClient, ChatwootMappingStore, list]:
    calls: list[dict] = []

    async def handler_wrapper(request):
        calls.append({"method": request.method, "url": str(request.url), "json": request.content})
        return handler(request)

    conn = sqlite3.connect(tmp_path / "m.db")
    mapping = ChatwootMappingStore(conn)
    http = AsyncClient(transport=MockTransport(handler_wrapper))
    cw = ChatwootClient("http://cw.test", 3, 2, "tok", mapping, client=http)
    return cw, mapping, calls


@pytest.mark.asyncio
async def test_ensure_conversation_creates_contact_then_conversation(tmp_path):
    def handler(request):
        if request.url.path.endswith("/contacts"):
            return Response(200, json={"payload": {"id": 7}})
        if request.url.path.endswith("/conversations"):
            return Response(200, json={"id": 555})
        return Response(404)

    cw, mapping, calls = _client_with(handler, tmp_path)
    cw_id = await cw.ensure_conversation("c1", "用户已转人工")
    assert cw_id == 555
    assert mapping.get("c1")["chatwoot_conversation_id"] == 555
    # 第二次 ensure 不再发 HTTP 请求
    cw_id2 = await cw.ensure_conversation("c1", "again")
    assert cw_id2 == 555
    assert len(calls) == 2  # contact + conversation，无第三次


@pytest.mark.asyncio
async def test_ensure_conversation_auth_header(tmp_path):
    def handler(request):
        assert request.headers["api_access_token"] == "tok"
        if request.url.path.endswith("/contacts"):
            return Response(200, json={"payload": {"id": 7}})
        return Response(200, json={"id": 555})

    cw, _, _ = _client_with(handler, tmp_path)
    await cw.ensure_conversation("c1", "s")
