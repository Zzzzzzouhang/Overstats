"""电竞法庭（court）业务主流程。

复刻 astrbot 插件 ow/court.py 的逻辑，但：
1. 对局数据严格复用 overstats 的 dashen_match 模块（query_match_detail 按 index 直查）；
2. LLM 调用**共用** config/shiqu_config.py 的独立配置（SHIQU_LLM_*），输出纯文本判决书；
3. 判决书原文落库到 shiqu_llm.sqlite3 的 court_llm_result 表（见 db/court_llm.py）。

渲染的 emoji / 标点处理与是区吗（shiqu）完全一致（复用 dashen_shiqu.render 的助手）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

from ..dashen_match.render import _extract_match_detail_data
from ..dashen_match.service import dashen_match_module
from ..dashen_shiqu.render import _normalize_llm_text  # 复用 shiqu 的标点归一化
from ..errors import ModuleError
from ..font_resolver import resolve_resource_dir
from .requests import CourtQuery
from ..dashen_shiqu.stat_db import (  # 复用 shiqu 的统计白名单 / 归一化
    build_broad_reference_text,
    load_stat_name_map,
    normalize_stat_value,
    should_skip_prompt_stat,
)
try:
    from overstats.config.shiqu_config import (  # 共用 shiqu 的独立 LLM 配置
        get_shiqu_llm_config,
        is_shiqu_llm_configured,
    )
except ModuleNotFoundError:  # pragma: no cover
    from config.shiqu_config import (
        get_shiqu_llm_config,
        is_shiqu_llm_configured,
    )
from ...db.match_stats import IDPoolDB
from ...db.court_llm import court_llm_recorder
from ..llm_call_status import court_llm_status

logger = logging.getLogger("overstats.court")

# ── 英雄 / 地图 / 模式 字典（来自 query_tool.json）──
try:
    import json as _json
    _QTOOL = _json.loads((resolve_resource_dir() / "query_tool.json").read_text("utf-8"))
except Exception:  # pragma: no cover
    _QTOOL = {"heroList": [], "mapList": [], "heroAttrList": []}

HERO_DICT = {h["heroGuid"]: {"name": h["name"], "role": h["roleType"]} for h in _QTOOL.get("heroList", [])}
MAP_DICT = {m["guid"]: m["name"] for m in _QTOOL.get("mapList", [])}
_MODE_DICT = {
    "quick": "快速", "QuickPlay": "快速", "IT_QUICKPLAY": "快速",
    "sport": "竞技", "SportPreset": "竞技（预设职责）", "IT_RANKED": "竞技",
    "sportfight": "角斗竞技", "SportFight": "角斗竞技", "IT_STADIUM": "角斗竞技",
    "quickfight": "角斗快速", "LeisureFight": "角斗快速", "IT_FIGHT": "角斗快速",
}

# ── 构建 text ↔ GUID / 英雄名 ↔ GUID 映射 ──
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

# ── 用户筛选后的白名单（与 court.py 一致）──
_ALLOWED_COMMON_TEXTS = {
    "消灭", "阵亡", "单独消灭", "最后一击",
    "武器命中率", "暴击命中率",
}
_ALLOWED_SPECIAL_BY_HERO: Dict[str, set] = {
    'D.Va': set(), '伊拉锐': {'治疗量'}, '半藏': set(), '卡西迪': {'暴击命中率', '武器命中率'},
    '卢西奥': {'拯救玩家', '治疗量'}, '回声': {'黏性炸弹直接命中率'}, '埃姆雷': {'暴击命中率', '武器命中率'},
    '堡垒': set(), '士兵：76': {'螺旋飞弹命中率'}, '天使': {'复活玩家', '拯救玩家', '治疗量'},
    '奥丽莎': {'能量标枪命中率'}, '安娜': {'开镜命中率', '拯救玩家', '麻醉镖命中率', '治疗量'},
    '安燃': set(), '巴蒂斯特': {'拯救玩家', '治疗命中率', '治疗量'}, '布丽吉塔': {'流星飞锤命中率', '鼓舞士气持续时间占比', '治疗量'},
    '弗蕾娅': set(), '托比昂': set(), '拉玛刹': {'猛拳命中率'}, '探奇': {'直接命中率'},
    '斩仇': {'锋锐剑气命中率'}, '无漾': {'治疗量'}, '末日铁拳': set(), '朱诺': {'拯救玩家', '治疗量'},
    '查莉娅': {'主要攻击模式命中率', '辅助攻击模式命中率'}, '死怨': {'交叉枪决命中率', '纵情狂飙空中发射命中率'},
    '死神': set(), '毛加': set(), '法老之鹰': {'击退消灭', '直接命中率'}, '渣客女王': {'锯齿利刃命中率'},
    '温斯顿': {'辅助攻击模式命中率'}, '源氏': set(), '狂鼠': {'直接命中率'}, '猎空': {'脉冲炸弹命中率'},
    '瑞稀': {'缚魂锁链命中率', '治疗量'}, '生命之梭': {'拯救玩家', '治疗量'}, '破坏球': set(),
    '禅雅塔': {'拯救玩家', '治疗量'}, '秩序之光': {'辅助攻击模式命中率'}, '索杰恩': {'充能射击命中率', '充能射击暴击率'},
    '美': {'冰锥命中率', '冰锥暴击率'}, '艾什': {'开镜命中率', '开镜暴击率'}, '莫伊拉': {'拯救玩家', '治疗量'},
    '莱因哈特': {'烈焰打击命中率'}, '西拉': {'追踪弹命中率'}, '西格玛': {'质量吸附命中率'},
    '路霸': {'链钩命中率'}, '金驭': set(), '雾子': {'拯救玩家', '治疗量'}, '飞天猫': {'治疗量'},
    '骇灾': set(), '黑影': set(), '黑百合': {'开镜暴击率'},
}

_ALLOWED_COMMON_GUIDS = {_ATTR_TEXT_TO_GUID[t] for t in _ALLOWED_COMMON_TEXTS if t in _ATTR_TEXT_TO_GUID}
_HERO_ATTR_GUIDS: Dict[str, set] = {}
_HERO_SPECIAL_ATTR_GUIDS: Dict[str, set] = {}
_GENERAL_ATTR_GUIDS = _ALLOWED_COMMON_GUIDS
for _hero_name, _allowed_special_texts in _ALLOWED_SPECIAL_BY_HERO.items():
    _hero_guid = _HERO_NAME_TO_GUID.get(_hero_name)
    if not _hero_guid:
        continue
    _special_guids = {_ATTR_TEXT_TO_GUID[t] for t in _allowed_special_texts if t in _ATTR_TEXT_TO_GUID}
    _HERO_SPECIAL_ATTR_GUIDS[_hero_guid] = _special_guids
    _HERO_ATTR_GUIDS[_hero_guid] = _ALLOWED_COMMON_GUIDS | _special_guids


def _stat_allowed_for_hero(value_guid: str, hero_guid: str) -> bool:
    return value_guid in _GENERAL_ATTR_GUIDS or value_guid in _HERO_ATTR_GUIDS.get(hero_guid, set())


# ── 法官 Prompt（与 court.py 一致）──
_COURT_SYSTEM_PROMPT = """[ROLE] 角色与语气设定
你是一位资深竞技游戏玩家兼数据分析师，擅长用「脱口秀式毒舌」风格对玩家的对局数据进行复盘点评。你的文字既有专业数据的支撑，又有极强的娱乐性和画面感，读起来像是一位又爱又恨的老队友在赛后吐槽。
语气要求：戏谑、犀利、阴阳怪气但不恶意，保持「损友」般的亲切感。善用反讽、夸张和反转。
修辞要求：大量使用游戏黑话与生活化比喻的混搭，避免干巴巴的描述。尽可能理解并且创造新的比喻。
在本庭中，你的身份是守望先锋电竞法庭的主审法官，圈内人称「数据判官」。语气严肃如宣读判决书，有功者当庭嘉奖绝不吝啬溢美之词，有过者罪状罗列条条诛心。可少量使用 emoji。

[CONTEXT] 游戏背景与修辞库
1. 守望先锋段位名称：青铜、白银、黄金、白金、钻石、大师、宗师、英杰。
2. 比喻参考库：
状态不稳定类：情绪不稳定的数据电池、心电图、数据过山车、随机数生成器、情绪盲盒、接触不良的数据电池、人形骰子、薛定谔的C位、信号不好的路由器、间歇性战神体验卡。
无效贡献类：空气掩护、用身体打伤害、行走的充电宝、战术性自杀、蹭地图经验涨KD、团队ATM机、敌方能量加速器、移动复活点。
高光统治类：战神下凡、把对面点位焊死、职业选手体验生活、人形外挂、把对面当兵线补、输出端装了GPS。
拉胯下限类：会飞的咸鱼、空中活靶子、开雾逛该的观光客、落地成盒、纯度极高的咸鱼、键盘撒米鸡啄选手、人机练习赛VIP。
数据结果背离类：华丽数据证明无用、KDA骗子、用队友的命换评分、胜利是队友扛着走的。

[OBJECTIVE] 核心任务
严格基于提供的原始对局数据，对焦点玩家及本局所有玩家进行审判分析，输出一份电竞法庭判决书。

[CONSTRAINTS] 硬性约束与底线
1. 仅针对游戏内数据、赛场表现、英雄数据点评，绝不涉及外貌、私生活、人品等人身攻击；不输出任何歧视、引战、恶意辱骂内容。
2. 所有判决严格基于提供的原始数据，禁止编造数据、篡改数据含义、夸大数据结论。
3. 严禁跨职责直接比较伤害或治疗等核心指标，阴阳调侃必须对应明确的数据论据。
4. 不讨论外挂、代练等违规行为，禁止进行反事实推演或假设性陈述，仅限描述已发生事件。

[WORKFLOW] 评判规则与审判工作流
步骤一：职责核心指标评估（综合评估，技能指标权重低）
坦克位参考：单独消灭、最后一击、(伤害减受疗)、阵亡数、击杀参与率等其他技能指标。
输出位参考：单独消灭、最后一击、伤害、阵亡数、击杀参与率等其他技能指标。
辅助位参考：最后一击、阵亡数、拯救玩家、单独消灭、伤害、治疗量、击杀参与率等其他技能指标。

步骤二：数据对比与评分
1. 将焦点玩家数据与同英雄「数据参考行」对比，低于参考值应扣分，禁止跨英雄比较。
2. 同一玩家同一局可能在「英雄片段」内出现多个英雄，时长小于3分钟的片段为低权重。
3. 最后一击和单独消灭应额外加分，频繁阵亡且团队贡献低应加重扣分。
4. 综合看英雄数据。例如有的输出英雄伤害低但最后一击高，有的辅助英雄输出高但治疗少，需综合参考值考虑，不要跨英雄对比。
5. 解构无效数据：不要被表面虚高数据欺骗（如坦克刷伤害无击杀且参与率低；辅助刷治疗无拯救且参与率低；输出伤害高但参与率低）。若空有治疗或伤害但击杀参与率极低，评价为「无效数据刷子」。注意部分输出英雄定位为收割或骚扰，可能伤害低但参与率高、最后一击多，值得赞赏。
6. 对于单独消灭高的玩家应赞赏，单独消灭低不批评不评价。
7. 比赛胜负不影响评分，只论数据。
8. 若某局数据异常（如焦点玩家和队友英雄全空、字段全空；击杀阵亡是参考值正负230%导致参考价值低），注释写「数据缺失，无法评价」。

步骤三：审判任务
1. 从焦点玩家所在队伍的队友（不含对手）中找出本局 MVP（最佳表现者），给出判决理由。
2. 从焦点玩家所在队伍的队友（不含对手）中找出本局最差玩家（被告），给出判决理由和「原罪清单」（具体犯了哪些错误，用数据说话）。
3. 对焦点玩家做出判决：是有功之臣还是拖累全队，给出评分 S/A/B/C/D。
4. 对焦点玩家所在队伍的所有玩家逐一做出有功/有过/无功无过的判决，附一句话理由。
5. 分析三路对位差距：数据已按坦克→输出→辅助排序。对位规则：坦克位一对一比较；输出位整体比较（我方输出组 vs 对方输出组，不要拆分编号）；辅助位整体比较（我方辅助组 vs 对方辅助组，不要拆分编号）。

[OUTPUT FORMAT] 输出格式与字段规范
1. 必须严格使用中文。最终只输出一个合法的 JSON 对象，禁止输出 markdown 代码块、注释或任何 JSON 之外的文字。
2. JSON 对象必须严格遵循下方 JSON Schema，包含完整字段（不输出 ⚖️/📋/🗺️ 等 emoji，渲染时会自动添加）：
   - case_no（字符串）：对局序号，如 "1"
   - location（字符串）：地图名
   - mvp（对象）：含 player（玩家名）和 reason（MVP 理由，约 50~80 字，数据驱动、生动有趣）
   - defendant（对象）：含 player（被告名，即最差表现者）和 charges（原罪清单，约 50~80 字，毒舌调侃、条条诛心）
   - focus_verdict（对象）：含 player（焦点玩家名）、score（评分 S/A/B/C/D）、reason（判决理由，约 50~80 字）
   - team_verdicts（数组）：全队每人一条，含 player（玩家名）和 verdict（一句话判决，约 20~40 字），焦点玩家排第一位
   - lane_analysis（对象）：含 tank（坦克位一对一对比分析）、dps（输出位整体对比）、healer（辅助位整体对比），各约 30~50 字
3. 所有判决严格基于原始数据，禁止编造数据。好的表现必须肯定赞赏，差的表现应毒舌调侃。评分 S 最好，D 最差。

JSON Schema 定义：
{court_json_schema}

[INPUT DATA] 输入数据
"""


# ── 多字段结构化 JSON Schema（与 shiqu 一致：response_format=json_schema）──
# 渲染端将结构化字段组装为带 ⚖️/📋/🗺️ 标记的完整判决书正文；
# 旧版单字段 {"verdict":"..."} 格式兼容（渲染端自动识别）。
_COURT_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "case_no", "location", "mvp", "defendant",
        "focus_verdict", "team_verdicts", "lane_analysis",
    ],
    "properties": {
        "case_no": {"type": "string", "description": "对局序号，如 1"},
        "location": {"type": "string", "description": "地图名"},
        "mvp": {
            "type": "object", "additionalProperties": False,
            "required": ["player", "reason"],
            "properties": {
                "player": {"type": "string"},
                "reason": {"type": "string"},
            },
        },
        "defendant": {
            "type": "object", "additionalProperties": False,
            "required": ["player", "charges"],
            "properties": {
                "player": {"type": "string"},
                "charges": {"type": "string"},
            },
        },
        "focus_verdict": {
            "type": "object", "additionalProperties": False,
            "required": ["player", "score", "reason"],
            "properties": {
                "player": {"type": "string"},
                "score": {"type": "string", "description": "S/A/B/C/D"},
                "reason": {"type": "string"},
            },
        },
        "team_verdicts": {
            "type": "array",
            "description": "全队审判列表（焦点玩家排第一位）",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["player", "verdict"],
                "properties": {
                    "player": {"type": "string"},
                    "verdict": {"type": "string"},
                },
            },
        },
        "lane_analysis": {
            "type": "object", "additionalProperties": False,
            "required": ["tank", "dps", "healer"],
            "properties": {
                "tank": {"type": "string"},
                "dps": {"type": "string"},
                "healer": {"type": "string"},
            },
        },
    },
}


def _is_valid_court_json(text: str) -> bool:
    """校验 LLM 返回是否为合法 court JSON（含全部必需字段）。"""
    try:
        obj = json.loads(text)
    except Exception:
        return False
    if not isinstance(obj, dict):
        return False
    required = ["case_no", "location", "mvp", "defendant", "focus_verdict", "team_verdicts", "lane_analysis"]
    if not all(k in obj for k in required):
        return False
    # 校验嵌套字段
    for key, sub_fields in [("mvp", ["player", "reason"]),
                             ("defendant", ["player", "charges"]),
                             ("focus_verdict", ["player", "score", "reason"])]:
        sub = obj.get(key)
        if not isinstance(sub, dict) or not all(f in sub for f in sub_fields):
            return False
    lanes = obj.get("lane_analysis")
    if not isinstance(lanes, dict) or not all(f in lanes for f in ["tank", "dps", "healer"]):
        return False
    tvs = obj.get("team_verdicts")
    if not isinstance(tvs, list) or len(tvs) < 1:
        return False
    for tv in tvs:
        if not isinstance(tv, dict) or "player" not in tv or "verdict" not in tv:
            return False
    return True


# ── Prompt 构建 ──

def _build_court_prompt(raw_data: Dict[str, Any], target_id: str, db: Optional[IDPoolDB] = None) -> str:
    """基于对局详情构建电竞法庭 LLM prompt（函数体移植自 court.py）。"""
    detail = raw_data.get("detail", {}) or {}
    detail_data = detail.get("data") or {} if isinstance(detail, dict) else {}
    source = raw_data.get("source_match", {}) or {}

    map_guid = str(detail_data.get("mapGuid") or source.get("mapGuid") or "")
    map_name = MAP_DICT.get(map_guid, "未知")

    result_map = {1: "胜利", 0: "平局", -1: "失败"}
    match_ret = detail_data.get("matchRet", source.get("matchRet"))
    result_text = result_map.get(match_ret, str(match_ret))
    team_score = detail_data.get("teamScore", source.get("teamScore", "?"))
    opp_score = detail_data.get("opponentScore", source.get("opponentScore", "?"))
    game_sec = detail_data.get("gameTimeSec", 0) or 0
    duration = f"{int(game_sec // 60):02d}:{int(game_sec % 60):02d}"

    raw_mode = str(detail_data.get("gameMode") or source.get("gameMode") or source.get("instanceType") or "")
    game_mode = _MODE_DICT.get(raw_mode, raw_mode)

    tm_list_raw = detail_data.get("teammateList", [])
    en_list_raw = detail_data.get("enemyList", [])

    def _calc_scores() -> Dict[str, int]:
        def _sum(players: List[dict], key: str) -> float:
            return sum(float(p.get(key, 0) or 0) for p in players if isinstance(p, dict))

        try:
            t = tm_list_raw
            e = en_list_raw
            gt = float(detail_data.get("gameTimeSec", 1) or 1)

            s_time = min(100.0, (gt / 1200.0) * 100.0)
            t_obj = _sum(t, "targetCompetingTime")
            e_obj = _sum(e, "targetCompetingTime")
            s_obj = (t_obj / (t_obj + e_obj) * 100.0) if (t_obj + e_obj) > 0 else 50.0
            t_block = _sum(t, "resistDamage")
            e_block = _sum(e, "resistDamage")
            s_block = (t_block / (t_block + e_block) * 100.0) if (t_block + e_block) > 0 else 50.0
            t_heal = _sum(t, "cure")
            t_death = _sum(t, "death")
            e_heal = _sum(e, "cure")
            e_death = _sum(e, "death")
            t_hd = t_heal / max(1.0, t_death)
            e_hd = e_heal / max(1.0, e_death)
            s_hd = (t_hd / (t_hd + e_hd) * 100.0) if (t_hd + e_hd) > 0 else 50.0
            total_death = t_death + e_death
            s_death = ((1.0 - (t_death / total_death)) * 100.0) if total_death > 0 else 50.0
            anti_pressure = s_time * 0.15 + s_obj * 0.2 + s_block * 0.2 + s_hd * 0.25 + s_death * 0.2

            t_elim = _sum(t, "kill")
            t_fb = _sum(t, "finalHit")
            t_assist = _sum(t, "assist")
            teamwork = min(100.0, (((t_elim - t_fb) + t_assist) / max(1.0, t_elim)) * 60.0)

            t_dmg = _sum(t, "heroDamage")
            e_dmg = _sum(e, "heroDamage")
            e_elim = _sum(e, "kill")
            e_fb = _sum(e, "finalHit")
            s_dmg = (t_dmg / (t_dmg + e_dmg) * 100.0) if (t_dmg + e_dmg) > 0 else 50.0
            s_kill = (t_elim / (t_elim + e_elim) * 100.0) if (t_elim + e_elim) > 0 else 50.0
            s_fb = (t_fb / (t_fb + e_fb) * 100.0) if (t_fb + e_fb) > 0 else 50.0
            aggressiveness = s_dmg * 0.4 + s_kill * 0.3 + s_fb * 0.3

            def _balance(v1: float, v2: float) -> float:
                return 1.0 - abs(v1 - v2) / max(1e-9, v1 + v2)

            balance_score = (_balance(t_dmg, e_dmg) + _balance(t_elim, e_elim) + _balance(t_heal, e_heal)) / 3.0 * 100.0
            match_quality = s_time * 0.4 + balance_score * 0.6

            return {
                "anti_pressure": int(anti_pressure),
                "teamwork": int(teamwork),
                "aggressiveness": int(aggressiveness),
                "match_quality": int(match_quality),
            }
        except Exception:
            return {"anti_pressure": 0, "teamwork": 0, "aggressiveness": 0, "match_quality": 0}

    scores = _calc_scores()

    lines = [
        "[对局概览]",
        "{",
        f"  地图: {map_name},",
        f"  模式: {game_mode},",
        f"  结果: {result_text} (比分 {team_score}:{opp_score}),",
        f"  时长: {duration},",
        f"  焦点玩家: {target_id},",
        f"  属性分: 抗压{scores['anti_pressure']} 团队{scores['teamwork']} 进攻{scores['aggressiveness']} 质量{scores['match_quality']}",
        "}",
        "",
    ]

    ROLE_ORDER = {"tank": 0, "dps": 1, "healer": 2}
    ROLE_LABEL_ZH = {"tank": "坦克", "dps": "输出", "healer": "辅助"}

    def _get_role(player: dict) -> str:
        hero_guid = str(player.get("heroGuid", ""))
        return HERO_DICT.get(hero_guid, {}).get("role", "unknown")

    def _sort_and_label(players: List[dict]) -> List[tuple]:
        if not players:
            return []
        indexed = [
            (ROLE_ORDER.get(_get_role(p), 9), i, p)
            for i, p in enumerate(players) if isinstance(p, dict)
        ]
        indexed.sort(key=lambda x: (x[0], x[1]))
        role_counters: Dict[str, int] = {}
        labeled = []
        for _, _, p in indexed:
            role = _get_role(p)
            cnt = role_counters.get(role, 0) + 1
            role_counters[role] = cnt
            label_zh = ROLE_LABEL_ZH.get(role, role)
            if sum(1 for pp in players if isinstance(pp, dict) and _get_role(pp) == role) > 1:
                position_label = f"{label_zh}{cnt}"
            else:
                position_label = label_zh
            labeled.append((p, position_label))
        return labeled

    _SKIP_FIELDS = {
        "name", "bnetId", "heroGuid", "heroIcon", "customerToken",
        "beginTs", "friendBnetIds", "endorserBnetIds", "perks",
        "killMax", "cureMax", "heroDamageMax", "resistDamageMax",
        "targetCompetingTime",
    }
    _STAT_FIELD_ORDER = [
        ("kill", "击杀"), ("assist", "助攻"), ("death", "阵亡"),
        ("finalHit", "最后一击"), ("heroDamage", "伤害"),
        ("damageTaken", "承伤"), ("cure", "治疗"),
        ("healingTaken", "受疗"), ("resistDamage", "格挡"),
    ]

    def _collect_all_stat_keys(players: List[dict]) -> List[tuple]:
        covered = {k for k, _ in _STAT_FIELD_ORDER}
        covered.update(_SKIP_FIELDS)
        seen: set = set()
        extra = []
        for p in players:
            if not isinstance(p, dict):
                continue
            for k, v in p.items():
                if k in covered or k in seen:
                    continue
                if isinstance(v, (int, float)):
                    seen.add(k)
                    extra.append((k, k))
        return extra

    _BIG_NUM_FIELDS = {"heroDamage", "damageTaken", "cure", "healingTaken", "resistDamage"}

    def _fmt_player(p: dict, pos_label: str, detail_text: str = "") -> str:
        name = str(p.get("name", "?"))
        hero_guid = str(p.get("heroGuid", ""))
        hero_name = HERO_DICT.get(hero_guid, {}).get("name", "?")
        display = f"*{name}" if name == target_id else name

        pairs = [f"位置: {pos_label}", f"玩家: {display}", f"英雄: {hero_name}"]
        for key, cn in _STAT_FIELD_ORDER:
            v = p.get(key, 0) or 0
            formatted = f"{int(v):,}" if key in _BIG_NUM_FIELDS else str(int(v))
            pairs.append(f"{cn}: {formatted}")
        for key, _ in extra_stat_keys:
            v = p.get(key, 0) or 0
            formatted = f"{int(v):,}" if key in _BIG_NUM_FIELDS else str(int(v))
            pairs.append(f"{key}: {formatted}")
        if detail_text:
            pairs.append(f"详细: {{ {detail_text} }}")
        return "{ " + ", ".join(pairs) + " }"

    def _hero_detail_text(p_dict: dict) -> str:
        hl = p_dict.get("_heroList")
        if not hl or not isinstance(hl, list):
            return ""
        name_map = load_stat_name_map()
        parts: List[str] = []
        for entry in hl:
            if not isinstance(entry, dict):
                continue
            sm = entry.get("statMap", {}) or {}
            ut = float(entry.get("userTimeSec", 600) or 600)
            hg = str(entry.get("heroId", ""))
            for guid, raw_val in sm.items():
                guid = str(guid)
                name = name_map.get(guid)
                if not name:
                    continue
                if hg and not _stat_allowed_for_hero(guid, hg):
                    continue
                if should_skip_prompt_stat(value_guid=guid, value_text=name):
                    continue
                nv = normalize_stat_value(raw_val, ut, value_text=name, value_guid=guid)
                if nv is None:
                    continue
                if nv == int(nv):
                    parts.append(f"{name}: {int(nv)}")
                else:
                    parts.append(f"{name}: {nv:.2f}")
        return ", ".join(parts) if parts else ""

    def fmt_team(label: str, players: List[dict]) -> None:
        if not players:
            return
        sorted_players = _sort_and_label(players)
        lines.append(f"[{label}]")
        lines.append("[")
        for p, pos_label in sorted_players:
            hd = _hero_detail_text(p)
            lines.append(f"  {_fmt_player(p, pos_label, hd)},")
            if db is not None:
                hg = str(p.get("heroGuid", ""))
                hn = HERO_DICT.get(hg, {}).get("name", "?")
                ref = build_broad_reference_text(db, str(p.get("name", "?")), hg, hn)
                if ref:
                    lines.append(f"    # 数据参考: {ref}")
        lines.append("]")
        lines.append("")

    extra_stat_keys: List[tuple] = []
    all_players = detail_data.get("teammateList", []) + detail_data.get("enemyList", [])
    extra_stat_keys = _collect_all_stat_keys(all_players)

    fmt_team("队友", detail_data.get("teammateList", []))
    fmt_team("对手", detail_data.get("enemyList", []))

    if not detail_data.get("teammateList") and not detail_data.get("enemyList"):
        lines.append("[注意：未能提取结构化数据，以下为原始 JSON，请自行解析字段含义]")
        lines.append(json.dumps(detail, ensure_ascii=False, indent=2))

    match_text = "\n".join(lines)
    return _COURT_SYSTEM_PROMPT.replace(
        "{court_json_schema}", json.dumps(_COURT_JSON_SCHEMA, ensure_ascii=False, indent=2)
    ) + match_text


# ── LLM 调用（与 shiqu 共用配置，输出纯文本）──

async def _call_llm(prompt: str) -> Optional[str]:
    """调用 court LLM（纯文本）。配置、信号量、网络行为均与 shiqu 一致（不含 json_schema）。"""
    cfg = get_shiqu_llm_config()
    if not (cfg.base_url and cfg.api_key and cfg.model):
        logger.error(
            "[court] LLM 配置不完整，请在 config/shiqu_config.py 中填写 "
            "SHIQU_LLM_BASE_URL / SHIQU_LLM_API_KEY / SHIQU_LLM_MODEL"
        )
        return None
    payload = {
        "model": cfg.model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": cfg.stream,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "court_verdict", "strict": True, "schema": _COURT_JSON_SCHEMA},
        },
    }
    headers = {"Authorization": f"Bearer {cfg.api_key}", "Content-Type": "application/json"}
    from ..analysis_common import build_async_client, get_analysis_proxy, LLM_SEMAPHORE

    proxy = get_analysis_proxy(cfg.base_url)
    await LLM_SEMAPHORE.acquire()
    try:
        async with build_async_client(timeout=cfg.timeout_seconds, proxy_url=proxy) as client:
            try:
                if cfg.stream:
                    parts: List[str] = []
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
                logger.error(f"[court] LLM 调用异常: {e}", exc_info=True)
                return None
    finally:
        LLM_SEMAPHORE.release()


# ── 主流程 ──

class CourtModule:
    def __init__(self) -> None:
        self.match_module = dashen_match_module

    async def _enrich_teammate_details(
        self, root: Dict[str, Any], customer_token: str, match_id: str = ""
    ) -> None:
        """用队友各自 token + 本局 match_id 重新拉取同局详情，补齐 _heroList。"""
        focus_match_id = str(match_id or root.get("matchId") or "")
        if not focus_match_id:
            return
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
                logger.warning(f"[court] 队友 {p.get('name')} 详情拉取失败: {exc}")

    async def analyze(
        self,
        query: CourtQuery,
        *,
        db: Optional[IDPoolDB] = None,
    ) -> Dict[str, Any]:
        """业务主入口：解析目标 → 拉取单局详情 → 补齐队友英雄 → 构建 Prompt → 调用 LLM → 落库。

        Args:
            query: CourtQuery（bnet_id / customer_token / index）
            db: 可选 IDPoolDB 实例（用于分段参考）
        Returns:
            {"target_id", "index", "raw_text", "map_name", "game_mode", "match_index"}
        Raises:
            ModuleError: 数据不足 / LLM 未配置 / LLM 失败
        """
        if not query.bnet_id and not query.customer_token:
            raise ModuleError(error="missing_target", message="bnet_id 或 customer_token 不能为空。", status_code=400)

        # ── use_db 模式：跳过上游抓取与 LLM 调用，直接复用数据库里最近一次判决书 ──
        if query.use_db:
            return await self._analyze_from_db(query)

        if not is_shiqu_llm_configured():
            raise ModuleError(
                error="court_llm_not_configured",
                message="电竞法庭 LLM 未配置，请在 config/shiqu_config.py 中填写 SHIQU_LLM_BASE_URL / SHIQU_LLM_API_KEY / SHIQU_LLM_MODEL。",
                status_code=500,
            )

        index = int(query.index or 0)
        # 通过 dashen_match 解析列表拿到 customer_token（bnet_id 二选一）
        from ..dashen_match.requests import DashenMatchQuery

        list_query = DashenMatchQuery(
            customer_token=query.customer_token,
            bnet_id=query.bnet_id,
            target_count=100,
            include_fight=False,
            include_previous_season=True,
        )
        try:
            list_output = await self.match_module.query_match_list(list_query, render=False)
        except Exception as exc:
            raise ModuleError(error="match_list_failed", message=f"获取对局列表失败: {exc}", status_code=502) from exc

        customer_token = list_output.customer_token
        resolved = list_output.resolved_bnet
        full_id = resolved.full_id if resolved else (query.bnet_id or f"token:{customer_token[:8]}")

        matches = list_output.matches or []
        if not matches or index < 0 or index >= len(matches):
            raise ModuleError(
                error="match_not_found",
                message=f"未找到第 {index + 1} 局对局（共 {len(matches)} 局）。",
                status_code=404,
            )

        try:
            detail_output = await self.match_module.query_match_detail(
                customer_token, matches[index], render=False
            )
        except Exception as exc:
            raise ModuleError(error="match_detail_failed", message=f"获取对局详情失败: {exc}", status_code=502) from exc

        root = _extract_match_detail_data(detail_output.detail.payload)
        # 本局 match_id：优先取列表项的 matchId，其次 source_match
        focus_match_id = str(
            matches[index].get("matchId")
            or (detail_output.detail.source_match or {}).get("matchId")
            or ""
        )
        # 焦点玩家真实昵称（对局内 name），用于 prompt 标识与高亮
        focus_display_name = full_id
        for _p in (root.get("teammateList", []) or []):
            if not isinstance(_p, dict):
                continue
            if str(_p.get("customerToken", "")) == str(customer_token):
                focus_display_name = str(_p.get("name") or full_id)
                break
        # 补齐队友英雄详情（_heroList）
        await self._enrich_teammate_details(root, customer_token, focus_match_id)

        map_name = MAP_DICT.get(str(root.get("mapGuid") or ""), "未知")
        raw_mode = str(root.get("gameMode") or (detail_output.detail.source_match or {}).get("gameMode") or "")
        game_mode = _MODE_DICT.get(raw_mode, raw_mode)

        prompt = _build_court_prompt(
            {
                "detail": {"data": root},
                "source_match": detail_output.detail.source_match or {},
            },
            focus_display_name,
            db=db,
        ).replace("{index_hint}", str(index + 1)).replace("{target_player}", focus_display_name)

        # ── LLM 调用（retry=0 → 仅 1 次；retry=N → 初始 1 次 + 重试 N 次，恢复等待交由单次调用超时承担）──
        last_text = ""
        result = None
        call_count = 0
        cfg = get_shiqu_llm_config()
        max_attempts = 1 + (cfg.retry or 0)
        _t0 = time.perf_counter()
        court_llm_status.mark_call_start(full_id)
        success = False
        try:
            for attempt in range(1, max_attempts + 1):
                call_count += 1
                last_text = await _call_llm(prompt) or ""
                # 强制 JSON 输出：校验 verdict 字段，非法则重试（最终失败在 finally 落库并报错）
                if last_text and _is_valid_court_json(last_text):
                    result = last_text
                    break
                logger.warning(f"[court] LLM 尝试 {attempt}/{max_attempts} 返回非合法 JSON，准备重试")
            success = bool(result)
            if not result:
                raise ModuleError(
                    error="court_llm_failed",
                    message="AI 判决书生成失败：大模型调用异常 / 返回内容不是合法 JSON。",
                    status_code=502,
                )
        finally:
            duration_ms = int((time.perf_counter() - _t0) * 1000)
            court_llm_status.mark_call_done(
                success, "" if success else "LLM 返回为空或调用异常"
            )
            # ── 落库（异步、best-effort，不阻塞主流程）──
            # 必须放在 finally 内：无论成功还是 raise 失败都落库，否则失败调用既不记录
            # 错误也不记录 prompt，无法排查。只存提示词 / 原始返回 / 调用诊断 + 渲染元数据；
            # 判决书由 raw_response 解析（失败时 raw_response 为 ""）。
            try:
                await court_llm_recorder.enqueue(
                    target_id=full_id,
                    raw_response=result,
                    match_index=index,
                    map_name=map_name,
                    game_mode=game_mode,
                    prompt=prompt,
                    ok=success,
                    duration_ms=duration_ms,
                    call_count=call_count,
                )
            except Exception as exc:
                logger.warning(f"[court] 落库失败（忽略）: {exc}")

        return {
            "target_id": full_id,
            "index": index,
            "raw_text": result,
            "map_name": map_name,
            "game_mode": game_mode,
            "match_index": index,
        }

    async def _analyze_from_db(self, query: CourtQuery) -> Dict[str, Any]:
        """use_db 模式：从 court_llm 数据库读取该玩家最近一次判决书与渲染元数据。

        use_db 模式下忽略 index，直接取该玩家最新一条记录（无论第几局）。
        """
        target_id = str(query.bnet_id or f"token:{query.customer_token[:8]}")
        row = await asyncio.to_thread(
            court_llm_recorder.db.get_latest_by_target, target_id
        )
        if not row or not (row.get("raw_response") or "").strip():
            raise ModuleError(
                error="db_record_not_found",
                message=f"数据库中未找到玩家 {target_id} 的判决书记录，请先正常生成一次。",
                status_code=404,
            )
        index = int(row.get("match_index", 0))
        # use_db 模式：渲染时间取该记录的落库时间，而非当前时间。
        created_at = row.get("created_at")
        generated_at = (
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(created_at)))
            if created_at
            else ""
        )
        return {
            "target_id": target_id,
            "index": index,
            "raw_text": row["raw_response"],
            "map_name": row.get("map_name") or "未知",
            "game_mode": row.get("game_mode") or "",
            "match_index": int(row.get("match_index", index)),
            "generated_at": generated_at,
        }


court_module = CourtModule()


__all__ = ["CourtModule", "CourtQuery", "court_module"]
