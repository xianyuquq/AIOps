"""应用配置。

所有配置项均可通过环境变量或 .env 文件覆盖，前缀 AIOPS_。
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AIOPS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---------- 基础 ----------
    app_name: str = "AI 智能运维平台"
    debug: bool = False
    host: str = "127.0.0.1"
    port: int = 8000

    # ---------- 数据库 ----------
    # 10 台以内规模使用 SQLite；量大时可切换为 postgresql+asyncpg://...
    database_url: str = "sqlite:///./data/aiops.db"

    # ---------- 安全 ----------
    # 用于 JWT 签名。生产环境必须通过环境变量覆盖。
    secret_key: str = "CHANGE-ME-IN-PRODUCTION-please-use-env-var"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 7

    # 凭据加密主密钥（base64 编码的 32 字节）。留空时自动生成并持久化到 data/master.key。
    # 生产环境建议通过环境变量注入，避免密钥与数据同机存放。
    master_key_b64: str = ""

    # 首个管理员账号（首次启动自动创建）
    admin_username: str = "admin"
    admin_password: str = "admin123"

    # ---------- SSH ----------
    ssh_connect_timeout: int = 10
    ssh_keepalive_interval: int = 30
    ssh_max_connections: int = 200
    ssh_max_per_host: int = 10
    ssh_command_timeout: int = 60
    # 首次连接时自动接受未知主机密钥（TOFU）。生产建议置 false 并人工确认指纹。
    ssh_auto_accept_host_key: bool = True

    # ---------- 采集与告警 ----------
    metrics_retention_days: int = 30
    collector_max_concurrency: int = 20
    alert_evaluation_interval: int = 15

    # ---------- LLM ----------
    llm_enabled: bool = False
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    llm_timeout: int = 60
    llm_max_tokens: int = 4096

    # ---------- 路径 ----------
    data_dir: str = "./data"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
