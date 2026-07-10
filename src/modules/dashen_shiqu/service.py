"""是区吗（shiqu）业务逻辑模块。

复刻 astrbot 插件 shiqu.py 的核心能力：
1. 复用项目内部 dashen_match / bnet_search 模块抓取最近预设/6v6 对局与队友数据；
2. 构建与原始一致的脱口秀式毒舌点评 Prompt（含分段参考，复用 IDPoolDB）；
3. 调用【独立 LLM】（配置见 config/shiqu_config.py），解析结构化结果；
4. 通过 render.py 用 PIL 渲染与原 HTML 视觉一致的判定书图片。

仅保留与判定生成相关的核心逻辑，去除 AstrBot 平台的队列/限流/封禁/冷却等机器人管理特性。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from overstats.config.shiqu_config import (
        get_shiqu_llm_config,
        get_shiqu_match_count,
        is_shiqu_llm_configured,
    )
    from overstats.src.db.match_stats import IDPoolDB
    from overstats.src.modules.dashen_match.render import _extract_match_detail_data
    from overstats.src.modules.dashen_match.requests import DashenMatchQuery
    from overstats.src.modules.dashen_match.service import dashen_match_module
    from overstats.src.modules.errors import ModuleError
    from overstats.src.modules.font_resolver import resolve_resource_dir
    from overstats.src.db.shiqu_llm import shiqu_llm_recorder
    from .stat_db import (
        build_broad_reference_text,
        load_stat_name_map,
        normalize_stat_value,
        should_skip_prompt_stat,
    )
    from ..llm_call_status import shiqu_llm_status
except ModuleNotFoundError:  # pragma: no cover
    from config.shiqu_config import (
        get_shiqu_llm_config,
        get_shiqu_match_count,
        is_shiqu_llm_configured,
    )
    from src.db.match_stats import IDPoolDB
    from src.modules.dashen_match.render import _extract_match_detail_data
    from src.modules.dashen_match.requests import DashenMatchQuery
    from src.modules.dashen_match.service import dashen_match_module
    from src.modules.errors import ModuleError
    from src.modules.font_resolver import resolve_resource_dir
    from src.db.shiqu_llm import shiqu_llm_recorder
    from .stat_db import (
        build_broad_reference_text,
        load_stat_name_map,
        normalize_stat_value,
        should_skip_prompt_stat,
    )
    from src.modules.llm_call_status import shiqu_llm_status


logger = logging.getLogger("overstats.shiqu")


# ── 从 query_tool.json 加载游戏数据 ──
_QTOOL_PATH = resolve_resource_dir() / "query_tool.json"
try:
    _QTOOL = json.loads(_QTOOL_PATH.read_text("utf-8"))
except Exception:
    _QTOOL = {}
HERO_DICT = {h["heroGuid"]: {"name": h["name"], "role": h["roleType"]} for h in _QTOOL.get("heroList", [])}
MAP_DICT = {m["guid"]: m["name"] for m in _QTOOL.get("mapList", [])}

_ATTR_TEXT_TO_GUID: Dict[str, str] = {}
_ATTR_GUID_TO_TEXT: Dict[str, str] = {}
_HERO_NAME_TO_GUID: Dict[str, str] = {}
for _attr in _QTOOL.get("heroAttrList", []):
    _vg, _vt = str(_attr.get("valueGuid", "")), str(_attr.get("valueText", ""))
    if _vg and _vt:
        _ATTR_TEXT_TO_GUID[_vt] = _vg
        _ATTR_GUID_TO_TEXT[_vg] = _vt
for _h in _QTOOL.get("heroList", []):
    _hn, _hg = str(_h.get("name", "")), str(_h.get("heroGuid", ""))
    if _hn and _hg:
        _HERO_NAME_TO_GUID[_hn] = _hg

_ALLOWED_COMMON_TEXTS = {
    "消灭", "阵亡", "单独消灭", "最后一击",
    "武器命中率", "暴击命中率",
}
_ALLOWED_SPECIAL_BY_HERO: Dict[str, set] = {
    'D.Va': set(), '伊拉锐': {'治疗量'}, '半藏': set(), '卡西迪': {'暴击命中率', '武器命中率'},
    '卢西奥': {'拯救玩家', '治疗量'}, '回声': {'黏性炸弹直接命中率'}, '埃姆雷': {'暴击命中率', '武器命中率'},
    '堡垒': set(), '士兵\uff1a76': {'螺旋飞弹命中率'}, '天使': {'复活玩家', '拯救玩家', '治疗量'},
    '奥丽莎': {'能量标枪命中率'}, '安娜': {'开镜命中率', '拯救玩家', '麻醉镖命中率', '治疗量'},
    '安燃': set(), '巴蒂斯特': {'拯救玩家', '治疗命中率', '治疗量'},
    '布丽吉塔': {'流星飞锤命中率', '鼓舞士气持续时间占比', '治疗量'}, '弗蕾娅': set(), '托比昂': set(),
    '拉玛刹': {'猛拳命中率'}, '探奇': {'直接命中率'}, '斩仇': {'锋锐剑气命中率'}, '无漾': {'治疗量'},
    '末日铁拳': set(), '朱诺': {'拯救玩家', '治疗量'}, '查莉娅': {'主要攻击模式命中率', '辅助攻击模式命中率'},
    '死怨': {'交叉枪决命中率', '纵情狂飙空中发射命中率'}, '死神': set(), '毛加': set(),
    '法老之鹰': {'击退消灭', '直接命中率'}, '渣客女王': {'锯齿利刃命中率'}, '温斯顿': {'辅助攻击模式命中率'},
    '源氏': set(), '狂鼠': {'直接命中率'}, '猎空': {'脉冲炸弹命中率'}, '瑞稀': {'缚魂锁链命中率', '治疗量'},
    '生命之梭': {'拯救玩家', '治疗量'}, '破坏球': set(), '禅雅塔': {'拯救玩家', '治疗量'},
    '秩序之光': {'辅助攻击模式命中率'}, '索杰恩': {'充能射击命中率', '充能射击暴击率'},
    '美': {'冰锥命中率', '冰锥暴击率'}, '艾什': {'开镜命中率', '开镜暴击率'}, '莫伊拉': {'拯救玩家', '治疗量'},
    '莱因哈特': {'烈焰打击命中率'}, '西拉': {'追踪弹命中率'}, '西格玛': {'质量吸附命中率'},
    '路霸': {'链钩命中率'}, '金驭': set(), '雾子': {'拯救玩家', '治疗量'}, '飞天猫': {'治疗量'},
    '骇灾': set(), '黑影': set(), '黑百合': {'开镜暴击率'},
}

_ALLOWED_COMMON_GUIDS = {_ATTR_TEXT_TO_GUID[t] for t in _ALLOWED_COMMON_TEXTS if t in _ATTR_TEXT_TO_GUID}
_HERO_ATTR_GUIDS: Dict[str, set] = {}
_HERO_SPECIAL_ATTR_GUIDS: Dict[str, set] = {}
_GENERAL_ATTR_GUIDS = _ALLOWED_COMMON_GUIDS
for _hero_name, _allowed in _ALLOWED_SPECIAL_BY_HERO.items():
    _hero_guid = _HERO_NAME_TO_GUID.get(_hero_name)
    if not _hero_guid:
        continue
    _special = {_ATTR_TEXT_TO_GUID[t] for t in _allowed if t in _ATTR_TEXT_TO_GUID}
    _HERO_SPECIAL_ATTR_GUIDS[_hero_guid] = _special
    _HERO_ATTR_GUIDS[_hero_guid] = _ALLOWED_COMMON_GUIDS | _special


def _stat_allowed_for_hero(value_guid: str, hero_guid: str) -> bool:
    return value_guid in _GENERAL_ATTR_GUIDS or value_guid in _HERO_ATTR_GUIDS.get(hero_guid, set())


def _infer_hero_guid_from_stat_map(stat_map: dict, fallback_hero_guid: str = "", *, allow_fallback: bool = False) -> str:
    stat_guids = {str(g) for g in (stat_map or {}).keys()}
    scores = []
    for hero_guid, special_guids in _HERO_SPECIAL_ATTR_GUIDS.items():
        hits = len(stat_guids & special_guids)
        if hits > 0:
            scores.append((hits, hero_guid))
    if scores:
        best = max(hit for hit, _ in scores)
        winners = [hg for hit, hg in scores if hit == best]
        if fallback_hero_guid in winners:
            return fallback_hero_guid
        if len(winners) == 1:
            return winners[0]
        return ""
    return fallback_hero_guid if allow_fallback else ""


_SHIQU_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["target_id", "score", "summary", "match_comments", "overall_comment", "teammate_comments"],
    "properties": {
        "target_id": {"type": "string"},
        "score": {"type": "integer", "minimum": 0, "maximum": 100},
        "summary": {"type": "string", "minLength": 1},
        "match_comments": {
            "type": "array", "minItems": 1,
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["index", "result", "hero", "comment"],
                "properties": {
                    "index": {"type": "integer", "minimum": 1},
                    "result": {"type": "string", "enum": ["胜", "负", "平", "未知"]},
                    "hero": {"type": "string"},
                    "comment": {"type": "string", "minLength": 1},
                },
            },
        },
        "overall_comment": {"type": "string", "minLength": 1},
        "teammate_comments": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["name", "games", "score", "comment"],
                "properties": {
                    "name": {"type": "string"},
                    "games": {"type": "integer", "minimum": 1},
                    "score": {"type": "integer", "minimum": 0, "maximum": 100},
                    "comment": {"type": "string", "minLength": 1},
                },
            },
        },
    },
}

_VERDICT_RULES = [
    {"score_min": 83, "labels": ("你是职业吗？",), "canonical": "你是职业吗？", "emoji": "😱", "class": "god"},
    {"score_min": 75, "labels": ("来了，暴力炸！",), "canonical": "来了，暴力炸！", "emoji": "🤤", "class": "boom"},
    {"score_min": 68, "labels": ("化蛹成蝶（？）",), "canonical": "化蛹成蝶（？）", "emoji": "🦋", "class": "butterfly"},
    {"score_min": 60, "labels": ("恭喜，你不是区！", "恭喜，你不是区"), "canonical": "恭喜，你不是区！", "emoji": "😂", "class": "ok"},
    {"score_min": 52, "labels": ("不幸，你可能是区？",), "canonical": "不幸，你可能是区？", "emoji": "🤔", "class": "mid"},
    {"score_min": 43, "labels": ("哦灭跌多，你就是区！", "哦灭跌多，你就是区"), "canonical": "哦灭跌多，你就是区！", "emoji": "🎉", "class": "bad"},
    {"score_min": 0, "labels": ("你个大区！！！",), "canonical": "你个大区！！！", "emoji": "😡", "class": "terrible"},
]
_VERDICT_BY_LABEL = {label: rule for rule in _VERDICT_RULES for label in rule["labels"]}

_PRESET_MODES = {"SportPreset", "LeisurePreset", "Sport6v6", "Leisure6v6"}


# ── 结构化结果解析 ──

def _clamp_score(value, default: int = 0) -> int:
    try:
        score = int(round(float(value)))
    except Exception:
        score = default
    return max(0, min(100, score))


def _score_rule(score: int) -> dict:
    for rule in _VERDICT_RULES:
        if score >= int(rule["score_min"]):
            return rule
    return _VERDICT_RULES[-1]


def _normalize_result(data: dict, target_id: str) -> dict:
    score = _clamp_score(data.get("score"), 0)
    result = {
        "target_id": str(data.get("target_id") or target_id),
        "score": score,
        "verdict": _score_rule(score)["canonical"],
        "summary": str(data.get("summary") or "暂无数据概况。").strip(),
        "match_comments": [],
        "overall_comment": str(data.get("overall_comment") or "暂无综合评价。").strip(),
        "teammate_comments": [],
    }
    for i, item in enumerate(data.get("match_comments") or [], start=1):
        if not isinstance(item, dict):
            continue
        result["match_comments"].append({
            "index": _clamp_score(item.get("index"), i),
            "result": str(item.get("result") or "未知"),
            "hero": str(item.get("hero") or "未知英雄"),
            "comment": str(item.get("comment") or "暂无点评。").strip(),
        })
    for item in data.get("teammate_comments") or []:
        if not isinstance(item, dict):
            continue
        tm_score = _clamp_score(item.get("score"), 0)
        teammate = {
            "name": str(item.get("name") or "未知队友"),
            "score": tm_score,
            "verdict": _score_rule(tm_score)["canonical"],
            "comment": str(item.get("comment") or "暂无点评。").strip(),
        }
        if item.get("games") is not None:
            teammate["games"] = max(1, _clamp_score(item.get("games"), 1))
        result["teammate_comments"].append(teammate)
    return result


def _repair_json(text: str) -> str:
    result = []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if not in_string:
            result.append(ch)
            if ch == '"' and (i == 0 or text[i - 1] != '\\'):
                in_string = True
        else:
            if ch == '\\' and i + 1 < n:
                result.append(text[i:i + 2])
                i += 1
            elif ch == '"':
                rest = text[i + 1:].lstrip()
                if not rest or rest[0] in ',:}]':
                    in_string = False
                else:
                    ch = '\\"'
                result.append(ch)
            else:
                result.append(ch)
        i += 1
    return ''.join(result)


def _repair_json_structure(text: str) -> str:
    text = re.sub(r'"\s*\]\s*,(\s*")', r'",\1', text)
    text = re.sub(r'"\s*\[\s*,(\s*")', r'",\1', text)
    text = re.sub(r',\s*([}\]])', r'\1', text)
    return text


def _repair_json_values(text: str) -> str:
    text = re.sub(r'"index"\s*:\s*(?!\s*\d+\s*[,}\]])[^,\}\]]+', '"index": 1', text)
    text = re.sub(r'"score"\s*:\s*(?!\s*\d+\s*[,}\]])[^,\}\]]+', '"score": 50', text)
    text = re.sub(r'"games"\s*:\s*(?!\s*\d+\s*[,}\]])[^,\}\]]+', '"games": 1', text)
    return text


def _extract_json_object(text: str) -> Optional[dict]:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    attempts = [
        cleaned,
        _repair_json(cleaned),
        _repair_json_structure(cleaned),
        _repair_json_values(cleaned),
        _repair_json(_repair_json_structure(cleaned)),
        _repair_json(_repair_json_values(cleaned)),
        _repair_json_structure(_repair_json_values(cleaned)),
    ]
    for attempt in attempts:
        try:
            data = json.loads(attempt)
            return data if isinstance(data, dict) else None
        except Exception:
            pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        attempts = [
            cleaned[start:end + 1],
            _repair_json(cleaned[start:end + 1]),
            _repair_json_structure(cleaned[start:end + 1]),
            _repair_json_values(cleaned[start:end + 1]),
            _repair_json(_repair_json_structure(cleaned[start:end + 1])),
            _repair_json(_repair_json_values(cleaned[start:end + 1])),
            _repair_json_structure(_repair_json_values(cleaned[start:end + 1])),
        ]
        for attempt in attempts:
            try:
                data = json.loads(attempt)
                return data if isinstance(data, dict) else None
            except Exception:
                pass
    return None


def _parse_llm_json_result(raw_text: str, target_id: str) -> Optional[dict]:
    data = _extract_json_object(raw_text)
    if data is None:
        return None
    return _normalize_result(data, target_id)


# ── Prompt 构建（与原始 shiqu.py 一致）──

def _build_prompt(matches: list, target_id: str, db: Optional[IDPoolDB] = None) -> str:
    ROLE_ORDER = {"tank": 0, "dps": 1, "healer": 2}
    ROLE_LABEL = {"tank": "坦克", "dps": "输出", "healer": "辅助"}

    def _get_role(p):
        return HERO_DICT.get(str(p.get("heroGuid", "")), {}).get("role", "unknown")

    def _sort_and_label(players):
        indexed = [(ROLE_ORDER.get(_get_role(p), 9), i, p) for i, p in enumerate(players) if isinstance(p, dict)]
        indexed.sort(key=lambda x: (x[0], x[1]))
        role_ct = {}
        labeled = []
        for _, _, p in indexed:
            r = _get_role(p)
            role_ct[r] = role_ct.get(r, 0) + 1
            lb = ROLE_LABEL.get(r, r)
            ct = sum(1 for pp in players if isinstance(pp, dict) and _get_role(pp) == r)
            labeled.append((p, f"{lb}{role_ct[r]}" if ct > 1 else lb))
        return labeled

    STAT_KEYS = [("kill", "击杀"), ("assist", "助攻"), ("death", "阵亡"), ("finalHit", "最后一击"),
                 ("heroDamage", "伤害"), ("damageTaken", "承伤"), ("cure", "治疗"),
                 ("healingTaken", "受疗"), ("resistDamage", "格挡")]
    # 击杀参与率 = (原始击杀 + 原始助攻) / 敌方原始总死亡数（均用本局原始数据，
    # 不做 10 分钟归一化）。
    KILL_GUID = "603482350067646495"
    ASSIST_GUID = "603482350067648392"
    ENTRY_STAT_GUIDS = [
        ("击杀", ("603482350067646495",)),
        ("阵亡", ("603482350067646506",)),
        ("最后一击", ("603482350067646507",)),
        ("单独消灭", ("603482350067646509",)),
        ("伤害", ("603482350067647671",)),
        ("治疗", ("603482350067647479", "603482350067646913")),
    ]

    def _rate(v, t):
        return f"{v * 600 / max(t, 60):.1f}" if t > 0 else str(v)

    def _fmt_num(v):
        if v is None:
            return "?"
        if isinstance(v, (int, float)) and float(v) == int(v):
            return str(int(v))
        return f"{float(v):.2f}"

    def _fmt_minsec(seconds):
        seconds = max(0, int(seconds or 0))
        return f"{seconds // 60:02d}:{seconds % 60:02d}"

    def _normalized_stat(sm, guids, ut, name_map, hero_guid):
        values = []
        for guid in guids:
            if guid not in sm or not _stat_allowed_for_hero(guid, hero_guid):
                continue
            name = name_map.get(guid, "")
            if should_skip_prompt_stat(value_guid=guid, value_text=name):
                continue
            nv = normalize_stat_value(sm.get(guid), ut, value_text=name, value_guid=guid)
            if nv is not None:
                values.append(nv)
        if not values:
            return None
        return max(values)

    def _hero_detail_text(entry, hero_guid, name_map):
        sm = entry.get("statMap", {}) or {}
        ut = float(entry.get("userTimeSec", 600) or 600)
        seen = {}
        for guid, raw_val in sm.items():
            guid = str(guid)
            if not _stat_allowed_for_hero(guid, hero_guid):
                continue
            name = name_map.get(guid)
            if not name:
                continue
            if should_skip_prompt_stat(value_guid=guid, value_text=name):
                continue
            nv = normalize_stat_value(raw_val, ut, value_text=name, value_guid=guid)
            if nv is None:
                continue
            seen.setdefault(name, _fmt_num(nv))
        return ", ".join(f"{name}: {value}" for name, value in seen.items())

    def _expand_player_segments(p):
        name_map = load_stat_name_map()
        hl = p.get("_heroList")
        fallback_hg = str(p.get("heroGuid", ""))
        if not hl or not isinstance(hl, list):
            return [{"player": p, "hero_guid": fallback_hg, "entry": None, "name_map": name_map}]
        segments = []
        long_entries = [entry for entry in hl if isinstance(entry, dict) and float(entry.get("userTimeSec", 0) or 0) >= 60]
        for entry in long_entries:
            if not isinstance(entry, dict):
                continue
            hg = str(entry.get("heroId", ""))
            if hg:
                segments.append({"player": p, "hero_guid": hg, "entry": entry, "name_map": name_map})
                continue
            sm = entry.get("statMap", {}) or {}
            hg = _infer_hero_guid_from_stat_map(sm, fallback_hg, allow_fallback=(len(long_entries) == 1))
            if not hg:
                continue
            segments.append({"player": p, "hero_guid": hg, "entry": entry, "name_map": name_map})
        if segments:
            return segments
        return []

    def _get_segment_role(seg):
        return HERO_DICT.get(str(seg.get("hero_guid", "")), {}).get("role", "unknown")

    def _player_primary_role(player_segments, fallback_player):
        best_role = HERO_DICT.get(str(fallback_player.get("heroGuid", "")), {}).get("role", "unknown")
        best_time = -1.0
        for seg in player_segments:
            entry = seg.get("entry") or {}
            ut = float(entry.get("userTimeSec", 0) or 0)
            role = _get_segment_role(seg)
            if ut > best_time:
                best_time = ut
                best_role = role
        return best_role

    def _sort_and_label_players(players):
        indexed = []
        for pi, p in enumerate(players):
            if not isinstance(p, dict):
                continue
            segments = _expand_player_segments(p)
            if not segments:
                continue
            role = _player_primary_role(segments, p)
            indexed.append((ROLE_ORDER.get(role, 9), pi, role, p, segments))
        indexed.sort(key=lambda x: (x[0], x[1]))
        role_ct = {}
        role_total = {}
        for _, _, role, _, _ in indexed:
            role_total[role] = role_total.get(role, 0) + 1
        labeled = []
        for _, _, role, p, segments in indexed:
            role_ct[role] = role_ct.get(role, 0) + 1
            lb = ROLE_LABEL.get(role, role)
            labeled.append((p, segments, f"{lb}{role_ct[role]}" if role_total.get(role, 0) > 1 else lb))
        return labeled

    def _fmt_hero_segment(seg, include_detail):
        p = seg["player"]
        hg = str(seg.get("hero_guid", ""))
        hn = HERO_DICT.get(hg, {}).get("name", "?")
        entry = seg.get("entry")
        parts = [f"英雄: {hn}"]
        if entry:
            ut = float(entry.get("userTimeSec", 0) or 0)
            sm = entry.get("statMap", {}) or {}
            parts.append(f"时长: {_fmt_minsec(ut)}")
            for cn, guids in ENTRY_STAT_GUIDS:
                nv = _normalized_stat(sm, guids, ut, seg["name_map"], hg)
                if nv is not None:
                    parts.append(f"{cn}: {_fmt_num(nv)}")
            detail = _hero_detail_text(entry, hg, seg["name_map"]) if include_detail else ""
        else:
            for k, cn in STAT_KEYS:
                v = int(p.get(k, 0) or 0)
                parts.append(f"{cn}: {_rate(v, game_sec)}")
            detail = ""
        if include_detail and detail:
            parts.append(f"详细: {{ {detail} }}")
        return "{ " + ", ".join(parts) + " }"

    def _fmt_player_block(p, segments, pos, game_sec, include_detail, enemy_total_deaths=0):
        name = str(p.get("name", "?"))
        display = f"*{name}" if name == target_id else name
        player_total_kills = 0.0
        for seg in segments:
            entry = seg.get("entry")
            if entry:
                # 原始击杀 + 原始助攻：直接取 statMap 中的原始值，不做 10 分钟归一化
                sm = entry.get("statMap", {}) or {}
                for g in (KILL_GUID, ASSIST_GUID):
                    raw = sm.get(g)
                    if raw is not None:
                        try:
                            player_total_kills += float(raw)
                        except (TypeError, ValueError):
                            pass
            else:
                player_total_kills += int(p.get("kill", 0) or 0) + int(p.get("assist", 0) or 0)
        kp_rate = player_total_kills / enemy_total_deaths if enemy_total_deaths > 0 else 0
        hero_text = ", ".join(_fmt_hero_segment(seg, include_detail) for seg in segments)
        return f"{{ 位置: {pos}, 玩家: {display}, 击杀参与率: {kp_rate:.3f}, 英雄片段: [ {hero_text} ] }}"

    def _player_ref_text(seg, player_name):
        if db is None:
            return ""
        hg = str(seg.get("hero_guid", ""))
        hn = HERO_DICT.get(hg, {}).get("name", "?")
        return build_broad_reference_text(db, player_name, hg, hn)

    result_map = {1: "胜", 0: "平", -1: "负"}
    lines = []
    for idx, m in enumerate(matches):
        detail_data = (m.get("detail", {}) or {}).get("data") or {}
        source = m.get("source_match", {}) or {}
        game_sec = float(detail_data.get("gameTimeSec", 600) or 600)
        map_guid = str(detail_data.get("mapGuid") or source.get("mapGuid") or "")
        ret = detail_data.get("matchRet", source.get("matchRet"))
        dur = f"{int(game_sec // 60):02d}:{int(game_sec % 60):02d}"
        lines.append(f"[第{idx + 1}局] {result_map.get(ret, '?')} {MAP_DICT.get(map_guid, '?')} {dur} 焦点玩家: {target_id}")
        lines.append("{")
        lines.append(f"  比分: {detail_data.get('teamScore', '?')}:{detail_data.get('opponentScore', '?')},")

        tm = detail_data.get("teammateList", [])
        en = detail_data.get("enemyList", [])
        enemy_total_deaths = sum(int((p if isinstance(p, dict) else {}).get("death", 0) or 0) for p in en)

        def _append_players(label, players, *, include_detail, include_reference):
            lines.append(f"  [{label}]")
            lines.append("  [")
            for p, segments, pos in _sort_and_label_players(players):
                player_name = str(p.get("name", "?"))
                lines.append(f"    {_fmt_player_block(p, segments, pos, game_sec, include_detail, enemy_total_deaths)},")
                if include_reference:
                    for seg in segments:
                        ref = _player_ref_text(seg, player_name)
                        if ref:
                            lines.append(f"    # 数据参考: {ref}")
            lines.append("  ],")

        if tm:
            _append_players("队友", tm, include_detail=True, include_reference=True)
        lines.append("}")
        lines.append("")

    n = len(matches)

    teammate_counts = {}
    for m in matches:
        detail_data = (m.get("detail", {}) or {}).get("data") or {}
        seen = set()
        for p in detail_data.get("teammateList", []):
            if not isinstance(p, dict):
                continue
            name = str(p.get("name", ""))
            if name == target_id or name in seen:
                continue
            if name:
                seen.add(name)
                teammate_counts[name] = teammate_counts.get(name, 0) + 1
    friend_ids = sorted(name for name, cnt in teammate_counts.items() if cnt >= 3)
    friend_id_text = "\n".join(f"- {name}" for name in friend_ids) or "无"
    match_text = "\n".join(lines)

    _metaphor_categories = [
        ("状态不稳定类", ["数据过山车", "随机数生成器", "情绪盲盒", "情绪不稳定的数据电池", "人形骰子", "薛定谔的C位", "信号不好的路由器", "间歇性战神体验卡"]),
        ("无效贡献类", ["空气掩护", "用身体打伤害", "行走的充电宝", "战术性自杀", "蹭地图经验涨KD", "团队ATM机", "敌方能量加速器", "移动复活点"]),
        ("高光统治类", ["战神下凡", "把对面点位焊死", "职业选手体验生活", "人形外挂", "把对面当兵补", "准心端装了GPS"]),
        ("拉胯下限类", ["会飞的咸鱼", "空中活靶子", "观光客", "落地成盒", "纯度极高的咸鱼", "键盘撒米鸡啄选手", "人机练习赛VIP"]),
        ("数据结果背离类", ["华丽数据证明无用", "KDA骗子", "用队友的命换评分", "胜利是队友扛着走的"]),
    ]
    random.shuffle(_metaphor_categories)
    _metaphor_lines = []
    for _cat_name, _items in _metaphor_categories:
        random.shuffle(_items)
        _metaphor_lines.append(f"{_cat_name}：{'、'.join(_items)}。")
    _metaphor_text = "\n".join(_metaphor_lines)

    return f"""[ROLE] 角色与语气设定
你是一位资深竞技游戏玩家兼数据分析师，擅长用「脱口秀式毒舌」风格对玩家的对局数据进行复盘点评。你的文字既有专业数据的支撑，又有极强的娱乐性和画面感，读起来像是一位又爱又恨的老队友在赛后吐槽。
语气要求：戏谑、犀利、阴阳怪气但不恶意，保持「损友」般的亲切感。善用反讽、夸张和反转。
修辞要求：大量使用游戏黑话与生活化比喻的混搭，避免干巴巴的描述。尽可能理解并且创造新的比喻。
输出评价符合人设和说话习惯，对好的部分赞赏，差的部分指出，可少量使用 emoji。

[CONTEXT] 游戏背景与修辞库
1. 守望先锋段位名称：青铜、白银、黄金、白金、钻石、大师、宗师、英杰。
2. 比喻参考库：
{_metaphor_text}

[OBJECTIVE] 核心任务
严格基于提供的原始对局数据，对焦点玩家及其好友进行复盘点评，并最终输出符合指定 JSON Schema 的合法 JSON 对象。

[CONSTRAINTS] 硬性约束与底线
1. 仅针对游戏内数据、赛场表现、英雄数据点评，绝不涉及外貌、私生活、人品等人身攻击；不输出任何歧视、引战、恶意辱骂内容。
2. 所有解读严格基于提供的原始数据，禁止编造数据、篡改数据含义、夸大数据结论。
3. 严禁单一数据全盘否定：必须复盘全部 {n} 场数据的宏观表现。如果玩家有打得极好的高光对局，必须予以承认和赞赏；差的对局应调侃。
4. 严禁跨职责直接比较伤害或治疗等核心指标，阴阳调侃必须对应明确的数据论据。
5. 不讨论外挂、代练等违规行为，禁止进行反事实推演或假设性陈述，仅限描述已发生事件。

[WORKFLOW] 评判规则与工作流
步骤一：职责核心指标评估（综合评估，技能指标权重低）
坦克位参考：单独消灭、最后一击、(伤害减受疗)、阵亡数、击杀参与率等其他技能指标。
输出位参考：单独消灭、最后一击、伤害、阵亡数、击杀参与率等其他技能指标。
辅助位参考：最后一击、阵亡数、拯救玩家、单独消灭、伤害、治疗量、击杀参与率等其他技能指标。

步骤二：数据对比与百分制评分（输出 score 整数 0到100）
1. 将焦点玩家数据与同英雄「数据参考行」对比，低于参考值应扣分，禁止跨英雄比较。
2. 同一玩家同一局可能在「英雄片段」内出现多个英雄，时长小于3分钟的片段为低权重。
3. 最后一击和单独消灭应额外加分，频繁阵亡且团队贡献低应加重扣分。
4. 综合看英雄数据。例如有的输出英雄伤害低但最后一击高，有的辅助英雄输出高但治疗少，需综合参考值考虑，不要跨英雄对比。
5. 解构无效数据：不要被表面虚高数据欺骗。若空有治疗或伤害但击杀参与率极低，评价为「无效数据刷子」。
6. 对于单独消灭高的玩家应赞赏，单独消灭低不批评不评价。
7. 比赛胜负不影响评分，只论数据。
8. 若某局数据异常，该局不参与评分或低权重，comment 写「数据缺失，无法评价」。

步骤三：综合判定结构构建（overall_comment 约 350 字，可少量使用emoji）
1. 用一个精准的比喻或定性标签概括玩家特点。
2. 列举高光场次与拉胯场次的极端对比，突出方差大、不稳定或偏科等特质。
3. 细节吐槽或表扬：针对具体英雄、技能释放、走位等进行画面感描述。
4. 收尾建议：以调侃口吻给出实质性建议。

步骤四：好友点评生成
1. 必须点评下方「焦点玩家的好友 ID」中出现的每一位好友，缺一不可。
2. 好友点评只能基于他们的比赛数据，比赛胜负不影响评价。
3. 评分标准同焦点玩家（大于等于50夸或赞赏，小于50串），但没有数据时语气要保守。

[OUTPUT FORMAT] 输出格式与字段规范
严格输出符合下方 JSON Schema 的合法 JSON 对象，禁止输出 markdown 代码块、注释或任何 JSON 之外的文字。
1. 所有字符串使用中文，内容简练。字符串内禁止英文双引号，引用请用「」或『』，emoji 可正常使用。
2. result 字段仅可取值：胜、负、平、未知。
3. summary 字段是纯客观数据概览点评（约 100 字）。
4. match_comments 字段必须覆盖比赛数据全部 {n} 局，index 从 1 递增到 {n}，禁止跳号或重复（约50字）。
5. teammate_comments 字段必须为焦点玩家的好友 ID 中的每一位好友都生成一条点评，缺一不可。

JSON Schema 定义：
{json.dumps(_SHIQU_JSON_SCHEMA, ensure_ascii=False, indent=2)}

[INPUT DATA] 输入数据
焦点玩家的好友 ID：
{friend_id_text}

比赛数据：
{match_text}
"""


# ── LLM 调用（独立配置）──

async def _call_llm(prompt: str) -> Optional[str]:
    cfg = get_shiqu_llm_config()
    if not (cfg.base_url and cfg.api_key and cfg.model):
        logger.error("[shiqu] LLM 配置不完整，请在 config/shiqu_config.py 中填写 SHIQU_LLM_BASE_URL / SHIQU_LLM_API_KEY / SHIQU_LLM_MODEL")
        return None
    payload = {
        "model": cfg.model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": cfg.stream,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "shiqu_result", "strict": True, "schema": _SHIQU_JSON_SCHEMA},
        },
    }
    headers = {"Authorization": f"Bearer {cfg.api_key}", "Content-Type": "application/json"}
    from ..analysis_common import build_async_client, get_analysis_proxy, LLM_SEMAPHORE
    proxy = get_analysis_proxy(cfg.base_url)
    await LLM_SEMAPHORE.acquire()  # 限制对外部 LLM 端点的并发（shiqu/court 共享 2 槽，受 AsyncRunner 单循环控制）
    try:
        async with build_async_client(timeout=cfg.timeout_seconds, proxy_url=proxy) as client:
            try:
                if cfg.stream:
                    parts = []
                    async with client.stream("POST", cfg.chat_url, json=payload, headers=headers) as resp:
                        resp.raise_for_status()
                        async for line in resp.aiter_lines():
                            line = line.strip()
                            if not line.startswith("data:"):
                                continue
                            chunk = line[5:].strip()
                            if chunk == "[DONE]":
                                break
                            try:
                                obj = json.loads(chunk)
                            except Exception:
                                continue
                            if not isinstance(obj, dict):
                                continue
                            choices = obj.get("choices")
                            if not isinstance(choices, list) or not choices:
                                continue
                            first = choices[0]
                            if not isinstance(first, dict):
                                continue
                            delta = first.get("delta") or {}
                            if isinstance(delta, dict) and "content" in delta:
                                parts.append(delta["content"])
                    return "".join(parts).strip() or None
                else:
                    resp = await client.post(cfg.chat_url, json=payload, headers=headers)
                    resp.raise_for_status()
                    data = resp.json()
                    choices = data.get("choices") if isinstance(data, dict) else None
                    if not isinstance(choices, list) or not choices:
                        return None
                    msg = choices[0] if isinstance(choices[0], dict) else {}
                    content = (msg.get("message") or {}).get("content") or ""
                    return str(content).strip() or None
            except Exception as e:
                logger.error(f"[shiqu] LLM 调用异常: {e}", exc_info=True)
                return None
    finally:
        LLM_SEMAPHORE.release()


# ── 主流程 ──

class ShiquModule:
    def __init__(self) -> None:
        self.match_module = dashen_match_module

    def _is_preset_mode(self, root: dict, source: Optional[dict] = None) -> bool:
        game_mode = str(root.get("gameMode") or (source or {}).get("gameMode") or "").strip()
        return game_mode in _PRESET_MODES

    async def _collect_preset_details(
        self, customer_token: str, entries: List[dict], match_count: int
    ) -> List[dict]:
        """逐条获取对局详情，仅保留预设/6v6 模式。

        严格复用 overstats 的 DashenMatchModule.query_match_detail：
        内部通过 DashenMatchRequests.get_match_detail 选择 query_match_info /
        fight_query_match_info，并用 render._extract_match_detail_data 提取根数据。
        模式判定优先取详情根数据 gameMode（与原版 _get_match_mode 一致）。
        """
        details: List[dict] = []
        for e in entries:
            if len(details) >= match_count:
                break
            if not isinstance(e, dict):
                continue
            match_id = str(e.get("matchId") or "")
            try:
                detail_output = await self.match_module.query_match_detail(customer_token, e, render=False)
            except Exception as exc:
                logger.warning(f"[shiqu] 拉取对局 {match_id} 详情失败: {exc}")
                continue
            detail = detail_output.detail
            root = _extract_match_detail_data(detail.payload)
            if not self._is_preset_mode(root, detail.source_match):
                continue
            details.append({
                "match_id": detail.match_id or match_id,
                "detail": {"data": root},
                "source_match": detail.source_match,
            })
        return details

    async def _enrich_teammate_details(self, details: List[dict], customer_token: str) -> None:
        """用队友各自 token + 比赛 match_id 重新拉取同局详情，补齐 _heroList。

        复用 overstats 的 DashenMatchModule.query_match_detail（按 match_id 直查），
        与原版 _fetch_match_by_token_match_id 的意图一致，但走项目内部模块而非 HTTP。
        """
        for m in details:
            source = m.get("source_match") or {}
            focus_match_id = str(m.get("match_id") or source.get("matchId") or "")
            if not focus_match_id:
                continue
            root = (m.get("detail", {}) or {}).get("data") or {}
            for p in (root.get("teammateList", []) or []):
                if not isinstance(p, dict):
                    continue
                if p.get("_heroList"):
                    continue
                teammate_token = str(p.get("customerToken", "") or "").strip()
                if not teammate_token:
                    continue
                try:
                    detail_output = await self.match_module.query_match_detail(teammate_token, focus_match_id, render=False)
                    tm_root = _extract_match_detail_data(detail_output.detail.payload)
                    hl = tm_root.get("heroList") or []
                    if hl:
                        p["_heroList"] = hl
                except Exception as exc:
                    logger.warning(f"[shiqu] 队友 {p.get('name')} 详情拉取失败: {exc}")

    async def analyze(self, query, *, db: Optional[IDPoolDB] = None) -> dict:
        """业务主入口：抓取对局 → 构建 Prompt → 调用独立 LLM → 解析结构化结果。

        数据查询阶段严格复用 overstats 的 dashen_match 模块：
        1. DashenMatchModule.query_match_list —— 完成 bnet_id → customer_token 解析与最近对局列表（含缓存）；
        2. DashenMatchModule.query_match_detail —— 逐条拉取对局详情（自动区分 fight/normal）；
        3. render._extract_match_detail_data —— 提取与原始 shiqu.py 一致的根数据结构。
        仅保留预设/6v6 对局，不足则重试一次（对齐原版网络抖动重试）。

        Args:
            query: ShiquQuery
            db: 可选 IDPoolDB 实例（用于分段参考）；为 None 时不附加参考数据。
        Returns:
            归一化后的判定结果 dict（含 score / verdict / summary / match_comments / overall_comment / teammate_comments）。
        Raises:
            ModuleError: 解析失败 / 数据不足 / LLM 未配置。
        """
        if not query.bnet_id and not query.customer_token:
            raise ModuleError(error="missing_target", message="bnet_id 或 customer_token 不能为空。", status_code=400)

        # ── use_db 模式：跳过上游抓取与 LLM 调用，直接复用数据库里最近一次判定 ──
        if query.use_db:
            return await self._analyze_from_db(query)

        if not is_shiqu_llm_configured():
            raise ModuleError(
                error="shiqu_llm_not_configured",
                message="是区吗 LLM 未配置，请在 config/shiqu_config.py 中填写 SHIQU_LLM_BASE_URL / SHIQU_LLM_API_KEY / SHIQU_LLM_MODEL。",
                status_code=500,
            )

        match_count = get_shiqu_match_count(query.match_count or 12)
        match_count = max(2, min(25, int(match_count)))

        list_query = DashenMatchQuery(
            customer_token=query.customer_token,
            bnet_id=query.bnet_id,
            target_count=100,
            include_fight=False,
            include_previous_season=True,
        )

        # ── 阶段一：严格复用 overstats 的对局列表查询（解析 + 缓存一体化）──
        list_output = await self.match_module.query_match_list(list_query, render=False)
        customer_token = list_output.customer_token
        resolved = list_output.resolved_bnet
        full_id = resolved.full_id if resolved else (query.bnet_id or f"token:{customer_token[:8]}")

        # ── 阶段二：逐条拉取详情，仅保留预设/6v6（复用 overstats query_match_detail）──
        details = await self._collect_preset_details(customer_token, list_output.matches, match_count)

        # 单次网络抖动重试（对齐原版 _fetch_matches 重试逻辑）
        if len(details) < 2 and query.bnet_id:
            try:
                list_output = await self.match_module.query_match_list(list_query, render=False)
                details = await self._collect_preset_details(customer_token, list_output.matches, match_count)
            except Exception as exc:
                logger.warning(f"[shiqu] 列表重试拉取失败: {exc}")

        if len(details) < 2:
            raise ModuleError(
                error="insufficient_matches",
                message=f"仅获取到 {len(details)} 场预设/6v6 对局，至少需要 2 场。",
                status_code=404,
                hint="[决斗领域]暂未适配；请确保该玩家有最近 2 场以上的竞技/快速预设对局。",
            )

        # ── 阶段二（补充）：队友多英雄 heroList 补齐（best-effort，复用 overstats 详情查询）──
        await self._enrich_teammate_details(details, customer_token)

        prompt = _build_prompt(details, full_id, db=db)

        # 调试/测试用：设置环境变量 SHIQU_DUMP_PROMPT 指向文件路径，
        # 即可把本次组成的提示词落盘（不影响正常判定流程）。
        _dump_prompt_path = os.environ.get("SHIQU_DUMP_PROMPT")
        if _dump_prompt_path:
            try:
                Path(_dump_prompt_path).expanduser().write_text(prompt, encoding="utf-8")
                logger.info(f"[shiqu] 提示词已保存到 {_dump_prompt_path}")
            except Exception as exc:  # pragma: no cover
                logger.warning(f"[shiqu] 提示词保存失败: {exc}")

        # 调试/测试用：设置环境变量 SHIQU_PROMPT_ONLY=1 即只生成提示词、跳过 LLM 调用，
        # 直接返回占位结果（不影响生产：不设该变量则完全无副作用）。
        if os.environ.get("SHIQU_PROMPT_ONLY"):
            logger.info("[shiqu] SHIQU_PROMPT_ONLY 已启用：跳过 LLM 调用，仅返回提示词。")
            return {
                "target_id": full_id,
                "ok": True,
                "prompt_only": True,
                "prompt_bytes": len(prompt.encode("utf-8")),
            }

        if len(prompt.encode("utf-8")) < 10240:
            raise ModuleError(
                error="insufficient_prompt_data",
                message="数据抓取量异常，可能没有足够的预设/6v6 比赛对局。",
                status_code=404,
            )

        result = None
        last_text = ""
        call_count = 0
        cfg = get_shiqu_llm_config()
        # retry=0 → 仅 1 次调用；retry=N → 初始 1 次 + 失败重试 N 次。
        # 模型恢复等待交给单次调用的超时（默认 600s）承担：若上游在连接内切换模型并回传内容，
        # 第一次调用即直接拿到结果并退出，不会进入重试。
        max_attempts = 1 + (cfg.retry or 0)
        _t0 = time.perf_counter()
        shiqu_llm_status.mark_call_start(full_id)
        success = False
        try:
            for attempt in range(1, max_attempts + 1):
                call_count += 1
                last_text = await _call_llm(prompt) or ""
                result = _parse_llm_json_result(last_text, full_id) if last_text else None
                if result:
                    break
                logger.warning(f"[shiqu] LLM 尝试 {attempt}/{max_attempts} 失败，准备重试")
            success = bool(result)
            if not result:
                raise ModuleError(
                    error="shiqu_llm_failed",
                    message="AI 判定生成失败：大模型调用异常 / 返回内容不是合法 JSON。",
                    status_code=502,
                )
        finally:
            duration_ms = int((time.perf_counter() - _t0) * 1000)
            shiqu_llm_status.mark_call_done(
                success, "" if success else "LLM 返回为空或 JSON 解析失败"
            )
            # ── 阶段四：LLM 调用遥测落库（异步、best-effort，不阻塞主流程）──
            # 必须放在 finally 内：无论成功还是 raise 失败都落库，否则失败调用既不记录
            # 错误也不记录 prompt，无法排查。只存提示词 / 原始返回 / 调用诊断；
            # score/verdict/summary 等渲染时由 raw_response 解析（失败时 raw_response 可能为空字符串）。
            try:
                await shiqu_llm_recorder.enqueue(
                    target_id=full_id,
                    prompt=prompt,
                    raw_response=last_text,
                    ok=success,
                    duration_ms=duration_ms,
                    call_count=call_count,
                )
            except Exception as exc:
                logger.warning(f"[shiqu] LLM 调用记录落库失败（已忽略）: {exc}")

        return result

    async def _analyze_from_db(self, query: ShiquQuery) -> Dict[str, Any]:
        """use_db 模式：从 shiqu_llm 数据库读取该玩家最近一次判定并解析成渲染用结构。

        跳过上游对局抓取与 LLM 调用；数据库中无记录则报错。
        """
        target_id = str(query.bnet_id or f"token:{query.customer_token[:8]}")
        row = await asyncio.to_thread(shiqu_llm_recorder.db.get_latest_by_target, target_id)
        if not row or not (row.get("raw_response") or "").strip():
            raise ModuleError(
                error="db_record_not_found",
                message=f"数据库中未找到玩家 {target_id} 的判定记录，请先正常生成一次。",
                status_code=404,
            )
        result = _parse_llm_json_result(row["raw_response"], target_id)
        if not result:
            raise ModuleError(
                error="db_record_parse_failed",
                message="数据库中的判定记录无法解析（raw_response 不是合法 JSON）。",
                status_code=500,
            )
        # use_db 模式：渲染时间取该记录的落库时间，而非当前时间。
        created_at = row.get("created_at")
        if created_at:
            result["generated_at"] = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(int(created_at))
            )
        return result


shiqu_module = ShiquModule()
