"""在真实 Kingbase 与 MySQL 中构造测试数据与持续写入负载。

用途：
  1. 让指标「动起来」——否则趋势预测没有可预测的东西
  2. 为备份容灾提供有实际内容的数据（空库的备份证明不了什么）

用户已明确授权在这两个测试库中伪造数据。**仅用于测试环境**。

做法：
  - 建表：用户 / 商品 / 订单
  - 灌入初始数据
  - 启动一个后台写入协程，持续插入订单（让 transactions、库大小持续增长）

运行：
  python tests/seed_test_data.py          # 建表 + 初始数据 + 持续写入（前台运行）
  python tests/seed_test_data.py --once   # 只建表灌数据，不持续写入
"""

from __future__ import annotations

import asyncio
import random
import string
import sys
from datetime import datetime, timedelta

from testenv import (  # noqa: E402
    KINGBASE_DB,
    KINGBASE_HOST,
    KINGBASE_PORT,
    KINGBASE_USER,
    LOCAL_DB_HOST,
    LOCAL_DB_PASSWORD,
    LOCAL_MYSQL_PORT,
    require_kingbase_password,
)

KINGBASE = dict(
    host=KINGBASE_HOST, port=KINGBASE_PORT, user=KINGBASE_USER,
    password=require_kingbase_password("seed_test_data.py"), dbname=KINGBASE_DB,
)
MYSQL = dict(
    host=LOCAL_DB_HOST, port=LOCAL_MYSQL_PORT, user="root",
    password=LOCAL_DB_PASSWORD, database="testdb",
)

ONCE = "--once" in sys.argv

CATEGORIES = ["电子产品", "图书", "服饰", "食品", "家居", "运动", "玩具", "美妆"]
CITIES = ["北京", "上海", "广州", "深圳", "杭州", "成都", "武汉", "西安", "南京", "重庆"]


# --------------------------------------------------------------------------- Kingbase


def kb_connect():
    import psycopg

    return psycopg.connect(
        host=KINGBASE["host"], port=KINGBASE["port"], user=KINGBASE["user"],
        password=KINGBASE["password"], dbname=KINGBASE["dbname"],
        connect_timeout=10,
    )


def kb_setup(conn) -> None:
    cur = conn.cursor()
    # 金仓支持 CREATE TABLE IF NOT EXISTS
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS shop_users (
            id          BIGSERIAL PRIMARY KEY,
            username    VARCHAR(64) NOT NULL,
            city        VARCHAR(32),
            created_at  TIMESTAMP DEFAULT now()
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS shop_products (
            id          BIGSERIAL PRIMARY KEY,
            name        VARCHAR(128) NOT NULL,
            category    VARCHAR(32),
            price       NUMERIC(10,2),
            stock       INTEGER DEFAULT 0
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS shop_orders (
            id          BIGSERIAL PRIMARY KEY,
            user_id     BIGINT,
            product_id  BIGINT,
            quantity    INTEGER,
            amount      NUMERIC(12,2),
            status      VARCHAR(16) DEFAULT 'paid',
            created_at  TIMESTAMP DEFAULT now()
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_orders_created ON shop_orders(created_at)")
    conn.commit()


def kb_seed(conn, users: int = 2000, products: int = 500, orders: int = 20000) -> tuple[int, int, int]:
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM shop_users")
    if cur.fetchone()[0] == 0:
        rows = [
            (f"user_{i:05d}", random.choice(CITIES),
             datetime.now() - timedelta(days=random.randint(0, 365)))
            for i in range(users)
        ]
        cur.executemany(
            "INSERT INTO shop_users (username, city, created_at) VALUES (%s, %s, %s)", rows
        )
    cur.execute("SELECT count(*) FROM shop_products")
    if cur.fetchone()[0] == 0:
        rows = [
            (f"商品-{i:04d}", random.choice(CATEGORIES),
             round(random.uniform(5, 5000), 2), random.randint(0, 1000))
            for i in range(products)
        ]
        cur.executemany(
            "INSERT INTO shop_products (name, category, price, stock) VALUES (%s, %s, %s, %s)",
            rows,
        )
    cur.execute("SELECT count(*) FROM shop_orders")
    existing = cur.fetchone()[0]
    if existing < orders:
        rows = [
            (random.randint(1, users), random.randint(1, products),
             random.randint(1, 5), round(random.uniform(10, 20000), 2),
             random.choice(["paid", "shipped", "done"]),
             datetime.now() - timedelta(minutes=random.randint(0, 43200)))
            for _ in range(orders - existing)
        ]
        cur.executemany(
            "INSERT INTO shop_orders (user_id, product_id, quantity, amount, status, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            rows,
        )
    conn.commit()
    cur.execute("SELECT (SELECT count(*) FROM shop_users), "
                "(SELECT count(*) FROM shop_products), (SELECT count(*) FROM shop_orders)")
    return cur.fetchone()


def kb_insert_orders(conn, n: int = 50) -> None:
    cur = conn.cursor()
    rows = [
        (random.randint(1, 2000), random.randint(1, 500), random.randint(1, 5),
         round(random.uniform(10, 20000), 2), "paid", datetime.now())
        for _ in range(n)
    ]
    cur.executemany(
        "INSERT INTO shop_orders (user_id, product_id, quantity, amount, status, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        rows,
    )
    conn.commit()


# --------------------------------------------------------------------------- MySQL


def my_connect():
    import pymysql

    return pymysql.connect(
        host=MYSQL["host"], port=MYSQL["port"], user=MYSQL["user"],
        password=MYSQL["password"], database=MYSQL["database"],
        charset="utf8mb4", connect_timeout=10, autocommit=False,
    )


def my_setup(conn) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS shop_users (
            id          BIGINT AUTO_INCREMENT PRIMARY KEY,
            username    VARCHAR(64) NOT NULL,
            city        VARCHAR(32),
            created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
        ) ENGINE=InnoDB
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS shop_products (
            id          BIGINT AUTO_INCREMENT PRIMARY KEY,
            name        VARCHAR(128) NOT NULL,
            category    VARCHAR(32),
            price       DECIMAL(10,2),
            stock       INTEGER DEFAULT 0
        ) ENGINE=InnoDB
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS shop_orders (
            id          BIGINT AUTO_INCREMENT PRIMARY KEY,
            user_id     BIGINT,
            product_id  BIGINT,
            quantity    INTEGER,
            amount      DECIMAL(12,2),
            status      VARCHAR(16) DEFAULT 'paid',
            created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
            KEY idx_orders_created (created_at)
        ) ENGINE=InnoDB
        """
    )
    conn.commit()


def my_seed(conn, users: int = 2000, products: int = 500, orders: int = 20000) -> tuple[int, int, int]:
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM shop_users")
    if cur.fetchone()[0] == 0:
        cur.executemany(
            "INSERT INTO shop_users (username, city, created_at) VALUES (%s, %s, %s)",
            [(f"user_{i:05d}", random.choice(CITIES),
              datetime.now() - timedelta(days=random.randint(0, 365))) for i in range(users)],
        )
    cur.execute("SELECT count(*) FROM shop_products")
    if cur.fetchone()[0] == 0:
        cur.executemany(
            "INSERT INTO shop_products (name, category, price, stock) VALUES (%s, %s, %s, %s)",
            [(f"商品-{i:04d}", random.choice(CATEGORIES),
              round(random.uniform(5, 5000), 2), random.randint(0, 1000))
             for i in range(products)],
        )
    cur.execute("SELECT count(*) FROM shop_orders")
    existing = cur.fetchone()[0]
    if existing < orders:
        batch, total = 2000, orders - existing
        for start in range(0, total, batch):
            cur.executemany(
                "INSERT INTO shop_orders (user_id, product_id, quantity, amount, status, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                [(random.randint(1, users), random.randint(1, products),
                  random.randint(1, 5), round(random.uniform(10, 20000), 2),
                  random.choice(["paid", "shipped", "done"]),
                  datetime.now() - timedelta(minutes=random.randint(0, 43200)))
                 for _ in range(min(batch, total - start))],
            )
            conn.commit()
    conn.commit()
    cur.execute("SELECT (SELECT count(*) FROM shop_users), "
                "(SELECT count(*) FROM shop_products), (SELECT count(*) FROM shop_orders)")
    return cur.fetchone()


def my_insert_orders(conn, n: int = 50) -> None:
    cur = conn.cursor()
    cur.executemany(
        "INSERT INTO shop_orders (user_id, product_id, quantity, amount, status, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        [(random.randint(1, 2000), random.randint(1, 500), random.randint(1, 5),
          round(random.uniform(10, 20000), 2), "paid", datetime.now()) for _ in range(n)],
    )
    conn.commit()


# --------------------------------------------------------------------------- 主流程


def setup_all() -> None:
    print("=" * 68)
    print("在测试库中构造数据（用户已授权，仅用于测试环境）")
    print("=" * 68)

    try:
        kb = kb_connect()
        try:
            kb_setup(kb)
            u, p, o = kb_seed(kb)
            print(f"\n[Kingbase] test 库")
            print(f"  shop_users={u:,}  shop_products={p:,}  shop_orders={o:,}")
        finally:
            kb.close()
    except Exception as exc:
        print(f"\n[Kingbase] 失败：{type(exc).__name__}: {exc}")

    try:
        my = my_connect()
        try:
            my_setup(my)
            u, p, o = my_seed(my)
            print(f"\n[MySQL] testdb 库")
            print(f"  shop_users={u:,}  shop_products={p:,}  shop_orders={o:,}")
        finally:
            my.close()
    except Exception as exc:
        print(f"\n[MySQL] 失败：{type(exc).__name__}: {exc}")


async def load_loop(interval: float = 5.0, batch: int = 50) -> None:
    """持续写入，让事务数与库大小增长——趋势预测需要动起来的数据。"""
    print(f"\n开始持续写入（每 {interval:.0f}s 各插入 {batch} 条订单，Ctrl+C 停止）…")
    kb = my = None
    round_no = 0
    while True:
        round_no += 1
        try:
            if kb is None:
                kb = kb_connect()
            kb_insert_orders(kb, batch)
        except Exception as exc:
            print(f"  [Kingbase] 写入失败：{type(exc).__name__}: {exc}")
            try:
                if kb:
                    kb.close()
            except Exception:
                pass
            kb = None
        try:
            if my is None:
                my = my_connect()
            my_insert_orders(my, batch)
        except Exception as exc:
            print(f"  [MySQL] 写入失败：{type(exc).__name__}: {exc}")
            try:
                if my:
                    my.close()
            except Exception:
                pass
            my = None

        if round_no % 6 == 0:
            print(f"  第 {round_no} 轮完成（各写入 {round_no * batch:,} 条）")
        await asyncio.sleep(interval)


if __name__ == "__main__":
    setup_all()
    if ONCE:
        print("\n（--once 模式：未启动持续写入）")
    else:
        try:
            asyncio.run(load_loop())
        except KeyboardInterrupt:
            print("\n已停止持续写入")
