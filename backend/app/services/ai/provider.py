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

    @property
    def enabled(self) -> bool:
        if not settings.llm_enabled:
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


def get_provider() -> LLMProvider:
    return LLMProvider()
