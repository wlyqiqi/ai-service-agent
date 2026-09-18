import pytest

from ai_service_agent.tools import kb as kb_module
from ai_service_agent.tools.kb import kb_search


class FakeClient:
    def __init__(self, result):
        self._result = result

    def query_context(self, query):
        return self._result


def _patch(monkeypatch, result):
    monkeypatch.setattr(kb_module._lightrag, "get_lightrag_client", lambda: FakeClient(result))


def test_kb_search_hit_with_sources(monkeypatch):
    _patch(monkeypatch, {
        "context": "炸篮 4.5L",
        "references": [{"file_path": "JoyAir_A1智能空气炸锅.md", "reference_id": "1"}],
    })
    out = kb_search.invoke("JoyAir A1 的炸篮容量是多少？")
    assert "炸篮 4.5L" in out
    assert "JoyAir_A1智能空气炸锅.md" in out


def test_kb_search_no_result(monkeypatch):
    _patch(monkeypatch, {"context": "", "references": []})
    out = kb_search.invoke("不存在的知识")
    assert "未查询到" in out


def test_kb_search_unavailable(monkeypatch):
    _patch(monkeypatch, {"error": "kb_unavailable"})
    out = kb_search.invoke("任何问题")
    assert "暂不可用" in out
    assert "人工客服" in out
