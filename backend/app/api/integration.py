"""对外集成接口：供外部系统调用平台的 AI 能力。

两种调用形态：

1. **平台原生接口** `POST /api/v1/ext/ai/chat`
   贴近平台语义，可选注入平台上下文（主机、告警、数据库现状）。
   适合你自己的调度系统、告警机器人、工单系统。

2. **OpenAI 兼容接口** `POST /api/v1/ext/v1/chat/completions`
   把平台当成一个 OpenAI 端点。任何支持自定义 base_url 的客户端
   （LangChain、Dify、ChatBox、各类 Agent 框架）改一行配置即可接入，
   不需要为平台写适配代码。

鉴权：请求头 `Authorization: Bearer <令牌>` 或 `X-API-Key: <令牌>`。
令牌在「AI 助手 → 访问令牌」里创建。

安全：外部请求**只能读、不能触发动作**。平台里的工具调用对
外部令牌一律不放行，避免外部系统间接操作服务器。
"""

from __future__ import annotations

import logging
import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import ApiToken

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ext", tags=["integration"])


# ============================================================ 鉴权


async def require_token(
    db: Annotated[Session, Depends(get_db)],
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header()] = None,
) -> ApiToken:
    from app.api.settings_api import resolve_api_token

    raw = None
    if authorization and authorization.lower().startswith("bearer "):
        raw = authorization[7:]
    elif x_api_key:
        raw = x_api_key

    if not raw:
        raise HTTPException(
            status_code=401,
            detail="缺少访问令牌。请在请求头带 Authorization: Bearer <令牌>",
        )

    row = resolve_api_token(db, raw)
    if row is None:
        raise HTTPException(status_code=401, detail="令牌无效、已停用或已过期")
    return row


TokenAuth = Annotated[ApiToken, Depends(require_token)]


# ============================================================ 原生接口


class ExtChatIn(BaseModel):
    question: str = Field(min_length=1, max_length=8000)
    # 是否注入平台现状（主机/告警/数据库），默认注入
    include_context: bool = True


class ExtMessage(BaseModel):
    role: str
    content: str


class ExtCompletionIn(BaseModel):
    model: str | None = None
    messages: list[ExtMessage] = Field(min_length=1)
    temperature: float | None = None
    max_tokens: int | None = None
    stream: bool = False


@router.get("/ai/status", summary="集成接口状态")
def ext_status(token: TokenAuth) -> dict:
    """外部系统可用来自检令牌与 AI 是否就绪。"""
    from app.services.ai.provider import get_provider

    provider = get_provider()
    return {
        "ok": True,
        "token_name": token.name,
        "scope": token.scope,
        "ai_enabled": provider.enabled,
        "model": provider.model,
    }


@router.post("/ai/chat", summary="调用平台 AI（原生）")
async def ext_chat(
    payload: ExtChatIn,
    token: TokenAuth,
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    """调用平台的运维 AI。

    `include_context=true` 时会把当前主机、告警、数据库状况一并给模型，
    因此外部系统可以直接问「现在有什么问题」而不必自己拼上下文。
    """
    from app.services.ai.copilot import ask
    from app.services.ai.provider import get_provider

    provider = get_provider(db)
    if not provider.enabled:
        raise HTTPException(
            status_code=503,
            detail="平台 AI 未启用或未配置。请在「AI 助手 → 模型配置」中完成设置。",
        )

    started = time.monotonic()
    try:
        # 外部调用一律走只读路径：不传平台工具，避免外部系统间接操作服务器
        result = await ask(payload.question, allow_tools=False)
    except Exception as exc:
        logger.exception("外部 AI 调用失败")
        raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}") from exc

    return {
        "answer": result.get("answer", ""),
        "model": result.get("model", ""),
        "enabled": True,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "token_name": token.name,
    }


# ============================================================ OpenAI 兼容


@router.post("/v1/chat/completions", summary="OpenAI 兼容接口")
async def ext_chat_completions(
    payload: ExtCompletionIn,
    token: TokenAuth,
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    """把平台当作一个 OpenAI 端点。

    这样任何支持自定义 base_url 的客户端都能接入：
        base_url = http://<平台地址>/api/v1/ext/v1
        api_key  = <平台访问令牌>
    """
    from app.services.ai.provider import get_provider

    provider = get_provider(db)
    if not provider.enabled:
        raise HTTPException(status_code=503, detail="平台 AI 未启用或未配置")

    messages = [{"role": m.role, "content": m.content} for m in payload.messages]
    try:
        result = await provider.chat(
            messages,
            temperature=payload.temperature if payload.temperature is not None else 0.2,
            max_tokens=payload.max_tokens,
        )
    except Exception as exc:
        logger.exception("OpenAI 兼容调用失败")
        raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}") from exc

    created = int(time.time())
    usage = getattr(result, "usage", None) or {}
    prompt_tokens = usage.get("prompt_tokens", 0) or 0
    completion_tokens = usage.get("completion_tokens", 0) or 0
    return {
        "id": f"chatcmpl-aiops-{created}",
        "object": "chat.completion",
        "created": created,
        "model": payload.model or provider.model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": result.content or ""},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": usage.get("total_tokens", prompt_tokens + completion_tokens),
        },
    }
