import importlib.util
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# 可选：用环境变量 OVERSTATS_CONFIG_FILE 指定一份「独立配置文件」，覆盖默认
# 的 config/config.py。副本 B 启动时只需在命令行设置该变量即可，无需改动代码。
# 覆盖文件只需定义想要修改的大写变量，其余项继续沿用 config.py 的默认值。
# 解析顺序：绝对路径 -> 相对于当前工作目录 -> 相对于本配置包目录。
# ---------------------------------------------------------------------------
from . import config as _default_config

_OVERRIDE_PATH = os.environ.get("OVERSTATS_CONFIG_FILE")
if _OVERRIDE_PATH and _OVERRIDE_PATH.strip():
    _raw = _OVERRIDE_PATH.strip()
    _candidate = Path(_raw)
    if not _candidate.is_absolute():
        _candidate = Path.cwd() / _candidate
    if not _candidate.exists():
        _candidate = Path(__file__).resolve().parent / _raw
    if not _candidate.exists():
        raise RuntimeError(f"OVERSTATS_CONFIG_FILE 指定的配置文件不存在: {_OVERRIDE_PATH}")
    _spec = importlib.util.spec_from_file_location("_overstats_config_override", str(_candidate))
    _override_module = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_override_module)
    for _name, _value in vars(_override_module).items():
        if _name.isupper():
            setattr(_default_config, _name, _value)
    # 兼容两种运行方式（python -m overstats.run / python run.py）
    sys.modules.setdefault("overstats.config.config", _default_config)
    sys.modules.setdefault("config.config", _default_config)

from .loader import (
    APIConfig,
    DashenClientConfig,
    DashenCredentialConfig,
    get_api_config,
    get_dashen_client_config,
    is_database_write_enabled,
)

__all__ = [
    "APIConfig",
    "DashenClientConfig",
    "DashenCredentialConfig",
    "get_api_config",
    "get_dashen_client_config",
    "is_database_write_enabled",
]
