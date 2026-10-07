import importlib.util
from pathlib import Path

from fastapi.testclient import TestClient

from ai_service_agent.harness.auth import create_access_token
from ai_service_agent.harness.server import create_app


_ROOT = Path(__file__).resolve().parents[1]


def _load_seed_module():
    spec = importlib.util.spec_from_file_location(
        "seed_mock_db_profile", _ROOT / "scripts" / "seed_mock_db.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _client(tmp_path):
    main_db = tmp_path / "main.db"
    mock_db = tmp_path / "mock.db"
    _load_seed_module().build_database(str(mock_db))
    secret = "test-secret-with-at-least-32-bytes!!"
    app = create_app(
        str(main_db),
        model="fake",
        auth_enabled=True,
        jwt_secret=secret,
        profile_db_path=str(mock_db),
    )
    return TestClient(app), secret


def test_me_profile_returns_caller_profile(tmp_path):
    client, secret = _client(tmp_path)
    token = create_access_token("u0001", secret, expires_minutes=60)
    response = client.get(
        "/me/profile",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "user_id": "u0001",
        "phone": "+84-92722552",
        "address": "No. 1, Chengyuan VN",
    }


def test_me_profile_requires_bearer_token(tmp_path):
    client, _ = _client(tmp_path)
    response = client.get("/me/profile")

    assert response.status_code == 401


def test_me_profile_returns_404_for_unknown_user(tmp_path):
    client, secret = _client(tmp_path)
    token = create_access_token("u9999", secret, expires_minutes=60)
    response = client.get(
        "/me/profile",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 404


def test_me_orders_requires_bearer_token(tmp_path):
    client, _ = _client(tmp_path)
    response = client.get("/me/orders?address_editable=true")

    assert response.status_code == 401


def test_me_orders_filters_address_editable_orders(tmp_path):
    client, secret = _client(tmp_path)
    token = create_access_token("u0010", secret, expires_minutes=60)
    response = client.get(
        "/me/orders?address_editable=true",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "user_id": "u0010",
        "address_editable": True,
        "orders": [
            {
                "order_id": "ORD-2026-10119",
                "status_display": "已付款",
                "ordered_at": "2026-08-21T21:24:29+08:00",
                "amount": 6360.2,
                "currency": "THB",
            }
        ],
        "count": 1,
    }


def test_me_orders_returns_404_for_unknown_user(tmp_path):
    client, secret = _client(tmp_path)
    token = create_access_token("u9999", secret, expires_minutes=60)
    response = client.get(
        "/me/orders?address_editable=true",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 404
