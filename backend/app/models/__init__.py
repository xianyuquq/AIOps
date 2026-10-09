"""SQLAlchemy ORM 模型。

规模定位：10 台以内，使用 SQLite（WAL 模式）。
升级路径：database_url 换成 PostgreSQL 即可，业务代码无需改动。
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Base(DeclarativeBase):
    pass


# ============================================================ 用户与认证


class User(Base):
    __tablename__ = "user"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    # admin: 全部权限 | ops: 可执行命令、上传文件 | viewer: 只读
    role: Mapped[str] = mapped_column(String(16), default="viewer")
    display_name: Mapped[str] = mapped_column(String(64), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


# ============================================================ 资产


class ServerGroup(Base):
    __tablename__ = "server_group"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    servers: Mapped[list["Server"]] = relationship(back_populates="group")


class Server(Base):
    __tablename__ = "server"

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int | None] = mapped_column(ForeignKey("server_group.id"))
    name: Mapped[str] = mapped_column(String(128))
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer, default=22)
    username: Mapped[str] = mapped_column(String(64))

    # ssh（Linux/麒麟/统信） | winrm（Windows Server）
    protocol: Mapped[str] = mapped_column(String(16), default="ssh")
    # password | private_key
    auth_type: Mapped[str] = mapped_column(String(16), default="password")

    # 加密后的凭据。ciphertext 与 nonce 必须成对使用。
    secret_ciphertext: Mapped[bytes | None] = mapped_column()
    secret_nonce: Mapped[bytes | None] = mapped_column()

    tags: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    # online | offline | unknown
    status: Mapped[str] = mapped_column(String(16), default="unknown")
    last_seen: Mapped[dt.datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    group: Mapped[ServerGroup | None] = relationship(back_populates="servers")

    __table_args__ = (
        UniqueConstraint("host", "port", "username", name="uq_server_endpoint"),
    )

    @property
    def platform(self) -> str:
        """推断目标平台，用于选择采集脚本。"""
        return "windows" if self.protocol == "winrm" else "linux"


# ============================================================ 监控


class MetricDefinition(Base):
    __tablename__ = "metric_definition"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    unit: Mapped[str] = mapped_column(String(16), default="")
    # system | database | custom
    category: Mapped[str] = mapped_column(String(32), default="custom")
    # gauge | counter | rate
    value_type: Mapped[str] = mapped_column(String(16), default="gauge")
    description: Mapped[str] = mapped_column(Text, default="")


class MetricScript(Base):
    """可插拔监控脚本。

    这是需求 1 的核心扩展点：用户可在 Web 界面编写 shell / python / sql 脚本，
    通过 emit_schema 声明式描述如何解析输出为指标。
    """

    __tablename__ = "metric_script"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    category: Mapped[str] = mapped_column(String(32), default="custom")
    # shell | python | sql
    runtime: Mapped[str] = mapped_column(String(16), default="shell")
    # linux | windows | any
    platform: Mapped[str] = mapped_column(String(16), default="linux")

    body: Mapped[str] = mapped_column(Text)
    # 声明输出格式与指标定义，见 docs/ARCHITECTURE.md §5.3
    emit_schema: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    # runtime == "sql" 时，本脚本适用于哪类数据库
    # mysql | postgresql | oracle | kingbase
    db_type: Mapped[str] = mapped_column(String(16), default="")

    interval_sec: Mapped[int] = mapped_column(Integer, default=60)
    timeout_sec: Mapped[int] = mapped_column(Integer, default=15)
    # {"all": true} 或 {"groups": [1,2], "servers": [3,4]}
    target_scope: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class MetricSample(Base):
    """时序采样点。

    主机指标与数据库指标共用本表，通过 server_id / db_instance_id 区分：
      - 主机指标：server_id 有值，db_instance_id 为空
      - 数据库指标：db_instance_id 有值，server_id 为空

    10 台 × 每 60s × ~40 指标 ≈ 57 万行/天。SQLite + 索引可轻松支撑，
    配合 metrics_retention_days 定期清理。
    """

    __tablename__ = "metric_sample"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    server_id: Mapped[int | None] = mapped_column(
        ForeignKey("server.id"), index=True, nullable=True
    )
    db_instance_id: Mapped[int | None] = mapped_column(
        ForeignKey("database_instance.id"), index=True, nullable=True
    )
    metric_key: Mapped[str] = mapped_column(String(64), index=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)
    value: Mapped[float] = mapped_column(Float)
    # 额外维度，例如磁盘挂载点 {"mount": "/var"}
    labels: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    __table_args__ = (
        Index("ix_sample_lookup", "server_id", "metric_key", "ts"),
        Index("ix_sample_db_lookup", "db_instance_id", "metric_key", "ts"),
        Index("ix_sample_ts", "ts"),
    )


# ============================================================ 告警


class AlertRule(Base):
    __tablename__ = "alert_rule"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    metric_key: Mapped[str] = mapped_column(String(64), index=True)
    # > >= < <= == !=
    operator: Mapped[str] = mapped_column(String(8))
    threshold: Mapped[float] = mapped_column(Float)
    # 持续满足多久才告警，用于消除毛刺
    duration_sec: Mapped[int] = mapped_column(Integer, default=60)
    # info | warning | critical
    severity: Mapped[str] = mapped_column(String(16), default="warning")
    target_scope: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # 告警确认后的静默窗口（分钟）
    silence_min: Mapped[int] = mapped_column(Integer, default=5)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # 触发时是否调用 LLM 做根因分析
    ai_analysis: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class AlertEvent(Base):
    __tablename__ = "alert_event"

    id: Mapped[int] = mapped_column(primary_key=True)
    rule_id: Mapped[int | None] = mapped_column(ForeignKey("alert_rule.id"))
    server_id: Mapped[int | None] = mapped_column(ForeignKey("server.id"), index=True)
    # 数据库指标告警归属（与 server_id 互斥）
    db_instance_id: Mapped[int | None] = mapped_column(
        ForeignKey("database_instance.id"), index=True
    )
    severity: Mapped[str] = mapped_column(String(16), default="warning")
    title: Mapped[str] = mapped_column(Text)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # firing | resolved | silenced | acknowledged
    status: Mapped[str] = mapped_column(String(16), default="firing", index=True)
    fired_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    acknowledged_by: Mapped[int | None] = mapped_column(ForeignKey("user.id"))
    # LLM 根因分析结果缓存
    ai_analysis: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    ai_analyzed_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    # 指纹用于聚合降噪：同规则+同指标在多台机器同时触发时合并展示
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    # 该指纹下受影响的服务器数量
    affected_count: Mapped[int] = mapped_column(Integer, default=1)


# ============================================================ 终端与审计


class TerminalSession(Base):
    __tablename__ = "terminal_session"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(128), default="")
    # mirror | diff | single | template
    mode: Mapped[str] = mapped_column(String(16), default="mirror")
    server_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("user.id"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime)


class CommandAudit(Base):
    """命令审计：谁在何时对哪台机器执行了什么、退出码多少。不可删除。"""

    __tablename__ = "command_audit"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[str | None] = mapped_column(String(36), index=True)
    server_id: Mapped[int | None] = mapped_column(ForeignKey("server.id"), index=True)
    command: Mapped[str] = mapped_column(Text)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    output_preview: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    operator: Mapped[int | None] = mapped_column(ForeignKey("user.id"))


# ============================================================ 数据库监控实例


class DatabaseInstance(Base):
    """被监控的数据库实例。

    支持两种采集模式：
    - direct: 平台直连数据库（需驱动）
    - ssh_proxy: 通过已连接的服务器跳板执行命令（适用于数据库不对外暴露端口的国产化环境）
    """

    __tablename__ = "database_instance"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    # mysql | postgresql | oracle | kingbase
    db_type: Mapped[str] = mapped_column(String(16))
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer)
    username: Mapped[str] = mapped_column(String(64), default="")
    secret_ciphertext: Mapped[bytes | None] = mapped_column()
    secret_nonce: Mapped[bytes | None] = mapped_column()
    database_name: Mapped[str] = mapped_column(String(64), default="")

    # direct | ssh_proxy
    collect_mode: Mapped[str] = mapped_column(String(16), default="direct")
    # ssh_proxy 模式下作为跳板的服务器
    proxy_server_id: Mapped[int | None] = mapped_column(ForeignKey("server.id"))
    # ssh_proxy 模式下，从跳板机连接数据库时使用的主机地址。
    # 常与 host 不同：数据库可能只监听内网/本机，或防火墙只放行跳板机。
    # 留空则使用 host。
    proxy_host: Mapped[str] = mapped_column(String(255), default="")
    # 数据库客户端可执行文件完整路径（如 ksql / psql / mysql）。
    # 留空时平台会在 PATH 与常见安装位置中查找。
    client_path: Mapped[str] = mapped_column(String(512), default="")

    status: Mapped[str] = mapped_column(String(16), default="unknown")
    last_error: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


# ============================================================ 备份与容灾


class BackupTask(Base):
    """容灾备份任务。

    设计要点：
    - 备份命令通过 SSH 在「执行服务器」（通常是数据库所在主机）上运行，
      因为备份工具（mysqldump / pg_dump 等）需要在能访问数据库的机器上执行。
    - 数据库密码**不写入脚本正文**，运行时通过环境变量
      （MYSQL_PWD / PGPASSWORD）注入，避免出现在 ps 输出与日志中。
    - 支持模板选择：界面提供各数据库的标准备份模板，用户选一个再改。
    """

    __tablename__ = "backup_task"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    # 备份对象：数据库实例（逻辑备份）或服务器（文件/配置备份）
    db_instance_id: Mapped[int | None] = mapped_column(ForeignKey("database_instance.id"))
    server_id: Mapped[int | None] = mapped_column(ForeignKey("server.id"))

    # 执行备份命令的服务器（需能访问数据库并已纳管 SSH）
    exec_server_id: Mapped[int | None] = mapped_column(ForeignKey("server.id"))

    # logical（逻辑导出）| physical（物理/文件级）| config（配置备份）
    backup_type: Mapped[str] = mapped_column(String(16), default="logical")
    # 使用的模板标识，便于界面回显；不参与执行
    template_key: Mapped[str] = mapped_column(String(48), default="")
    # 备份命令（含 {{host}} {{port}} {{user}} {{database}} {{dir}} {{name}} 占位符）
    script: Mapped[str] = mapped_column(Text)
    # 备份文件存放目录（远端）
    target_dir: Mapped[str] = mapped_column(String(255), default="/var/backups/aiops")
    # 是否压缩
    compress: Mapped[bool] = mapped_column(Boolean, default=True)

    # 调度：标准 5 段 cron 表达式，如 "0 2 * * *"（每天 02:00）
    schedule_cron: Mapped[str] = mapped_column(String(64), default="")
    # 保留天数，超期文件由任务自动清理（0 表示不清理）
    retention_days: Mapped[int] = mapped_column(Integer, default=7)
    timeout_sec: Mapped[int] = mapped_column(Integer, default=1800)

    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    last_run_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    last_status: Mapped[str] = mapped_column(String(16), default="")
    last_message: Mapped[str] = mapped_column(Text, default="")
    last_duration_ms: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class BackupRun(Base):
    """备份执行历史（每次运行一条）。"""

    __tablename__ = "backup_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("backup_task.id"), index=True)
    # success | failed | running
    status: Mapped[str] = mapped_column(String(16), default="running")
    started_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    # 生成的备份文件路径（若能识别）
    artifact: Mapped[str] = mapped_column(String(512), default="")
    artifact_size: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), default=0)
    # 清理掉的过期文件数
    purged: Mapped[int] = mapped_column(Integer, default=0)
    output: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    trigger: Mapped[str] = mapped_column(String(16), default="manual")  # manual | schedule


# ============================================================ 运行时设置


class AppSetting(Base):
    """运行时可改的平台设置（键值对）。

    为什么需要：像 LLM 的 base_url / api_key / model 这类配置原先只能通过
    环境变量传入，改一次就要重启服务。运维现场换模型、换网关、换密钥是常事，
    因此改为可在界面里修改并持久化。

    取值优先级：**数据库 > 环境变量 > 代码默认值**。
    这样既能用界面覆盖，也保留了「用环境变量做初始值 / 做 CI 注入」的能力。
    """

    __tablename__ = "app_setting"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    # 敏感值（如 API Key）单独存放密文与 nonce，绝不存明文
    secret_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    secret_nonce: Mapped[bytes | None] = mapped_column(LargeBinary)
    # 密文无法直接判断「用户是否填过」，因此单独记一个标志
    has_secret: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )
    updated_by: Mapped[str] = mapped_column(String(64), default="")


# ============================================================ 对外集成


class ApiToken(Base):
    """供外部系统调用平台能力的访问令牌。

    设计要点：
    - 只存**哈希**（与用户口令同样处理），数据库泄露也无法反推出令牌；
    - 保留可读前缀，便于在界面上辨认令牌；
    - 记录 last_used_at / call_count，便于判断哪条还在用、哪条可以吊销。
    """

    __tablename__ = "api_token"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    # 形如 aiops_ab12cd34…，仅用于展示
    prefix: Mapped[str] = mapped_column(String(24), index=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # ai  —— 可调用 AI 能力
    # read—— 只读，不能触发任何动作
    scope: Mapped[str] = mapped_column(String(32), default="ai")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    created_by: Mapped[str] = mapped_column(String(64), default="")
    last_used_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    call_count: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
