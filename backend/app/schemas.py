"""Pydantic 数据传输对象。"""

from __future__ import annotations

import datetime as dt
from typing import Any, Literal

from pydantic import BaseModel, Field

# ============================================================ 认证


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    username: str
    role: str


class UserOut(BaseModel):
    id: int
    username: str
    role: str
    display_name: str
    is_active: bool

    model_config = {"from_attributes": True}


class UserCreate(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=6, max_length=128)
    role: Literal["admin", "ops", "viewer"] = "viewer"
    display_name: str = ""


# ============================================================ 服务器


class ServerGroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = ""


class ServerGroupOut(BaseModel):
    id: int
    name: str
    description: str

    model_config = {"from_attributes": True}


class ServerIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    host: str = Field(min_length=1, max_length=255)
    port: int = 22
    username: str = Field(min_length=1, max_length=64)
    protocol: Literal["ssh", "winrm"] = "ssh"
    auth_type: Literal["password", "private_key"] = "password"
    # 明文凭据仅在写入时传入，落库前加密，读取接口永不返回
    secret: str = ""
    group_id: int | None = None
    tags: dict[str, Any] = Field(default_factory=dict)


class ServerUpdate(BaseModel):
    name: str | None = None
    host: str | None = None
    port: int | None = None
    username: str | None = None
    protocol: Literal["ssh", "winrm"] | None = None
    auth_type: Literal["password", "private_key"] | None = None
    secret: str | None = None
    group_id: int | None = None
    tags: dict[str, Any] | None = None


class ServerOut(BaseModel):
    id: int
    name: str
    host: str
    port: int
    username: str
    protocol: str
    auth_type: str
    group_id: int | None
    tags: dict[str, Any]
    status: str
    last_seen: dt.datetime | None
    last_error: str
    has_secret: bool = False

    model_config = {"from_attributes": True}


class ConnectionTestResult(BaseModel):
    server_id: int
    ok: bool
    message: str
    duration_ms: int = 0


# ============================================================ 监控


class MetricDefinitionOut(BaseModel):
    id: int
    key: str
    name: str
    unit: str
    category: str
    value_type: str
    description: str

    model_config = {"from_attributes": True}


class MetricScriptIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    category: str = "custom"
    runtime: Literal["shell", "python", "sql"] = "shell"
    platform: Literal["linux", "windows", "any"] = "linux"
    # runtime == "sql" 时指定目标数据库类型
    db_type: Literal["", "mysql", "postgresql", "oracle", "kingbase"] = ""
    body: str
    emit_schema: dict[str, Any] = Field(default_factory=dict)
    interval_sec: int = 60
    timeout_sec: int = 15
    target_scope: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class MetricScriptUpdate(BaseModel):
    name: str | None = None
    category: str | None = None
    runtime: Literal["shell", "python", "sql"] | None = None
    platform: Literal["linux", "windows", "any"] | None = None
    db_type: Literal["", "mysql", "postgresql", "oracle", "kingbase"] | None = None
    body: str | None = None
    emit_schema: dict[str, Any] | None = None
    interval_sec: int | None = None
    timeout_sec: int | None = None
    target_scope: dict[str, Any] | None = None
    enabled: bool | None = None


class MetricScriptOut(BaseModel):
    id: int
    name: str
    category: str
    runtime: str
    platform: str
    db_type: str = ""
    body: str
    emit_schema: dict[str, Any]
    interval_sec: int
    timeout_sec: int
    target_scope: dict[str, Any]
    enabled: bool
    is_builtin: bool

    model_config = {"from_attributes": True}


class ScriptTestRequest(BaseModel):
    server_id: int | None = None


class ScriptTestResult(BaseModel):
    ok: bool
    raw_output: str = ""
    metrics: list[dict[str, Any]] = Field(default_factory=list)
    error: str = ""
    duration_ms: int = 0
    truncated: bool = False
    risk_warning: str = ""


class MetricPoint(BaseModel):
    ts: dt.datetime
    value: float
    labels: dict[str, Any] | None = None


class MetricSeries(BaseModel):
    server_id: int
    server_name: str
    metric_key: str
    unit: str = ""
    points: list[MetricPoint] = Field(default_factory=list)


class DashboardSummary(BaseModel):
    server_total: int
    server_online: int
    alert_firing: int
    alert_critical: int
    script_total: int
    script_enabled: int
    top_cpu: list[dict[str, Any]] = Field(default_factory=list)
    top_memory: list[dict[str, Any]] = Field(default_factory=list)
    top_disk: list[dict[str, Any]] = Field(default_factory=list)
    # 数据库概况
    database_total: int = 0
    database_online: int = 0
    databases: list[dict[str, Any]] = Field(default_factory=list)
    # 趋势预测
    forecasts: list[dict[str, Any]] = Field(default_factory=list)


# ============================================================ 告警


class AlertRuleIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    metric_key: str
    operator: Literal[">", ">=", "<", "<=", "==", "!="] = ">"
    threshold: float
    duration_sec: int = 60
    severity: Literal["info", "warning", "critical"] = "warning"
    target_scope: dict[str, Any] = Field(default_factory=dict)
    silence_min: int = 5
    enabled: bool = True
    ai_analysis: bool = True


class AlertRuleOut(BaseModel):
    id: int
    name: str
    metric_key: str
    operator: str
    threshold: float
    duration_sec: int
    severity: str
    target_scope: dict[str, Any]
    silence_min: int
    enabled: bool
    ai_analysis: bool

    model_config = {"from_attributes": True}


class AlertEventOut(BaseModel):
    id: int
    rule_id: int | None
    server_id: int | None
    server_name: str = ""
    severity: str
    title: str
    detail: dict[str, Any]
    status: str
    fired_at: dt.datetime
    resolved_at: dt.datetime | None
    ai_analysis: dict[str, Any] | None
    fingerprint: str
    affected_count: int

    model_config = {"from_attributes": True}


# ============================================================ 数据库监控


class DatabaseInstanceIn(BaseModel):
    name: str
    db_type: Literal["mysql", "postgresql", "oracle", "kingbase"]
    host: str
    port: int
    username: str = ""
    secret: str = ""
    database_name: str = ""
    collect_mode: Literal["direct", "ssh_proxy"] = "direct"
    proxy_server_id: int | None = None
    # 从跳板机连接数据库用的地址（数据库只监听本机/内网时填 127.0.0.1）
    proxy_host: str = ""
    # 数据库客户端可执行文件完整路径（不在 PATH 中时必须填）
    client_path: str = ""
    enabled: bool = True


class DatabaseInstanceUpdate(BaseModel):
    """更新数据库实例：全部字段可选，未提供的保持原值。

    secret 留空表示「不修改密码」——这样编辑名称等信息时不会误清空凭据。
    """

    name: str | None = None
    db_type: Literal["mysql", "postgresql", "oracle", "kingbase"] | None = None
    host: str | None = None
    port: int | None = None
    username: str | None = None
    secret: str | None = None
    database_name: str | None = None
    collect_mode: Literal["direct", "ssh_proxy"] | None = None
    proxy_server_id: int | None = None
    proxy_host: str | None = None
    client_path: str | None = None
    enabled: bool | None = None


class DatabaseInstanceOut(BaseModel):
    id: int
    name: str
    db_type: str
    host: str
    port: int
    username: str
    database_name: str
    collect_mode: str
    proxy_server_id: int | None
    proxy_host: str = ""
    client_path: str = ""
    status: str
    last_error: str
    enabled: bool
    has_secret: bool = False

    model_config = {"from_attributes": True}


# ============================================================ 备份与容灾


class BackupTaskIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    db_instance_id: int | None = None
    server_id: int | None = None
    exec_server_id: int | None = None
    backup_type: Literal["logical", "physical", "config"] = "logical"
    template_key: str = ""
    script: str = Field(min_length=1)
    target_dir: str = "/var/backups/aiops"
    compress: bool = True
    schedule_cron: str = ""
    retention_days: int = 7
    timeout_sec: int = 1800
    enabled: bool = False


class BackupTaskUpdate(BaseModel):
    """更新备份任务：全部字段可选。

    注意：更新用的 schema 必须是「全字段可选」的。
    这里踩过两次坑——数据库实例与备份任务的更新接口最初复用了创建用的
    schema，导致只传一个字段就返回 422，前端表现为「改不了」。
    """

    name: str | None = None
    db_instance_id: int | None = None
    server_id: int | None = None
    exec_server_id: int | None = None
    backup_type: Literal["logical", "physical", "config"] | None = None
    template_key: str | None = None
    script: str | None = None
    target_dir: str | None = None
    compress: bool | None = None
    schedule_cron: str | None = None
    retention_days: int | None = None
    timeout_sec: int | None = None
    enabled: bool | None = None


class BackupTaskOut(BaseModel):
    id: int
    name: str
    db_instance_id: int | None
    server_id: int | None
    exec_server_id: int | None
    backup_type: str
    template_key: str
    script: str
    target_dir: str
    compress: bool
    schedule_cron: str
    retention_days: int
    timeout_sec: int
    enabled: bool
    last_run_at: dt.datetime | None
    last_status: str
    last_message: str
    last_duration_ms: int
    db_instance_name: str = ""
    exec_server_name: str = ""

    model_config = {"from_attributes": True}


# ============================================================ AI


class AIAskRequest(BaseModel):
    question: str = Field(min_length=1)
    session_id: str | None = None


class AIAskResponse(BaseModel):
    answer: str
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    model: str = ""
    enabled: bool = True


class RCARequest(BaseModel):
    alert_id: int
