"""平台设置（LLM 配置）与对外访问令牌管理。

这两件事放在一起，是因为它们同属「让平台能被配置、能被外部接入」：
- LLM 配置：原先只能靠环境变量，改一次要重启；现在可在界面里改，即时生效
- 访问令牌：外部系统调用平台 AI 能力时的凭据
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.crypto import hash_password, verify_password
from app.core.security import AdminUser, CurrentUser, OperatorUser
from app.db import get_db
from app.models import ApiToken, utcnow

router = APIRouter(tags=["settings"])

# 令牌明文形如 aiops_ab12…；前缀用于在界面上辨认，哈希用于校验
TOKEN_PREFIX = "aiops_"
TOKEN_BYTES = 24


# ============================================================ LLM 设置


class LLMSettingsIn(BaseModel):
    enabled: bool | None = None
    base_url: str | None = None
    model: str | None = None
    timeout: int | None = Field(default=None, ge=5, le=600)
    max_tokens: int | None = Field(default=None, ge=64, le=32768)
    # 留空表示「不修改密钥」；传空字符串表示清空
    api_key: str | None = None


@router.get("/settings/llm", summary="读取 LLM 配置")
def get_llm_settings(_: CurrentUser, db: Annotated[Session, Depends(get_db)]) -> dict:
    """返回当前生效的 LLM 配置。

    **不返回密钥明文**，只返回「是否已配置」；同时给出每一项的来源
    （界面设置 / 环境变量 / 默认值），便于排查「为什么改了没生效」。
    """
    from app.services.settings_store import public_view

    return public_view(db)


@router.put("/settings/llm", summary="修改 LLM 配置")
def update_llm_settings(
    payload: LLMSettingsIn,
    user: OperatorUser,
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    """修改后**立即生效，无需重启**。"""
    from app.services.settings_store import public_view, set_values

    values: dict[str, Any] = {}
    if payload.enabled is not None:
        values["llm_enabled"] = payload.enabled
    if payload.base_url is not None:
        url = payload.base_url.strip().rstrip("/")
        if url and not url.startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="接口地址需以 http:// 或 https:// 开头")
        values["llm_base_url"] = url or "https://api.deepseek.com/v1"
    if payload.model is not None:
        values["llm_model"] = payload.model.strip()
    if payload.timeout is not None:
        values["llm_timeout"] = payload.timeout
    if payload.max_tokens is not None:
        values["llm_max_tokens"] = payload.max_tokens

    set_values(db, values, user=user.username, secret=payload.api_key)
    return public_view(db)


@router.post("/settings/llm/test", summary="测试 LLM 连接")
async def test_llm(
    _: OperatorUser, db: Annotated[Session, Depends(get_db)]
) -> dict:
    """真实发一次最小请求，确认地址/密钥/模型三者可用。

    只发一句话，不注入平台上下文，避免把监控数据发到未确认的端点上。
    """
    from app.services.ai.provider import get_provider
    from app.services.ai.provider import LLMDisabled

    provider = get_provider(db)
    if not provider.enabled:
        return {
            "ok": False,
            "message": "LLM 未启用，或未填写 API Key（本地 Ollama 等端点可免 Key）",
            "model": provider.model,
            "base_url": provider.base_url,
        }

    started = utcnow()
    try:
        result = await provider.chat(
            [{"role": "user", "content": "请只回复两个字：连接成功"}],
            temperature=0,
            max_tokens=32,
        )
    except LLMDisabled as exc:
        return {"ok": False, "message": str(exc), "model": provider.model,
                "base_url": provider.base_url}
    except Exception as exc:
        return {
            "ok": False,
            "message": f"{type(exc).__name__}: {str(exc)[:300]}",
            "model": provider.model,
            "base_url": provider.base_url,
        }

    duration = int((utcnow() - started).total_seconds() * 1000)
    return {
        "ok": True,
        "message": "连接成功",
        "model": provider.model,
        "base_url": provider.base_url,
        "reply": (result.content or "")[:200],
        "duration_ms": duration,
    }


# ============================================================ 访问令牌


class TokenIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    scope: str = Field(default="ai", pattern="^(ai|read)$")
    expires_days: int | None = Field(default=None, ge=1, le=3650)


def _token_out(row: ApiToken) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "prefix": row.prefix,
        "enabled": row.enabled,
        "scope": row.scope,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "created_by": row.created_by,
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        "call_count": row.call_count,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "expired": bool(row.expires_at and row.expires_at < utcnow()),
    }


@router.get("/tokens", summary="访问令牌列表")
def list_tokens(_: CurrentUser, db: Annotated[Session, Depends(get_db)]) -> list[dict]:
    rows = db.query(ApiToken).order_by(ApiToken.id.desc()).all()
    return [_token_out(r) for r in rows]


@router.post("/tokens", summary="创建访问令牌")
def create_token(
    payload: TokenIn,
    user: AdminUser,
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    """创建令牌。

    **明文只在这一次响应里返回**，之后数据库里只有哈希，无法再取回。
    请立即保存。
    """
    plaintext = TOKEN_PREFIX + secrets.token_hex(TOKEN_BYTES)
    expires = None
    if payload.expires_days:
        expires = utcnow() + timedelta(days=payload.expires_days)

    row = ApiToken(
        name=payload.name,
        prefix=plaintext[:16],
        token_hash=hash_password(plaintext),
        scope=payload.scope,
        created_by=user.username,
        expires_at=expires,
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    data = _token_out(row)
    data["token"] = plaintext          # 仅此一次
    data["notice"] = "请立即保存此令牌，关闭后无法再次查看。"
    return data


@router.patch("/tokens/{token_id}", summary="启用/停用令牌")
def update_token(
    token_id: int,
    payload: dict,
    _: AdminUser,
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    row = db.get(ApiToken, token_id)
    if row is None:
        raise HTTPException(status_code=404, detail="令牌不存在")
    if "enabled" in payload:
        row.enabled = bool(payload["enabled"])
    if "scope" in payload and payload["scope"] in ("ai", "read"):
        row.scope = payload["scope"]
    db.add(row)
    db.commit()
    db.refresh(row)
    return _token_out(row)


@router.delete("/tokens/{token_id}", summary="删除令牌")
def delete_token(
    token_id: int, _: AdminUser, db: Annotated[Session, Depends(get_db)]
) -> dict:
    row = db.get(ApiToken, token_id)
    if row is None:
        raise HTTPException(status_code=404, detail="令牌不存在")
    db.delete(row)
    db.commit()
    return {"ok": True, "id": token_id}


# ============================================================ 令牌鉴权


def resolve_api_token(db: Session, raw: str | None) -> ApiToken | None:
    """校验外部令牌。按前缀定位后比对哈希。"""
    if not raw:
        return None
    raw = raw.strip()
    if not raw.startswith(TOKEN_PREFIX):
        return None
    candidates = (
        db.query(ApiToken)
        .filter(ApiToken.prefix == raw[:16], ApiToken.enabled.is_(True))
        .all()
    )
    for row in candidates:
        if verify_password(raw, row.token_hash):
            if row.expires_at and row.expires_at < utcnow():
                return None
            row.last_used_at = utcnow()
            row.call_count = (row.call_count or 0) + 1
            db.add(row)
            db.commit()
            return row
    return None
