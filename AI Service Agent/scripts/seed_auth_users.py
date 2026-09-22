import argparse
from contextlib import closing
import sqlite3

from ai_service_agent.harness.auth import UserStore


def seed_users(runtime_conn: sqlite3.Connection, business_conn: sqlite3.Connection, password: str) -> int:
    users = UserStore(runtime_conn)
    rows = business_conn.execute(
        "SELECT customer_id, name, phone FROM customers ORDER BY customer_id"
    ).fetchall()
    for user_id, display_name, phone in rows:
        users.upsert(user_id, phone, display_name, password)
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-db", default="data/ai_service.db")
    parser.add_argument("--mock-db", default="data/mock_ecommerce.db")
    parser.add_argument("--password", default="123456")
    args = parser.parse_args()

    with (
        closing(sqlite3.connect(args.runtime_db)) as runtime,
        closing(sqlite3.connect(args.mock_db)) as business,
    ):
        with runtime, business:
            count = seed_users(runtime, business, args.password)
    print(f"Seeded {count} auth users")


if __name__ == "__main__":
    main()
