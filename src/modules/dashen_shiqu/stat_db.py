"""是区吗功能的分段参考数据读取。

复用项目现有的数据库访问层 `src.db.match_stats.IDPoolDB`（含表名常量与连接配置），
而不是自行建立 sqlite3 连接。对外提供：
1. 统计值归一化（per-10min / 比率口径）；
2. 该英雄的分位参考分布（p2~p98）；
3. 把单场数值换算成 0-100 的「同英雄参考指数」（50=中位数、越大越好）。

提示词只发送指数，不再发送原始数值：原始治疗量/伤害这类大数值会让模型把注意力
放在量级上，而两端差异极大的指标（命中率 vs 治疗量）也无法用同一把尺子比较。
"""

from __future__ import annotations

import math
from typing import Dict, Optional

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

# 参考分布锚点：（百分位, comp_data_summary 列名, 内部短键）
_ANCHOR_COLUMNS = (
    (2, "bottom2", "p2"),
    (5, "bottom5", "p5"),
    (10, "bottom10", "p10"),
    (20, "bottom20", "p20"),
    (50, "median", "p50"),
    (80, "top20", "p80"),
    (90, "top10", "p90"),
    (95, "top5", "p95"),
    (98, "top2", "p98"),
)
# 样本过少时分位锚点噪声极大（下游就是几条记录），退化为「中位数比值」兜底。
# 线上 comp_data_summary 的 count 普遍在 1.5 万~5.5 万，这个门槛只对冷门英雄/新英雄生效。
_MIN_REF_SAMPLES = 30
# 参考分布按英雄缓存：同一局里同英雄会被反复查询，缓存命中率很高
_BANDS_CACHE: Dict[tuple, tuple] = {}
_BANDS_CACHE_MAX = 256


def should_skip_prompt_stat(value_guid: str = "", value_text: str = "") -> bool:
    return str(value_guid or "") in _SKIP_GUIDS or str(value_text or "") in _SKIP_TEXTS


def _is_percent_stat(value_text: str) -> bool:
    text = str(value_text or "")
    return text in _HERO_AVG_PERCENT_TEXTS or any(kw in text for kw in _HERO_AVG_PERCENT_KEYWORDS)


def is_rate_stat(value_text: str) -> bool:
    """比率类统计（命中率/暴击率/占比，取值天然落在 0-1）：分位插值用线性空间。"""
    return _is_percent_stat(value_text)


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


# ── 参考分布（分位锚点）──


def _collect_anchor_lists(rows, name_map: Dict[str, str]):
    """把 get_statmap_summary 的一批行整理成 {统计文本: {锚点键: [值...]}} 与样本数。"""
    anchors: Dict[str, Dict[str, list]] = {}
    counts: Dict[str, int] = {}
    for (statmap_name, _rank), info in (rows or {}).items():
        guid = str(statmap_name)
        name = name_map.get(guid)
        if not name or should_skip_prompt_stat(value_guid=guid, value_text=name):
            continue
        bucket = anchors.setdefault(name, {})
        for _pct, column, key in _ANCHOR_COLUMNS:
            raw = info.get(column)
            if raw is None:
                continue
            try:
                bucket.setdefault(key, []).append(float(raw))
            except (TypeError, ValueError):
                continue
        try:
            cnt = int(info.get("count") or 0)
        except (TypeError, ValueError):
            cnt = 0
        if cnt > counts.get(name, 0):
            counts[name] = cnt
    return anchors, counts


def _compute_reference_bands(db: IDPoolDB, hero_guid: str) -> Dict[str, Dict[str, float]]:
    name_map = load_stat_name_map()
    rows_grouped = db.get_statmap_summary(hero_guid, rank_scores=list(_BROAD_REFERENCE_BUCKETS)) or {}
    rows_overall = db.get_statmap_summary(hero_guid, group_by_rank=False) or {}
    sources = [_collect_anchor_lists(rows, name_map) for rows in (rows_grouped, rows_overall)]

    names = set()
    for anchors, _counts in sources:
        names |= set(anchors)

    bands: Dict[str, Dict[str, float]] = {}
    for name in names:
        merged: Dict[str, float] = {}
        for _pct, _column, key in _ANCHOR_COLUMNS:
            # 与旧版中位数合并口径一致：先在来源内取均值（竞技各段位），再对来源取均值（快速 50%）
            vals = [sum(lst) / len(lst) for anchors, _c in sources if (lst := anchors.get(name, {}).get(key))]
            if vals:
                merged[key] = sum(vals) / len(vals)
        if merged:
            merged["count"] = float(max((counts.get(name, 0) for _a, counts in sources), default=0))
            bands[name] = merged
    return bands


def build_reference_bands(db: Optional[IDPoolDB], hero_guid: str) -> Dict[str, Dict[str, float]]:
    """返回 {统计文本: {"p2"..."p98", "count"}}：该英雄的分位参考分布。

    与旧版 build_broad_reference_map 同源同权重（竞技分桶 + 快速整体各 50%），
    额外保留上/下分位锚点，供 stat_index 做百分位插值。结果按英雄缓存。
    """
    if db is None:
        return {}
    hero_guid = str(hero_guid or "")
    if not hero_guid:
        return {}
    cache_key = (id(db), hero_guid)
    cached = _BANDS_CACHE.get(cache_key)
    if cached is not None and cached[0] is db:
        return cached[1]
    bands = _compute_reference_bands(db, hero_guid)
    if len(_BANDS_CACHE) >= _BANDS_CACHE_MAX:
        _BANDS_CACHE.clear()
    _BANDS_CACHE[cache_key] = (db, bands)
    return bands


# ── 数值 → 参考指数 ──


def _interp_percentile(value: float, bands: Dict[str, float], log_scale: bool) -> Optional[float]:
    """在分位锚点之间分段线性插值，返回估计百分位 0-100。"""
    pts = [(pct, float(bands[key])) for pct, _column, key in _ANCHOR_COLUMNS if bands.get(key) is not None]
    if len(pts) < 3:
        return None

    # 小样本下分位数可能交叉（p80 > p90），强制单调避免插值出现负斜率
    mono: list = []
    for pct, val in pts:
        val = max(val, 0.0)
        if mono and val < mono[-1][1]:
            val = mono[-1][1]
        mono.append((pct, val))

    def _norm(x: float) -> float:
        return math.log1p(max(x, 0.0)) if log_scale else x

    if value <= mono[0][1]:
        return float(mono[0][0])
    if value >= mono[-1][1]:
        return float(mono[-1][0])
    for (p0, v0), (p1, v1) in zip(mono, mono[1:]):
        if value > v1:
            continue
        x0, x1 = _norm(v0), _norm(v1)
        if x1 - x0 <= 1e-12:  # 平段（多数玩家都是 0）：直接落到上端锚点
            return float(p1)
        return p0 + (_norm(value) - x0) / (x1 - x0) * (p1 - p0)
    return float(mono[-1][0])


def _ratio_percentile(value: float, bands: Optional[Dict[str, float]], log_scale: bool) -> Optional[float]:
    """无可用锚点时的兜底：中位数比值经 tanh 压缩到 0-100，比值再离谱也不会爆炸。"""
    if not bands:
        return None
    ref = bands.get("p50")
    if ref is None:
        return None
    ref = float(ref)
    if ref <= 0:
        return 50.0 if value <= 0 else 60.0
    if value <= 0:
        return 2.0
    compress = 1.5 if log_scale else 1.0
    return 50.0 + 50.0 * math.tanh(compress * math.log(value / ref))


def stat_index(
    value,
    bands: Optional[Dict[str, float]],
    *,
    reverse: bool = False,
    log_scale: bool = False,
) -> Optional[int]:
    """把单场数值换算成 0-100 的「同英雄参考指数」：50=中位数、100=顶尖、0=垫底。

    优先用分位锚点做分段线性插值（log_scale=True 时在 log1p 空间插值，适配治疗量这类
    长尾计数；比率类走线性空间）；样本不足或没有锚点时退化为「中位数比值 + tanh 压缩」。
    reverse=True 用于阵亡等「越低越好」的项，返回 100-百分位，使所有指数统一成越大越好。
    无法换算（无参考）返回 None，由调用方决定如何标注。
    """
    if value is None:
        return None
    try:
        val = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(val):
        return None
    val = max(val, 0.0)

    pct = None
    if bands and float(bands.get("count") or 0) >= _MIN_REF_SAMPLES:
        pct = _interp_percentile(val, bands, log_scale)
    if pct is None:
        pct = _ratio_percentile(val, bands, log_scale)
    if pct is None:
        return None
    if reverse:
        pct = 100.0 - pct
    return max(0, min(100, int(round(pct))))


# ── 兼容旧接口（只取中位数）──


def build_broad_reference_map(
    db: Optional[IDPoolDB],
    hero_guid: str,
    present_guids: Optional[set] = None,
) -> Dict[str, float]:
    """返回 {统计文本: 中位数}。present_guids 给定时只保留真实出现过的统计项。"""
    bands = build_reference_bands(db, hero_guid)
    if not bands:
        return {}
    if present_guids is None:
        return {name: info["p50"] for name, info in bands.items() if info.get("p50") is not None}
    name_map = load_stat_name_map()
    out: Dict[str, float] = {}
    for guid in present_guids:
        name = name_map.get(str(guid))
        info = bands.get(name) if name else None
        if info and info.get("p50") is not None:
            out[name] = info["p50"]
    return out


def build_broad_reference_text(
    db: Optional[IDPoolDB],
    player_name: str,
    hero_guid: str,
    hero_name: str,
    present_guids: Optional[set] = None,
) -> str:
    """构建「玩家（英雄）统计项中位数...」的单行参考文本（开庭等模块在用）。"""
    ref_map = build_broad_reference_map(db, hero_guid, present_guids=present_guids)
    if not ref_map:
        return ""
    parts = [f"{name}{med:.1f}" for name, med in ref_map.items()]
    return f"  {player_name}（{hero_name}）" + ", ".join(parts)
