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
