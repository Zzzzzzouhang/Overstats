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
        build_broad_reference_map,
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
        build_broad_reference_map,
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


# 压缩提示词：英雄片段字段短键映射（中文统计名 → 短键）。
# 与 prompt 中「字段说明」严格对应：h英雄 t时长 k消灭 d阵亡 f最后一击
# s单独消灭 acc命中率 cr暴击率 heal治疗 save拯救。ref 同义。
_STAT_TEXT_TO_SHORT = {
    "消灭": "k", "阵亡": "d", "最后一击": "f", "单独消灭": "s",
    "武器命中率": "acc", "暴击命中率": "cr", "治疗量": "heal", "拯救玩家": "save",
}
# 命中率一般写作「武器命中率」，个别英雄为「命中率」，两者都映射到 acc。
if "命中率" in _ATTR_TEXT_TO_GUID and "武器命中率" not in _ATTR_TEXT_TO_GUID:
    _STAT_TEXT_TO_SHORT.setdefault("武器命中率", "acc")

_STAT_GUID_TO_SHORT: Dict[str, str] = {}
for _txt, _short in _STAT_TEXT_TO_SHORT.items():
    _g = _ATTR_TEXT_TO_GUID.get(_txt)
    if _g:
        _STAT_GUID_TO_SHORT[_g] = _short
# 用 guids 反查，避免重复映射
_STAT_SHORT_TO_GUID = {v: k for k, v in _STAT_GUID_TO_SHORT.items()}



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


# 严格子集：只用 type / enum / required / properties / items，避免 strict 模式不支持的
# minLength / minItems / minimum / maximum / additionalProperties 导致网关整段拒答（返回空）。
_SHIQU_JSON_SCHEMA = {
    "type": "object",
    "required": ["target_id", "score", "summary", "match_comments", "overall_comment", "teammate_comments"],
    "properties": {
        "target_id": {"type": "string"},
        "score": {"type": "integer"},
        "summary": {"type": "string"},
        "match_comments": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["index", "result", "hero", "comment"],
                "properties": {
                    "index": {"type": "integer"},
                    "result": {"type": "string", "enum": ["胜", "负", "平", "未知"]},
                    "hero": {"type": "string"},
                    "comment": {"type": "string"},
                },
            },
        },
        "overall_comment": {"type": "string"},
        "teammate_comments": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name", "games", "score", "comment"],
                "properties": {
                    "name": {"type": "string"},
                    "games": {"type": "integer"},
                    "score": {"type": "integer"},
                    "comment": {"type": "string"},
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


_RESULT_ALIASES = {
    "胜": "胜", "赢": "胜", "胜利": "胜", "w": "胜", "win": "胜",
    "负": "负", "输": "负", "败": "负", "失败": "负", "l": "负", "lose": "负", "loss": "负",
    "平": "平", "平局": "平", "d": "平", "draw": "平",
    "未知": "未知", "": "未知", "none": "未知", "null": "未知",
}


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
        raw_result = str(item.get("result") or "未知").strip()
        norm_result = _RESULT_ALIASES.get(raw_result, None)
        if norm_result is None:
            # 尝试修复被 Latin-1 错误编码的乱码（如 '\u00e8\u0083\u009c' -> '胜'）
            try:
                hexes = re.findall(r"\\u([0-9a-fA-F]{4})", raw_result)
                if len(hexes) >= 2:
                    decoded = bytes(int(h, 16) for h in hexes).decode("utf-8", "ignore")
                    norm_result = _RESULT_ALIASES.get(decoded, None)
            except Exception:
                norm_result = None
        if norm_result is None:
            norm_result = "未知"
        result["match_comments"].append({
            "index": _clamp_score(item.get("index"), i),
            "result": norm_result,
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


def _repair_json_delimiters(text: str) -> str:
    """补全模型输出中缺失的 JSON 分隔符（逗号/冒号）。

    覆盖两类高频格式错误：
    1) 键名后直接换行/空格再接冒号（如 ``"match_comments" \\n :``）；
    2) 对象 ``}`` / 数组 ``]`` 结束后、下一个值或 ``}`` / ``]`` 之前缺少逗号
       （如 ``...胜"}\\n{...``、``...胜"}]\\n]``）。
    """
    # key 与 ':' 之间允许任意空白（包括换行）
    t = re.sub(r'("(?:[^"\\]|\\.)*")\s*:', r'\1:', text)
    # 在结构闭合符 } ] 与后续的结构起始符 " { [ } ] 之间补逗号
    t = re.sub(r'([}\]])\s*(?=["{\[\]}])', r'\1,', t)
    # 去掉数组/对象开头处的多余逗号（如 [, {...）
    t = re.sub(r'([\[{])\s*,', r'\1', t)
    return t


def _fix_mojibake(text: str) -> str:
    """还原被错误 Latin-1 转义的 UTF-8 字节序列，如 '\\u00e8\\u0083\\u009c' -> '胜'。"""
    def repl(m):
        hexes = re.findall(r"\\u([0-9a-fA-F]{4})", m.group(0))
        try:
            raw = bytes(int(h, 16) for h in hexes)
            return raw.decode("utf-8", "ignore") or m.group(0)
        except Exception:
            return m.group(0)
    # 仅当整段由多个 \\uXXXX 连成（非合法 JSON 转义）时才尝试还原
    return re.sub(r"(?:\\u[0-9a-fA-F]{4}){2,}", repl, text)


def _extract_json_object(text: str) -> Optional[dict]:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    # 兼容根对象为数组（如模型返回 [{...}]）的情况
    cleaned = _fix_mojibake(cleaned)
    if cleaned[:1] == "[":
        arr = None
        try:
            arr = json.loads(cleaned)
        except Exception:
            pass
        if isinstance(arr, list) and arr:
            cleaned = json.dumps(arr[0], ensure_ascii=False) if not isinstance(arr[0], dict) else cleaned
            if isinstance(arr[0], dict):
                return arr[0]
    attempts = [
        cleaned,
        _repair_json_delimiters(cleaned),
        _repair_json(cleaned),
        _repair_json_structure(cleaned),
        _repair_json_values(cleaned),
        _repair_json(_repair_json_structure(cleaned)),
        _repair_json(_repair_json_values(cleaned)),
        _repair_json_structure(_repair_json_values(cleaned)),
        _repair_json_delimiters(_repair_json(cleaned)),
        _repair_json_delimiters(_repair_json_structure(cleaned)),
        _repair_json_delimiters(_repair_json_values(cleaned)),
        _repair_json(_repair_json_delimiters(_repair_json_structure(cleaned))),
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
        snippet = _fix_mojibake(cleaned[start:end + 1])
        attempts = [
            snippet,
            _repair_json_delimiters(snippet),
            _repair_json(snippet),
            _repair_json_structure(snippet),
            _repair_json_values(snippet),
            _repair_json(_repair_json_structure(snippet)),
            _repair_json(_repair_json_values(snippet)),
            _repair_json_structure(_repair_json_values(snippet)),
            _repair_json_delimiters(_repair_json_structure(snippet)),
            _repair_json_delimiters(_repair_json_values(snippet)),
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

def _segment_present_guids(entry: dict, hero_guid: str, name_map: dict) -> set:
    """返回该分段真实数据中「存在 + 白名单允许 + 非跳过 + 可归一化」的统计 guid 集合。

    与 _fmt_hero_line 使用的过滤口径完全一致，用于约束参考数据：
    真实对局里没出现的数据，给参考没有意义。
    """
    sm = (entry or {}).get("statMap", {}) or {}
    ut = float((entry or {}).get("userTimeSec", 600) or 600)
    guids: set = set()
    for guid, raw_val in sm.items():
        g = str(guid)
        if not _stat_allowed_for_hero(g, hero_guid):
            continue
        name = name_map.get(g)
        if not name:
            continue
        if should_skip_prompt_stat(value_guid=g, value_text=name):
            continue
        if normalize_stat_value(raw_val, ut, value_text=name, value_guid=g) is None:
            continue
        guids.add(g)
    return guids


# ── 压缩提示词：模块级共享数据结构 ──

def _get_role(p) -> str:
    return HERO_DICT.get(str(p.get("heroGuid", "")), {}).get("role", "unknown")


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


def _player_primary_role(player_segments, fallback_player) -> str:
    best_role = HERO_DICT.get(str(fallback_player.get("heroGuid", "")), {}).get("role", "unknown")
    best_time = -1.0
    for seg in player_segments:
        entry = seg.get("entry") or {}
        ut = float(entry.get("userTimeSec", 0) or 0)
        role = HERO_DICT.get(str(seg.get("hero_guid", "")), {}).get("role", "unknown")
        if ut > best_time:
            best_time = ut
            best_role = role
    return best_role


def _fmt_compact_num(v):
    if v is None:
        return None
    f = float(v)
    if abs(f - round(f)) < 1e-9:
        return int(round(f))
    return round(f, 2)


# 消灭参与率计算用到的原始统计 guid（与旧版 _build_prompt 口径一致）。
_KILL_GUID = "603482350067646495"
_ASSIST_GUID = "603482350067648392"
_DEATH_GUID = "603482350067646506"


def _fmt_hero_line(seg, db) -> str:
    """把单个英雄分段格式化为行协议：

      h=英雄 t=时长(秒) k=消灭 d=阵亡 f=最后一击 s=单独消灭 acc=命中 cr=暴击 heal=治疗 save=拯救
      [特殊命中率=val ...] | ref:同义参考值

    英雄自身字段按 per-10min 归一化（与 normalize_stat_value 口径一致）。
    普通字段用短键；该英雄的特殊命中率字段（ALLOWED_SPECIAL_BY_HERO）以中文名原样给出，
    避免跨英雄歧义。ref 为同英雄分段参考中位数（build_broad_reference_map），仅含真实出现字段。
    """
    hg = str(seg.get("hero_guid", ""))
    hn = HERO_DICT.get(hg, {}).get("name", "?")
    entry = seg.get("entry")
    if not entry:
        return f"  h={hn}"
    ut = float(entry.get("userTimeSec", 0) or 0)
    sm = entry.get("statMap", {}) or {}
    name_map = seg.get("name_map") or load_stat_name_map()

    common_parts: list = []
    ref_guids: set = set()
    for g, short in _STAT_GUID_TO_SHORT.items():
        if g not in sm:
            continue
        if not _stat_allowed_for_hero(g, hg):
            continue
        nm = name_map.get(g, "")
        if should_skip_prompt_stat(value_guid=g, value_text=nm):
            continue
        nv = normalize_stat_value(sm.get(g), ut, value_text=nm, value_guid=g)
        if nv is None:
            continue
        common_parts.append(f"{short}={_fmt_compact_num(nv)}")
        ref_guids.add(g)

    special_parts: list = []
    for g in _HERO_SPECIAL_ATTR_GUIDS.get(hg, set()):
        if g in _STAT_GUID_TO_SHORT or g not in sm:
            continue
        nm = name_map.get(g, "")
        if not nm or should_skip_prompt_stat(value_guid=g, value_text=nm):
            continue
        nv = normalize_stat_value(sm.get(g), ut, value_text=nm, value_guid=g)
        if nv is None:
            continue
        special_parts.append(f"{nm}={_fmt_compact_num(nv)}")
        ref_guids.add(g)

    parts = [f"h={hn}", f"t={int(round(ut))}"] + common_parts + special_parts
    line = "  " + " ".join(parts)

    if db and ref_guids:
        ref_map = build_broad_reference_map(db, hg, present_guids=ref_guids)
        if ref_map:
            ref_items: list = []
            for g, short in _STAT_GUID_TO_SHORT.items():
                if g in ref_guids:
                    nm = name_map.get(g, "")
                    if nm in ref_map:
                        ref_items.append(f"{short}={_fmt_compact_num(ref_map[nm])}")
            for g in _HERO_SPECIAL_ATTR_GUIDS.get(hg, set()):
                if g in _STAT_GUID_TO_SHORT or g not in ref_guids:
                    continue
                nm = name_map.get(g, "")
                if nm in ref_map:
                    ref_items.append(f"{nm}={_fmt_compact_num(ref_map[nm])}")
            if ref_items:
                line += " | ref:" + " ".join(ref_items)
    return line


def _compact_label_players(players, *, enemy: bool = False):
    ROLE_ORDER = {"tank": 0, "dps": 1, "healer": 2}
    ROLE_SHORT = {"tank": "坦", "dps": "输", "healer": "辅"}
    indexed = []
    for pi, p in enumerate(players):
        if not isinstance(p, dict):
            continue
        segs = _expand_player_segments(p)
        if not segs:
            continue
        role = _player_primary_role(segs, p)
        indexed.append((ROLE_ORDER.get(role, 9), pi, role, p, segs))
    indexed.sort(key=lambda x: (x[0], x[1]))
    prefix = "敌" if enemy else ""
    role_ct: dict = {}
    role_total = {r: sum(1 for x in indexed if x[2] == r) for _, _, r, _, _ in indexed}
    labeled = []
    for _, _, role, p, segs in indexed:
        role_ct[role] = role_ct.get(role, 0) + 1
        lb = ROLE_SHORT.get(role, role)
        pos = f"{prefix}{lb}{role_ct[role]}" if role_total.get(role, 0) > 1 else f"{prefix}{lb}"
        labeled.append((p, segs, pos))
    return labeled


def _compute_enemy_total_deaths(enemy_list: list) -> float:
    total = 0.0
    for p in enemy_list or []:
        if not isinstance(p, dict):
            continue
        d = p.get("death")
        if d is None and isinstance(p.get("_heroList"), list):
            for e in p["_heroList"]:
                sm = (e or {}).get("statMap", {}) or {}
                if _DEATH_GUID in sm:
                    try:
                        total += float(sm[_DEATH_GUID])
                    except (TypeError, ValueError):
                        pass
        else:
            try:
                total += float(d or 0)
            except (TypeError, ValueError):
                pass
    return total


def _compute_pr(player: dict, enemy_total_deaths: float) -> float:
    """(原始消灭 + 原始助攻/2) / 敌方原始总死亡数；与旧版口径一致。"""
    total_kill = 0.0
    total_assist = 0.0
    for seg in _expand_player_segments(player):
        entry = seg.get("entry")
        if entry:
            sm = entry.get("statMap", {}) or {}
            for g, bucket in ((_KILL_GUID, "kill"), (_ASSIST_GUID, "assist")):
                raw = sm.get(g)
                if raw is None:
                    continue
                try:
                    val = float(raw)
                except (TypeError, ValueError):
                    continue
                if bucket == "kill":
                    total_kill += val
                else:
                    total_assist += val
        else:
            total_kill += int(player.get("kill", 0) or 0)
            total_assist += int(player.get("assist", 0) or 0)
    kp = total_kill + total_assist / 2.0
    return kp / enemy_total_deaths if enemy_total_deaths > 0 else 0.0


def _fmt_match_block(m: dict, target_id: str, idx: int, db) -> str:
    """把一局比赛格式化为行协议块：

    第{i}局|res|map|比分
    {pos}|{p}|{pr}
      h=... t=... ... | ref:...
    """
    detail_data = (m.get("detail", {}) or {}).get("data") or {}
    source = m.get("source_match", {}) or {}
    map_guid = str(detail_data.get("mapGuid") or source.get("mapGuid") or "")
    ret = detail_data.get("matchRet", source.get("matchRet"))
    result_map = {1: "胜", 0: "平", -1: "负"}
    res = result_map.get(ret, "未知")
    score_line = f"{detail_data.get('teamScore', '?')}:{detail_data.get('opponentScore', '?')}"
    tm = detail_data.get("teammateList", []) or []
    en = detail_data.get("enemyList", []) or []
    enemy_total_deaths = _compute_enemy_total_deaths(en)

    lines = [f"第{idx}局|{res}|{MAP_DICT.get(map_guid, '?')}|{score_line}"]
    for p, segs, pos in _compact_label_players(tm):
        name = str(p.get("name", "?"))
        display = f"*{name}" if name == target_id else name
        pr = _compute_pr(p, enemy_total_deaths)
        lines.append(f"{pos}|{display}|{pr:.3f}")
        for seg in segs:
            lines.append(_fmt_hero_line(seg, db))
    return "\n".join(lines)


def _compute_friend_list(matches: list, target_id: str) -> List[str]:
    """统计好友出现场次，出现≥3局视为好友，返回好友 ID 列表（仅 ID，games 不再预计算）。"""
    teammate_counts: dict = {}
    for m in matches:
        detail_data = (m.get("detail", {}) or {}).get("data") or {}
        seen = set()
        for p in detail_data.get("teammateList", []):
            if not isinstance(p, dict):
                continue
            name = str(p.get("name", ""))
            if name == target_id or name in seen or not name:
                continue
            seen.add(name)
            teammate_counts[name] = teammate_counts.get(name, 0) + 1
    return [name for name, cnt in sorted(teammate_counts.items()) if cnt >= 3]


_METAPHOR_CATEGORIES = [
    ("状态不稳定类", ["数据过山车", "随机数生成器", "情绪盲盒", "情绪不稳定的数据电池", "人形骰子", "薛定谔的C位", "信号不好的路由器", "间歇性战神体验卡"]),
    ("无效贡献类", ["空气掩护", "用身体打伤害", "行走的充电宝", "战术性自杀", "蹭地图经验涨KD", "团队ATM机", "敌方能量加速器", "移动复活点"]),
    ("高光统治类", ["战神下凡", "把对面点位焊死", "职业选手体验生活", "人形外挂", "把对面当兵补", "准心端装了GPS"]),
    ("拉胯下限类", ["会飞的咸鱼", "空中活靶子", "观光客", "落地成盒", "纯度极高的咸鱼", "键盘撒米鸡啄选手", "人机练习赛VIP"]),
    ("数据结果背离类", ["华丽数据证明无用", "KDA骗子", "用队友的命换评分", "胜利是队友扛着走的"]),
]


def _build_metaphor_text() -> str:
    cats = list(_METAPHOR_CATEGORIES)
    random.shuffle(cats)
    lines = []
    for cat_name, items in cats:
        items = list(items)
        random.shuffle(items)
        lines.append(f"{cat_name}：{'、'.join(items)}。")
    return "\n".join(lines)


def _build_system_prompt() -> str:
    """常驻、与具体对局无关的内容，放入 system（可被 prompt caching 命中）。"""
    return f"""[ROLE] 角色与语气
你是一位资深竞技游戏玩家兼数据分析师，用「脱口秀式毒舌」风格复盘对局数据。戏谑、犀利、阴阳怪气但不恶意，保持损友亲切感；善用反讽、夸张、反转；大量使用游戏黑话与生活化比喻混搭，可理解并创造新比喻；对好的部分赞赏，差的部分指出。

[CONTEXT] 背景
守望先锋段位：青铜、白银、黄金、白金、钻石、大师、宗师、英杰。

[OBJECTIVE] 任务
严格基于提供的原始对局数据，对焦点玩家及其好友复盘点评，输出符合指定 JSON Schema 的合法 JSON 对象。

[CONSTRAINTS] 硬约束
1. 只评游戏数据，不评外貌/私生活/人品；不引战、不歧视、不聊外挂代练；不编造数据；不跨职责/跨英雄比较；高光必夸，差局调侃；胜负不影响评分。
2. 仅描述已发生事件，禁止反事实推演或假设性陈述。

[WORKFLOW] 评分规则
步骤一 职责核心指标：
坦克参考 单独消灭、最后一击、(伤害减受疗)、阵亡、消灭参与率；
输出参考 单独消灭、最后一击、伤害、阵亡、消灭参与率；
辅助参考 最后一击、阵亡、拯救、单独消灭、伤害、治疗、消灭参与率。
步骤二 对比与评分（score 整数 0-100，基准线50）：
1. 与同英雄 ref 对比，低于参考扣分，禁止跨英雄比较；
2. 同一局多英雄，时长<3分钟片段低权重；
3. 最后一击、单独消灭额外加分；频繁阵亡且贡献低加重扣分；
4. 综合看英雄数据（如输出伤害低但最后一击高、辅助输出高但治疗少），不跨英雄对比；
5. 解构无效数据：空有治疗/伤害但消灭参与率极低，判为「无效数据刷子」；
6. 单独消灭高应赞赏，低不批评；
7. 比赛胜负不影响评分；
8. 某局数据异常则不参与评分或低权重，comment 写「数据缺失，无法评价」。
步骤三 overall_comment（≤300字，≤2个emoji）：
1. 精准比喻/定性标签概括特点；
2. 高光与拉胯极端对比；
3. 细节画面感吐槽/表扬；
4. 调侃口吻收尾建议。
步骤四 好友点评：
1. 覆盖全部好友，score≥50夸，<50串；
2. 仅基于比赛数据，胜负不影响评价；缺数据保守评价。

[OUTPUT] 输出格式
严格输出符合 JSON Schema 的合法 JSON 对象，禁止 markdown/代码块/注释/JSON 外文字。
- 字符串用中文、简练；禁止英文双引号，引用用「」或『』；emoji 按上限使用。
- result 仅可：胜、负、平、未知。
- summary≤100字，客观数据概览。
- match_comments：遍历 matches，每局输出一条；index 用输入 index；comment≤45字且含至少1个数字，≤1 emoji；异常写「数据缺失，无法评价」。
- overall_comment≤300字，≤2 emoji。
- teammate_comments：覆盖全部好友；name 用给定值，score≥50夸<50串，comment≤60字，0 emoji。
JSON Schema：
{json.dumps(_SHIQU_JSON_SCHEMA, ensure_ascii=False, indent=2)}"""


def _build_user_prompt(matches: list, target_id: str, db: Optional[IDPoolDB] = None) -> str:
    """动态内容：焦点玩家、好友、压缩后的行协议比赛数据、修辞库。"""
    match_text = "\n\n".join(_fmt_match_block(m, target_id, i + 1, db) for i, m in enumerate(matches))
    friend_names = _compute_friend_list(matches, target_id)
    friends_line = ", ".join(friend_names) if friend_names else "无"
    return f"""target_id={target_id}

friends={friends_line}

字段说明：pos=位置 p=玩家ID（*前缀为焦点玩家） pr=消灭参与率；h=英雄 t=时长(秒) k=消灭 d=阵亡 f=最后一击 s=单独消灭 acc=命中率 cr=暴击率 heal=治疗 save=拯救；ref=同英雄参考值，字段同义。英雄行可附加该英雄特殊命中率字段（如 螺旋飞弹命中率、辅助攻击模式命中率），以中文名给出，ref 同义。胜负以焦点玩家所在阵营为准。

{match_text}

[修辞库]
{_build_metaphor_text()}"""


# ── LLM 调用（独立配置）──

async def _call_llm(system_prompt: str, user_prompt: str) -> Optional[str]:
    cfg = get_shiqu_llm_config()
    if not (cfg.base_url and cfg.api_key and cfg.model):
        logger.error("[shiqu] LLM 配置不完整，请在 config/shiqu_config.py 中填写 SHIQU_LLM_BASE_URL / SHIQU_LLM_API_KEY / SHIQU_LLM_MODEL")
        return None
    payload = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": cfg.stream,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "shiqu_result", "strict": False, "schema": _SHIQU_JSON_SCHEMA},
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

# shiqu 数据抓取步并发上限。与上游 datamsapi 的 domain 信号量（默认 2）对齐：
# 模块级 Semaphore 跨所有 shiqu 请求共享，把提交侧并发压在 2，避免无谓地把
# 远超真实在飞上限（domain=2）的任务塞进队列，也降低抖动期扇出放大雪崩的风险。
SHIQU_MATCH_CONCURRENCY = int(os.getenv("OVERSTATS_SHIQU_MATCH_CONCURRENCY", "2"))
_SHIQU_FETCH_SEMAPHORE: "Optional[asyncio.Semaphore]" = None


def _shiqu_fetch_semaphore() -> "asyncio.Semaphore":
    """懒加载模块级并发信号量，绑定到运行中的事件循环，避免启动期创建。"""
    global _SHIQU_FETCH_SEMAPHORE
    if _SHIQU_FETCH_SEMAPHORE is None:
        _SHIQU_FETCH_SEMAPHORE = asyncio.Semaphore(max(1, SHIQU_MATCH_CONCURRENCY))
    return _SHIQU_FETCH_SEMAPHORE


class ShiquModule:
    def __init__(self) -> None:
        self.match_module = dashen_match_module

    def _is_preset_mode(self, root: dict, source: Optional[dict] = None) -> bool:
        game_mode = str(root.get("gameMode") or (source or {}).get("gameMode") or "").strip()
        return game_mode in _PRESET_MODES

    async def _collect_preset_details(
        self, customer_token: str, entries: List[dict], match_count: int
    ) -> List[dict]:
        """并发获取对局详情（受模块级 Semaphore 限流），仅保留预设/6v6 模式。

        严格复用 overstats 的 DashenMatchModule.query_match_detail：
        内部通过 DashenMatchRequests.get_match_detail 选择 query_match_info /
        fight_query_match_info，并用 render._extract_match_detail_data 提取根数据。
        模式判定优先取详情根数据 gameMode（与原版 _get_match_mode 一致）。

        将原本的串行逐条 await 改为带 Semaphore 的并发 gather（对齐 quick_strength
        的 match_concurrency 模式）：既提升抓取速度，又通过模块级信号量把全局并发
        压在 SHIQU_MATCH_CONCURRENCY 以内，避免多查询叠加打满上游共享通道。
        """
        sem = _shiqu_fetch_semaphore()

        async def _fetch_one(e: Any) -> Optional[dict]:
            if not isinstance(e, dict):
                return None
            match_id = str(e.get("matchId") or "")
            try:
                async with sem:
                    detail_output = await self.match_module.query_match_detail(
                        customer_token, e, render=False
                    )
            except Exception as exc:
                logger.warning(f"[shiqu] 拉取对局 {match_id} 详情失败: {exc}")
                return None
            detail = detail_output.detail
            root = _extract_match_detail_data(detail.payload)
            if not self._is_preset_mode(root, detail.source_match):
                return None
            return {
                "match_id": detail.match_id or match_id,
                "detail": {"data": root},
                "source_match": detail.source_match,
            }

        # 削峰：最多只提交 match_count*3 条，避免网络抖动期一次性扇出全部
        # entries（可达 100 条）→ 放大 ConnectTimeout 与 60s 冻号雪崩。
        entries_iter = entries if len(entries) <= match_count * 3 else entries[: match_count * 3]
        results = await asyncio.gather(
            *(_fetch_one(e) for e in entries_iter), return_exceptions=True
        )
        details: List[dict] = []
        for r in results:
            if isinstance(r, dict):
                details.append(r)
                if len(details) >= match_count:
                    break
        return details

    async def _enrich_teammate_details(self, details: List[dict], customer_token: str) -> None:
        """并发补齐队友多英雄 heroList（受模块级 Semaphore 限流）。

        用队友各自 token + 比赛 match_id 重新拉取同局详情，补齐 _heroList。
        复用 overstats 的 DashenMatchModule.query_match_detail（按 match_id 直查），
        与原版 _fetch_match_by_token_match_id 的意图一致，但走项目内部模块而非 HTTP。
        改为带 Semaphore 的并发 gather，避免队友数量多时一次性打满上游通道。
        """
        sem = _shiqu_fetch_semaphore()
        tasks = []
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

                async def _fetch_teammate(
                    tok: str = teammate_token,
                    mid: str = focus_match_id,
                    peer: dict = p,
                ) -> None:
                    try:
                        async with sem:
                            detail_output = await self.match_module.query_match_detail(
                                tok, mid, render=False
                            )
                        tm_root = _extract_match_detail_data(detail_output.detail.payload)
                        hl = tm_root.get("heroList") or []
                        if hl:
                            peer["_heroList"] = hl
                    except Exception as exc:
                        logger.warning(
                            f"[shiqu] 队友 {peer.get('name')} 详情拉取失败: {exc}"
                        )

                tasks.append(_fetch_teammate())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def _build_list_key(preset_ids: Sequence[str]) -> str:
        """根据对局 match_id 列表生成稳定的指纹键。

        排序后拼接，确保对局顺序变化不影响判等。
        """
        return "|".join(sorted(str(mid) for mid in preset_ids))

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

        # ── 阶段 1.5：对局列表缓存命中检查 ──
        # 若本次上游返回的对局列表（按 match_count 截取）与上次提示词所用列表完全一致，
        # 则跳过耗时的逐条详情抓取 + 队友补齐，直接复用上次缓存的 details 来构建提示词；
        # LLM 调用仍照常进行（不跳过），保证判定结果始终由最新模型/提示词生成。
        # 注意：命中基于"列表相同则视为数据相同"的假设（队友快照等不在比较范围内）。
        _list_ids = [
            str(m.get("matchId") or "")
            for m in (list_output.matches or [])[:match_count]
        ]
        _list_ids = [mid for mid in _list_ids if mid]
        list_key = f"{match_count}#" + self._build_list_key(_list_ids)
        cache_hit = False
        details: List[dict] = []
        if query.use_cache and _list_ids:
            _cached = await asyncio.to_thread(
                shiqu_llm_recorder.db.get_cached_details, full_id, list_key
            )
            if _cached:
                details = _cached
                cache_hit = True
                logger.info(f"[shiqu] 对局列表命中缓存，复用上次抓取的 details（跳过详情抓取与队友补齐），list_key={list_key}")

        if not cache_hit:
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

        system_prompt = _build_system_prompt()
        user_prompt = _build_user_prompt(details, full_id, db=db)
        _full_prompt = system_prompt + "\n\n" + user_prompt

        # 调试/测试用：设置环境变量 SHIQU_DUMP_PROMPT 指向文件路径，
        # 即可把本次组成的提示词落盘（不影响正常判定流程）。
        _dump_prompt_path = os.environ.get("SHIQU_DUMP_PROMPT")
        if _dump_prompt_path:
            try:
                Path(_dump_prompt_path).expanduser().write_text(
                    f"===== SYSTEM =====\n{system_prompt}\n\n===== USER =====\n{user_prompt}",
                    encoding="utf-8",
                )
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
                "prompt_bytes": len(user_prompt.encode("utf-8")),
            }

        if len(user_prompt.encode("utf-8")) < 10240:
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
                last_text = await _call_llm(system_prompt, user_prompt) or ""
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
                    prompt=_full_prompt,
                    raw_response=last_text,
                    ok=success,
                    duration_ms=duration_ms,
                    call_count=call_count,
                )
            except Exception as exc:
                logger.warning(f"[shiqu] LLM 调用记录落库失败（已忽略）: {exc}")

            # ── 阶段四（补充）：对局列表 → details 缓存 ──
            # 仅当非缓存命中且 LLM 成功解析出结果时落库，供下次同样的对局列表复用、
            # 跳过详情抓取与队友补齐（LLM 调用仍照常进行）。失败/命中分支不写缓存。
            if success and query.use_cache and not cache_hit:
                try:
                    await asyncio.to_thread(
                        shiqu_llm_recorder.db.save_cached_details,
                        full_id, list_key, details,
                    )
                except Exception as exc:
                    logger.warning(f"[shiqu] 对局详情缓存写入失败（已忽略）: {exc}")

        if result is not None:
            result["cache_hit"] = cache_hit
            result["target_id"] = full_id
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
