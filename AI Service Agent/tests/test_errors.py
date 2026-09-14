from ai_service_agent.harness.errors import normalize_error


def test_normalize_error_shape():
    e = ValueError("bad")
    out = normalize_error(e)
    assert out["type"] == "error"
    assert out["data"]["code"] == "ValueError"
    assert out["data"]["message"] == "bad"
