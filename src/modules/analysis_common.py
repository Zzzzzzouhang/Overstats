from __future__ import annotations

import os
from typing import Any, Dict, Optional

import asyncio
import httpx

# 跨模块共享的 LLM 并发信号量：shiqu 与 court 共用这些槽位，
# 使对外部 LLM 端点的同时调用总数受单一上限约束（默认 4，可用
# OVERSTATS_LLM_MAX_CONCURRENT 调整）。shiqu 的 LLM 阶段不占用
# dashen_request_queue 槽位，多个请求可同时进入该阶段并行发送，
# 因此该上限即"往 LLM 同时发送"的最大并发。
LLM_SEMAPHORE = asyncio.Semaphore(
    max(1, int(os.getenv("OVERSTATS_LLM_MAX_CONCURRENT", "4") or 4))
)

try:
    from overstats.config import config as app_config
except ModuleNotFoundError:
    from config import config as app_config


def _normalized_base_url(base_url: str) -> str:
    return str(base_url or "").strip().lower().rstrip("/")


def should_use_analysis_proxy(base_url: str) -> bool:
    normalized = _normalized_base_url(base_url)
    return normalized.startswith("https://api.openai.com/v1") or normalized.startswith(
        "https://generativelanguage.googleapis.com/v1beta/openai"
    )


def get_analysis_proxy(base_url: str) -> str:
    if not should_use_analysis_proxy(base_url):
        return ""
    return str(getattr(app_config, "ANALYSIS_PROXY", "") or "").strip()


def build_async_client(*, timeout: float, proxy_url: str = "", **kwargs: Any) -> httpx.AsyncClient:
    client_kwargs: Dict[str, Any] = dict(kwargs)
    client_kwargs["timeout"] = timeout
    if proxy_url:
        try:
            return httpx.AsyncClient(proxy=proxy_url, **client_kwargs)
        except TypeError:
            return httpx.AsyncClient(proxies=proxy_url, **client_kwargs)
    return httpx.AsyncClient(**client_kwargs)
