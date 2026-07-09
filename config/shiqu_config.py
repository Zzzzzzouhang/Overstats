from __future__ import annotations

"""是区吗（shiqu）功能的独立 LLM 配置管理。

该模块与项目主分析 LLM（ANALYSIS_BASE_URL / ANALYSIS_API_KEY）以及 config/config.py
完全解耦，不读取 config.py 中的任何 SHIQU_LLM_* 属性。所有配置仅来自环境变量
OVERSTATS_SHIQU_LLM_*，便于单独切换模型、限流或预算，且不污染主配置。
"""

import os
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ShiquLLMConfig:
    # LLM 服务根地址（仅从此值自动拼接 /chat/completions，无需手写完整路径）。
    # 示例：
    #   "https://api.openai.com/v1"
    #   "https://api.deepseek.com"
    #   "http://127.0.0.1:8000/v1"
    base_url: ""

    # LLM 服务 API Key（对应 Authorization: Bearer <api_key>）。
    # 示例："sk-xxxxxxxxxxxxxxxxxxxxxxxx"
    api_key: "replace-with-your-analysis-api-key"

    # 模型名（具体取值取决于所用服务）。
    # 示例："gpt-4o-mini" / "deepseek-chat" / "qwen2.5-72b-instruct" "deepseek-v4-flash"
    model: ""

    # 是否使用 SSE 流式响应；关闭时走普通 JSON 一次性返回。默认 True。
    stream: bool = True

    # 单次 LLM 请求超时（秒）。判定书 Prompt 较长，默认 300（5 分钟）。
    timeout_seconds: int = 300

    # LLM 失败重试次数。0 = 仅调用 1 次（不重试）；N = 初始 1 次 + 重试 N 次。
    retry: int = 0

    @property
    def chat_url(self) -> str:
        base = str(self.base_url or "").rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        if base.endswith("/v1") or base.endswith("/openai"):
            return f"{base}/chat/completions"
        return f"{base}/chat/completions"


def _get(name: str, default: str = "") -> str:
    """仅从环境变量 OVERSTATS_{name} 读取，不回退到 config.py。"""
    env_value = os.getenv(f"OVERSTATS_{name}")
    if env_value:
        return env_value.strip()
    return str(default or "").strip()


def _parse_bool(value: str, default: bool = True) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "on"}


def get_shiqu_llm_config() -> ShiquLLMConfig:
    """返回是区吗功能使用的独立 LLM 配置。"""
    return ShiquLLMConfig(
        base_url=_get("SHIQU_LLM_BASE_URL"),
        api_key=_get("SHIQU_LLM_API_KEY"),
        model=_get("SHIQU_LLM_MODEL"),
        stream=_parse_bool(_get("SHIQU_LLM_STREAM", "true")),
        timeout_seconds=int(_get("SHIQU_LLM_TIMEOUT", "300") or 300),
        retry=int(_get("SHIQU_LLM_RETRY", "0") or 0),
    )


def is_shiqu_llm_configured() -> bool:
    cfg = get_shiqu_llm_config()
    return bool(cfg.base_url and cfg.api_key and cfg.model)


def get_shiqu_match_count(default: int = 12) -> int:
    try:
        return max(2, min(25, int(_get("SHIQU_MATCH_COUNT", str(default)) or default)))
    except (TypeError, ValueError):
        return default
