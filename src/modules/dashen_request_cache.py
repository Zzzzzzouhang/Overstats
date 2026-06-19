from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

try:
    from overstats.src.db.match_stats import IDPoolDB
except ModuleNotFoundError:
    from src.db.match_stats import IDPoolDB


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
    return str(season)


def match_id_set_from_db(db: Optional[IDPoolDB] = None) -> set[str]:
    db = db or IDPoolDB()
    try:
        return db.get_all_match_ids()
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
    existing_match_ids: Optional[set[str]] = None,
    stop_from_page: int = 4,
    target_count: Optional[int] = None,
    db: Optional[IDPoolDB] = None,
) -> PaginatedMatchesResult:
    """Fetch match-list pages with per-page singleflight and DB-snapshot stopping."""

    db_adapter = db or IDPoolDB()
    snapshot_ids = set(existing_match_ids) if existing_match_ids is not None else match_id_set_from_db(db_adapter)
    normalized_batch_size = max(1, int(batch_size or 1))
    first_stop_page = max(1, int(stop_from_page or 4))
    page = 1
    matches: List[Dict[str, Any]] = []
    pages_requested = 0
    stop_reason = ""
    start_reuse_count = list_page_singleflight.reuse_count

    while True:
        if page >= first_stop_page:
            page_numbers = [page]
        else:
            max_pre_stop = first_stop_page - page
            page_numbers = list(range(page, page + min(normalized_batch_size, max_pre_stop)))

        async def fetch_one(page_num: int) -> Any:
            key = (
                "list",
                str(source_kind or "normal"),
                str(customer_token or ""),
                str(game_mode or ""),
                season_key(season),
                int(page_num),
            )
            return await list_page_singleflight.do(key, lambda: fetch_page(page_num))

        payloads = await asyncio.gather(
            *(fetch_one(page_num) for page_num in page_numbers),
            return_exceptions=True,
        )
        pages_requested += len(page_numbers)

        batch_has_data = False
        batch_max_begin_ts = 0
        batch_hit_existing = False
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
                match_id = str(entry.get("matchId") or "").strip()
                if page_num >= first_stop_page and match_id and match_id in snapshot_ids:
                    batch_hit_existing = True
                if min_begin_ts is not None and begin_ts < int(min_begin_ts):
                    continue
                matches.append(entry)

        if not batch_has_data:
            stop_reason = "empty_page"
            break
        if min_begin_ts is not None and batch_max_begin_ts and batch_max_begin_ts < int(min_begin_ts):
            stop_reason = "min_begin_ts"
            break
        if target_count is not None and len(matches) >= int(target_count) and max(page_numbers) >= first_stop_page - 1:
            stop_reason = "target_count"
            break
        if batch_hit_existing:
            stop_reason = "existing_match_id"
            break
        page += len(page_numbers)

    if stop_reason and page_numbers:
        try:
            await asyncio.to_thread(
                db_adapter.update_match_list_page_stop_reason,
                source_kind=str(source_kind or "normal"),
                customer_token=str(customer_token or ""),
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


__all__ = [
    "AsyncSingleFlight",
    "PaginatedMatchesResult",
    "current_cache_week",
    "fetch_paginated_match_entries",
    "list_page_singleflight",
    "match_id_set_from_db",
    "rank_singleflight",
    "season_key",
]
