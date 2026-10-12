"""为本地 PostgreSQL 测试容器灌入测试数据。

为什么单独一个脚本：`seed_test_data.py` 覆盖的是 Kingbase 与 MySQL，
PostgreSQL 一直漏着，导致它的库是空的、大盘上「数据行数 / 库容量」
这两个指标没有意义。

只依赖 psycopg（项目已有），不需要额外的命令行客户端。

用法：
    cd E:\\Agent\\backend
    ..\\.venv\\Scripts\\python.exe tests\\seed_postgres.py
"""

from __future__ import annotations

import random
import sys
from datetime import datetime, timedelta

# 与 docs/TEST-ENV.md 中记录的一致；可用环境变量覆盖
HOST = "127.0.0.1"
PORT = 5432
USER = "postgres"
PASSWORD = "Test1234"
DBNAME = "testdb"

USERS = 2000
PRODUCTS = 500
ORDERS = 20000

CATEGORIES = ["电子产品", "图书", "服饰", "食品", "家居", "运动", "玩具", "美妆"]
CITIES = ["北京", "上海", "广州", "深圳", "杭州", "成都", "武汉", "西安", "南京", "重庆"]

DDL = [
    """
    CREATE TABLE IF NOT EXISTS shop_users (
        id BIGSERIAL PRIMARY KEY,
        username VARCHAR(64) NOT NULL,
        city VARCHAR(32),
        created_at TIMESTAMP DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS shop_products (
        id BIGSERIAL PRIMARY KEY,
        name VARCHAR(128) NOT NULL,
        category VARCHAR(32),
        price NUMERIC(10,2),
        stock INTEGER DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS shop_orders (
        id BIGSERIAL PRIMARY KEY,
        user_id BIGINT,
        product_id BIGINT,
        quantity INTEGER,
        amount NUMERIC(12,2),
        status VARCHAR(16) DEFAULT 'paid',
        created_at TIMESTAMP DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_orders_created ON shop_orders(created_at)",
]


def main() -> int:
    try:
        import psycopg
    except ImportError:
        print("需要 psycopg：..\\.venv\\Scripts\\python.exe -m pip install 'psycopg[binary]'")
        return 1

    try:
        conn = psycopg.connect(
            host=HOST, port=PORT, user=USER, password=PASSWORD,
            dbname=DBNAME, connect_timeout=10,
        )
    except Exception as exc:
        print(f"连接失败：{type(exc).__name__}: {exc}")
        print("请确认容器在跑： docker ps --filter name=aiops-pg")
        return 1

    cur = conn.cursor()
    for stmt in DDL:
        cur.execute(stmt)

    cur.execute("SELECT count(*) FROM shop_users")
    if cur.fetchone()[0] == 0:
        cur.executemany(
            "INSERT INTO shop_users (username, city, created_at) VALUES (%s, %s, %s)",
            [
                (f"user_{i:05d}", random.choice(CITIES),
                 datetime.now() - timedelta(days=random.randint(0, 365)))
                for i in range(USERS)
            ],
        )

    cur.execute("SELECT count(*) FROM shop_products")
    if cur.fetchone()[0] == 0:
        cur.executemany(
            "INSERT INTO shop_products (name, category, price, stock) "
            "VALUES (%s, %s, %s, %s)",
            [
                (f"商品-{i:04d}", random.choice(CATEGORIES),
                 round(random.uniform(5, 5000), 2), random.randint(0, 1000))
                for i in range(PRODUCTS)
            ],
        )

    cur.execute("SELECT count(*) FROM shop_orders")
    existing = cur.fetchone()[0]
    if existing < ORDERS:
        batch = 2000
        todo = ORDERS - existing
        for start in range(0, todo, batch):
            n = min(batch, todo - start)
            cur.executemany(
                "INSERT INTO shop_orders "
                "(user_id, product_id, quantity, amount, status, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                [
                    (random.randint(1, USERS), random.randint(1, PRODUCTS),
                     random.randint(1, 5), round(random.uniform(10, 20000), 2),
                     random.choice(["paid", "shipped", "done"]),
                     datetime.now() - timedelta(minutes=random.randint(0, 43200)))
                    for _ in range(n)
                ],
            )

    conn.commit()
    cur.execute(
        "SELECT (SELECT count(*) FROM shop_users), "
        "(SELECT count(*) FROM shop_products), "
        "(SELECT count(*) FROM shop_orders)"
    )
    u, p, o = cur.fetchone()
    print(f"PostgreSQL testdb: shop_users={u:,}  shop_products={p:,}  shop_orders={o:,}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
