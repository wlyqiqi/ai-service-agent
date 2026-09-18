import json

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
    assert _client_with(handler).query_context("x") == {"context": "", "references": []}


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
        {"file_path": "b.md", "reference_id": "2"},
    ]


def test_query_context_empty_references():
    def handler(request):
        return httpx.Response(200, json={"response": "", "references": []})

    assert _client_with(handler).query_context("x") == {"context": "", "references": []}


def test_query_context_unavailable_on_non_json_response():
    def handler(request):
        return httpx.Response(200, text="<html>Bad Gateway</html>")

    assert _client_with(handler).query_context("x") == {"error": "kb_unavailable"}

def test_query_context_unavailable_on_connect_error():
    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    assert _client_with(handler).query_context("x") == {"error": "kb_unavailable"}


def test_get_lightrag_client_is_singleton():
    reset_lightrag_client()
    a = get_lightrag_client()
    b = get_lightrag_client()
    assert a is b
    reset_lightrag_client()
