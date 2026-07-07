from __future__ import annotations

import asyncio
from collections import Counter
import datetime
import importlib
import json
import os
from pathlib import Path
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

try:
    from overstats.src.modules.errors import ModuleError
except ModuleNotFoundError:
    from src.modules.errors import ModuleError

try:
    from overstats.src.client.apiclient import dashen_api_client
except ModuleNotFoundError:
    from src.client.apiclient import dashen_api_client

try:
    from overstats.src.modules.dashen_request_cache import cache_owner_key, fetch_paginated_match_entries
except ModuleNotFoundError:
    from src.modules.dashen_request_cache import cache_owner_key, fetch_paginated_match_entries

PROJECT_ROOT = Path(__file__).resolve().parents[3]
MIG_ROOT = Path(__file__).resolve().parents[4]
TITLE_BY_SCOPE = {
    "today": "今日总结",
    "yesterday": "昨日总结",
    "week": "本周总结",
}
DEFAULT_SUMMARY_CONCURRENCY = 2
DEFAULT_MATCH_LOOKBACK_DAYS = 8
DEFAULT_WEATHER_LOOKBACK_DAYS = 45
DEFAULT_WEATHER_CACHE_TTL = 600
DEFAULT_WEATHER_CACHE_MAX = 256
DEFAULT_FIGHT_PAGE_BATCH = 2
DEFAULT_FULL_MATCH_LIST_REFRESH_DAYS = 3

_RUNTIME: "SummaryRuntime | None" = None
_WEATHER_MATCH_CACHE: Dict[Tuple[str, bool, int, str], Dict[str, Any]] = {}


def _read_env_int(name: str, default: int) -> int:
    raw = str(os.getenv(name, "") or "").strip()
    if not raw:
        return int(default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return int(default)


SUMMARY_CONCURRENCY = max(
    1,
    _read_env_int("OVERSTATS_SUMMARY_CONCURRENCY", DEFAULT_SUMMARY_CONCURRENCY),
)
SUMMARY_SEMAPHORE = asyncio.Semaphore(SUMMARY_CONCURRENCY)
SUMMARY_MATCH_FETCH_LOOKBACK_DAYS = max(
    8,
    _read_env_int("OVERSTATS_SUMMARY_MATCH_LOOKBACK_DAYS", DEFAULT_MATCH_LOOKBACK_DAYS),
)
SUMMARY_WEATHER_FETCH_LOOKBACK_DAYS = max(
    8,
    _read_env_int("OVERSTATS_SUMMARY_WEATHER_LOOKBACK_DAYS", DEFAULT_WEATHER_LOOKBACK_DAYS),
)
SUMMARY_WEATHER_MATCH_CACHE_TTL = max(
    60,
    _read_env_int("OVERSTATS_SUMMARY_WEATHER_CACHE_TTL", DEFAULT_WEATHER_CACHE_TTL),
)
SUMMARY_WEATHER_MATCH_CACHE_MAX_SIZE = max(
    32,
    _read_env_int("OVERSTATS_SUMMARY_WEATHER_CACHE_MAX", DEFAULT_WEATHER_CACHE_MAX),
)
FIGHT_PAGE_BATCH = max(
    1,
    _read_env_int("OVERSTATS_SUMMARY_FIGHT_PAGE_BATCH", DEFAULT_FIGHT_PAGE_BATCH),
)
SUMMARY_FULL_MATCH_LIST_REFRESH_DAYS = max(
    1,
    _read_env_int("OVERSTATS_SUMMARY_FULL_MATCH_LIST_REFRESH_DAYS", DEFAULT_FULL_MATCH_LIST_REFRESH_DAYS),
)

@dataclass(frozen=True)
class SummaryRuntime:
    dashen: Any
    summary: Any


class StageTimer:
    def __init__(self) -> None:
        self.started = time.perf_counter()
        self.last = self.started
        self.rows: List[Dict[str, Any]] = []

    def mark(self, stage: str, extra: Optional[str] = None) -> None:
        now = time.perf_counter()
        self.rows.append(
            {
                "stage": stage,
                "delta_ms": int((now - self.last) * 1000),
                "total_ms": int((now - self.started) * 1000),
                "extra": extra or "",
            }
        )
        self.last = now


def title_for_scope(scope: str) -> str:
    normalized = str(scope or "today").strip().lower()
    if normalized not in TITLE_BY_SCOPE:
        raise ModuleError(
            error="invalid_summary_scope",
            message=f"Unsupported summary scope: {normalized}",
            status_code=400,
            hint='Supported scopes: "today", "yesterday", "week".',
            details={"scope": normalized},
        )
    return TITLE_BY_SCOPE[normalized]


def _load_runtime() -> SummaryRuntime:
    global _RUNTIME
    if _RUNTIME is not None:
        return _RUNTIME

    for candidate_root in (PROJECT_ROOT, MIG_ROOT):
        candidate_text = str(candidate_root)
        if candidate_text not in sys.path:
            sys.path.insert(0, candidate_text)

    runtime_prefixes = (
        "src.modules.dashen_summary.runtime",
        "overstats.src.modules.dashen_summary.runtime",
        "Overstats.src.modules.dashen_summary.runtime",
    )
    import_failures: List[Dict[str, str]] = []
    for prefix in runtime_prefixes:
        try:
            dashen = importlib.import_module(f"{prefix}.dashen")
            summary = importlib.import_module(f"{prefix}.season_conclusion")
            _RUNTIME = SummaryRuntime(dashen=dashen, summary=summary)
            return _RUNTIME
        except Exception as exc:
            import_failures.append(
                {
                    "prefix": prefix,
                    "exception": type(exc).__name__,
                    "message": str(exc),
                }
            )

    primary_failure = import_failures[0] if import_failures else {}
    raise ModuleError(
        error="summary_runtime_import_failed",
        message="Failed to load local summary runtime.",
        status_code=500,
        details={
            "exception": primary_failure.get("exception", "ImportError"),
            "message": primary_failure.get("message", "Unknown import failure."),
            "attempted_prefixes": list(runtime_prefixes),
            "import_failures": import_failures,
            "sys_path_roots": [str(PROJECT_ROOT), str(MIG_ROOT)],
        },
    )


def _resolved_target_from_query(query: Any) -> Dict[str, Any]:
    full_id = str(getattr(query, "full_id", "") or "").strip()
    bnet_id = str(getattr(query, "bnet_id", "") or "").strip()
    customer_token = str(getattr(query, "customer_token", "") or "").strip()
    battletag = full_id
    battlenum = "0"
    if "#" in full_id:
        battletag, battlenum = full_id.rsplit("#", 1)
    elif "#" in bnet_id:
        battletag, battlenum = bnet_id.rsplit("#", 1)
        if not full_id:
            full_id = bnet_id
    if not full_id:
        full_id = bnet_id or battletag or "Unknown"
    return {
        "full_id": full_id,
        "bnet_id": bnet_id,
        "customer_token": customer_token,
        "battletag": battletag.strip() or full_id,
        "battlenum": str(battlenum).strip() or "0",
        "icon_url": str(getattr(query, "icon_url", "") or "").strip(),
    }


async def _ensure_target_icon_url(resolved_target: Dict[str, Any]) -> Dict[str, Any]:
    if str(resolved_target.get("icon_url") or "").strip():
        return resolved_target

    customer_token = str(resolved_target.get("customer_token") or "").strip()
    if not customer_token:
        return resolved_target

    try:
        payload = await dashen_api_client.query_card(customer_token)
    except Exception:
        return resolved_target

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return resolved_target

    icon_url = str(data.get("icon") or "").strip()
    if icon_url:
        resolved_target["icon_url"] = icon_url

    full_id = str(resolved_target.get("full_id") or "").strip()
    if not full_id:
        resolved_target["full_id"] = str(data.get("name") or "").strip()

    bnet_id = str(resolved_target.get("bnet_id") or "").strip()
    if not bnet_id:
        resolved_target["bnet_id"] = str(data.get("bnetId") or "").strip()

    return resolved_target


def _extract_match_entries(payload: Any, *preferred_keys: str) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    data = payload.get("data", payload)
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    for key in preferred_keys or ("matchList", "recentMatchList"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _merge_unique_match_entries(existing_entries: List[Dict[str, Any]], new_entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged = []
    seen = set()
    for source in (existing_entries or [], new_entries or []):
        for match in source:
            if not isinstance(match, dict):
                continue
            item = dict(match)
            match_id = str(item.get("matchId") or "").strip()
            if match_id:
                if match_id in seen:
                    continue
                seen.add(match_id)
            merged.append(item)
    merged.sort(key=lambda item: item.get("beginTs") or 0, reverse=True)
    return merged


def _summary_match_begin_ts(match: Dict[str, Any]) -> int:
    try:
        return int((match or {}).get("beginTs") or 0)
    except (TypeError, ValueError):
        return 0


def _summary_recent_fetch_min_ts(days: Optional[int] = None) -> int:
    lookback_days = SUMMARY_MATCH_FETCH_LOOKBACK_DAYS if days is None else int(days)
    return int((time.time() - lookback_days * 24 * 3600) * 1000)


def _summary_match_list_min_begin_ts(
    runtime: SummaryRuntime,
    customer_token: str,
    bnet_id: str,
) -> Tuple[Optional[int], str]:
    owner_key = cache_owner_key(customer_token=customer_token, bnet_id=bnet_id)
    db_adapter = getattr(runtime.dashen, "db", None)
    last_fetched_at = 0
    if db_adapter is not None:
        try:
            last_fetched_at = int(
                db_adapter.get_match_list_last_fetched_at(
                    customer_token=owner_key,
                    source_kinds=("normal", "fight"),
                )
                or 0
            )
        except Exception:
            last_fetched_at = 0

    now_ts = int(time.time())
    max_age_sec = int(SUMMARY_FULL_MATCH_LIST_REFRESH_DAYS) * 24 * 3600
    age_sec = now_ts - last_fetched_at if last_fetched_at > 0 else 0
    if last_fetched_at <= 0 or age_sec > max_age_sec:
        reason = "missing" if last_fetched_at <= 0 else "expired"
        return None, (
            f"full_scan=1; reason={reason}; owner_key={owner_key}; "
            f"last_fetched_at={last_fetched_at}; age_sec={age_sec}; max_age_sec={max_age_sec}"
        )

    min_begin_ts = _summary_recent_fetch_min_ts()
    return min_begin_ts, (
        f"full_scan=0; owner_key={owner_key}; last_fetched_at={last_fetched_at}; "
        f"age_sec={age_sec}; max_age_sec={max_age_sec}; min_begin_ts={min_begin_ts}"
    )


def _should_include_previous_season(runtime: SummaryRuntime, title_text: str, reference_time: Optional[datetime.datetime] = None) -> bool:
    rollover = getattr(runtime.dashen, "DASHEN_SEASON_ROLLOVER_AT", None)
    if not isinstance(rollover, datetime.datetime):
        return True
    ref_time = reference_time or datetime.datetime.now()
    if title_text in {TITLE_BY_SCOPE["today"], TITLE_BY_SCOPE["yesterday"]}:
        return ref_time <= (rollover + datetime.timedelta(days=2))
    if title_text == TITLE_BY_SCOPE["week"]:
        return ref_time <= (rollover + datetime.timedelta(days=7))
    return True


async def _get_summary_fight_match_list(
    runtime: SummaryRuntime,
    customer_token: str,
    game_mode: str,
    include_previous_season: bool = True,
    min_begin_ts: Optional[int] = None,
    bnet_id: str = "",
) -> List[Dict[str, Any]]:
    match_list: List[Dict[str, Any]] = []
    season_candidates = (
        runtime.dashen.get_recent_dashen_fill_seasons()
        if include_previous_season
        else runtime.dashen.get_recent_dashen_seasons(include_previous=False)
    )
    for match_season in season_candidates:
        result = await fetch_paginated_match_entries(
            source_kind="fight",
            customer_token=customer_token,
            game_mode=game_mode,
            season=match_season,
            batch_size=FIGHT_PAGE_BATCH,
            fetch_page=lambda current_page, match_season=match_season: runtime.dashen._run_summary_request(
                runtime.dashen.dashen_api_client.fight_query_match_list(
                    customer_token,
                    game_mode=game_mode,
                    page=current_page,
                    season=match_season,
                )
            ),
            extract_entries=lambda payload: _extract_match_entries(payload, "matchList", "recentMatchList")
            if isinstance(payload, dict) and payload.get("code") == 0
            else [],
            begin_ts_getter=_summary_match_begin_ts,
            min_begin_ts=min_begin_ts,
            bnet_id=bnet_id,
            db=getattr(runtime.dashen, "db", None),
        )
        season_match_list = []
        for match in result.matches:
            if not isinstance(match, dict):
                continue
            item = dict(match)
            item["gameMode"] = game_mode
            item["_summaryFightOnly"] = True
            item["_dashenSeason"] = match_season
            season_match_list.append(item)
        if season_match_list:
            match_list = _merge_unique_match_entries(match_list, season_match_list)
    return match_list


async def _get_summary_match_lists_with_fight(
    runtime: SummaryRuntime,
    customer_token: str,
    include_previous_season: bool = True,
    min_begin_ts: Optional[int] = None,
    bnet_id: str = "",
) -> List[Dict[str, Any]]:
    tasks = [
        runtime.dashen.get_history_leis_matchK(
            customer_token,
            merge_all_recent_seasons=include_previous_season,
            min_begin_ts=min_begin_ts,
            bnet_id=bnet_id,
        ),
        runtime.dashen.get_history_comp_matchK(
            customer_token,
            merge_all_recent_seasons=include_previous_season,
            min_begin_ts=min_begin_ts,
            bnet_id=bnet_id,
        ),
        _get_summary_fight_match_list(runtime, customer_token, "QuickFight", include_previous_season, min_begin_ts, bnet_id),
        _get_summary_fight_match_list(runtime, customer_token, "LeisureFight", include_previous_season, min_begin_ts, bnet_id),
        _get_summary_fight_match_list(runtime, customer_token, "SportFight", include_previous_season, min_begin_ts, bnet_id),
    ]
    result_lists = await asyncio.gather(*tasks)
    all_matches = []
    seen = set()
    for idx, result in enumerate(result_lists):
        for match in result or []:
            if not isinstance(match, dict) or not match.get("beginTs"):
                continue
            item = dict(match)
            match_id = item.get("matchId")
            if match_id and match_id in seen:
                continue
            if idx >= 2:
                item["_summaryFightOnly"] = True
            if match_id:
                seen.add(match_id)
            all_matches.append(item)
    all_matches.sort(key=lambda item: item.get("beginTs") or 0, reverse=True)
    return all_matches


async def _get_weather_matches(
    runtime: SummaryRuntime,
    customer_token: str,
    include_previous_season: bool,
    fallback_matches: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    now_ts = time.time()
    cache_key = (
        str(customer_token),
        bool(include_previous_season),
        int(SUMMARY_WEATHER_FETCH_LOOKBACK_DAYS),
        datetime.datetime.now().strftime("%Y-%m-%d"),
    )
    cached = _WEATHER_MATCH_CACHE.get(cache_key)
    if cached and cached.get("expiry", 0) > now_ts:
        return [dict(match) for match in cached.get("matches", []) if isinstance(match, dict)]

    weather_min_ts = _summary_recent_fetch_min_ts(SUMMARY_WEATHER_FETCH_LOOKBACK_DAYS)
    weather_matches = [
        dict(match)
        for match in (fallback_matches or [])
        if isinstance(match, dict) and _summary_match_begin_ts(match) >= weather_min_ts
    ]
    weather_matches = [match for match in weather_matches if isinstance(match, dict)]
    weather_matches.sort(key=lambda item: item.get("beginTs") or 0, reverse=True)
    _WEATHER_MATCH_CACHE[cache_key] = {
        "expiry": now_ts + SUMMARY_WEATHER_MATCH_CACHE_TTL,
        "matches": [dict(match) for match in weather_matches],
    }
    while len(_WEATHER_MATCH_CACHE) > SUMMARY_WEATHER_MATCH_CACHE_MAX_SIZE:
        _WEATHER_MATCH_CACHE.pop(next(iter(_WEATHER_MATCH_CACHE)), None)
    return weather_matches or fallback_matches


def _today_period(all_matches: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not all_matches:
        return []
    today_matches = []
    now_ts = time.time() * 1000
    last_match_ts = int(all_matches[0].get("beginTs") or 0)
    if now_ts - last_match_ts > 24 * 3600 * 1000:
        return []
    for match in all_matches:
        begin_ts = int(match.get("beginTs") or 0)
        if (last_match_ts - begin_ts > 5 * 3600 * 1000) or (now_ts - begin_ts > 24 * 3600 * 1000):
            break
        today_matches.append(match)
        last_match_ts = begin_ts
    return today_matches


def _yesterday_period(all_matches: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], datetime.datetime]:
    now = datetime.datetime.now()
    today_start = datetime.datetime(now.year, now.month, now.day)
    yesterday_start = today_start - datetime.timedelta(days=1)
    yesterday_end = today_start - datetime.timedelta(seconds=1)
    start_ts = yesterday_start.timestamp() * 1000
    end_ts = yesterday_end.timestamp() * 1000
    matches = [m for m in all_matches if start_ts <= int(m.get("beginTs") or 0) <= end_ts]
    matches.sort(key=lambda item: item.get("beginTs") or 0, reverse=True)
    return matches, yesterday_end


def _week_period(all_matches: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    now_ts = time.time() * 1000
    seven_days_ago = now_ts - 7 * 24 * 3600 * 1000
    matches = [m for m in all_matches if int(m.get("beginTs") or 0) >= seven_days_ago]
    matches.sort(key=lambda item: item.get("beginTs") or 0, reverse=True)
    return matches


def _summary_detail_decision_extra(runtime: SummaryRuntime) -> str:
    try:
        decisions = runtime.summary.get_last_summary_detail_decisions()
    except Exception:
        decisions = []
    if not decisions:
        return ""
    reason_counts = Counter(str(item.get("reason") or "") for item in decisions)
    api_items = [item for item in decisions if item.get("api_fetch")]
    api_preview = [
        {
            "match_id": item.get("match_id"),
            "reason": item.get("reason"),
            "db_frozen": item.get("db_frozen"),
            "age_sec": item.get("age_sec"),
            "db_game_time_sec": item.get("db_game_time_sec"),
            "db_last_update": item.get("db_last_update"),
        }
        for item in api_items[:200]
    ]
    try:
        return json.dumps(
            {
                "reason_counts": dict(reason_counts),
                "api_fetch_count": len(api_items),
                "api_fetches": api_preview,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    except Exception:
        return f"reason_counts={dict(reason_counts)}; api_fetch_count={len(api_items)}"


async def _build_image_base64(
    runtime: SummaryRuntime,
    resolved_target: Dict[str, Any],
    matches: List[Dict[str, Any]],
    title_text: str,
    all_matches: List[Dict[str, Any]],
    timer: StageTimer,
) -> str:
    customer_token = resolved_target["customer_token"]
    focus_bnet_id = str(resolved_target.get("bnet_id") or "")
    timer.mark("REQUEST_READY", f"title={title_text}; match_count={len(matches)}; all_match_count={len(all_matches or [])}")
    detail_task = asyncio.create_task(
        runtime.summary._fetch_details(customer_token, matches, focus_bnet_id=focus_bnet_id)
    )
    quick_dist_task = asyncio.create_task(runtime.summary._build_quick_strength_distribution_data(customer_token, matches))
    detail_pairs, quick_dist_data = await asyncio.gather(detail_task, quick_dist_task)
    timer.mark(
        "DETAIL_AND_QUICK_DIST_DONE",
        f"detail_count={len(detail_pairs)}; quick_dist_points={len((quick_dist_data or {}).get('sampled_matches') or [])}",
    )
    decision_extra = _summary_detail_decision_extra(runtime)
    if decision_extra:
        timer.mark("DETAIL_DECISIONS", decision_extra)
    stats = runtime.summary._build_stats(matches, detail_pairs, resolved_target)
    timer.mark("STATS_DONE")

    image = await runtime.summary._render_period_image(
        stats,
        resolved_target,
        matches,
        detail_pairs,
        title_text,
        all_matches=all_matches,
        quick_dist_data=quick_dist_data,
        render_stage_log=lambda stage, extra=None: timer.mark(f"RENDER_{stage}", extra),
    )
    timer.mark("RENDER_DONE")
    image_b64 = runtime.summary._summary_png_b64(image)
    timer.mark("ENCODE_DONE", f"payload_kb={runtime.summary._base64_payload_kb(image_b64)}")
    return image_b64


async def render_summary_payload(query: Any) -> Dict[str, Any]:
    runtime = _load_runtime()
    scope = str(getattr(query, "scope", "today") or "today").strip().lower()
    title_text = title_for_scope(scope)
    timer = StageTimer()
    timer.mark("REQUEST_START")
    if SUMMARY_SEMAPHORE.locked():
        timer.mark("MODULE_BUSY", f"summary_concurrency={SUMMARY_CONCURRENCY}")
        raise ModuleError(
            error="summary_busy",
            message="Summary module is busy. Please retry later.",
            status_code=429,
            details={"summary_concurrency": SUMMARY_CONCURRENCY},
        )

    async with SUMMARY_SEMAPHORE:
        resolved = _resolved_target_from_query(query)
        resolved = await _ensure_target_icon_url(resolved)
        customer_token = resolved["customer_token"]
        if not customer_token:
            raise ModuleError(
                error="missing_customer_token",
                message="customer_token is required after summary target resolution.",
                status_code=400,
            )
        timer.mark("TARGET_RESOLVED", f"full_id={resolved.get('full_id')}")

        reference_time = None
        if scope == "yesterday":
            reference_time = _yesterday_period([])[1]
        include_previous = _should_include_previous_season(runtime, title_text, reference_time=reference_time)
        bnet_id = str(resolved.get("bnet_id") or "").strip()
        match_list_min_begin_ts, match_list_refresh_extra = _summary_match_list_min_begin_ts(
            runtime,
            customer_token,
            bnet_id,
        )
        # ponytail: the weather chart needs a 45-day lookback, but the summary
        # incremental fetch stops at ~8 days.  Expand min_begin_ts so
        # _get_summary_match_lists_with_fight covers the weather window.
        # fetch_paginated_match_entries reads from DB page cache first, so the
        # extra pages add zero API calls when already cached from the initial
        # full fetch.
        weather_min_ts = _summary_recent_fetch_min_ts(SUMMARY_WEATHER_FETCH_LOOKBACK_DAYS)
        if match_list_min_begin_ts is not None and match_list_min_begin_ts > weather_min_ts:
            match_list_min_begin_ts = weather_min_ts
        timer.mark("MATCH_LIST_REFRESH_DECISION", match_list_refresh_extra)
        all_raw_matches = await _get_summary_match_lists_with_fight(
            runtime,
            customer_token,
            include_previous_season=include_previous,
            min_begin_ts=match_list_min_begin_ts,
            bnet_id=bnet_id,
        )
        all_raw_matches.sort(key=lambda item: item.get("beginTs") or 0, reverse=True)
        timer.mark("MATCH_LIST_FETCHED", f"raw_count={len(all_raw_matches)}")
        if not all_raw_matches:
            raise ModuleError(
                error="summary_empty",
                message="近期没有找到对局记录。",
                status_code=404,
                details={"scope": scope},
            )

        if scope == "today":
            period_matches = _today_period(all_raw_matches)
            empty_message = "你在过去的 24 小时内没有对局记录。"
        elif scope == "yesterday":
            period_matches, reference_time = _yesterday_period(all_raw_matches)
            empty_message = "你在昨日没有对局记录。"
        else:
            period_matches = _week_period(all_raw_matches)
            empty_message = "你在过去 7 天内没有对局记录。"

        timer.mark("PERIOD_FILTER_DONE", f"period_count={len(period_matches)}")
        if not period_matches:
            raise ModuleError(
                error="summary_empty",
                message=empty_message,
                status_code=404,
                details={"scope": scope},
            )

        weather_matches = await _get_weather_matches(runtime, customer_token, include_previous, all_raw_matches)
        timer.mark("WEATHER_MATCH_LIST_FETCHED", f"weather_count={len(weather_matches)}; fallback_count={len(all_raw_matches)}")
        image_b64 = await _build_image_base64(runtime, resolved, period_matches, title_text, weather_matches, timer)

    return {
        "ok": True,
        "scope": scope,
        "title": title_text,
        "full_id": resolved.get("full_id"),
        "worker_url": "local-module",
        "match_count": len(period_matches),
        "all_match_count": len(weather_matches or []),
        "payload_kb": runtime.summary._base64_payload_kb(image_b64),
        "image_base64": image_b64,
        "timings": timer.rows,
    }
