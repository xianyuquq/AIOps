"""Kingbase 跳板采集的回归测试。

这些用例来自在**真实 KingbaseES V009R001C010** 上实测得到的结论，
每一条都对应一个会让采集静默失败（或挂起）的坑：

1. ksql **不认 PGPASSWORD**，会转成交互式口令提示；
   而 SSH 命令的 stdin 一直开着，导致命令挂起直到超时。
   必须用 `-W` + 从 stdin 喂口令。
2. ksql / psql / mysql 通常**不在 SSH 会话的 PATH** 中，
   直接调用得到 exit 127「未找到命令」，用户看不出该填什么。
3. 数据库常常只监听本机或内网，用对外地址连不上，
   需要单独的「跳板内连接地址」。
4. 口令**不得出现在命令行**（否则 ps 可见）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.db_collector import (
    CLIENT_BINARIES,
    DBConnectionInfo,
    _proxy_env,
    build_proxy_command,
)

# 用**明显是假的**口令做命令构造测试：这些用例只检查拼接结果，
# 不需要真实凭据，也避免把真实口令写进源码。
PASSWORD = "SYNTHETIC-TEST-SECRET-not-a-real-credential"


def make_info(db_type: str, **kw) -> DBConnectionInfo:
    base = dict(
        instance_id=1,
        name="t",
        db_type=db_type,
        host="192.168.203.128",
        port=54321,
        username="system",
        database_name="test",
        password=PASSWORD,
    )
    base.update(kw)
    return DBConnectionInfo(**base)


# ============================================================ 口令传递


def test_kingbase_uses_stdin_not_pgpassword():
    """★ 金仓必须走 stdin 喂口令：ksql 不认 PGPASSWORD。"""
    cmd = build_proxy_command(make_info("kingbase"), "SELECT 1")
    assert "printf" in cmd and "-W" in cmd, f"未使用 -W + stdin 方式：{cmd[:200]}"
    assert "DB_PASSWORD" in cmd


def test_postgresql_uses_pgpassword():
    """PostgreSQL 的 psql 认 PGPASSWORD，应沿用环境变量方式。"""
    cmd = build_proxy_command(make_info("postgresql"), "SELECT 1")
    assert "-W" not in cmd
    assert "PGPASSWORD" not in cmd  # 口令由环境变量注入，命令里不出现


def test_password_never_in_command_line():
    """★ 口令绝不能出现在命令行中（ps 可见）。"""
    for db_type in ("mysql", "postgresql", "kingbase", "oracle"):
        cmd = build_proxy_command(make_info(db_type), "SELECT 1")
        assert PASSWORD not in cmd, f"{db_type} 的命令行里出现了明文口令"
        assert "qazpl" not in cmd


def test_password_env_mapping():
    """各数据库的口令环境变量名。"""
    assert _proxy_env(make_info("mysql"))["MYSQL_PWD"] == PASSWORD
    assert _proxy_env(make_info("postgresql"))["PGPASSWORD"] == PASSWORD
    # 金仓走 DB_PASSWORD（配合 stdin）
    assert _proxy_env(make_info("kingbase"))["DB_PASSWORD"] == PASSWORD
    assert _proxy_env(make_info("oracle"))["DB_PASSWORD"] == PASSWORD


# ============================================================ 客户端路径解析


def test_client_binary_is_resolved_not_assumed():
    """★ 客户端必须显式解析：它通常不在 SSH 会话的 PATH 中。"""
    for db_type, binary in CLIENT_BINARIES.items():
        cmd = build_proxy_command(make_info(db_type), "SELECT 1")
        assert "command -v" in cmd, f"{db_type} 未尝试解析客户端路径"
        assert binary in cmd
        assert "CLIENT" in cmd


def test_missing_client_gives_actionable_error():
    """找不到客户端时要给出可操作的提示，而不是让用户猜。"""
    cmd = build_proxy_command(make_info("kingbase"), "SELECT 1")
    assert "客户端路径" in cmd, "错误提示未告知用户该填什么"
    assert "exit 127" in cmd


def test_explicit_client_path_is_used():
    """显式指定路径时应直接使用，不再搜索。"""
    path = "/data/Kingbase/ES/V9/KESRealPro/V009R001C010/Server/bin/ksql"
    cmd = build_proxy_command(make_info("kingbase", client_path=path), "SELECT 1")
    assert path in cmd
    assert "command -v" not in cmd, "已指定路径时不应再搜索"


def test_kingbase_search_covers_common_layouts():
    """自动搜索应覆盖金仓常见安装位置（实测该机器就在 /data 下）。"""
    cmd = build_proxy_command(make_info("kingbase"), "SELECT 1")
    assert "/data/Kingbase/ES/" in cmd, "未覆盖 /data/Kingbase/ES 布局"
    assert "/opt/Kingbase/ES/" in cmd


# ============================================================ 跳板内连接地址


def test_proxy_host_used_when_set():
    """★ 跳板模式下应使用「跳板内连接地址」，而不是对外地址。"""
    info = make_info("kingbase", proxy_host="127.0.0.1")
    cmd = build_proxy_command(info, "SELECT 1")
    assert "127.0.0.1" in cmd
    assert "192.168.203.128" not in cmd, "仍然使用了对外地址"


def test_proxy_host_falls_back_to_host():
    info = make_info("kingbase", proxy_host="")
    cmd = build_proxy_command(info, "SELECT 1")
    assert "192.168.203.128" in cmd


# ============================================================ SQL 与方言


def test_kingbase_sql_quoted_correctly():
    """SQL 里的单引号必须被正确转义，否则会被 shell 吃掉
    （金仓会报「缺少表 db.connections 的 FROM 子句」）。"""
    sql = "SELECT 'db.connections.total', count(*) FROM sys_stat_activity"
    cmd = build_proxy_command(make_info("kingbase"), sql)
    # shlex.quote 会把内部单引号转成 '\''
    assert "'\"'\"'" in cmd or "\\'" in cmd, f"SQL 未被安全引用：{cmd[-160:]}"
    # 反斜杠转义后，SQL 中的 db.connections.total 仍应作为字符串字面量存在
    assert "db.connections.total" in cmd


def test_kingbase_builtin_script_uses_sys_views():
    """内置金仓脚本必须使用 sys_ 前缀视图（不是 pg_）。"""
    from app.builtin_scripts import KINGBASE_BODY

    assert "sys_stat_activity" in KINGBASE_BODY
    assert "sys_stat_database" in KINGBASE_BODY
    assert "sys_settings" in KINGBASE_BODY
    assert "sys_locks" in KINGBASE_BODY
    assert "pg_stat_activity" not in KINGBASE_BODY, "混入了 PostgreSQL 视图名"


def test_kingbase_and_postgresql_scripts_differ():
    """金仓与 PostgreSQL 的采集 SQL 必须是两份（视图前缀不同）。"""
    from app.builtin_scripts import KINGBASE_BODY, POSTGRESQL_BODY

    assert KINGBASE_BODY != POSTGRESQL_BODY
    assert "sys_" in KINGBASE_BODY and "pg_" in POSTGRESQL_BODY


def test_unsupported_db_type_rejected():
    from app.services.db_collector import DBError

    with pytest.raises(DBError):
        build_proxy_command(make_info("unknown-db"), "SELECT 1")


# ============================================================ 默认库名


def test_kingbase_default_database_is_not_postgres():
    """★ 金仓的默认维护库叫 kingbase，不是 PostgreSQL 的 postgres。

    用 postgres 会得到 `FATAL: 数据库 "postgres" 不存在`，
    而用户根本不知道该填什么库名——这是实测踩到的坑。
    """
    from app.services.db_collector import default_database

    assert default_database("kingbase") == "kingbase"
    assert default_database("postgresql") == "postgres"
    assert default_database("kingbase") != default_database("postgresql")


def test_direct_connect_uses_type_specific_default():
    """未填库名时，应按数据库类型选择默认库。"""
    import inspect

    from app.services import db_collector

    src = inspect.getsource(db_collector._connect_postgres)
    assert "default_database" in src, "未按类型选择默认库名"
    assert '"postgres"' not in src, "仍硬编码 postgres"


# ============================================================ 错误信息


def test_missing_database_error_is_actionable():
    """库不存在时，错误信息应列出可用库并指引修改配置。"""
    from app.services.db_collector import DBConnectionInfo, _describe_connect_failure

    info = DBConnectionInfo(
        instance_id=1, name="t", db_type="kingbase",
        host="10.0.0.1", port=54321, username="system",
        database_name="nosuchdb", password="x",
    )

    class FakeError(Exception):
        pass

    err = _describe_connect_failure(
        info, FakeError('FATAL: 数据库 "nosuchdb" 不存在')
    )
    msg = str(err)
    assert "不存在" in msg
    assert "库名" in msg, "未指引用户填写库名"
    assert "nosuchdb" in msg


def test_auth_failure_error_mentions_password():
    from app.services.db_collector import DBConnectionInfo, _describe_connect_failure

    info = DBConnectionInfo(
        instance_id=1, name="t", db_type="kingbase",
        host="10.0.0.1", port=54321, username="system",
        database_name="test", password="x",
    )
    err = _describe_connect_failure(
        info, Exception("FATAL: password authentication failed for user")
    )
    assert "密码" in str(err) or "认证" in str(err)


def test_timeout_error_mentions_firewall():
    from app.services.db_collector import DBConnectionInfo, _describe_connect_failure

    info = DBConnectionInfo(
        instance_id=1, name="t", db_type="kingbase",
        host="10.0.0.1", port=54321, username="system",
        database_name="test", password="x",
    )
    err = _describe_connect_failure(info, Exception("connection timeout expired"))
    msg = str(err)
    assert "超时" in msg
    assert "防火墙" in msg, "未提示检查防火墙"


def test_error_message_never_empty():
    """任何异常都要产出非空且带库类型/地址的说明。"""
    from app.services.db_collector import DBConnectionInfo, _describe_connect_failure

    info = DBConnectionInfo(
        instance_id=1, name="t", db_type="kingbase",
        host="10.0.0.1", port=54321, username="system",
        database_name="test", password="x",
    )
    for exc in (Exception(""), Exception("weird"), RuntimeError()):
        msg = str(_describe_connect_failure(info, exc))
        assert msg.strip()
        assert "Kingbase" in msg or "10.0.0.1" in msg


# ============================================================ 内置脚本注册


def test_builtin_kingbase_script_has_db_type():
    """内置金仓脚本必须声明 db_type，否则调度时找不到目标实例。"""
    from app.builtin_scripts import BUILTIN_SCRIPTS

    kb = next(s for s in BUILTIN_SCRIPTS if "Kingbase" in s["name"])
    assert kb["runtime"] == "sql"
    assert kb["db_type"] == "kingbase"
    assert kb["emit_schema"]["metrics"], "缺少指标声明"
