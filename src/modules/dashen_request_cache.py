from __future__ import annotations

import asyncio
import base64
import datetime as dt
import os
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs

try:
    from overstats.src.db.match_stats import IDPoolDB
    from overstats.src.modules.season_config import get_dashen_current_season
except ModuleNotFoundError:
    from src.db.match_stats import IDPoolDB
    from src.modules.season_config import get_dashen_current_season


def _report_cache_hit(source: str) -> None:
    try:
        from overstats.src.server import report_db_cache_hit
    except ModuleNotFoundError:
        try:
            from src.server import report_db_cache_hit
        except ModuleNotFoundError:
            return
    report_db_cache_hit(source)


FetchPage = Callable[[int], Awaitable[Any]]
ExtractEntries = Callable[[Any], List[Dict[str, Any]]]
BeginTsGetter = Callable[[Dict[str, Any]], int]


class AsyncSingleFlight:
    """Event-loop local request coalescing for identical async work."""

    def __init__(self) -> None:
        self._lock: Optional[asyncio.Lock] = None
        self._tasks: Dict[Tuple[Any, ...], asyncio.Task[Any]] = {}
        self.reuse_count = 0

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def do(self, key: Tuple[Any, ...], factory: Callable[[], Awaitable[Any]]) -> Any:
        normalized_key = tuple(key)
        async with self._get_lock():
            task = self._tasks.get(normalized_key)
            if task is not None:
                self.reuse_count += 1
                _report_cache_hit("memory")
            else:
                task = asyncio.create_task(factory())
                self._tasks[normalized_key] = task
        try:
            return await task
        finally:
            if task.done():
                async with self._get_lock():
                    if self._tasks.get(normalized_key) is task:
                        self._tasks.pop(normalized_key, None)


rank_singleflight = AsyncSingleFlight()
list_page_singleflight = AsyncSingleFlight()


def current_cache_week(now: Optional[dt.datetime] = None) -> str:
    tz = dt.timezone(dt.timedelta(hours=8))
    current = now or dt.datetime.now(tz)
    if current.tzinfo is None:
        current = current.replace(tzinfo=tz)
    local = current.astimezone(tz)
    monday = (local - dt.timedelta(days=local.weekday())).date()
    return monday.isoformat()


def season_key(season: Any) -> str:
    if season in (None, "", 0, "0"):
        return "current"
    try:
        if int(season) == int(get_dashen_current_season()):
            return "current"
    except (TypeError, ValueError):
        pass
    return str(season)


def _bnet_id_from_customer_token(customer_token: str) -> str:
    token = str(customer_token or "").strip()
    if not token:
        return ""
    try:
        padded = token + "=" * (-len(token) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("ascii", errors="ignore")).decode("utf-8", errors="ignore")
    except Exception:
        return ""
    try:
        query = parse_qs(decoded, keep_blank_values=True)
    except Exception:
        return ""
    values = query.get("bnetId") or query.get("bnetid") or []
    return str(values[0] if values else "").strip()


def cache_owner_key(*, customer_token: str = "", bnet_id: str = "") -> str:
    token_bnet_id = _bnet_id_from_customer_token(customer_token)
    if token_bnet_id:
        return f"dashen_bnet:{token_bnet_id}"
    normalized_bnet = str(bnet_id or "").strip()
    if normalized_bnet:
        return f"bnet:{normalized_bnet}"
    return str(customer_token or "").strip()


def _read_env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, "") or "").strip() or int(default))
    except (TypeError, ValueError):
        return int(default)


MATCH_LIST_PAGE_CACHE_TTL = max(0, _read_env_int("OVERSTATS_MATCH_LIST_PAGE_CACHE_TTL", 300))
MATCH_LIST_PAGE_MEMORY_CACHE_TTL = max(
    0,
    _read_env_int("OVERSTATS_MATCH_LIST_PAGE_MEMORY_CACHE_TTL", 300),
)
MATCH_LIST_PAGE_MEMORY_CACHE_MAX = max(128, _read_env_int("OVERSTATS_MATCH_LIST_PAGE_MEMORY_CACHE_MAX", 2048))
_MATCH_LIST_PAGE_MEMORY_CACHE: "OrderedDict[Tuple[Any, ...], Tuple[float, Any]]" = OrderedDict()


def _get_memory_page_cache(key: Tuple[Any, ...]) -> Any:
    if MATCH_LIST_PAGE_MEMORY_CACHE_TTL <= 0:
        return None
    cached = _MATCH_LIST_PAGE_MEMORY_CACHE.get(key)
    if cached is None:
        return None
    expires_at, payload = cached
    if time.time() >= float(expires_at or 0):
        _MATCH_LIST_PAGE_MEMORY_CACHE.pop(key, None)
        return None
    try:
        _MATCH_LIST_PAGE_MEMORY_CACHE.move_to_end(key)
    except Exception:
        pass
    _report_cache_hit("memory")
    return payload


def _peek_memory_page_cache(key: Tuple[Any, ...]) -> Any:
    if MATCH_LIST_PAGE_MEMORY_CACHE_TTL <= 0:
        return None
    cached = _MATCH_LIST_PAGE_MEMORY_CACHE.get(key)
    if cached is None:
        return None
    expires_at, payload = cached
    if time.time() >= float(expires_at or 0):
        _MATCH_LIST_PAGE_MEMORY_CACHE.pop(key, None)
        return None
    return payload


def _set_memory_page_cache(key: Tuple[Any, ...], payload: Any) -> None:
    if MATCH_LIST_PAGE_MEMORY_CACHE_TTL <= 0 or payload is None:
        return
    _MATCH_LIST_PAGE_MEMORY_CACHE[key] = (time.time() + MATCH_LIST_PAGE_MEMORY_CACHE_TTL, payload)
    try:
        _MATCH_LIST_PAGE_MEMORY_CACHE.move_to_end(key)
    except Exception:
        pass
    while len(_MATCH_LIST_PAGE_MEMORY_CACHE) > MATCH_LIST_PAGE_MEMORY_CACHE_MAX:
        _MATCH_LIST_PAGE_MEMORY_CACHE.popitem(last=False)


def match_id_set_from_db(
    db: Optional[IDPoolDB] = None,
    *,
    bnet_id: str = "",
    customer_token: str = "",
    limit: int = 1000,
) -> set[str]:
    """Return a set of known match_ids for callers that need a DB snapshot.

    - If *bnet_id* is provided, return only that player's match_ids (max *limit*).
    - Else if *customer_token* is provided, resolve to bnet_id first.
    - Else fall back to the global set (backward compatible).
    """
    db = db or IDPoolDB()
    try:
        resolved_bnet = str(bnet_id or "").strip()
        if not resolved_bnet and customer_token:
            resolved_bnet = str(db.resolve_bnet_id_by_token(customer_token) or "").strip()
        if resolved_bnet:
            result = db.get_player_match_ids(resolved_bnet, limit=limit)
        else:
            result = db.get_all_match_ids()
        if result:
            _report_cache_hit("db")
        return result
    except Exception:
        return set()


@dataclass(frozen=True)
class PaginatedMatchesResult:
    matches: List[Dict[str, Any]]
    stop_reason: str = ""
    pages_requested: int = 0
    singleflight_reuse_count: int = 0


async def fetch_paginated_match_entries(
    *,
    source_kind: str,
    customer_token: str,
    game_mode: str,
    season: Any,
    batch_size: int,
    fetch_page: FetchPage,
    extract_entries: ExtractEntries,
    begin_ts_getter: BeginTsGetter,
    min_begin_ts: Optional[int] = None,
    bnet_id: str = "",
    stop_from_page: int = 4,
    db: Optional[IDPoolDB] = None,
) -> PaginatedMatchesResult:
    """Fetch match-list pages with per-page singleflight and page-cache reuse.

    Time-window requests stop only after crossing the requested boundary.
    Open-ended requests keep the original behavior and scan until an empty page.
    """

    db_adapter = db or IDPoolDB()
    owner_key = cache_owner_key(customer_token=customer_token, bnet_id=bnet_id)
    normalized_batch_size = max(1, int(batch_size or 1))
    first_stop_page = max(1, int(stop_from_page or 4))
    page = 1
    matches: List[Dict[str, Any]] = []
    pages_requested = 0
    stop_reason = ""
    start_reuse_count = list_page_singleflight.reuse_count
    refresh_all_pages = False

    def page_cache_key(page_num: int) -> Tuple[Any, ...]:
        return (
            "list",
            str(source_kind or "normal"),
            owner_key,
            str(game_mode or ""),
            season_key(season),
            int(page_num),
        )

    def cached_payload_for_page(page_num: int, *, report_hit: bool) -> Any:
        key = page_cache_key(page_num)
        cached_memory_payload = _get_memory_page_cache(key) if report_hit else _peek_memory_page_cache(key)
        if cached_memory_payload is not None:
            return cached_memory_payload
        cached_page = db_adapter.get_match_list_page_cache(
            source_kind=str(source_kind or "normal"),
            customer_token=owner_key,
            game_mode=str(game_mode or ""),
            season_key=season_key(season),
            page=int(page_num),
            max_age_sec=MATCH_LIST_PAGE_CACHE_TTL,
        )
        if cached_page is not None and isinstance(cached_page.get("payload"), dict):
            if report_hit:
                _report_cache_hit("db")
            payload = cached_page.get("payload")
            _set_memory_page_cache(key, payload)
            return payload
        return None

    def payload_match_ids(payload: Any) -> List[str]:
        ids: List[str] = []
        try:
            entries = extract_entries(payload)
        except Exception:
            entries = []
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            match_id = str(entry.get("matchId") or "").strip()
            if match_id:
                ids.append(match_id)
        return ids

    while True:
        if page == 1:
            page_numbers = [1]
        elif page >= first_stop_page:
            page_numbers = [page]
        else:
            max_pre_stop = first_stop_page - page
            page_numbers = list(range(page, page + min(normalized_batch_size, max_pre_stop)))

        async def fetch_one(page_num: int) -> Any:
            key = page_cache_key(page_num)
            force_upstream = int(page_num) == 1 or refresh_all_pages
            cached_payload = None if force_upstream else cached_payload_for_page(page_num, report_hit=True)
            if cached_payload is not None:
                return cached_payload
            payload = await list_page_singleflight.do(key, lambda: fetch_page(page_num))
            _set_memory_page_cache(key, payload)
            return payload

        first_page_cached_payload = None
        if page_numbers == [1]:
            first_page_cached_payload = cached_payload_for_page(1, report_hit=False)

        payloads = await asyncio.gather(
            *(fetch_one(page_num) for page_num in page_numbers),
            return_exceptions=True,
        )
        pages_requested += len(page_numbers)

        if page_numbers == [1] and payloads and not isinstance(payloads[0], Exception):
            cached_ids = payload_match_ids(first_page_cached_payload)
            fresh_ids = payload_match_ids(payloads[0])
            if cached_ids and fresh_ids and cached_ids != fresh_ids:
                refresh_all_pages = True

        batch_has_data = False
        batch_max_begin_ts = 0
        for page_num, payload in zip(page_numbers, payloads):
            if isinstance(payload, Exception):
                continue
            entries = extract_entries(payload)
            if not entries:
                continue
            batch_has_data = True
            for raw_entry in entries:
                if not isinstance(raw_entry, dict):
                    continue
                entry = dict(raw_entry)
                begin_ts = int(begin_ts_getter(entry) or 0)
                batch_max_begin_ts = max(batch_max_begin_ts, begin_ts)
                if min_begin_ts is not None and begin_ts < int(min_begin_ts):
                    continue
                matches.append(entry)

        if not batch_has_data:
            stop_reason = "empty_page"
            break
        if min_begin_ts is not None and batch_max_begin_ts and batch_max_begin_ts < int(min_begin_ts):
            stop_reason = "min_begin_ts"
            break
        page += len(page_numbers)

    if stop_reason and page_numbers:
        try:
            await asyncio.to_thread(
                db_adapter.update_match_list_page_stop_reason,
                source_kind=str(source_kind or "normal"),
                customer_token=owner_key,
                game_mode=str(game_mode or ""),
                season_key=season_key(season),
                page=int(page_numbers[-1]),
                stop_reason=stop_reason,
            )
        except Exception:
            pass

    return PaginatedMatchesResult(
        matches=matches,
        stop_reason=stop_reason,
        pages_requested=pages_requested,
        singleflight_reuse_count=list_page_singleflight.reuse_count - start_reuse_count,
    )


async def fetch_match_list_page_cached(
    *,
    source_kind: str,
    customer_token: str,
    game_mode: str,
    season: Any,
    page: int,
    fetch_page: FetchPage,
    bnet_id: str = "",
    db: Optional[IDPoolDB] = None,
    force_page1_upstream: bool = True,
) -> Any:
    """Fetch one queryMatchList page while preserving caller-owned scan logic.

    This helper intentionally does not inspect entries, enforce time windows, or
    stop early. It only chooses the source for a single page so callers such as
    dashen-profile can keep their original page-count and boundary behavior.
    """

    db_adapter = db or IDPoolDB()
    owner_key = cache_owner_key(customer_token=customer_token, bnet_id=bnet_id)
    normalized_page = max(1, int(page or 1))
    key = (
        "list",
        str(source_kind or "normal"),
        owner_key,
        str(game_mode or ""),
        season_key(season),
        normalized_page,
    )

    force_upstream = bool(force_page1_upstream and normalized_page == 1)
    if not force_upstream:
        cached_payload = _get_memory_page_cache(key)
        if cached_payload is not None:
            return cached_payload
        cached_page = db_adapter.get_match_list_page_cache(
            source_kind=str(source_kind or "normal"),
            customer_token=owner_key,
            game_mode=str(game_mode or ""),
            season_key=season_key(season),
            page=normalized_page,
            max_age_sec=MATCH_LIST_PAGE_CACHE_TTL,
        )
        if cached_page is not None and isinstance(cached_page.get("payload"), dict):
            _report_cache_hit("db")
            payload = cached_page.get("payload")
            _set_memory_page_cache(key, payload)
            return payload

    payload = await list_page_singleflight.do(key, lambda: fetch_page(normalized_page))
    _set_memory_page_cache(key, payload)
    return payload


__all__ = [
    "AsyncSingleFlight",
    "PaginatedMatchesResult",
    "current_cache_week",
    "cache_owner_key",
    "fetch_paginated_match_entries",
    "fetch_match_list_page_cached",
    "list_page_singleflight",
    "match_id_set_from_db",
    "rank_singleflight",
    "season_key",
]
