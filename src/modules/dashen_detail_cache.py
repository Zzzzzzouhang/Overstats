from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

try:
    from overstats.src.db.match_stats import IDPoolDB
except ModuleNotFoundError:
    from src.db.match_stats import IDPoolDB


def _report_cache_hit(source: str) -> None:
    try:
        from overstats.src.server import report_db_cache_hit
    except ModuleNotFoundError:
        try:
            from src.server import report_db_cache_hit
        except ModuleNotFoundError:
            return
    report_db_cache_hit(source)


def _loads_list(value: Any) -> List[Any]:
    if not value:
        return []
    if isinstance(value, list):
        return list(value)
    try:
        parsed = json.loads(str(value))
    except Exception:
        return []
    return list(parsed) if isinstance(parsed, list) else []


def _player_dict(row: Dict[str, Any], *, include_competitive_fields: bool = False) -> Dict[str, Any]:
    rank_bucket = row.get("rank_bucket")
    rank_score = row.get("rank_score") or rank_bucket or 0
    player = {
        "bnetId": row.get("player_bnet_id"),
        "name": row.get("player_name", ""),
        "heroGuid": row.get("hero_guid", ""),
        "kill": row.get("kill", 0),
        "assist": row.get("assist", 0),
        "death": row.get("death", 0),
        "heroDamage": row.get("hero_damage", 0),
        "cure": row.get("healing", 0),
        "resistDamage": row.get("damage_blocked", 0),
        "damageTaken": row.get("hero_damage_taken", 0),
        "finalHit": row.get("final_hit", 0),
        "soloKills": row.get("solo_kills", 0),
        "targetCompetingTime": row.get("target_competing_time", 0),
        "healingTaken": row.get("healing_taken", 0),
        "rankInfo": {"rankScore": rank_score},
        "friendBnetIds": _loads_list(row.get("friend_bnet_ids_json")),
        "endorserBnetIds": _loads_list(row.get("endorse_bnet_ids_json")),
    }
    if include_competitive_fields:
        # match_player currently does not persist customerToken or raw rankScore.
        # Only expose values when future schema additions provide them.
        customer_token = str(row.get("customer_token") or "").strip()
        if customer_token:
            player["customerToken"] = customer_token
    return player


def load_normal_match_detail_from_db(
    match_id: str,
    *,
    focus_bnet_id: str = "",
    db: Optional[IDPoolDB] = None,
    require_hero_list: bool = True,
    include_competitive_fields: bool = False,
    report_hit: bool = True,
) -> Optional[Dict[str, Any]]:
    """Reconstruct a queryMatchInfo-like normal match detail from SQLite.

    Returns upstream-compatible payload data, not the outer {code,data} wrapper.
    The function is deliberately conservative and returns None when required
    fields are missing so callers can fall back to upstream queryMatchInfo.
    """

    normalized_match_id = str(match_id or "").strip()
    if not normalized_match_id:
        return None
    db_adapter = db or IDPoolDB()
    try:
        meta = db_adapter.get_match_meta([normalized_match_id]).get(normalized_match_id)
        if not meta or not int(meta.get("game_time_sec") or 0):
            return None
        players = (db_adapter.get_match_players([normalized_match_id]) or {}).get(normalized_match_id) or []
        if len(players) < 10:
            return None

        resolved_focus_bnet = str(focus_bnet_id or "").strip()
        if not resolved_focus_bnet:
            for player in players:
                if player.get("friend_bnet_ids_json"):
                    resolved_focus_bnet = str(player.get("player_bnet_id") or "").strip()
                    break

        focus_side = "team"
        for player in players:
            if resolved_focus_bnet and str(player.get("player_bnet_id") or "") == resolved_focus_bnet:
                focus_side = str(player.get("side") or "team")
                break
        match_ret = db_adapter.get_player_result(meta, focus_side)

        teammates = [
            _player_dict(player, include_competitive_fields=include_competitive_fields)
            for player in players
            if str(player.get("side") or "") == "team"
        ]
        enemies = [
            _player_dict(player, include_competitive_fields=include_competitive_fields)
            for player in players
            if str(player.get("side") or "") == "enemy"
        ]
        if len(teammates) < 5 or len(enemies) < 5:
            return None
        if focus_side == "enemy":
            teammates, enemies = enemies, teammates

        name_map: Dict[str, str] = {}
        for player in players:
            bnet_id = str(player.get("player_bnet_id") or "").strip()
            name = str(player.get("player_name") or "").strip()
            if bnet_id and name:
                name_map[bnet_id] = name

        hero_list: List[Dict[str, Any]] = []
        if resolved_focus_bnet:
            for row in db_adapter.get_hero_details_by_match_player(normalized_match_id, resolved_focus_bnet):
                try:
                    stat_map = json.loads(row.get("stat_map_json") or "{}")
                except Exception:
                    stat_map = {}
                if not stat_map:
                    continue
                hero_list.append(
                    {
                        "heroGuid": row.get("hero_guid", ""),
                        "heroId": row.get("hero_guid", ""),
                        "userTimeSec": row.get("use_time_sec", 0),
                        "useTimeRate": row.get("use_time_rate", 0),
                        "statMap": stat_map,
                    }
                )
        if require_hero_list and not hero_list:
            return None

        if report_hit:
            _report_cache_hit("db")
        return {
            "matchRet": match_ret,
            "gameTimeSec": int(meta.get("game_time_sec") or 0),
            "mapGuid": str(meta.get("map_guid") or ""),
            "startTime": int(meta.get("start_time") or 0),
            "bnetId": resolved_focus_bnet,
            "heroList": hero_list,
            "teammateList": teammates,
            "enemyList": enemies,
            "nameMap": name_map,
        }
    except Exception:
        return None


def normal_detail_payload_from_db(
    match_id: str,
    *,
    focus_bnet_id: str = "",
    db: Optional[IDPoolDB] = None,
    require_hero_list: bool = True,
    include_competitive_fields: bool = False,
    report_hit: bool = True,
) -> Optional[Dict[str, Any]]:
    data = load_normal_match_detail_from_db(
        match_id,
        focus_bnet_id=focus_bnet_id,
        db=db,
        require_hero_list=require_hero_list,
        include_competitive_fields=include_competitive_fields,
        report_hit=report_hit,
    )
    if data is None:
        return None
    return {"code": 0, "success": True, "msg": "ok", "data": data}


__all__ = ["load_normal_match_detail_from_db", "normal_detail_payload_from_db"]
