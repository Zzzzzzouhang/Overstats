"""Shared helpers for LLM (analysis) endpoint calls."""
from __future__ import annotations

import asyncio
import os
from typing import Any, Dict

import httpx

try:
    from overstats.config import config as app_config
except ModuleNotFoundError:
    from config import config as app_config


def _read_env_int(name: str, default: int) -> int:
    raw = str(os.getenv(name, "") or "").strip()
    if not raw:
        return int(default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return int(default)


# 外部 LLM 端点的并发上限。
# 是区吗（shiqu）使用独立信号量，不与开庭 / 单局锐评抢槽位。
LLM_SEMAPHORE = asyncio.Semaphore(
    max(1, _read_env_int("OVERSTATS_LLM_MAX_CONCURRENT", 4))
)
SHIQU_LLM_SEMAPHORE = asyncio.Semaphore(
    max(1, _read_env_int("OVERSTATS_SHIQU_LLM_MAX_CONCURRENT", 6))
)


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
