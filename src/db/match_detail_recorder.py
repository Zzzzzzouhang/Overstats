from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import parse_qs, urlsplit

try:
    from overstats.config import is_database_write_enabled
    from overstats.src.db.match_stats import (
        IDPoolDB,
        MATCH_META_FIELDS,
        MATCH_META_TABLE,
    )
    from overstats.src.modules.dashen_summary.runtime.stat_reference import (
        normalize_dashen_hero_stat_value,
        normalize_hero_rank_score,
    )
    from overstats.src.modules.query_tool import read_query_tool
    from overstats.src.modules.dashen_request_cache import current_cache_week
    from overstats.src.modules.dashen_request_cache import cache_owner_key
except ModuleNotFoundError:
    from config import is_database_write_enabled
    from src.db.match_stats import (
        IDPoolDB,
        MATCH_META_FIELDS,
        MATCH_META_TABLE,
    )
    from src.modules.dashen_summary.runtime.stat_reference import (
        normalize_dashen_hero_stat_value,
        normalize_hero_rank_score,
    )
    from src.modules.query_tool import read_query_tool
    from src.modules.dashen_request_cache import current_cache_week
    from src.modules.dashen_request_cache import cache_owner_key


@dataclass(frozen=True)
class _MatchDetailEvent:
    url: str
    payload: Dict[str, Any]
    game_mode: str = ""


@dataclass
class ParsedNormalMatchDetailBatch:
    hero_detail_rows: List[Dict[str, Any]] = field(default_factory=list)
    comp_data_rows: List[Dict[str, Any]] = field(default_factory=list)
    perk_pick_rows: List[Dict[str, Any]] = field(default_factory=list)
    comp_summary_keys: set[tuple[str, str, Optional[int]]] = field(default_factory=set)
    perk_summary_keys: set[tuple[str, int, Optional[int]]] = field(default_factory=set)
    match_meta_row: Optional[Dict[str, Any]] = None
    match_player_rows: List[Dict[str, Any]] = field(default_factory=list)

    def extend(self, other: "ParsedNormalMatchDetailBatch") -> None:
        self.hero_detail_rows.extend(other.hero_detail_rows)
        self.comp_data_rows.extend(other.comp_data_rows)
        self.perk_pick_rows.extend(other.perk_pick_rows)
        self.comp_summary_keys.update(other.comp_summary_keys)
        self.perk_summary_keys.update(other.perk_summary_keys)
        if other.match_meta_row is not None:
            self.match_meta_row = other.match_meta_row
        self.match_player_rows.extend(other.match_player_rows)


_STAT_VALUE_TEXT_BY_GUID: Optional[Dict[str, str]] = None
_HERO_ROLE_MAP: Optional[Dict[str, str]] = None


def _load_hero_role_map() -> Dict[str, str]:
    """Build a hero_guid -> role_type mapping from query_tool.json."""
    global _HERO_ROLE_MAP
    cached = _HERO_ROLE_MAP
    if cached is not None:
        return cached
    mapping: Dict[str, str] = {}
    try:
        config = read_query_tool(default={})
    except Exception:
        config = {}
    for hero in config.get("heroList") or []:
        if not isinstance(hero, dict):
            continue
        hero_guid = str(
            hero.get("heroGuid")
            or hero.get("heroId")
            or hero.get("guid")
            or hero.get("id")
            or ""
        ).strip()
        if not hero_guid:
            continue
        role_type = str(hero.get("roleType") or "").strip().lower()
        if role_type == "support":
            role_type = "healer"
        if role_type:
            mapping[hero_guid] = role_type
    _HERO_ROLE_MAP = mapping
    return mapping


def _resolve_role_type(hero_guid: str, fallback: str = "") -> str:
    """Resolve role_type from hero_guid using the query_tool.json mapping."""
    normalized = str(hero_guid or "").strip()
    if not normalized:
        return fallback
    role = _load_hero_role_map().get(normalized, "")
    return role or fallback


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _player_kad_totals(player: Dict[str, Any]) -> tuple[int, int, int, bool]:
    kill = _safe_int(player.get("kill"))
    assist = _safe_int(player.get("assist"))
    death = _safe_int(player.get("death"))
    has_signal = (kill + assist + death) != 0

    hero_kill = 0
    hero_assist = 0
    hero_death = 0
    hero_has_signal = False
    for hero in player.get("heroList") or []:
        if not isinstance(hero, dict):
            continue
        current_kill = _safe_int(hero.get("kill"))
        current_assist = _safe_int(hero.get("assist"))
        current_death = _safe_int(hero.get("death"))
        if current_kill + current_assist + current_death != 0:
            hero_has_signal = True
        hero_kill += current_kill
        hero_assist += current_assist
        hero_death += current_death

    if not has_signal and hero_has_signal:
        kill, assist, death = hero_kill, hero_assist, hero_death
        has_signal = True
    return kill, assist, death, has_signal


def _normalize_use_time_rate(value: Any, use_time_sec: Any, game_time_sec: Any) -> float:
    numeric = _safe_float(value, -1.0)
    if numeric < 0:
        use_time_numeric = max(0.0, _safe_float(use_time_sec, 0.0))
        game_time_numeric = max(0.0, _safe_float(game_time_sec, 0.0))
        if game_time_numeric <= 0:
            return 0.0
        numeric = use_time_numeric / game_time_numeric
    elif numeric > 1.0:
        numeric = numeric / 100.0
    return max(0.0, min(1.0, numeric))


def _load_stat_value_text_by_guid() -> Dict[str, str]:
    global _STAT_VALUE_TEXT_BY_GUID
    cached = _STAT_VALUE_TEXT_BY_GUID
    if cached is not None:
        return cached
    mapping: Dict[str, str] = {}
    try:
        config = read_query_tool(default={})
    except Exception:
        config = {}
    for item in config.get("heroAttrList", []) or []:
        if not isinstance(item, dict):
            continue
        value_guid = str(item.get("valueGuid") or "").strip()
        if not value_guid or value_guid in mapping:
            continue
        mapping[value_guid] = str(item.get("valueText") or "")
    _STAT_VALUE_TEXT_BY_GUID = mapping
    return mapping


def _extract_match_id_from_url(request_url: str) -> str:
    try:
        parsed = urlsplit(str(request_url or ""))
    except Exception:
        return ""
    query = parse_qs(parsed.query, keep_blank_values=True)
    values = query.get("matchId") or query.get("matchid") or []
    return str(values[0] if values else "").strip()


def _player_name(player: Dict[str, Any]) -> str:
    for key in ("name", "userName", "playerName"):
        value = str(player.get(key) or "").strip()
        if value:
            return value
    return ""


def _find_player_by_bnet(players: Sequence[Dict[str, Any]], bnet_id: str) -> Dict[str, Any]:
    normalized = str(bnet_id or "").strip()
    if not normalized:
        return {}
    for player in players or []:
        if str(player.get("bnetId") or "").strip() == normalized:
            return dict(player)
    return {}


def _find_player_by_token(players: Sequence[Dict[str, Any]], customer_token: str) -> Dict[str, Any]:
    normalized = str(customer_token or "").strip()
    if not normalized:
        return {}
    for player in players or []:
        if str(player.get("customerToken") or "").strip() == normalized:
            return dict(player)
    return {}


def _find_player_by_hero(players: Sequence[Dict[str, Any]], hero_guid: str) -> Dict[str, Any]:
    normalized = str(hero_guid or "").strip()
    if not normalized:
        return {}
    for player in players or []:
        if str(player.get("heroGuid") or "").strip() == normalized:
            return dict(player)
    return {}


def find_normal_match_focus_player(data: Dict[str, Any]) -> Dict[str, Any]:
    players = [
        dict(item)
        for item in list(data.get("teammateList") or []) + list(data.get("enemyList") or [])
        if isinstance(item, dict)
    ]
    if not players:
        return {}

    focus = _find_player_by_bnet(players, str(data.get("bnetId") or ""))
    if focus:
        return focus

    focus = _find_player_by_token(players, str(data.get("customerToken") or ""))
    if focus:
        return focus

    hero_list = [item for item in (data.get("heroList") or []) if isinstance(item, dict)]
    if hero_list:
        focus = _find_player_by_hero(players, str(hero_list[0].get("heroId") or hero_list[0].get("heroGuid") or ""))
        if focus:
            return focus

    return {}


def normalize_match_detail_stat_value(
    value_guid: Any,
    value: Any,
    user_time_sec: Any,
    *,
    value_text: Any = None,
) -> Optional[float]:
    resolved_value_text = value_text
    if resolved_value_text in (None, ""):
        resolved_value_text = _load_stat_value_text_by_guid().get(str(value_guid or ""), "")
    return normalize_dashen_hero_stat_value(value, user_time_sec, resolved_value_text, value_guid)


def extract_normal_match_detail_records(
    payload: Dict[str, Any],
    *,
    request_url: str = "",
    extracted_at: Optional[int] = None,
    match_mode: str = "",
    match_list_json: Optional[str] = None,
) -> ParsedNormalMatchDetailBatch:
    result = ParsedNormalMatchDetailBatch()
    if not isinstance(payload, dict) or payload.get("code") != 0:
        return result

    data = payload.get("data")
    if not isinstance(data, dict):
        return result

    match_id = _extract_match_id_from_url(request_url)
    if not match_id:
        return result

    focus_player = find_normal_match_focus_player(data)
    focus_bnet_id = str((focus_player or {}).get("bnetId") or data.get("bnetId") or "").strip()
    if not focus_bnet_id:
        return result

    now_ts = _safe_int(extracted_at if extracted_at is not None else time.time())
    focus_player_name = _player_name(focus_player) or str(data.get("name") or "").strip()
    focus_rank_score = _safe_int((focus_player.get("rankInfo") or {}).get("rankScore"), 0)
    focus_rank_bucket = normalize_hero_rank_score(focus_rank_score)
    map_guid = str(data.get("mapGuid") or "").strip()
    start_time = _safe_int(data.get("startTime"), 0)
    game_time_sec = _safe_int(data.get("gameTimeSec"), 0)
    match_result = _safe_int(data.get("matchRet"), 0)

    # Determine which side the focus player is on.
    focus_side = "team"
    for enemy_player in data.get("enemyList") or []:
        if not isinstance(enemy_player, dict):
            continue
        if str(enemy_player.get("bnetId") or "").strip() == focus_bnet_id:
            focus_side = "enemy"
            break

    # Build match_meta_row
    result.match_meta_row = {
        "match_id": match_id,
        "match_result": match_result,
        "focus_player_side": focus_side,
        "match_mode": match_mode,
        "map_guid": map_guid,
        "start_time": start_time,
        "game_time_sec": game_time_sec,
        "match_list_json": match_list_json,
        "frozen": 0,
        "last_update": now_ts,
    }

    for hero in data.get("heroList") or []:
        if not isinstance(hero, dict):
            continue
        hero_guid = str(hero.get("heroId") or hero.get("heroGuid") or "").strip()
        if not hero_guid:
            continue
        stat_map = hero.get("statMap")
        if not isinstance(stat_map, dict) or not stat_map:
            continue

        use_time_sec = _safe_float(hero.get("userTimeSec"), 0.0)
        use_time_rate = _normalize_use_time_rate(hero.get("useTimeRate"), use_time_sec, game_time_sec)
        result.hero_detail_rows.append(
            {
                "match_id": match_id,
                "player_bnet_id": focus_bnet_id,
                "player_name": focus_player_name,
                "hero_guid": hero_guid,
                "rank_score": focus_rank_bucket,
                "rank_bucket": focus_rank_bucket,
                "use_time_sec": use_time_sec,
                "use_time_rate": use_time_rate,
                "map_guid": map_guid,
                "start_time": start_time,
                "game_time_sec": game_time_sec,
                "stat_map_json": json.dumps(stat_map, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                "last_update": now_ts,
            }
        )

        for statmap_name, raw_value in stat_map.items():
            statmap_key = str(statmap_name or "").strip()
            if not statmap_key:
                continue
            normalized_value = normalize_match_detail_stat_value(statmap_key, raw_value, use_time_sec)
            if normalized_value is None:
                continue
            raw_numeric = _safe_float(raw_value, 0.0)
            result.comp_data_rows.append(
                {
                    "match_id": match_id,
                    "player_bnet_id": focus_bnet_id,
                    "hero_guid": hero_guid,
                    "statmap_name": statmap_key,
                    "statmap_value": normalized_value,
                    "statmap_raw_value": raw_numeric,
                    "rank_score": focus_rank_bucket,
                    "rank_bucket": focus_rank_bucket,
                    "use_time_sec": use_time_sec,
                    "use_time_rate": use_time_rate,
                    "last_update": now_ts,
                }
            )
            result.comp_summary_keys.add((hero_guid, statmap_key, focus_rank_bucket))

    for player in list(data.get("teammateList") or []) + list(data.get("enemyList") or []):
        if not isinstance(player, dict):
            continue
        hero_guid = str(player.get("heroGuid") or "").strip()
        player_bnet_id = str(player.get("bnetId") or "").strip()
        if not hero_guid or not player_bnet_id:
            continue
        perks = player.get("perks") or []
        if not isinstance(perks, list) or not perks:
            continue
        player_name = _player_name(player)
        rank_score = _safe_int((player.get("rankInfo") or {}).get("rankScore"), 0)
        rank_bucket = normalize_hero_rank_score(rank_score)
        for slot_index, perk in enumerate(perks):
            if not isinstance(perk, dict):
                continue
            perk_guid = str(perk.get("guid") or perk.get("perkGuid") or perk.get("id") or "").strip()
            if not perk_guid:
                continue
            perk_level = _safe_int(perk.get("perkLevel"), 0) or (slot_index + 1)
            result.perk_pick_rows.append(
                {
                    "match_id": match_id,
                    "player_bnet_id": player_bnet_id,
                    "player_name": player_name,
                    "hero_guid": hero_guid,
                    "perk_guid": perk_guid,
                    "perk_level": perk_level,
                    "slot_index": slot_index,
                    "rank_score": rank_bucket,
                    "rank_bucket": rank_bucket,
                    "start_time": start_time,
                    "last_update": now_ts,
                }
            )
            result.perk_summary_keys.add((hero_guid, perk_level, rank_bucket))

    # Build match_player_rows for all 10 players (teammates + enemies).
    # This is separate from the perk loop above because we want a row for every
    # player, not just those with perks.
    for side_label, player_list in (
        ("team", data.get("teammateList") or []),
        ("enemy", data.get("enemyList") or []),
    ):
        for player in player_list or []:
            if not isinstance(player, dict):
                continue
            player_bnet_id = str(player.get("bnetId") or "").strip()
            if not player_bnet_id:
                continue
            p_hero_guid = str(player.get("heroGuid") or "").strip()
            if not p_hero_guid and player.get("heroList"):
                first_hero = (player.get("heroList") or [{}])[0] or {}
                p_hero_guid = str(first_hero.get("heroGuid") or first_hero.get("heroId") or "").strip()
            p_rank_score = _safe_int((player.get("rankInfo") or {}).get("rankScore"), 0)
            p_rank_bucket = normalize_hero_rank_score(p_rank_score)
            p_role_type = _resolve_role_type(p_hero_guid)
            # Store friendBnetIds for ALL players (not just focus player).
            # This fixes the friend-as-stranger bug on DB fallback reads.
            raw_friend_ids = player.get("friendBnetIds") or []
            friend_ids = [str(x) for x in raw_friend_ids if str(x).strip()]
            friend_ids_json: Optional[str] = (
                json.dumps(friend_ids, separators=(",", ":")) if friend_ids else None
            )
            # Store endorserBnetIds for all players.
            raw_endorse_ids = player.get("endorserBnetIds") or []
            endorse_ids = [str(x) for x in raw_endorse_ids if str(x).strip()]
            endorse_ids_json: Optional[str] = (
                json.dumps(endorse_ids, separators=(",", ":")) if endorse_ids else None
            )
            kill, assist, death, has_kad_signal = _player_kad_totals(player)
            result.match_player_rows.append(
                {
                    "match_id": match_id,
                    "player_bnet_id": player_bnet_id,
                    "player_name": _player_name(player),
                    "side": side_label,
                    "hero_guid": p_hero_guid,
                    "rank_bucket": p_rank_bucket,
                    "role_type": p_role_type,
                    "kill": kill,
                    "assist": assist,
                    "death": death,
                    "hero_damage": _safe_int(player.get("heroDamage")),
                    "healing": _safe_int(player.get("cure")),
                    "damage_blocked": _safe_int(player.get("resistDamage")),
                    "friend_bnet_ids_json": friend_ids_json,
                    "hero_damage_taken": _safe_int(player.get("damageTaken")),
                    "final_hit": _safe_int(player.get("finalHit") or player.get("finalBlows")),
                    "solo_kills": _safe_int(player.get("soloKills")),
                    "target_competing_time": _safe_float(player.get("targetCompetingTime")),
                    "healing_taken": _safe_int(player.get("healingTaken")),
                    "endorse_bnet_ids_json": endorse_ids_json,
                    "last_update": now_ts,
                    "_has_kad_signal": has_kad_signal,
                }
            )

    return result


class MatchDetailRecorder:
    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db = IDPoolDB(db_path)
        self._queue: asyncio.Queue[Optional[_MatchDetailEvent]] = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task[None]] = None
        self._started = False
        self._closed = False

    async def start(self) -> None:
        if self._started or not is_database_write_enabled():
            return
        await asyncio.to_thread(self.db.initialize_match_detail_schema)
        self._worker_task = asyncio.create_task(self._worker(), name="match-detail-recorder-worker")
        self._started = True

    async def enqueue(self, url: str, payload: Dict[str, Any], *, game_mode: str = "") -> None:
        if self._closed or not is_database_write_enabled():
            return
        if not self._started:
            await self.start()
        await self._queue.put(
            _MatchDetailEvent(str(url or ""), dict(payload or {}), game_mode=game_mode)
        )

    async def close(self) -> None:
        if not self._started or self._closed:
            return
        self._closed = True
        await self._queue.join()
        await self._queue.put(None)
        if self._worker_task is not None:
            await self._worker_task
        self._worker_task = None

    async def _worker(self) -> None:
        while True:
            event = await self._queue.get()
            if event is None:
                self._queue.task_done()
                return
            batch = [event]
            while True:
                try:
                    extra = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if extra is None:
                    await self._queue.put(None)
                    break
                batch.append(extra)
            try:
                await asyncio.to_thread(self._write_batch, batch)
            finally:
                for _ in batch:
                    self._queue.task_done()

    def _write_batch(self, batch: Sequence[_MatchDetailEvent]) -> None:
        extracted_at = int(time.time())
        for event in batch or []:
            parsed = extract_normal_match_detail_records(
                event.payload,
                request_url=event.url,
                extracted_at=extracted_at,
                match_mode=event.game_mode,
            )
            self.db.write_match_detail_batch(
                hero_detail_rows=parsed.hero_detail_rows,
                comp_data_rows=parsed.comp_data_rows,
                perk_pick_rows=parsed.perk_pick_rows,
                comp_summary_keys=parsed.comp_summary_keys,
                perk_summary_keys=parsed.perk_summary_keys,
                match_meta_row=parsed.match_meta_row,
                match_player_rows=parsed.match_player_rows,
            )


# ---------------------------------------------------------------------------
# MatchListRecorder — records queryMatchList payloads into match_meta
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _MatchListEvent:
    url: str
    payload: Dict[str, Any]
    game_mode: str = ""


def _extract_match_list_entries(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract match list entries from a queryMatchList response."""
    if not isinstance(payload, dict) or payload.get("code") != 0:
        return []
    data = payload.get("data")
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("matchList", "recentMatchList"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _extract_match_list_request_meta(url: str, fallback_game_mode: str = "") -> Dict[str, Any]:
    try:
        parsed = urlsplit(str(url or ""))
        qs = parse_qs(parsed.query, keep_blank_values=True)
        path = str(parsed.path or "")
    except Exception:
        qs = {}
        path = ""
    source_kind = "fight" if "/fight/" in path else "normal"
    season_values = qs.get("season") or []
    page_values = qs.get("page") or []
    token_values = qs.get("token") or []
    mode_values = qs.get("gameMode") or qs.get("gamemode") or []
    try:
        page_num = int(page_values[0] if page_values else 0)
    except (TypeError, ValueError):
        page_num = 0
    return {
        "source_kind": source_kind,
        "customer_token": str(token_values[0] if token_values else "").strip(),
        "game_mode": str(mode_values[0] if mode_values else fallback_game_mode or "").strip(),
        "season_key": str(season_values[0] if season_values else "current").strip() or "current",
        "page": page_num,
    }


class MatchListRecorder:
    """Async queue worker that writes queryMatchList entries to match_meta.

    Uses INSERT OR IGNORE so that existing queryMatchInfo data (more complete)
    is never overwritten.
    """

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db = IDPoolDB(db_path)
        self._queue: asyncio.Queue[Optional[_MatchListEvent]] = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task[None]] = None
        self._started = False
        self._closed = False

    async def start(self) -> None:
        if self._started or not is_database_write_enabled():
            return
        await asyncio.to_thread(self.db.initialize_match_detail_schema)
        self._worker_task = asyncio.create_task(
            self._worker(), name="match-list-recorder-worker"
        )
        self._started = True

    async def enqueue(
        self, url: str, payload: Dict[str, Any], *, game_mode: str = ""
    ) -> None:
        if self._closed or not is_database_write_enabled():
            return
        if not self._started:
            await self.start()
        await self._queue.put(
            _MatchListEvent(str(url or ""), dict(payload or {}), game_mode=game_mode)
        )

    async def close(self) -> None:
        if not self._started or self._closed:
            return
        self._closed = True
        await self._queue.join()
        await self._queue.put(None)
        if self._worker_task is not None:
            await self._worker_task
        self._worker_task = None

    async def _worker(self) -> None:
        while True:
            event = await self._queue.get()
            if event is None:
                self._queue.task_done()
                return
            batch = [event]
            while True:
                try:
                    extra = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if extra is None:
                    await self._queue.put(None)
                    break
                batch.append(extra)
            try:
                await asyncio.to_thread(self._write_batch, batch)
            finally:
                for _ in batch:
                    self._queue.task_done()

    def _write_batch(self, batch: Sequence[_MatchListEvent]) -> None:
        rows: List[tuple] = []
        page_cache_rows: List[Dict[str, Any]] = []
        now_ts = int(time.time())
        for event in batch or []:
            entries = _extract_match_list_entries(event.payload)
            request_meta = _extract_match_list_request_meta(event.url, event.game_mode)
            if request_meta.get("customer_token"):
                request_meta = dict(request_meta)
                request_meta["customer_token"] = cache_owner_key(
                    customer_token=str(request_meta.get("customer_token") or "")
                )
            match_ids = [
                str(entry.get("matchId") or "").strip()
                for entry in entries
                if isinstance(entry, dict) and str(entry.get("matchId") or "").strip()
            ]
            if request_meta.get("customer_token") and request_meta.get("game_mode") and request_meta.get("page"):
                page_cache_rows.append(
                    {
                        **request_meta,
                        "payload_json": json.dumps(
                            event.payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                        ),
                        "match_ids_json": json.dumps(match_ids, ensure_ascii=False, separators=(",", ":")),
                        "entry_count": len(entries),
                        "fetched_at": now_ts,
                        "stop_reason": "",
                    }
                )
            for entry in entries:
                match_id = str(entry.get("matchId") or "").strip()
                if not match_id:
                    continue
                begin_ts = _safe_int(entry.get("beginTs"), 0)
                rows.append(
                    tuple(
                        {
                            "match_id": match_id,
                            "match_result": _safe_int(entry.get("matchRet")),
                            "focus_player_side": "",
                            "match_mode": (
                                str(entry.get("gameMode") or "").strip()
                                or event.game_mode
                            ),
                            "map_guid": str(entry.get("mapGuid") or ""),
                            "start_time": begin_ts // 1000 if begin_ts > 0 else 0,
                            "game_time_sec": 0,
                            "match_list_json": json.dumps(
                                entry, ensure_ascii=False, sort_keys=True,
                                separators=(",", ":"),
                            ),
                            "frozen": 0,
                            "last_update": now_ts,
                        }.get(k)
                        for k in MATCH_META_FIELDS
                    )
                )
        if rows:
            self.db.write_match_list_batch(rows)
        if page_cache_rows:
            self.db.write_match_list_page_cache_batch(page_cache_rows)


# ---------------------------------------------------------------------------
# CountInfoRecorder — records queryCountInfo payloads into player_competitive_rank
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _CountInfoEvent:
    url: str
    payload: Dict[str, Any]
    customer_token: str = ""


def _extract_customer_token_from_url(url: str) -> str:
    try:
        parsed = urlsplit(str(url or ""))
        from urllib.parse import parse_qs as _parse_qs

        qs = _parse_qs(parsed.query, keep_blank_values=True)
        values = qs.get("token") or []
        return str(values[0] if values else "").strip()
    except Exception:
        return ""


def _extract_season_from_url(url: str) -> Optional[int]:
    """Extract the ``season`` query parameter from the request URL."""
    try:
        parsed = urlsplit(str(url or ""))
        qs = parse_qs(parsed.query, keep_blank_values=True)
        values = qs.get("season") or []
        raw = str(values[0] if values else "").strip()
        if raw:
            return int(raw)
    except Exception:
        pass
    return None


def _normalize_role_type(role_type: Any) -> str:
    normalized = str(role_type or "").strip().lower()
    if normalized == "support":
        return "healer"
    return normalized


def _convert_rank_score(score: Any) -> Optional[int]:
    """Convert raw rankScore from queryCountInfo to normalised score bucket."""
    try:
        score_num = int(score)
    except (TypeError, ValueError):
        return None
    if score_num <= 0:
        return None
    rank = (score_num // 100) + 2
    tier = (score_num % 100)
    tier = (tier % 10) - 5
    return int(rank * 500 + tier * 100)


class CountInfoRecorder:
    """Async queue worker that writes queryCountInfo guideCountData to
    player_competitive_rank.

    Resolves customer_token → bnet_id via player_identity_map.
    """

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db = IDPoolDB(db_path)
        self._queue: asyncio.Queue[Optional[_CountInfoEvent]] = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task[None]] = None
        self._started = False
        self._closed = False

    async def start(self) -> None:
        if self._started or not is_database_write_enabled():
            return
        await asyncio.to_thread(self.db.initialize_match_detail_schema)
        self._worker_task = asyncio.create_task(
            self._worker(), name="count-info-recorder-worker"
        )
        self._started = True

    async def enqueue(
        self,
        url: str,
        payload: Dict[str, Any],
        *,
        customer_token: str = "",
    ) -> None:
        if self._closed or not is_database_write_enabled():
            return
        if not self._started:
            await self.start()
        token = customer_token or _extract_customer_token_from_url(url)
        await self._queue.put(
            _CountInfoEvent(str(url or ""), dict(payload or {}), customer_token=token)
        )

    async def close(self) -> None:
        if not self._started or self._closed:
            return
        self._closed = True
        await self._queue.join()
        await self._queue.put(None)
        if self._worker_task is not None:
            await self._worker_task
        self._worker_task = None

    async def _worker(self) -> None:
        while True:
            event = await self._queue.get()
            if event is None:
                self._queue.task_done()
                return
            batch = [event]
            while True:
                try:
                    extra = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if extra is None:
                    await self._queue.put(None)
                    break
                batch.append(extra)
            try:
                await asyncio.to_thread(self._write_batch, batch)
            finally:
                for _ in batch:
                    self._queue.task_done()

    def _write_batch(self, batch: Sequence[_CountInfoEvent]) -> None:
        records_by_player: Dict[str, List[Dict[str, Any]]] = {}
        cache_week = current_cache_week()
        checked_at = int(time.time())
        for event in batch or []:
            if not isinstance(event.payload, dict) or event.payload.get("code") != 0:
                continue
            data = event.payload.get("data")
            if not isinstance(data, dict):
                continue
            guide_count_data = data.get("guideCountData") or []
            # Extract season from URL query params (e.g. ...?season=22)
            event_season = _extract_season_from_url(event.url)
            # Resolve token -> bnet_id: prefer data.bnetId from payload,
            # then fall back to DB-based resolution.
            player_bnet_id = str(data.get("bnetId") or "").strip()
            if not player_bnet_id and event.customer_token:
                player_bnet_id = self.db.resolve_bnet_id_by_token(event.customer_token)
            if not player_bnet_id:
                continue
            records_by_player.setdefault(player_bnet_id, [])
            now_ts = int(time.time())
            for row in guide_count_data:
                if not isinstance(row, dict):
                    continue
                role_type = _normalize_role_type(row.get("roleType"))
                rank_score = _convert_rank_score(
                    (row.get("lastRankInfo") or {}).get("rankScore")
                )
                if not role_type or rank_score is None:
                    continue
                records_by_player.setdefault(player_bnet_id, []).append(
                    {
                        "player_bnet_id": player_bnet_id,
                        "role_type": role_type,
                        "rank_score": rank_score,
                        "season": event_season,
                        "source_match_id": "",
                    }
                )
        for player_bnet_id, records in records_by_player.items():
            self.db.upsert_player_competitive_rank_snapshot(
                player_bnet_id,
                cache_week=cache_week,
                game_mode="sport",
                role_rank_records=records,
                checked_at=checked_at,
            )


__all__ = [
    "CountInfoRecorder",
    "MatchDetailRecorder",
    "MatchListRecorder",
    "ParsedNormalMatchDetailBatch",
    "extract_normal_match_detail_records",
    "find_normal_match_focus_player",
    "normalize_match_detail_stat_value",
]
