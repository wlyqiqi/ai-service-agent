from contextlib import closing
import sqlite3
import sys
from pathlib import Path

from ai_service_agent.harness.auth import UserStore

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import seed_auth_users as seed_module
from scripts.seed_auth_users import seed_users


def test_seed_users_syncs_customers_idempotently(tmp_path):
    with closing(sqlite3.connect(tmp_path / "business.db")) as business:
        business.execute(
            "CREATE TABLE customers (customer_id TEXT PRIMARY KEY, name TEXT NOT NULL, phone TEXT NOT NULL)"
        )
        business.execute("INSERT INTO customers VALUES ('u1', '张伟', '+65-81234567')")
        business.commit()

        with closing(sqlite3.connect(tmp_path / "runtime.db")) as runtime:
            first_count = seed_users(runtime, business, "123456")
            runtime.commit()
            second_count = seed_users(runtime, business, "123456")
            runtime.commit()

            assert first_count == second_count == 1
            assert business.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 1
            assert runtime.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
            assert UserStore(runtime).authenticate("+65-81234567", "123456")["user_id"] == "u1"


def test_main_closes_sqlite_connections(tmp_path, monkeypatch):
    opened_connections = []
    real_connect = sqlite3.connect

    class TrackingConnection:
        def __init__(self, path):
            self._connection = real_connect(path)
            self.closed = False

        def execute(self, sql, parameters=()):
            return self._connection.execute(sql, parameters)

        def commit(self):
            self._connection.commit()

        def close(self):
            if not self.closed:
                self._connection.close()
                self.closed = True

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return self._connection.__exit__(exc_type, exc_value, traceback)

    def tracking_connect(path):
        connection = TrackingConnection(path)
        opened_connections.append(connection)
        return connection

    with closing(real_connect(tmp_path / "business.db")) as business:
        business.execute(
            "CREATE TABLE customers (customer_id TEXT PRIMARY KEY, name TEXT NOT NULL, phone TEXT NOT NULL)"
        )
        business.commit()

    monkeypatch.setattr(seed_module.sqlite3, "connect", tracking_connect)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "seed_auth_users.py",
            "--runtime-db",
            str(tmp_path / "runtime.db"),
            "--mock-db",
            str(tmp_path / "business.db"),
        ],
    )
    seed_module.main()

    assert len(opened_connections) == 2
    assert all(connection.closed for connection in opened_connections)
