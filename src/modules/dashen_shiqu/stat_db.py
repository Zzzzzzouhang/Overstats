"""是区吗功能的分段参考数据读取。

复用项目现有的数据库访问层 `src.db.match_stats.IDPoolDB`（含表名常量与连接配置），
而不是自行建立 sqlite3 连接。仅对外提供归一化与「聚合参考文本」构造能力。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

try:
    from overstats.src.db.match_stats import IDPoolDB
    from overstats.src.modules.font_resolver import resolve_resource_dir
except ModuleNotFoundError:  # pragma: no cover
    from src.db.match_stats import IDPoolDB
    from src.modules.font_resolver import resolve_resource_dir


_NAME_MAP: Optional[Dict[str, str]] = None
_SKIP_GUIDS = {"603482350067646497", "603482350067648623"}  # 游戏时间、英雄获胜
_SKIP_TEXTS = {"英雄获胜", "累计游戏时间", "累积游戏时间"}

_HERO_AVG_PERCENT_KEYWORDS = ("率", "效率", "占比")
_HERO_AVG_PERCENT_TEXTS = {"英雄获胜"}
_HERO_AVG_RAW_VALUE_TEXTS = {"英雄获胜", "累计游戏时间", "累积游戏时间"}
_HERO_AVG_RAW_VALUE_GUIDS = {"603482350067646497", "603482350067648623"}
# 与 astrbot 版原始口径一致：竞技段位 + 快速(-1) 各占 50% 权重
_BROAD_REFERENCE_BUCKETS = (0, 4, 5, 6, 7, *range(25, 45))


def should_skip_prompt_stat(value_guid: str = "", value_text: str = "") -> bool:
    return str(value_guid or "") in _SKIP_GUIDS or str(value_text or "") in _SKIP_TEXTS


def _is_percent_stat(value_text: str) -> bool:
    text = str(value_text or "")
    return text in _HERO_AVG_PERCENT_TEXTS or any(kw in text for kw in _HERO_AVG_PERCENT_KEYWORDS)


def _is_raw_stat(value_text: str = "", value_guid: str = "") -> bool:
    return str(value_guid or "") in _HERO_AVG_RAW_VALUE_GUIDS or str(value_text or "") in _HERO_AVG_RAW_VALUE_TEXTS


def normalize_stat_value(value, user_time_sec: float, value_text: str = "", value_guid: str = "") -> Optional[float]:
    """归一化英雄统计值，与后端 normalize_dashen_hero_stat_value 等价。"""
    if value is None:
        return None
    try:
        v = float(value)
        ut = float(user_time_sec or 0)
    except (TypeError, ValueError):
        return None
    if _is_percent_stat(value_text):
        return max(0.0, min(1.0, v))
    if _is_raw_stat(value_text, value_guid):
        return v
    time_coef = ut / 600.0
    if time_coef <= 0:
        return None
    return v / time_coef


def load_stat_name_map() -> Dict[str, str]:
    global _NAME_MAP
    if _NAME_MAP is not None:
        return _NAME_MAP
    try:
        cfg_path = resolve_resource_dir() / "query_tool.json"
        cfg = __import__("json").loads(cfg_path.read_text("utf-8"))
        _NAME_MAP = {a["valueGuid"]: a["valueText"] for a in cfg.get("heroAttrList", [])}
    except Exception:
        _NAME_MAP = {}
    return _NAME_MAP


def build_broad_reference_text(
    db: Optional[IDPoolDB],
    player_name: str,
    hero_guid: str,
    hero_name: str,
) -> str:
    """为一局玩家构建聚合参考文本（竞技 + 快速各 50% 权重），复用 IDPoolDB.get_statmap_summary。"""
    if db is None:
        return ""
    name_map = load_stat_name_map()

    comp = db.get_statmap_summary(hero_guid, rank_scores=list(_BROAD_REFERENCE_BUCKETS)) or {}
    qpt = db.get_statmap_summary(hero_guid, group_by_rank=False) or {}

    comp_med: Dict[str, list[float]] = {}
    for (statmap_name, _rs), info in comp.items():
        name = name_map.get(str(statmap_name))
        if not name or should_skip_prompt_stat(value_guid=str(statmap_name), value_text=name):
            continue
        median = info.get("median")
        if median is None:
            continue
        comp_med.setdefault(name, []).append(float(median))

    qpt_med: Dict[str, float] = {}
    for (statmap_name, _rs), info in qpt.items():
        name = name_map.get(str(statmap_name))
        if not name or should_skip_prompt_stat(value_guid=str(statmap_name), value_text=name):
            continue
        median = info.get("median")
        if median is None:
            continue
        qpt_med[name] = float(median)

    all_names = set(comp_med) | set(qpt_med)
    parts: list[str] = []
    for name in all_names:
        vals: list[float] = []
        comp_vals = comp_med.get(name)
        if comp_vals:
            vals.append(sum(comp_vals) / len(comp_vals))
        qpt_val = qpt_med.get(name)
        if qpt_val is not None:
            vals.append(qpt_val)
        if not vals:
            continue
        med = sum(vals) / len(vals)
        parts.append(f"{name}{med:.1f}")

    if not parts:
        return ""
    return f"  {player_name}（{hero_name}）" + ", ".join(parts)
