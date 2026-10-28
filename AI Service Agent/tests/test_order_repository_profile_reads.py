"""OrderRepository profile/orders read seam for Transport (/me/* + agent_bot)."""

import importlib.util
from pathlib import Path

import pytest

from ai_service_agent.domain.orders import (
    OrderRepositoryUnavailable,
    SQLiteOrderRepository,
)

_ROOT = Path(__file__).resolve().parents[1]


def _seed_db(tmp_path: Path) -> Path:
    spec = importlib.util.spec_from_file_location(
        "seed_mock_db_repo_profile", _ROOT / "scripts" / "seed_mock_db.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    db_path = tmp_path / "mock.db"
    module.build_database(str(db_path))
    return db_path


def test_get_user_profile_returns_phone_and_address(tmp_path):
    repo = SQLiteOrderRepository(str(_seed_db(tmp_path)))

    profile = repo.get_user_profile("u0001")
    assert profile["user_id"] == "u0001"
    assert profile["phone"] == "+84-92722552"
    assert profile["address"] == "No. 1, Chengyuan VN"
    # 坐席审批卡要显示可读的客户身份，不能只有内部 id
    assert profile["name"]


def test_get_user_profile_returns_none_for_unknown_user(tmp_path):
    repo = SQLiteOrderRepository(str(_seed_db(tmp_path)))

    assert repo.get_user_profile("u9999") is None


def test_get_user_profile_raises_when_db_missing(tmp_path):
    repo = SQLiteOrderRepository(str(tmp_path / "missing.db"))

    with pytest.raises(OrderRepositoryUnavailable):
        repo.get_user_profile("u0001")


def test_list_user_orders_filters_address_editable(tmp_path):
    repo = SQLiteOrderRepository(str(_seed_db(tmp_path)))

    orders = repo.list_user_orders("u0010", address_editable=True)

    assert orders == [
        {
            "order_id": "ORD-2026-10119",
            "status_display": "已付款",
            "ordered_at": "2026-08-21T21:24:29+08:00",
            "amount": 34520.5,
            "currency": "THB",
        }
    ]


def test_list_user_orders_returns_none_for_unknown_user(tmp_path):
    repo = SQLiteOrderRepository(str(_seed_db(tmp_path)))

    assert repo.list_user_orders("u9999", address_editable=True) is None


def test_list_user_orders_raises_when_db_missing(tmp_path):
    repo = SQLiteOrderRepository(str(tmp_path / "missing.db"))

    with pytest.raises(OrderRepositoryUnavailable):
        repo.list_user_orders("u0001")
