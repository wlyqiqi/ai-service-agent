"""生成模拟电商数据库 data/mock_ecommerce.db（确定性：固定种子 + 固定时间锚点）。

用途：order_lookup（及后续保修查询等 read 工具）的真实数据源替代品——
工具 ToolMeta.schema 与对外接口不变，仅替换内部实现（与 kb_search stub→真实检索同一模式）。

设计要点：
- 零新依赖：仅 Python 标准库（sqlite3 / random / datetime / argparse）。
- 中文姓名：姓氏池 + 名字池组合（应用户定案，不程序生成东南亚姓名）。
- 时间锚点固定为 2026-09-15 10:00 (UTC+8)，同一命令产物逐行一致。
- 规范化评测夹具订单（ORD-1 / ORD-2 / ORD-10086 / ORD-CANCEL-1，属主 u1/u2）
  一并入库，保证 DB 自洽；运行时 ORDER_STORE 仍优先（相对时间窗口语义不变）。

运行：uv run python scripts/seed_mock_db.py [--out data/mock_ecommerce.db]
"""

from __future__ import annotations

import argparse
import random
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

SEED = 20260915
ANCHOR = datetime(2026, 9, 15, 10, 0, 0, tzinfo=timezone(timedelta(hours=8)))

N_CUSTOMERS = 120
N_PRODUCTS = 40
N_ORDERS = 400
N_DEVICES = 150
N_TICKETS = 30
N_CONVERSATIONS = 40

SURNAMES = [
    "王", "李", "张", "刘", "陈", "杨", "黄", "赵", "周", "吴", "徐", "孙", "马", "朱", "胡",
    "郭", "何", "林", "罗", "高", "郑", "梁", "谢", "宋", "唐", "许", "韩", "冯", "邓", "曹",
]
GIVEN_CHARS = [
    "伟", "芳", "娜", "敏", "静", "丽", "强", "磊", "军", "洋", "勇", "艳", "杰", "娟", "涛",
    "明", "超", "霞", "平", "刚", "辉", "颖", "晨", "宇", "浩", "婷", "雪", "琳", "丹", "悦",
    "思", "远", "泽", "欣", "怡", "然", "子", "涵", "雨", "晴", "嘉", "懿", "俊", "雅", "馨",
]

MARKETS = ["MY", "SG", "TH", "ID", "VN"]
CURRENCY = {"MY": "MYR", "SG": "SGD", "TH": "THB", "ID": "IDR", "VN": "VND"}
RATE = {"MY": 0.66, "SG": 0.185, "TH": 4.9, "ID": 2200.0, "VN": 3500.0}
PHONE_PREFIX = {"MY": "+60-1", "SG": "+65-8", "TH": "+66-8", "ID": "+62-81", "VN": "+84-9"}
CARRIERS = {
    "MY": ["J&T Express", "Ninja Van", "Shopee Xpress"],
    "SG": ["Ninja Van", "J&T Express"],
    "TH": ["Flash Express", "J&T Express"],
    "ID": ["JNE", "J&T Express"],
    "VN": ["GHN", "GHTK"],
}
CHANNELS = ["官网", "Chengyuan Home App", "Shopee", "Lazada", "TikTok Shop"]

PRODUCT_CATALOG = [
    ("厨房电器", "CY-AF", "智能空气炸锅", (399, 899)),
    ("厨房电器", "CY-RC", "智能电饭煲", (299, 799)),
    ("厨房电器", "CY-BL", "智能破壁机", (599, 1299)),
    ("厨房电器", "CY-CM", "智能咖啡机", (899, 1999)),
    ("清洁电器", "CY-VS", "无线吸尘器", (799, 1699)),
    ("清洁电器", "CY-RV", "扫地机器人", (1299, 2999)),
]
FIRMWARES = ["v1.0.0", "v1.1.2", "v1.2.0", "v2.0.1", "v2.1.0", "v2.3.1"]
ORDER_STATUS_WEIGHTS = [
    ("completed", 55), ("delivered", 15), ("shipped", 12), ("paid", 8),
    ("refunding", 4), ("refunded", 3), ("cancelled", 3),
]
TICKET_TYPES = ["维修申请", "退换货", "配件补发", "退款咨询"]
TICKET_STATUSES = ["open", "processing", "resolved", "closed"]

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE customers (
    customer_id   TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    phone         TEXT NOT NULL,
    email         TEXT NOT NULL,
    market        TEXT NOT NULL,
    language      TEXT NOT NULL,
    tier          TEXT NOT NULL,
    registered_at TEXT NOT NULL
);
CREATE TABLE products (
    product_id       TEXT PRIMARY KEY,
    sku              TEXT NOT NULL UNIQUE,
    category         TEXT NOT NULL,
    series           TEXT NOT NULL,
    product_name     TEXT NOT NULL,
    base_price_cny   REAL NOT NULL,
    latest_firmware  TEXT NOT NULL,
    released_at      TEXT NOT NULL,
    warranty_months  INTEGER NOT NULL
);
CREATE TABLE orders (
    order_id     TEXT PRIMARY KEY,
    customer_id  TEXT NOT NULL REFERENCES customers(customer_id),
    status       TEXT NOT NULL,
    ordered_at   TEXT NOT NULL,
    signed_at    TEXT,
    cancelled_at TEXT,
    total_amount REAL NOT NULL,
    currency     TEXT NOT NULL,
    channel      TEXT NOT NULL,
    market       TEXT NOT NULL
);
CREATE INDEX idx_orders_customer ON orders(customer_id);
CREATE TABLE order_items (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id   TEXT NOT NULL REFERENCES orders(order_id),
    product_id TEXT NOT NULL REFERENCES products(product_id),
    quantity   INTEGER NOT NULL,
    unit_price REAL NOT NULL
);
CREATE INDEX idx_items_order ON order_items(order_id);
CREATE TABLE shipments (
    shipment_id   TEXT PRIMARY KEY,
    order_id      TEXT NOT NULL REFERENCES orders(order_id),
    carrier       TEXT NOT NULL,
    tracking_no   TEXT NOT NULL UNIQUE,
    status        TEXT NOT NULL,
    shipped_at    TEXT,
    eta           TEXT
);
CREATE TABLE devices (
    device_id       TEXT PRIMARY KEY,
    customer_id     TEXT NOT NULL REFERENCES customers(customer_id),
    product_id      TEXT NOT NULL REFERENCES products(product_id),
    serial_no       TEXT NOT NULL UNIQUE,
    firmware        TEXT NOT NULL,
    bound_at        TEXT NOT NULL,
    warranty_until  TEXT NOT NULL
);
CREATE INDEX idx_devices_customer ON devices(customer_id);
CREATE TABLE service_tickets (
    ticket_id   TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers(customer_id),
    order_id    TEXT REFERENCES orders(order_id),
    device_id   TEXT REFERENCES devices(device_id),
    issue_type  TEXT NOT NULL,
    status      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    resolved_at TEXT
);
CREATE TABLE conversations (
    conversation_id TEXT PRIMARY KEY,
    customer_id     TEXT NOT NULL REFERENCES customers(customer_id),
    ticket_id       TEXT REFERENCES service_tickets(ticket_id),
    mode            TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
"""


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _rand_dt(rng: random.Random, start: datetime, max_days: int) -> datetime:
    return start + timedelta(
        days=rng.randint(0, max_days),
        hours=rng.randint(0, 23),
        minutes=rng.randint(0, 59),
        seconds=rng.randint(0, 59),
    )


def _weighted(rng: random.Random, pairs: list[tuple[str, int]]) -> str:
    total = sum(w for _, w in pairs)
    roll = rng.randint(1, total)
    for value, weight in pairs:
        roll -= weight
        if roll <= 0:
            return value
    return pairs[-1][0]


def _make_name(rng: random.Random) -> str:
    return rng.choice(SURNAMES) + "".join(rng.choice(GIVEN_CHARS) for _ in range(rng.choice([1, 2])))


def build_database(path: str) -> dict[str, int]:
    """生成数据库并返回各表行数（确定性产物，可重复调用）。"""
    rng = random.Random(SEED)
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    conn = sqlite3.connect(out)
    try:
        conn.executescript(SCHEMA)
        counts = _populate(conn, rng)
        conn.commit()
        return counts
    finally:
        conn.close()


def _populate(conn: sqlite3.Connection, rng: random.Random) -> dict[str, int]:
    # ---- customers：u1/u2 为规范化夹具属主，其余 u0003.. ----
    customers = [
        ("u1", "张伟", "+65-81234567", "zhangwei.demo@example.com", "SG", "zh-CN", "VIP",
         _iso(ANCHOR - timedelta(days=800))),
        ("u2", "李娜", "+60-198765432", "lina.demo@example.com", "MY", "zh-CN", "普通会员",
         _iso(ANCHOR - timedelta(days=650))),
    ]
    used_names = {"张伟", "李娜"}
    for i in range(1, N_CUSTOMERS + 1):
        market = rng.choice(MARKETS)
        name = _make_name(rng)
        while name in used_names:
            name = _make_name(rng)
        used_names.add(name)
        customers.append((
            f"u{i:04d}", name,
            f"{PHONE_PREFIX[market]}{rng.randint(1000000, 9999999)}",
            f"user{i:04d}@example.com", market,
            rng.choice(["zh-CN", "en", "th", "id", "vi"]),
            rng.choice(["普通会员", "普通会员", "普通会员", "高级会员", "VIP"]),
            _iso(_rand_dt(rng, ANCHOR - timedelta(days=730), 720)),
        ))
    conn.executemany("INSERT INTO customers VALUES (?,?,?,?,?,?,?,?)", customers)

    # ---- products ----
    products = []
    fw_index = 0
    for category, series, series_name, (lo, hi) in PRODUCT_CATALOG:
        for n in range(1, 8):  # 6 系列 x 7 = 42 → 截取 40
            if len(products) >= N_PRODUCTS:
                break
            base = rng.choice([lo, (lo + hi) // 2, hi])
            fw = FIRMWARES[fw_index % len(FIRMWARES)]
            fw_index += 1
            pid = f"P{len(products) + 1:03d}"
            products.append((
                pid, f"{series}-{n:02d}", category, series,
                f"澄元{series_name} {rng.choice(['标准版', 'Pro', 'Pro Max', '青春版'])} {n:02d}",
                float(base), fw,
                _iso(_rand_dt(rng, ANCHOR - timedelta(days=900), 700)),
                rng.choice([12, 24]),
            ))
    conn.executemany("INSERT INTO products VALUES (?,?,?,?,?,?,?,?,?)", products)

    # ---- orders（含规范化夹具 4 单）----
    signed_status = {"shipped", "delivered", "completed"}
    orders = [
        ("ORD-1", "u1", "delivered", 2, None, "SGD", 89.85, "官网", "SG"),
        ("ORD-2", "u2", "delivered", 2, None, "MYR", 263.34, "Shopee", "MY"),
        ("ORD-10086", "u1", "delivered", 12, None, "SGD", 147.60, "Lazada", "SG"),
        ("ORD-CANCEL-1", "u1", "cancelled", 20, 3, "SGD", 65.34, "官网", "SG"),
    ]
    for j, (oid, cust, status, ago, cago, cur, amt, channel, market) in enumerate(orders):
        ordered_at = ANCHOR - timedelta(days=ago)
        signed = _iso(ordered_at + timedelta(days=2)) if status in signed_status else None
        cancelled = _iso(ANCHOR - timedelta(days=cago)) if cago else None
        orders[j] = (oid, cust, status, _iso(ordered_at), signed, cancelled, amt, cur, channel, market)
    statuses = []
    for status, weight in ORDER_STATUS_WEIGHTS:
        statuses += [status] * weight
    for k in range(N_ORDERS - 4):
        cust = f"u{rng.randint(3, N_CUSTOMERS):04d}"
        market = next(c[4] for c in customers if c[0] == cust)
        status = rng.choice(statuses)
        ordered_at = _rand_dt(rng, ANCHOR - timedelta(days=180), 179)
        signed = _iso(ordered_at + timedelta(days=rng.randint(1, 4))) if status in signed_status else None
        cancelled = _iso(ordered_at + timedelta(days=1)) if status == "cancelled" else None
        orders.append((
            f"ORD-2026-{10001 + k:05d}", cust, status, _iso(ordered_at), signed, cancelled,
            0.0, CURRENCY[market], rng.choice(CHANNELS), market,
        ))
    conn.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?)", orders)

    # ---- order_items + 金额回填 ----
    items = []
    for order in orders:
        market = order[9]
        n_items = rng.choice([1, 1, 1, 2, 3])
        picks = rng.sample(range(len(products)), n_items)
        total = 0.0
        for pidx in picks:
            pid, _, _, _, _, base, _, _, _ = products[pidx]
            qty = rng.choice([1, 1, 1, 2])
            unit = round(base * RATE[market], 2)
            total += unit * qty
            items.append((order[0], pid, qty, unit))
        conn.execute(
            "UPDATE orders SET total_amount=? WHERE order_id=?", (round(total, 2), order[0])
        )
    conn.executemany("INSERT INTO order_items(order_id,product_id,quantity,unit_price) VALUES (?,?,?,?)", items)

    # ---- shipments ----
    shipments = []
    sn = 0
    for order in orders:
        status = order[2]
        sn += 1
        if status == "paid":
            ship_status, shipped_at, eta = "pending", None, _iso(datetime.fromisoformat(order[3]) + timedelta(days=3))
        elif status == "cancelled":
            sn -= 1
            continue
        else:
            ship_status = "delivered" if status in {"delivered", "completed", "refunded"} else "in_transit"
            shipped_at = order[4] or _iso(datetime.fromisoformat(order[3]) + timedelta(days=1))
            eta = _iso(datetime.fromisoformat(shipped_at) + timedelta(days=rng.randint(2, 5)))
        market = order[9]
        shipments.append((
            f"SHP-{sn:05d}", order[0], rng.choice(CARRIERS[market]),
            f"TRK{rng.randint(10 ** 11, 10 ** 12 - 1)}", ship_status, shipped_at, eta,
        ))
    conn.executemany("INSERT INTO shipments VALUES (?,?,?,?,?,?,?)", shipments)

    # ---- devices（约 1/4 留旧固件触发更新场景）----
    devices = []
    for d in range(1, N_DEVICES + 1):
        cust = f"u{rng.randint(1, N_CUSTOMERS):04d}"
        cust = "u1" if cust == "u2" and d % 2 else cust
        prod = products[rng.randrange(len(products))]
        bound_at = _rand_dt(rng, ANCHOR - timedelta(days=730), 720)
        latest_idx = FIRMWARES.index(prod[6])
        fw = FIRMWARES[rng.randint(max(0, latest_idx - 3), latest_idx)] if d % 4 else FIRMWARES[0]
        devices.append((
            f"DEV-{d:04d}", cust, prod[0], f"CYSN{rng.randint(10 ** 8, 10 ** 9 - 1)}",
            fw, _iso(bound_at),
            _iso(bound_at + timedelta(days=365 * rng.choice([1, 1, 2]))),
        ))
    conn.executemany("INSERT INTO devices VALUES (?,?,?,?,?,?,?)", devices)

    # ---- service_tickets（含保修期内/临期/过期边界案例）----
    tickets = []
    for t in range(1, N_TICKETS + 1):
        cust = f"u{rng.randint(1, N_CUSTOMERS):04d}"
        status = rng.choice(TICKET_STATUSES)
        created = _rand_dt(rng, ANCHOR - timedelta(days=90), 89)
        tickets.append((
            f"TKT-{t:04d}", cust,
            orders[rng.randrange(len(orders))][0] if t % 3 else None,
            devices[rng.randrange(len(devices))][0] if t % 2 else None,
            rng.choice(TICKET_TYPES), status, _iso(created),
            _iso(created + timedelta(days=rng.randint(1, 14))) if status in {"resolved", "closed"} else None,
        ))
    conn.executemany("INSERT INTO service_tickets VALUES (?,?,?,?,?,?,?,?)", tickets)

    # ---- conversations ----
    conversations = []
    for c in range(1, N_CONVERSATIONS + 1):
        conversations.append((
            f"conv-{c:04d}", f"u{rng.randint(1, N_CUSTOMERS):04d}",
            f"TKT-{rng.randint(1, N_TICKETS):04d}" if c % 4 == 0 else None,
            rng.choice(["ai", "ai", "ai", "human"]), _iso(_rand_dt(rng, ANCHOR - timedelta(days=30), 29)),
        ))
    conn.executemany("INSERT INTO conversations VALUES (?,?,?,?,?)", conversations)

    tables = ["customers", "products", "orders", "order_items", "shipments", "devices", "service_tickets", "conversations"]
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}


def main() -> None:
    parser = argparse.ArgumentParser(description="生成澄元模拟电商数据库（确定性）")
    parser.add_argument("--out", default="data/mock_ecommerce.db")
    args = parser.parse_args()
    counts = build_database(args.out)
    print(f"已生成 {args.out}")
    for table, n in counts.items():
        print(f"  {table:<16} {n:>5} 行")


if __name__ == "__main__":
    main()
