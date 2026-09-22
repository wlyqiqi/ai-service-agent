import sqlite3
import sys
from pathlib import Path

from ai_service_agent.harness.auth import UserStore

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.seed_auth_users import seed_users


def test_seed_users_syncs_customers(tmp_path):
    business = sqlite3.connect(tmp_path / "business.db")
    business.execute(
        "CREATE TABLE customers (customer_id TEXT PRIMARY KEY, name TEXT NOT NULL, phone TEXT NOT NULL)"
    )
    business.execute("INSERT INTO customers VALUES ('u1', '张伟', '+65-81234567')")
    business.commit()

    runtime = sqlite3.connect(tmp_path / "runtime.db")
    count = seed_users(runtime, business, "123456")
    runtime.commit()

    assert count == 1
    assert UserStore(runtime).authenticate("+65-81234567", "123456")["user_id"] == "u1"
