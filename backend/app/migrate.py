"""轻量 schema 迁移。

项目当前用 SQLite，`create_all` 只会创建缺失的**表**，不会给已有表加列，
也无法把 NOT NULL 改成可空。因此这里做两件事：

1. 为已有表补齐新增的可空列（ALTER TABLE ADD COLUMN）
2. 重建 metric_sample，让 server_id 可空并新增 db_instance_id
   （数据库指标没有 server_id，需要该列为空）

为什么不用 Alembic：本项目规模定位是单机 SQLite，
引入迁移框架的维护成本高于收益。等切到 PostgreSQL 时再迁移到 Alembic。

⚠️ 关于重建表的一个坑（踩过一次）：
SQLAlchemy 的 `engine.begin()` 配合 pysqlite 驱动时，DDL 语句会被**隐式提交**
（pysqlite 默认不在 DDL 前发 BEGIN），于是「建新表」被提交、
而「拷数据的 INSERT」在后续失败时回滚，最终留下一个空的半成品表。
因此重建必须用**原生 sqlite3 + 显式事务**来做，而不是走 SQLAlchemy 事务。
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from sqlalchemy import inspect, text

from app.config import settings
from app.db import engine

logger = logging.getLogger(__name__)

# 迁移状态，供 /api/health 暴露
MIGRATION_STATE: dict[str, object] = {"ok": True, "message": ""}

_NEW_TABLE_SQL = """
CREATE TABLE metric_sample_new (
    id INTEGER NOT NULL PRIMARY KEY,
    server_id INTEGER,
    db_instance_id INTEGER,
    metric_key VARCHAR(64) NOT NULL,
    ts DATETIME NOT NULL,
    value FLOAT NOT NULL,
    labels JSON,
    FOREIGN KEY(server_id) REFERENCES server (id),
    FOREIGN KEY(db_instance_id) REFERENCES database_instance (id)
)
"""


def _sqlite_path() -> Path | None:
    url = settings.database_url
    if not url.startswith("sqlite"):
        return None
    raw = url.split("///", 1)[-1]
    if raw in ("", ":memory:"):
        return None
    return Path(raw).resolve()


def _columns(conn, table: str) -> dict[str, dict]:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    # (cid, name, type, notnull, dflt_value, pk)
    return {row[1]: {"type": row[2], "notnull": row[3], "pk": row[5]} for row in rows}


def _migrate_metric_sample_sqlite(db_path: Path) -> None:
    """用原生 sqlite3 重建 metric_sample（显式事务，避免隐式提交的一半状态）。"""
    # 先释放连接池里可能存在的读事务，否则重建可能拿不到写锁
    engine.dispose()

    conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=60)
    try:
        # 立即切换到内存临时存储，避免后续重建时因临时目录不可写而失败
        conn.execute("PRAGMA temp_store=MEMORY")
        cols = {r[1]: r for r in conn.execute("PRAGMA table_info(metric_sample)").fetchall()}
        if not cols:
            return  # 表不存在，create_all 会按新模型建

        server_id_notnull = cols["server_id"][3] == 1 if "server_id" in cols else False
        has_db_col = "db_instance_id" in cols
        if not server_id_notnull and has_db_col:
            return  # 已是新结构

        before = conn.execute("SELECT COUNT(*) FROM metric_sample").fetchone()[0]
        logger.info(
            "正在重建 metric_sample 以支持数据库指标（现有 %d 行）…", before
        )

        # PRAGMA 必须在事务外设置。
        # temp_store=MEMORY 是必须的：重建表时 SQLite 需要临时存储，
        # 若临时目录不可写（受限环境）会报 SQLITE_CANTOPEN
        # （"unable to open database file"），极具误导性。
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DROP TABLE IF EXISTS metric_sample_new")
            conn.execute(_NEW_TABLE_SQL)

            if has_db_col:
                conn.execute(
                    """
                    INSERT INTO metric_sample_new
                        (id, server_id, db_instance_id, metric_key, ts, value, labels)
                    SELECT id, server_id, db_instance_id, metric_key, ts, value, labels
                    FROM metric_sample
                    """
                )
            else:
                conn.execute(
                    """
                    INSERT INTO metric_sample_new
                        (id, server_id, db_instance_id, metric_key, ts, value, labels)
                    SELECT id, server_id, NULL, metric_key, ts, value, labels
                    FROM metric_sample
                    """
                )

            copied = conn.execute("SELECT COUNT(*) FROM metric_sample_new").fetchone()[0]
            if copied != before:
                raise RuntimeError(
                    f"拷贝行数不一致（原 {before} 行，新表 {copied} 行），已回滚"
                )

            conn.execute("DROP TABLE metric_sample")
            conn.execute("ALTER TABLE metric_sample_new RENAME TO metric_sample")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_sample_lookup "
                "ON metric_sample (server_id, metric_key, ts)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_sample_db_lookup "
                "ON metric_sample (db_instance_id, metric_key, ts)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_sample_ts ON metric_sample (ts)"
            )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            try:
                conn.execute("PRAGMA foreign_keys=ON")
            except Exception:
                pass

        after = conn.execute("SELECT COUNT(*) FROM metric_sample").fetchone()[0]
        if after != before:
            raise RuntimeError(f"重建后行数不一致（{before} -> {after}）")
        logger.info("metric_sample 重建完成，保留 %d 行数据", after)
    finally:
        conn.close()
        # 让后续连接使用重建后的结构
        engine.dispose()


# 已有表需要补齐的可空列：(表名, 列名, 列定义)
_ADDITIVE_COLUMNS: list[tuple[str, str, str]] = [
    ("metric_script", "db_type", "VARCHAR(16) DEFAULT ''"),
    ("database_instance", "last_error", "TEXT DEFAULT ''"),
    ("database_instance", "collect_mode", "VARCHAR(16) DEFAULT 'direct'"),
    ("database_instance", "proxy_server_id", "INTEGER"),
    ("database_instance", "enabled", "BOOLEAN DEFAULT 1"),
    ("database_instance", "status", "VARCHAR(16) DEFAULT 'unknown'"),
    ("database_instance", "database_name", "VARCHAR(64) DEFAULT ''"),
    ("database_instance", "proxy_host", "VARCHAR(255) DEFAULT ''"),
    ("database_instance", "client_path", "VARCHAR(512) DEFAULT ''"),
    ("alert_event", "db_instance_id", "INTEGER"),
    ("backup_task", "exec_server_id", "INTEGER"),
]


def run_migrations() -> None:
    """执行所有必要的 schema 变更（幂等）。

    迁移失败**不应阻止平台启动**：主机监控、终端、告警等功能不受影响。
    失败原因会记录到 MIGRATION_STATE，并通过 /api/health 暴露出来，
    便于用户看到「数据库指标暂不可用」而不是面对一个起不来的服务。
    """
    MIGRATION_STATE["ok"] = True
    MIGRATION_STATE["message"] = ""

    # ---------- 关键迁移：metric_sample 重建 ----------
    db_path = _sqlite_path()
    if db_path is not None and db_path.exists():
        try:
            inspector = inspect(engine)
            if "metric_sample" in inspector.get_table_names():
                _migrate_metric_sample_sqlite(db_path)
        except Exception as exc:
            logger.error(
                "重建 metric_sample 失败：%s。"
                "数据库指标写入将不可用，但平台其余功能正常。",
                exc,
            )
            MIGRATION_STATE["ok"] = False
            MIGRATION_STATE["message"] = f"metric_sample 迁移失败：{exc}"
    elif db_path is not None:
        # 首次运行，没有旧库
        pass

    # ---------- 补充可空列 ----------
    try:
        inspector = inspect(engine)
        existing_tables = set(inspector.get_table_names())
        with engine.begin() as conn:
            for table, column, ddl in _ADDITIVE_COLUMNS:
                if table not in existing_tables:
                    continue
                try:
                    if column in _columns(conn, table):
                        continue
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
                    logger.info("已为 %s 添加列 %s", table, column)
                except Exception as exc:
                    logger.debug("跳过 %s.%s：%s", table, column, exc)
    except Exception as exc:
        logger.warning("补充可空列时出现问题：%s", exc)
