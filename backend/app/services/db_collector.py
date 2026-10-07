"""数据库指标采集。

这是此前**完全缺失**的部分：界面能登记数据库实例，但没有任何代码真正连上去采集。

支持两种采集模式：
  - direct   ：平台直接连接数据库（需要对应驱动）
  - ssh_proxy：通过已纳管服务器执行数据库客户端命令
               （适用于数据库不对外暴露端口、只能从跳板机访问的场景）

驱动是阻塞式的，因此统一用 asyncio.to_thread 包到线程里，避免阻塞事件循环。

关于 Kingbase（人大金仓）：它兼容 PostgreSQL 协议，因此复用 psycopg 驱动；
但**系统视图前缀是 sys_ 而非 pg_**，所以 SQL 脚本是独立的——
驱动相同、SQL 不同，这正是内置脚本里 Kingbase 单独一份的原因。
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from dataclasses import dataclass
from typing import Any

from app.services.script_runner import ParsedMetric

logger = logging.getLogger(__name__)

# 各数据库的默认端口（新增实例时用）
DEFAULT_PORTS = {
    "mysql": 3306,
    "postgresql": 5432,
    "oracle": 1521,
    "kingbase": 54321,
}

DB_TYPE_LABELS = {
    "mysql": "MySQL / MariaDB",
    "postgresql": "PostgreSQL",
    "oracle": "Oracle",
    "kingbase": "Kingbase 人大金仓",
}


# 各数据库在「未指定库名」时使用的默认连接库。
# 注意 Kingbase 的默认维护库叫 kingbase（不是 PostgreSQL 的 postgres）——
# 用错会得到 `FATAL: 数据库 "postgres" 不存在` 这种让人摸不着头脑的报错。
DEFAULT_DATABASES = {
    "mysql": "",
    "postgresql": "postgres",
    "kingbase": "kingbase",
    "oracle": "ORCL",
}


def default_database(db_type: str) -> str:
    return DEFAULT_DATABASES.get(db_type, "")


class DBError(RuntimeError):
    """数据库连接或执行失败。"""


@dataclass
class DBConnectionInfo:
    """连接信息快照（避免持有 ORM 对象）。"""

    instance_id: int
    name: str
    db_type: str
    host: str
    port: int
    username: str
    database_name: str
    password: str | None
    # 跳板模式下从跳板机连接数据库所用的地址；留空则用 host。
    # 典型场景：数据库只监听 127.0.0.1，或防火墙只放行跳板机。
    proxy_host: str = ""
    # 数据库客户端可执行文件完整路径；留空则自动查找。
    client_path: str = ""

    @property
    def connect_host_for_proxy(self) -> str:
        return self.proxy_host or self.host


# --------------------------------------------------------------------------- 驱动连接


def _connect_mysql(info: DBConnectionInfo, timeout: int):
    import pymysql

    return pymysql.connect(
        host=info.host,
        port=info.port,
        user=info.username,
        password=info.password or "",
        database=info.database_name or None,
        connect_timeout=timeout,
        read_timeout=timeout,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.Cursor,
    )


def _connect_postgres(info: DBConnectionInfo, timeout: int):
    import psycopg

    # Kingbase 走同一驱动（协议兼容），但默认库名不同
    return psycopg.connect(
        host=info.host,
        port=info.port,
        user=info.username,
        password=info.password or "",
        dbname=info.database_name or default_database(info.db_type),
        connect_timeout=timeout,
    )


def _connect_oracle(info: DBConnectionInfo, timeout: int):
    import oracledb

    service = info.database_name or default_database("oracle")
    dsn = oracledb.makedsn(info.host, info.port, service_name=service)
    return oracledb.connect(user=info.username, password=info.password or "", dsn=dsn)


def _list_databases(db_type: str) -> list[str]:
    """尽力列出可用的库名，用于「库不存在」时给出可操作提示。"""
    try:
        import psycopg

        conn = psycopg.connect(
            host=_LAST_INFO.host, port=_LAST_INFO.port, user=_LAST_INFO.username,
            password=_LAST_INFO.password or "",
            dbname="kingbase" if db_type == "kingbase" else "postgres",
            connect_timeout=6,
        )
        try:
            cur = conn.cursor()
            cur.execute("SELECT datname FROM sys_database ORDER BY 1")
            return [r[0] for r in cur.fetchall()]
        finally:
            conn.close()
    except Exception:
        return []


# 记录最近一次连接信息，供错误诊断复用
_LAST_INFO: DBConnectionInfo | None = None


def _describe_connect_failure(info: DBConnectionInfo, exc: Exception) -> DBError:
    """把驱动异常翻译成可操作的中文说明。"""
    text = str(exc) or ""
    kind = type(exc).__name__
    label = DB_TYPE_LABELS.get(info.db_type, info.db_type)
    target = f"{info.host}:{info.port}"

    low = text.lower()
    # 库不存在 —— 这是最常见也最容易让人困惑的一类
    if "不存在" in text or "does not exist" in low:
        available = _list_databases(info.db_type)
        hint = ""
        if available:
            hint = f"该服务器上可用的库：{'、'.join(available)}。"
        default = default_database(info.db_type)
        return DBError(
            f"连接 {label} {target} 失败：目标库 "
            f"“{info.database_name or default}” 不存在。{hint}"
            f"请在实例配置中填写正确的「库名 / 服务名」。"
        )

    # 认证失败
    if "password" in low or "authentication" in low or "口令" in text or "认证" in text:
        return DBError(
            f"连接 {label} {target} 失败：用户名或密码不正确（用户 {info.username}）。"
            f"（服务端返回：{text[:200]}）"
        )

    # 连接被拒 / 超时
    if "timeout" in low or "超时" in text:
        return DBError(
            f"连接 {label} {target} 超时：请检查网络、防火墙是否放行该端口，"
            f"以及数据库的 listen_addresses / sys_hba 是否允许本机访问。"
        )
    if "refused" in low or "拒绝" in text:
        return DBError(f"连接 {label} {target} 被拒绝：端口未开放或数据库未运行。")

    return DBError(f"连接 {label} {target} 失败（{kind}）：{text[:300] or '无详细信息'}")


_CONNECTORS = {
    "mysql": _connect_mysql,
    "postgresql": _connect_postgres,
    "kingbase": _connect_postgres,
    "oracle": _connect_oracle,
}


def _rows_from_cursor(cursor, db_type: str) -> list[tuple]:
    """取出形如 (metric_key, value) 的结果行。"""
    rows = cursor.fetchall()
    out: list[tuple] = []
    for row in rows:
        if row is None or len(row) < 2:
            continue
        out.append((row[0], row[1]))
    return out


def _run_sql_blocking(info: DBConnectionInfo, sql: str, timeout: int) -> list[tuple]:
    """在阻塞线程中连接并执行 SQL。"""
    global _LAST_INFO
    _LAST_INFO = info

    connector = _CONNECTORS.get(info.db_type)
    if connector is None:
        raise DBError(f"不支持的数据库类型：{info.db_type}")

    try:
        conn = connector(info, timeout)
    except Exception as exc:
        raise _describe_connect_failure(info, exc) from exc

    try:
        cursor = conn.cursor()
        try:
            statements = [s.strip() for s in sql.split(";") if s.strip()]
            collected: list[tuple] = []
            for stmt in statements:
                cursor.execute(stmt)
                if cursor.description is None:
                    continue  # 非查询语句
                collected.extend(_rows_from_cursor(cursor, info.db_type))
            return collected
        finally:
            try:
                cursor.close()
            except Exception:
                pass
    except DBError:
        raise
    except Exception as exc:
        raise DBError(f"执行 SQL 失败（{type(exc).__name__}）：{exc}") from exc
    finally:
        try:
            conn.close()
        except Exception:
            pass


async def collect_direct(info: DBConnectionInfo, sql: str, timeout: int = 15) -> list[ParsedMetric]:
    """直连数据库采集。"""
    rows = await asyncio.to_thread(_run_sql_blocking, info, sql, timeout)
    return _rows_to_metrics(rows)


def _rows_to_metrics(rows: list[tuple]) -> list[ParsedMetric]:
    metrics: list[ParsedMetric] = []
    for key, value in rows:
        if key is None:
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            # 非数值结果跳过（例如某些视图返回文本）
            continue
        metrics.append(ParsedMetric(key=str(key), value=numeric, labels={}))
    return metrics


# --------------------------------------------------------------------------- SSH 跳板


# 各数据库的客户端可执行文件名
CLIENT_BINARIES = {
    "mysql": "mysql",
    "postgresql": "psql",
    "kingbase": "ksql",
    "oracle": "sqlplus",
}

# 自动查找客户端时的常见安装位置（Kingbase / PostgreSQL / MySQL）
_CLIENT_SEARCH_DIRS = {
    "ksql": [
        "/opt/Kingbase/ES/*/Server/bin",
        "/opt/Kingbase/ES/*/KESRealPro/*/Server/bin",
        "/data/Kingbase/ES/*/Server/bin",
        "/data/Kingbase/ES/*/KESRealPro/*/Server/bin",
        "/opt/kingbase/ES/*/Server/bin",
        "/usr/local/Kingbase/*/Server/bin",
        "/home/kingbase/*/Server/bin",
    ],
    "psql": [
        "/usr/pgsql-*/bin",
        "/usr/local/pgsql/bin",
        "/usr/lib/postgresql/*/bin",
        "/opt/PostgreSQL/*/bin",
    ],
    "mysql": [
        "/usr/bin",
        "/usr/local/mysql/bin",
        "/opt/mysql/bin",
    ],
    "sqlplus": [
        "/usr/lib/oracle/*/client64/bin",
        "/opt/oracle/instantclient*",
        "$ORACLE_HOME/bin",
    ],
}


def _client_resolution(client: str, explicit_path: str | None) -> str:
    """生成解析客户端可执行文件的 shell 片段。

    为什么需要：数据库客户端（ksql/psql/mysql）通常不在 SSH 会话的 PATH 里，
    直接调用会得到 exit 127「未找到命令」，而用户很难从这句话看出该填什么。
    这里先查 PATH，再在常见安装目录里找，都找不到时给出明确可操作的提示。
    """
    if explicit_path:
        return f"CLIENT={shlex.quote(explicit_path)}"

    patterns = " ".join(f"{d}/{client}" for d in _CLIENT_SEARCH_DIRS.get(client, []))
    return (
        f'CLIENT=$(command -v {client} 2>/dev/null '
        f"|| ls -1 {patterns} 2>/dev/null | head -1)"
    )


def build_proxy_command(info: DBConnectionInfo, sql: str) -> str:
    """构造通过 SSH 执行的数据库客户端命令。

    密码通过环境变量传递（MYSQL_PWD / PGPASSWORD），
    避免出现在命令行里被 ps 看到。

    连接地址使用 info.connect_host_for_proxy：跳板模式下数据库常常只监听
    本机或内网，用对外地址反而连不上。
    """
    q_sql = shlex.quote(sql)
    client = CLIENT_BINARIES.get(info.db_type)
    if client is None:
        raise DBError(f"不支持通过 SSH 跳板采集的数据库类型：{info.db_type}")

    host = shlex.quote(info.connect_host_for_proxy)
    user = shlex.quote(info.username)

    # 统一先解析客户端路径，并在缺失时给出可操作的错误
    resolve = _client_resolution(client, info.client_path or None)
    guard = (
        f'[ -n "$CLIENT" ] || {{ echo "未找到 {client} 客户端：'
        f'请在数据库实例配置中填写「客户端路径」" >&2; exit 127; }}'
    )

    if info.db_type == "mysql":
        db = f" {shlex.quote(info.database_name)}" if info.database_name else ""
        run = f'"$CLIENT" -h {host} -P {info.port} -u {user}{db} -N -B -e {q_sql}'
    elif info.db_type == "postgresql":
        db = shlex.quote(info.database_name or "postgres")
        run = (
            f'"$CLIENT" -h {host} -p {info.port} -U {user} -d {db} '
            f"-t -A -F '\t' -c {q_sql}"
        )
    elif info.db_type == "kingbase":
        db = shlex.quote(info.database_name or "test")
        # 关键差异：金仓的 ksql **不认 PGPASSWORD**，会转成交互式口令提示，
        # 而 SSH 命令的 stdin 一直开着，于是命令挂起到超时。
        # 实测可行且不泄露口令的做法：用 -W 强制提示，再把口令从 stdin 喂进去
        # （命令行里只出现变量名 $DB_PASSWORD，不会出现在 ps 输出中）。
        run = (
            f"printf '%s\\n' \"$DB_PASSWORD\" | "
            f'"$CLIENT" -W -h {host} -p {info.port} -U {user} -d {db} '
            f"-t -A -F '\t' -c {q_sql}"
        )
    elif info.db_type == "oracle":
        service = shlex.quote(info.database_name or "ORCL")
        run = (
            f'"$CLIENT" -S -L /nolog <<\'SQLEOF\'\n'
            f"connect {info.username}/\"$DB_PASSWORD\"@{info.connect_host_for_proxy}:"
            f"{info.port}/{info.database_name or 'ORCL'}\n"
            f"{sql}\n"
            f"exit\n"
            f"SQLEOF"
        )
    else:  # pragma: no cover - 已在上方拦截
        raise DBError(f"不支持通过 SSH 跳板采集的数据库类型：{info.db_type}")

    return f"{resolve}; {guard}; {run}"


def _proxy_env(info: DBConnectionInfo) -> dict[str, str]:
    """构造密码环境变量，避免密码出现在命令行。

    注意金仓：ksql 不认 PGPASSWORD，因此这里走 DB_PASSWORD +
    在命令中把口令从 stdin 喂给 -W 提示（见 build_proxy_command）。
    """
    if info.db_type == "mysql":
        return {"MYSQL_PWD": info.password or ""}
    if info.db_type == "postgresql":
        return {"PGPASSWORD": info.password or ""}
    return {"DB_PASSWORD": info.password or ""}


def _parse_proxy_output(raw: str) -> list[tuple]:
    """解析命令行客户端输出（制表符分隔，无表头）。"""
    rows: list[tuple] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            # 兼容空格分隔（pymysql -B 默认制表符，这里做兜底）
            parts = line.split()
            if len(parts) < 2:
                continue
        rows.append(("/".join(parts[:-1]) if len(parts) > 2 else parts[0], parts[-1]))
    return rows


async def collect_via_proxy(executor, info: DBConnectionInfo, sql: str,
                            timeout: int = 15) -> list[ParsedMetric]:
    """通过已纳管服务器执行数据库客户端命令完成采集。"""
    command = build_proxy_command(info, sql)

    # 把口令作为环境变量注入，避免出现在命令行。
    # 必须用 export：`VAR=x cmd1; cmd2` 只对 cmd1 生效，
    # 而这里命令由多段组成（解析客户端路径 / 守卫 / 真正执行），
    # 用前缀赋值会让后面的段落拿不到变量。
    env = _proxy_env(info)
    env_prefix = " ".join(f"export {k}={shlex.quote(v)};" for k, v in env.items())
    full_command = f"{env_prefix} {command}" if env_prefix else command

    result = await executor.run(full_command, timeout=timeout)
    if not result.ok:
        detail = (result.stderr or result.stdout or "").strip()[:300]
        raise DBError(
            f"通过跳板机采集失败（退出码 {result.exit_code}）：{detail or '无输出'}"
        )
    return _rows_to_metrics(_parse_proxy_output(result.stdout))


# --------------------------------------------------------------------------- 连接测试


async def test_connection(info: DBConnectionInfo, timeout: int = 8,
                          executor=None) -> tuple[bool, str, int]:
    """测试数据库连通性。返回 (是否成功, 说明, 耗时毫秒)。"""
    import time

    probe_sql = {
        "mysql": "SELECT 1",
        "postgresql": "SELECT 1",
        "kingbase": "SELECT 1",
        "oracle": "SELECT 1 FROM DUAL",
    }.get(info.db_type, "SELECT 1")

    started = time.monotonic()
    try:
        if executor is not None:
            await collect_via_proxy(executor, info, probe_sql, timeout)
        else:
            await collect_direct(info, probe_sql, timeout)
    except DBError as exc:
        return False, str(exc), int((time.monotonic() - started) * 1000)
    except Exception as exc:
        return (
            False,
            f"连接失败（{type(exc).__name__}）：{exc or '无详细信息'}",
            int((time.monotonic() - started) * 1000),
        )
    return True, "连接成功", int((time.monotonic() - started) * 1000)
