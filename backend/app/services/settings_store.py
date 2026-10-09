"""运行时可改设置的存储层。

原先 LLM 相关配置只能通过环境变量传入，改一次就要重启服务。
运维现场换模型、换网关、换密钥是常事，因此改为可在界面里修改并持久化。

取值优先级：**数据库 > 环境变量 > 代码默认值**。
这样既能用界面覆盖，也保留了「用环境变量做初始值 / CI 注入」的能力，
并且升级老部署时行为不变（没在界面里改过就一直走环境变量）。

敏感值（API Key）用与服务器口令相同的 AES-256-GCM 信封加密，
数据库里不出现明文。
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.config import settings
from app.core.crypto import decrypt_secret, encrypt_secret
from app.models import AppSetting, utcnow

logger = logging.getLogger(__name__)

# LLM 配置项：key -> (类型, 环境变量回退值)
LLM_FIELDS: dict[str, tuple[str, object]] = {
    "llm_enabled": ("bool", lambda: settings.llm_enabled),
    "llm_base_url": ("str", lambda: settings.llm_base_url),
    "llm_model": ("str", lambda: settings.llm_model),
    "llm_timeout": ("int", lambda: settings.llm_timeout),
    "llm_max_tokens": ("int", lambda: settings.llm_max_tokens),
}
LLM_SECRET_FIELDS = {"llm_api_key": lambda: settings.llm_api_key}

ALL_KEYS = set(LLM_FIELDS) | set(LLM_SECRET_FIELDS)


# ---------------------------------------------------------------- 基础读写


def _get_row(db: Session, key: str) -> AppSetting | None:
    return db.get(AppSetting, key)


def _coerce(kind: str, raw: str, default: object) -> object:
    if kind == "bool":
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if kind == "int":
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default
    return raw


def get_value(db: Session, key: str) -> tuple[object, str]:
    """返回 (值, 来源)。来源为 db / env / default。"""
    row = _get_row(db, key)
    if row is not None and row.value != "":
        if key in LLM_SECRET_FIELDS:
            # 密文在 secret_* 字段里，这里不返回明文
            return None, "db"
        kind, fallback = LLM_FIELDS[key]
        return _coerce(kind, row.value, fallback()), "db"

    if key in LLM_SECRET_FIELDS:
        env = LLM_SECRET_FIELDS[key]()
        return env, "env" if env else "default"

    kind, fallback = LLM_FIELDS[key]
    default = fallback()
    # 环境变量由 pydantic-settings 读入 settings，与默认值同源；
    # 若与默认值不同则说明是环境变量给的
    source = "env" if default != _hard_default(key) else "default"
    return default, source


def _hard_default(key: str) -> object:
    """代码里的出厂默认值，用于区分「环境变量给的值」与「默认值」。"""
    return {
        "llm_enabled": False,
        "llm_base_url": "https://api.deepseek.com/v1",
        "llm_model": "deepseek-chat",
        "llm_timeout": 60,
        "llm_max_tokens": 4096,
    }.get(key)


def get_secret(db: Session, key: str) -> tuple[str, bool, str]:
    """读取敏感值。返回 (明文, 是否有值, 来源)。

    明文只在此处解密，不通过任何接口返回给前端。
    """
    row = _get_row(db, key)
    if row is not None and row.has_secret and row.secret_ciphertext and row.secret_nonce:
        try:
            return decrypt_secret(row.secret_ciphertext, row.secret_nonce), True, "db"
        except Exception:
            logger.exception("解密 %s 失败", key)
            return "", False, "db"
    env = LLM_SECRET_FIELDS[key]() if key in LLM_SECRET_FIELDS else ""
    return env, bool(env), "env" if env else "default"


def set_values(
    db: Session, values: dict[str, object], user: str = "", secret: str | None = None
) -> None:
    """写入设置。secret 为 None 表示「不修改密钥」。"""
    for key, raw in values.items():
        if key not in LLM_FIELDS:
            continue
        kind, _ = LLM_FIELDS[key]
        text = "true" if (kind == "bool" and raw) else ("false" if kind == "bool" else str(raw))
        row = _get_row(db, key)
        if row is None:
            row = AppSetting(key=key)
            db.add(row)
        row.value = text
        row.updated_at = utcnow()
        row.updated_by = user
        db.add(row)

    if secret is not None:
        row = _get_row(db, "llm_api_key")
        if row is None:
            row = AppSetting(key="llm_api_key")
            db.add(row)
        if secret == "":
            # 显式清空
            row.secret_ciphertext = None
            row.secret_nonce = None
            row.has_secret = False
        else:
            ciphertext, nonce = encrypt_secret(secret)
            row.secret_ciphertext = ciphertext
            row.secret_nonce = nonce
            row.has_secret = True
        row.value = ""
        row.updated_at = utcnow()
        row.updated_by = user
        db.add(row)

    db.commit()


# ---------------------------------------------------------------- LLM 配置


def get_llm_config(db: Session) -> dict:
    """组装当前生效的 LLM 配置（含明文密钥，仅供内部使用）。"""
    cfg: dict = {}
    sources: dict = {}
    for key in LLM_FIELDS:
        value, source = get_value(db, key)
        cfg[key] = value
        sources[key] = source
    api_key, has_key, key_source = get_secret(db, "llm_api_key")
    cfg["llm_api_key"] = api_key
    cfg["has_api_key"] = has_key
    sources["llm_api_key"] = key_source
    cfg["_sources"] = sources
    return cfg


def public_view(db: Session) -> dict:
    """给前端看的配置：不含明文密钥。"""
    cfg = get_llm_config(db)
    return {
        "enabled": cfg["llm_enabled"],
        "base_url": cfg["llm_base_url"],
        "model": cfg["llm_model"],
        "timeout": cfg["llm_timeout"],
        "max_tokens": cfg["llm_max_tokens"],
        "has_api_key": cfg["has_api_key"],
        # 让用户知道这个值是从哪来的：界面设置 / 环境变量 / 默认
        "sources": cfg["_sources"],
    }
