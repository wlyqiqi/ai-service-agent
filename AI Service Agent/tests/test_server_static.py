"""Transport 静态托管：API 路由优先于静态挂载；用户页 index 已下线，品牌落地页仍可同源提供。"""

from fastapi.testclient import TestClient

from ai_service_agent.harness.server import create_app


def test_root_has_no_user_page_index(tmp_path):
    client = TestClient(create_app(str(tmp_path / "s.db"), model="fake"))
    r = client.get("/")
    assert r.status_code == 404


def test_landing_page_still_served(tmp_path):
    client = TestClient(create_app(str(tmp_path / "s.db"), model="fake"))
    r = client.get("/landing.html")
    assert r.status_code == 200
    assert "澄元" in r.text


def test_api_routes_take_precedence_over_static_mount(tmp_path):
    client = TestClient(create_app(str(tmp_path / "s.db"), model="fake"))
    r = client.post("/sessions", json={"user_id": "u1"})
    assert r.status_code == 200
    assert r.json()["mode"] == "ai"
    r2 = client.get("/conversations/not-exist/messages")
    assert r2.status_code == 404  # API 404 优先于静态兜底
