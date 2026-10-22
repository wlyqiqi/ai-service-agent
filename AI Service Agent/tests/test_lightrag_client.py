import json
import logging

import httpx

from ai_service_agent.tools.lightrag import LightRagClient, get_lightrag_client, reset_lightrag_client


def _client_with(handler):
    return LightRagClient(base_url="http://test", transport=httpx.MockTransport(handler))


def test_query_context_parses_references_and_context():
    def handler(request):
        body = json.loads(request.content)
        assert body["query"] == "JoyAir A1 的炸篮容量是多少？"
        assert body["mode"] == "naive"
        assert body["only_need_context"] is True
        assert body["include_references"] is True
        assert body["include_chunk_content"] is True
        assert body["top_k"] == 5
        assert body["chunk_top_k"] == 5
        assert body["enable_rerank"] is False
        return httpx.Response(200, json={
            "response": "",
            "references": [
                {"reference_id": "1", "file_path": "JoyAir_A1智能空气炸锅.md",
                 "content": ["document_id: YX-PRODUCT-A1-001 炸篮 4.5L",
                             "document_id: YX-PRODUCT-A1-002 适用机型 A1"]},
            ],
        })

    out = _client_with(handler).query_context("JoyAir A1 的炸篮容量是多少？")
    assert "炸篮 4.5L" in out["context"]
    assert "document_id: YX-PRODUCT-A1-002" in out["context"]
    assert "\n" in out["context"]
    assert out["references"] == [
        {"file_path": "JoyAir_A1智能空气炸锅.md", "reference_id": "1"},
    ]


def test_query_context_malformed_references_dict_degrades():
    def handler(request):
        return httpx.Response(200, json={"response": "", "references": {"foo": "bar"}})

    # references 为 dict（畸形形状）：确定性降级，不抛异常
    out = _client_with(handler).query_context("x")
    assert out["context"] == ""
    assert out["references"] == []
    assert out["metadata"]["retrieved_chunks"] == 0


def test_query_context_skips_non_dict_entries():
    def handler(request):
        return httpx.Response(200, json={"response": "", "references": [
            {"reference_id": "1", "file_path": "a.md", "content": ["ok chunk"]},
            "not-a-dict",
            None,
            {"reference_id": "2", "file_path": "b.md", "content": ""},
        ]})

    out = _client_with(handler).query_context("x")
    assert out["context"] == "ok chunk"
    assert out["references"] == [
        {"file_path": "a.md", "reference_id": "1"},
    ]


def test_query_context_empty_references():
    def handler(request):
        return httpx.Response(200, json={"response": "", "references": []})

    out = _client_with(handler).query_context("x")
    assert out["context"] == ""
    assert out["references"] == []
    assert out["metadata"] == {
        "retrieved_chunks": 0,
        "selected_chunks": 0,
        "retrieved_chars": 0,
        "context_chars": 0,
        "truncated": False,
    }


def test_query_context_unavailable_on_non_json_response():
    def handler(request):
        return httpx.Response(200, text="<html>Bad Gateway</html>")

    assert _client_with(handler).query_context("x") == {"error": "kb_unavailable"}

def test_query_context_unavailable_on_connect_error(caplog):
    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    with caplog.at_level(logging.WARNING, logger="ai_service_agent.tools.lightrag"):
        assert _client_with(handler).query_context("x") == {"error": "kb_unavailable"}

    # The cause must reach the logs: "kb_unavailable" alone is indistinguishable
    # from an empty retrieval and leaves nothing to debug.
    assert "lightrag query failed" in caplog.text
    assert "boom" in caplog.text


def test_query_context_limits_single_long_chunk():
    long_chunk = "a" * 5000

    def handler(request):
        return httpx.Response(200, json={"references": [
            {"reference_id": "1", "file_path": "long.md", "content": long_chunk},
        ]})

    out = _client_with(handler).query_context("x")
    assert out["context"] == long_chunk[:1200]
    assert out["metadata"] == {
        "retrieved_chunks": 1,
        "selected_chunks": 1,
        "retrieved_chars": 5000,
        "context_chars": 1200,
        "truncated": True,
    }


def test_query_context_allows_chunk_truncated_to_equal_total_budget():
    def handler(request):
        return httpx.Response(200, json={"references": [
            {"reference_id": "1", "file_path": "a.md", "content": "a" * 120},
        ]})

    client = LightRagClient(
        base_url="http://test",
        max_chunk_chars=100,
        max_context_chars=100,
        transport=httpx.MockTransport(handler),
    )

    out = client.query_context("x")

    assert out["context"] == "a" * 100
    assert out["metadata"] == {
        "retrieved_chunks": 1,
        "selected_chunks": 1,
        "retrieved_chars": 120,
        "context_chars": 100,
        "truncated": True,
    }


def test_query_context_stops_at_total_budget():
    def handler(request):
        return httpx.Response(200, json={"references": [
            {"reference_id": "1", "file_path": "a.md", "content": "a" * 60},
            {"reference_id": "2", "file_path": "b.md", "content": "b" * 40},
            {"reference_id": "3", "file_path": "c.md", "content": "c" * 30},
        ]})

    client = LightRagClient(
        base_url="http://test",
        max_chunk_chars=50,
        max_context_chars=100,
        transport=httpx.MockTransport(handler),
    )
    out = client.query_context("x")
    assert out["context"] == ("a" * 50) + "\n\n" + ("b" * 40)
    assert out["metadata"] == {
        "retrieved_chunks": 3,
        "selected_chunks": 2,
        "retrieved_chars": 130,
        "context_chars": 92,
        "truncated": True,
    }


def test_query_context_returns_only_selected_references():
    def handler(request):
        return httpx.Response(200, json={"references": [
            {"reference_id": "1", "file_path": "a.md", "content": "a" * 60},
            {"reference_id": "2", "file_path": "b.md", "content": "b" * 40},
            {"reference_id": "3", "file_path": "c.md", "content": "c" * 30},
        ]})

    client = LightRagClient(
        base_url="http://test",
        max_chunk_chars=50,
        max_context_chars=100,
        transport=httpx.MockTransport(handler),
    )
    out = client.query_context("x")
    assert out["context"] == ("a" * 50) + "\n\n" + ("b" * 40)
    assert out["references"] == [
        {"file_path": "a.md", "reference_id": "1"},
        {"file_path": "b.md", "reference_id": "2"},
    ]


def test_query_context_within_budget_keeps_sources():
    def handler(request):
        return httpx.Response(200, json={"references": [
            {"reference_id": "1", "file_path": "a.md", "content": "a" * 20},
            {"reference_id": "2", "file_path": "b.md", "content": "b" * 30},
        ]})

    out = _client_with(handler).query_context("x")
    assert out["context"] == ("a" * 20) + "\n\n" + ("b" * 30)
    assert [ref["file_path"] for ref in out["references"]] == ["a.md", "b.md"]
    assert out["metadata"]["selected_chunks"] == 2
    assert out["metadata"]["truncated"] is False


def test_get_lightrag_client_is_singleton():
    reset_lightrag_client()
    a = get_lightrag_client()
    b = get_lightrag_client()
    assert a is b
    reset_lightrag_client()


def test_lightrag_client_disables_env_proxy():
    """本机 LightRAG 与 Chatwoot 同约定：不走 HTTP_PROXY。"""
    client = LightRagClient(base_url="http://localhost:9621")
    assert client._client.trust_env is False
