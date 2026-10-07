"""数据库引擎与会话管理。

10 台以内规模使用 SQLite + WAL 模式：
- WAL 允许读写并发，避免采集写入阻塞 Web 查询
- 单文件便于备份与迁移
"""

from __future__ import annotations

import logging
from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.models import Base

logger = logging.getLogger(__name__)


def _ensure_sqlite_dir(url: str) -> None:
    """确保 SQLite 文件所在目录存在。"""
    if not url.startswith("sqlite"):
        return
    # sqlite:///./data/aiops.db -> ./data/aiops.db
    raw = url.split("///", 1)[-1]
    if raw in ("", ":memory:"):
        return
    Path(raw).parent.mkdir(parents=True, exist_ok=True)


_ensure_sqlite_dir(settings.database_url)

connect_args = {"check_same_thread": False} if settings.is_sqlite else {}
engine = create_engine(
    settings.database_url,
    connect_args=connect_args,
    pool_pre_ping=True,
    future=True,
)

if settings.is_sqlite:

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _record):  # pragma: no cover - 驱动回调
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=10000")
        # temp_store=MEMORY：SQLite 处理大结果集时（排序、GROUP BY、重建表）
        # 会在临时目录建临时文件。若临时目录不可用（受限容器、
        # 只读 /tmp、TEMP 指向不可写路径），SQLite 会直接报
        # "unable to open database file"（SQLITE_CANTOPEN）——
        # 这个报错和"数据库打不开"很像，极具误导性，实际原因是临时文件。
        # 改用内存临时存储即可彻底规避；本项目数据量在内存中完全放得下。
        cursor.execute("PRAGMA temp_store=MEMORY")
        cursor.close()


SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    future=True,
    # commit 后不过期属性：避免 commit 之后（例如会话已关闭）再读取字段时
    # 触发刷新而导致 DetachedInstanceError。本项目大量存在「先查库、
    # 再交给长生命周期服务使用」的场景，此设置可消除一整类隐性错误。
    expire_on_commit=False,
)


def init_db() -> None:
    """建表。生产环境应改用 Alembic 迁移。"""
    Base.metadata.create_all(bind=engine)
    logger.info("数据库已初始化：%s", settings.database_url)


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖：请求级数据库会话。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
