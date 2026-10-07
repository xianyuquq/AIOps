"""容灾备份脚本模板。

设计要点：

1. **密码不写进脚本正文**。备份命令运行时通过环境变量注入密码
   （`MYSQL_PWD` / `PGPASSWORD`），因此密码不会出现在命令行的 `ps` 输出里，
   也不会被记进备份日志。模板里用 `{{user}}` 占位，密码由平台注入。

2. **占位符**（执行前替换）：
   `{{host}} {{port}} {{user}} {{database}} {{dir}} {{name}} {{stamp}} {{retention}}`

3. **保留策略**：命令末尾自带按天清理，避免磁盘被备份文件撑满。
   这是容灾方案里最容易遗漏、后果最严重的一环。

4. 模板只是**起点**。用户选中模板后可以自由修改命令，
   也可以从「自定义」开始写自己的备份流程（例如先停写、再物理拷贝、再启写）。
"""

from __future__ import annotations

# 通用的备份目录准备与过期清理片段
_PREP = 'mkdir -p {{dir}}'

_PURGE = (
    '# 清理超过保留期的备份文件（{{retention}} 天）\n'
    'find {{dir}} -maxdepth 1 -type f -name "{{name}}_*" -mtime +{{retention}} -print -delete'
)

# 产物校验：备份「成功」但文件是空的，是容灾里最致命的情况——
# 出事时才发现没有可恢复的数据。
#
# 实测踩到过：`mysqldump ... | gzip > out.gz` 里 mysqldump 报错退出，
# 但 gzip 对空输入照样成功，于是退出码 0、gzip -t 也通过，
# 平台把这次执行记为 success，产物却只有 20 字节（空 gzip 包）。
# 根因是 **set -e 不捕获管道左侧的失败**。
#
# 因此这里做了两件事：
#   1. set -o pipefail —— 管道任一环节失败即整体失败
#   2. 显式的产物体检 —— 非空、大小合理、解压后有内容
_VERIFY_ARTIFACT = r"""# ---- 备份产物体检（防止「成功但为空」）----
[ -s "$OUT" ] || { echo "备份产物不存在或为空：$OUT" >&2; exit 1; }
SIZE=$(wc -c < "$OUT" | tr -d ' ')
if [ "$SIZE" -lt __MIN_SIZE__ ]; then
  echo "备份产物过小（${SIZE} 字节，低于 __MIN_SIZE__ 字节），判定为失败" >&2
  exit 1
fi
__CONTENT_CHECK__
echo "BACKUP_VERIFIED size=${SIZE}"
"""

# 文本型产物（如 gzip 过的 SQL）才做「解压后有内容」检查。
#
# 二进制归档（-Fc 的 .dump）**不能**这么做：
# 实测中它把一份 922KB 的有效金仓归档判成了失败 ——
# dump 前 64 字节是不可打印的二进制，`grep -q .` 匹配不上。
# 那类产物的有效性由 `sys_restore -l` / `pg_restore -l` 负责校验，更准确。
_CONTENT_CHECK_TEXT = (
    'if ! __DECOMPRESS__ | head -c 64 | grep -aq .; then\n'
    '  echo "备份产物解压后为空，判定为失败" >&2\n'
    "  exit 1\n"
    "fi"
)

# gzip 类产物的最小合理字节数（空 gzip 包约 20 字节）
_MIN_GZ = 512
# 自定义格式归档（-Fc）空库也有几百字节，给一个保守下限
_MIN_DUMP = 512


def _pipefail_head() -> str:
    """set -e 之外再加 pipefail。

    不加的话 `失败的命令 | gzip > out` 会被当成成功，见 _VERIFY_ARTIFACT 的说明。
    """
    return "set -e\nset -o pipefail"

# 备份工具解析器。
# 为什么要这一步：mysqldump / pg_dump / sys_dump 通常**不在 SSH 会话的 PATH** 中
# （实测麒麟机器上金仓装在 /data/Kingbase/ES/*/KESRealPro/*/Server/bin，
# 直接调用会得到 `bash: sys_dump：未找到命令`，用户完全不知道该填什么）。
_TOOL_RESOLVER = r"""# ---- 解析备份工具路径（这些工具通常不在 PATH 中）----
find_tool() {
  _n="$1"
  _p=$(command -v "$_n" 2>/dev/null) && [ -x "$_p" ] && { echo "$_p"; return; }
  for _d in /opt/Kingbase/ES/*/Server/bin \
            /data/Kingbase/ES/*/KESRealPro/*/Server/bin \
            /opt/Kingbase/ES/*/KESRealPro/*/Server/bin \
            /opt/kingbase/ES/*/Server/bin \
            /usr/pgsql-*/bin /usr/local/pgsql/bin /usr/lib/postgresql/*/bin \
            /usr/local/mysql/bin /opt/mysql/bin /usr/bin; do
    [ -x "$_d/$_n" ] && { echo "$_d/$_n"; return; }
  done
}
__TOOL_VARS__
"""


def _resolver(tools: dict[str, str]) -> str:
    """生成工具解析片段。tools: {变量名: 可执行文件名}"""
    lines = []
    for var, binary in tools.items():
        lines.append(
            f'{var}=$(find_tool {binary})\n'
            f'[ -n "${var}" ] || {{ echo "未找到 {binary}：'
            f'请在任务脚本中填写完整路径（可用 which {binary} 查询）" >&2; exit 127; }}'
        )
    return _TOOL_RESOLVER.replace("__TOOL_VARS__", "\n".join(lines))

# --------------------------------------------------------------------------- 逻辑备份

_MYSQL_RESOLVER = _resolver({"DUMP": "mysqldump"})
_PG_RESOLVER = _resolver({"DUMP": "pg_dump"})
_KB_RESOLVER = _resolver({"SYSDUMP": "sys_dump", "SYSRESTORE": "sys_restore"})

# 两类产物各自的体检片段
_VERIFY_GZIP = (
    _VERIFY_ARTIFACT.replace("__MIN_SIZE__", str(_MIN_GZ))
    .replace("__CONTENT_CHECK__", _CONTENT_CHECK_TEXT)
    .replace("__DECOMPRESS__", 'gzip -dc "$OUT"')
)
# 二进制归档只查大小，内容有效性交给 restore -l
_VERIFY_ARCHIVE = _VERIFY_ARTIFACT.replace("__MIN_SIZE__", str(_MIN_DUMP)).replace(
    "__CONTENT_CHECK__", ""
)

_MYSQL_LOGICAL = f"""{_pipefail_head()}
{_PREP}
{_MYSQL_RESOLVER}
STAMP={{{{stamp}}}}
OUT={{{{dir}}}}/{{{{name}}}}_${{STAMP}}.sql.gz
# --single-transaction 保证 InnoDB 一致性快照且不锁表；
# 口令通过 MYSQL_PWD 环境变量传入，不出现在命令行中。
"$DUMP" --single-transaction --quick --routines --triggers --events \\
  -h {{{{host}}}} -P {{{{port}}}} -u {{{{user}}}} \\
  --databases {{{{database}}}} | gzip -c > "$OUT"
echo "BACKUP_FILE=$OUT"
ls -lh "$OUT"
gzip -t "$OUT" && echo "GZIP_OK"
{_VERIFY_GZIP}
{_PURGE}
"""

_PG_LOGICAL = f"""{_pipefail_head()}
{_PREP}
{_PG_RESOLVER}
STAMP={{{{stamp}}}}
OUT={{{{dir}}}}/{{{{name}}}}_${{STAMP}}.dump
# 自定义格式（-Fc）支持并行恢复与选择性恢复；口令通过 PGPASSWORD 传入。
"$DUMP" -h {{{{host}}}} -p {{{{port}}}} -U {{{{user}}}} -d {{{{database}}}} \\
  -Fc -f "$OUT"
echo "BACKUP_FILE=$OUT"
ls -lh "$OUT"
{_VERIFY_ARCHIVE}
{_PURGE}
"""

_KINGBASE_LOGICAL = f"""{_pipefail_head()}
{_PREP}
{_KB_RESOLVER}
STAMP={{{{stamp}}}}
OUT={{{{dir}}}}/{{{{name}}}}_${{STAMP}}.dump
# 关键：金仓的 sys_dump **不认 PGPASSWORD**（与 ksql 一样），
# 会转成交互式口令提示；若 stdin 一直开着，备份会永远挂在那儿直到超时。
# 正确做法是用 -W 强制提示，再把口令从 stdin 喂进去。
# 命令行里只出现 $DB_PASSWORD 变量名，明文口令不会进入 ps / 日志。
printf '%s\\n' "$DB_PASSWORD" | "$SYSDUMP" -W -h {{{{host}}}} -p {{{{port}}}} \\
  -U {{{{user}}}} -d {{{{database}}}} -Fc -f "$OUT"
echo "BACKUP_FILE=$OUT"
ls -lh "$OUT"
# 校验归档可读，确保备份真的能用
"$SYSRESTORE" -l "$OUT" | head -3
echo "ARCHIVE_OK"
{_VERIFY_ARCHIVE}
{_PURGE}
"""

_ORACLE_LOGICAL = f"""set -e
{_PREP}
STAMP={{{{stamp}}}}
# Data Pump 需要数据库内的 DIRECTORY 对象；这里用默认目录并导出到文件系统。
# 生产环境请按实际 DIRECTORY 名称调整。
expdp {{{{user}}}}/\\"$DB_PASSWORD\\"@{{{{host}}}}:{{{{port}}}}/{{{{database}}}} \\
  schemas={{{{user}}}} directory=DATA_PUMP_DIR \\
  dumpfile={{{{name}}}}_${{STAMP}}.dmp logfile={{{{name}}}}_${{STAMP}}.log
echo "BACKUP_FILE=${{STAMP}}"
{_PURGE}
"""

# --------------------------------------------------------------------------- 配置备份

_PG_CONFIG = f"""set -e
{_PREP}
STAMP={{{{stamp}}}}
OUT={{{{dir}}}}/{{{{name}}}}_conf_${{STAMP}}.tar.gz
# 备份配置文件与访问控制，便于灾难后快速重建实例。
tar czf "$OUT" \\
  -C / \\
  $(ls -d /var/lib/pgsql/data/pg_hba.conf /var/lib/pgsql/data/postgresql.conf \\
        /etc/postgresql/*/main/*.conf 2>/dev/null) 2>/dev/null || true
echo "BACKUP_FILE=$OUT"
ls -lh "$OUT" 2>/dev/null || echo "未找到配置文件（请按实际安装路径调整）"
{_PURGE}
"""

_MYSQL_CONFIG = f"""set -e
{_PREP}
STAMP={{{{stamp}}}}
OUT={{{{dir}}}}/{{{{name}}}}_conf_${{STAMP}}.tar.gz
tar czf "$OUT" -C / \\
  $(ls -d /etc/my.cnf /etc/mysql/my.cnf /etc/mysql/mysql.conf.d/*.cnf 2>/dev/null) 2>/dev/null || true
echo "BACKUP_FILE=$OUT"
ls -lh "$OUT" 2>/dev/null || echo "未找到配置文件（请按实际安装路径调整）"
{_PURGE}
"""

# --------------------------------------------------------------------------- 模板注册表

BACKUP_TEMPLATES: dict[str, dict] = {
    # key = 数据库类型，供「按实例生成草稿」使用
    "mysql": {
        "label": "MySQL / MariaDB 逻辑备份（mysqldump）",
        "script": _MYSQL_LOGICAL,
        "target_dir": "/var/backups/aiops/mysql",
        "schedule_cron": "0 2 * * *",
        "notes": "InnoDB 一致性快照、不锁表；输出 sql.gz。",
    },
    "postgresql": {
        "label": "PostgreSQL 逻辑备份（pg_dump -Fc）",
        "script": _PG_LOGICAL,
        "target_dir": "/var/backups/aiops/pgsql",
        "schedule_cron": "0 2 * * *",
        "notes": "自定义格式，支持并行恢复与单表恢复。",
    },
    "kingbase": {
        "label": "Kingbase 逻辑备份（sys_dump -Fc）",
        "script": _KINGBASE_LOGICAL,
        "target_dir": "/var/backups/aiops/kingbase",
        "schedule_cron": "0 2 * * *",
        "notes": "使用金仓自带的 sys_dump，参数与 pg_dump 一致。",
    },
    "oracle": {
        "label": "Oracle Data Pump 逻辑导出（expdp）",
        "script": _ORACLE_LOGICAL,
        "target_dir": "/var/backups/aiops/oracle",
        "schedule_cron": "0 2 * * *",
        "notes": "需要数据库内已创建 DIRECTORY 对象；按实际情况调整 directory 名。",
    },
}

# 供界面「模板选择」使用的列表（含配置类模板）
BACKUP_TEMPLATE_CHOICES: list[dict] = [
    {
        "key": "mysql_logical",
        "group": "逻辑备份",
        "label": "MySQL / MariaDB — mysqldump",
        "db_type": "mysql",
        "backup_type": "logical",
        "script": _MYSQL_LOGICAL,
        "target_dir": "/var/backups/aiops/mysql",
        "schedule_cron": "0 2 * * *",
        "notes": "InnoDB 一致性快照、不锁表；输出 sql.gz。",
    },
    {
        "key": "postgresql_logical",
        "group": "逻辑备份",
        "label": "PostgreSQL — pg_dump -Fc",
        "db_type": "postgresql",
        "backup_type": "logical",
        "script": _PG_LOGICAL,
        "target_dir": "/var/backups/aiops/pgsql",
        "schedule_cron": "0 2 * * *",
        "notes": "自定义格式，支持并行恢复与单表恢复。",
    },
    {
        "key": "kingbase_logical",
        "group": "逻辑备份",
        "label": "Kingbase — sys_dump -Fc",
        "db_type": "kingbase",
        "backup_type": "logical",
        "script": _KINGBASE_LOGICAL,
        "target_dir": "/var/backups/aiops/kingbase",
        "schedule_cron": "0 2 * * *",
        "notes": "使用金仓自带的 sys_dump。",
    },
    {
        "key": "oracle_logical",
        "group": "逻辑备份",
        "label": "Oracle — expdp",
        "db_type": "oracle",
        "backup_type": "logical",
        "script": _ORACLE_LOGICAL,
        "target_dir": "/var/backups/aiops/oracle",
        "schedule_cron": "0 2 * * *",
        "notes": "需数据库内已创建 DIRECTORY 对象。",
    },
    {
        "key": "postgresql_config",
        "group": "配置备份",
        "label": "PostgreSQL — 配置文件备份",
        "db_type": "postgresql",
        "backup_type": "config",
        "script": _PG_CONFIG,
        "target_dir": "/var/backups/aiops/config",
        "schedule_cron": "0 3 * * 0",
        "notes": "备份 postgresql.conf / pg_hba.conf，便于灾难后重建。",
    },
    {
        "key": "mysql_config",
        "group": "配置备份",
        "label": "MySQL — 配置文件备份",
        "db_type": "mysql",
        "backup_type": "config",
        "script": _MYSQL_CONFIG,
        "target_dir": "/var/backups/aiops/config",
        "schedule_cron": "0 3 * * 0",
        "notes": "备份 my.cnf 及其 include 目录。",
    },
]


def get_template(key: str) -> dict | None:
    for item in BACKUP_TEMPLATE_CHOICES:
        if item["key"] == key:
            return item
    return None
