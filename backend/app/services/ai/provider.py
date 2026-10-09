"""LLM Provider 抽象。

统一走 OpenAI 兼容协议，因此可对接 DeepSeek / 通义千问 / OpenAI / 本地 Ollama
等任意兼容端点，不锁定厂商（见 ARCHITECTURE.md §13.1 调整 4）。

关键约束：llm_enabled=False 时平台必须全功能可用，AI 模块优雅降级。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class LLMDisabled(RuntimeError):
    """未启用 LLM 时抛出，调用方应降级为规则提示。"""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ChatResult:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    model: str = ""
    usage: dict[str, Any] = field(default_factory=dict)


class LLMProvider:
    """OpenAI 兼容协议的轻量客户端。

    直接使用 httpx 而不引入 openai SDK，是为了保持依赖精简，
    并避免 SDK 版本与各家兼容端点的差异问题。
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: int | None = None,
    ) -> None:
        self.base_url = (base_url or settings.llm_base_url).rstrip("/")
        self.api_key = api_key if api_key is not None else settings.llm_api_key
        self.model = model or settings.llm_model
        self.timeout = timeout or settings.llm_timeout
        # 由界面设置决定的总开关；None 表示「界面没设过，看环境变量」
        self.forced_enabled: bool | None = None

    @property
    def enabled(self) -> bool:
        # 界面上的开关优先；未由界面设置过时回退到环境变量
        flag = getattr(self, "forced_enabled", None)
        if flag is None:
            flag = settings.llm_enabled
        if not flag:
            return False
        # 本地 Ollama 等端点不需要 api_key
        if not self.api_key and "localhost" not in self.base_url and "127.0.0.1" not in self.base_url:
            return False
        return True

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> ChatResult:
        if not self.enabled:
            raise LLMDisabled("LLM 未启用或未配置 API Key")

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens or settings.llm_max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers=headers,
            )
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"LLM 请求失败（HTTP {resp.status_code}）：{resp.text[:500]}"
                )
            data = resp.json()

        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}

        tool_calls: list[ToolCall] = []
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function") or {}
            raw_args = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(
                ToolCall(id=tc.get("id", ""), name=fn.get("name", ""), arguments=args)
            )

        return ChatResult(
            content=message.get("content") or "",
            tool_calls=tool_calls,
            model=data.get("model", self.model),
            usage=data.get("usage") or {},
        )


def get_provider(db: Any = None) -> LLMProvider:
    """构造 LLM 提供者。

    配置来源优先级：数据库（界面里改的） > 环境变量 > 代码默认。
    因此在界面上换模型 / 换网关后**立即生效、无需重启**——
    这与「配置只在启动时读一次」的老做法不同。
    """
    if db is None:
        from app.db import SessionLocal

        session = SessionLocal()
        try:
            return _build_from_settings(session)
        finally:
            session.close()
    return _build_from_settings(db)


def _build_from_settings(db: Any) -> LLMProvider:
    try:
        from app.services.settings_store import get_llm_config

        cfg = get_llm_config(db)
    except Exception:
        logger.exception("读取 LLM 配置失败，回退到环境变量")
        return LLMProvider()

    provider = LLMProvider(
        base_url=cfg.get("llm_base_url"),
        api_key=cfg.get("llm_api_key") or None,
        model=cfg.get("llm_model"),
        timeout=cfg.get("llm_timeout"),
    )
    # 开关以数据库为准（环境变量只作为初始值）
    provider.forced_enabled = bool(cfg.get("llm_enabled"))
    return provider
