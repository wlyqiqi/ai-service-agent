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


def _sig(payload: bytes, secret: str = "sec", ts: str = "1700000000") -> str:
    # Chatwoot v4.x：HMAC(secret, "<ts>.<raw body>")
    return "sha256=" + hmac.new(secret.encode(), ts.encode() + b"." + payload, hashlib.sha256).hexdigest()


def test_signature_valid():
    payload = b'{"event":"message_created"}'
    assert verify_chatwoot_signature(payload, _sig(payload, "sec"), "sec", "1700000000") is True


def test_signature_tampered_payload():
    payload = b'{"event":"message_created"}'
    assert verify_chatwoot_signature(b'{"event":"evil"}', _sig(payload, "sec"), "sec", "1700000000") is False


def test_signature_missing_or_bad_format():
    assert verify_chatwoot_signature(b"{}", None, "sec", "1700000000") is False
    assert verify_chatwoot_signature(b"{}", "deadbeef", "sec", "1700000000") is False
    assert verify_chatwoot_signature(b"{}", _sig(b"{}", "sec"), "sec", None) is False  # v4 缺时间戳


import pytest
from httpx import AsyncClient, MockTransport, Response

from ai_service_agent.harness.chatwoot import ChatwootClient, ChatwootMappingStore


def _client_with(handler, tmp_path) -> tuple[ChatwootClient, ChatwootMappingStore, list]:
    calls: list[dict] = []

    async def handler_wrapper(request):
        calls.append({
            "method": request.method,
            "url": str(request.url),
            "headers": dict(request.headers),
            "json": request.content,
        })
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
            return Response(200, json={"payload": {"contact": {"id": 7}}})
        if request.url.path.endswith("/conversations"):
            return Response(200, json={"id": 555, "messages": [{"id": 777, "content": "首条"}]})
        return Response(404)

    cw, mapping, calls = _client_with(handler, tmp_path)
    cw_id = await cw.ensure_conversation("c1", "用户已转人工")
    assert cw_id == 555
    assert mapping.get("c1")["chatwoot_conversation_id"] == 555
    assert mapping.is_runtime_message(777) is True  # 首条消息 id 记录（webhook 回显去重用）
    # 第二次 ensure 不再发 HTTP 请求
    cw_id2 = await cw.ensure_conversation("c1", "again")
    assert cw_id2 == 555
    assert len(calls) == 2  # contact + conversation，无第三次


@pytest.mark.asyncio
async def test_ensure_conversation_auth_header(tmp_path):
    def handler(request):
        assert request.headers["api_access_token"] == "tok"
        if request.url.path.endswith("/contacts"):
            return Response(200, json={"payload": {"contact": {"id": 7}}})
        return Response(200, json={"id": 555})

    cw, _, _ = _client_with(handler, tmp_path)
    await cw.ensure_conversation("c1", "s")


@pytest.mark.asyncio
async def test_send_as_contact_posts_content_and_skips_without_mapping(tmp_path):
    import json

    def handler(request):
        assert request.headers["api_access_token"] == "tok"
        body = json.loads(request.content)
        assert body["content"] == "在吗"
        assert body["sender_type"] == "Contact"
        assert body["message_type"] == "incoming"
        return Response(200, json={"id": 1, "content": "在吗"})

    cw, mapping, calls = _client_with(handler, tmp_path)
    # 无映射 -> 直接返回，不发请求
    await cw.send_as_contact("c-nope", "在吗")
    assert calls == []
    # 有映射 -> 正常推送（body 契约：content 顶层）
    mapping.put("c1", 555)
    await cw.send_as_contact("c1", "在吗")
    assert len(calls) == 1
    assert mapping.is_runtime_message(1) is True  # 推送消息 id 记录（webhook 回显去重用）


@pytest.mark.asyncio
async def test_send_bot_message_posts_outgoing_content(tmp_path):
    def handler(request):
        assert request.headers["api_access_token"] == "tok"
        body = json.loads(request.content)
        assert body["content"] == "Hello from AI"
        assert body["message_type"] == "outgoing"
        return Response(200, json={"id": 100})

    cw, mapping, calls = _client_with(handler, tmp_path)
    await cw.send_bot_message("c-nope", "Hello from AI")
    assert calls == []
    mapping.put("c1", 555)
    await cw.send_bot_message("c1", "Hello from AI")
    assert len(calls) == 1
    assert mapping.is_runtime_message(100) is True


@pytest.mark.asyncio
async def test_send_interactive_message_posts_form_contract(tmp_path):
    attrs = {"items": [{"name": "addr", "type": "text", "label": "Address"}]}

    def handler(request):
        body = json.loads(request.content)
        assert body["content"] == "Fill address"
        assert body["content_type"] == "form"
        assert body["content_attributes"] == attrs
        assert body["message_type"] == "outgoing"
        return Response(200, json={"id": 101})

    cw, mapping, _ = _client_with(handler, tmp_path)
    mapping.put("c1", 555)
    await cw.send_interactive_message("c1", "Fill address", "form", attrs)
    assert mapping.is_runtime_message(101) is True


def test_agent_message_contract():
    from ai_service_agent.harness.chatwoot import is_agent_message

    assert is_agent_message({
        "id": 502,
        "content": "坐席回复",
        "message_type": "outgoing",
        "private": False,
        "sender": {"type": "user"},
    })
    assert is_agent_message({
        "id": 503,
        "content": "坐席回复",
        "message_type": 1,
        "private": False,
        "sender": {"type": "user"},
    })
    assert not is_agent_message({
        "id": 504,
        "content": "私密备注",
        "message_type": 1,
        "private": True,
        "sender": {"type": "user"},
    })
    assert not is_agent_message({
        "id": 505,
        "content": "系统事件",
        "message_type": 2,
        "private": False,
        "sender": {"type": "user"},
    })
    assert not is_agent_message({
        "id": 506,
        "content": "bot 回复",
        "message_type": 1,
        "private": False,
        "sender": {"type": "agent_bot"},
    })
    assert not is_agent_message({
        "id": 507,
        "content": None,
        "message_type": 1,
        "private": False,
        "sender": {"type": "user"},
    })


def test_chatwoot_message_id_format():
    from ai_service_agent.harness.chatwoot import chatwoot_message_id

    assert chatwoot_message_id({"id": 502}) == "chatwoot-502"


def test_agent_message_rejects_template_empty_and_non_string_content():
    from ai_service_agent.harness.chatwoot import is_agent_message

    assert not is_agent_message({
        "id": 508,
        "content": "模板消息",
        "message_type": 3,
        "private": False,
        "sender": {"type": "user"},
    })
    assert not is_agent_message({
        "id": 509,
        "content": "",
        "message_type": 1,
        "private": False,
        "sender": {"type": "user"},
    })
    assert not is_agent_message({
        "id": 510,
        "content": 123,
        "message_type": 1,
        "private": False,
        "sender": {"type": "user"},
    })


@pytest.mark.asyncio
async def test_list_messages_sends_api_access_token(tmp_path):
    cw, mapping, calls = _client_with(
        lambda request: Response(200, json={"payload": []}),
        tmp_path,
    )
    mapping.put("c1", 555)

    messages = await cw.list_messages("c1")

    assert calls[0]["headers"]["api_access_token"] == "tok"
    assert messages == []


@pytest.mark.asyncio
async def test_list_messages_without_mapping_skips_http(tmp_path):
    cw, _, calls = _client_with(
        lambda request: Response(200, json={"payload": []}),
        tmp_path,
    )

    messages = await cw.list_messages("c1")

    assert messages == []
    assert calls == []


@pytest.mark.asyncio
async def test_list_messages_returns_application_api_payload(tmp_path):
    cw, mapping, calls = _client_with(
        lambda request: Response(200, json={"payload": [
            {"id": 502, "content": "坐席回复", "message_type": 1,
             "private": False, "sender": {"type": "user"}}
        ]}),
        tmp_path,
    )
    mapping.put("c1", 555)

    messages = await cw.list_messages("c1")

    assert [m["id"] for m in messages] == [502]
    assert len(calls) == 1
    assert calls[0]["method"] == "GET"
    assert calls[0]["url"].endswith("/accounts/3/conversations/555/messages")



def test_mapping_runtime_message(tmp_path):
    conn = sqlite3.connect(tmp_path / "m.db")
    m = ChatwootMappingStore(conn)
    assert m.is_runtime_message(88) is False
    m.put_runtime_message(88, "c1")
    assert m.is_runtime_message(88) is True
    assert m.is_runtime_message(99) is False
import json
