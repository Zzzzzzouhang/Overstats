"""Resolve on-disk sqlite paths, honoring OVERSTATS_DB_DIR for instance isolation.

When ``OVERSTATS_DB_DIR`` is set, every SQLite database used by the service is
placed under that directory instead of alongside this module. This lets a second
instance (e.g. a "副本 B") keep a fully independent database without touching the
code or the default ``config/config.py``.
"""
from __future__ import annotations

import os
from pathlib import Path


def resolve_db_path(filename: str) -> Path:
    db_dir = os.environ.get("OVERSTATS_DB_DIR")
    if db_dir and db_dir.strip():
        base = Path(db_dir.strip())
    else:
        base = Path(__file__).resolve().parent
    return base / filename


def resolve_metrics_db_path(filename: str) -> Path:
    """观测类数据库（request_metrics）单独寻址，便于多开时各实例写各自的文件。

    优先级：OVERSTATS_METRICS_DB_DIR → OVERSTATS_DB_DIR → src/db。
    业务库（match_stats / shiqu_llm / leaderboard）仍由 resolve_db_path 决定。
    """
    metrics_dir = os.environ.get("OVERSTATS_METRICS_DB_DIR")
    if metrics_dir and metrics_dir.strip():
        return Path(metrics_dir.strip()) / filename
    return resolve_db_path(filename)
