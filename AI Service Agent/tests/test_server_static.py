"""Transport 同源托管用户页面测试（user-page-spec §10）：Runtime 在 "/" 提供 web/index.html，
API 路由注册在前、优先级不受挂载影响。"""

from fastapi.testclient import TestClient

from ai_service_agent.harness.server import create_app


def test_static_serves_index_at_root(tmp_path):
    client = TestClient(create_app(str(tmp_path / "s.db"), model="fake"))
    r = client.get("/")
    assert r.status_code == 200
    assert "澄元" in r.text  # 页面品牌标识存在 => 渲染的是 web/index.html


def test_api_routes_take_precedence_over_static_mount(tmp_path):
    client = TestClient(create_app(str(tmp_path / "s.db"), model="fake"))
    r = client.post("/sessions", json={"user_id": "u1"})
    assert r.status_code == 200
    assert r.json()["mode"] == "ai"
    r2 = client.get("/conversations/not-exist/messages")
    assert r2.status_code == 404  # API 404 优先于静态兜底
