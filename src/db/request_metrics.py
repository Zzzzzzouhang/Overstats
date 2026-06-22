from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit

try:
    from overstats.config import is_database_write_enabled
except ModuleNotFoundError:
    from config import is_database_write_enabled


REQUEST_METRICS_DB_PATH = Path(__file__).resolve().parent / "request_metrics.sqlite3"
REQUEST_METRICS_TABLE = "request_url_stats"
REQUEST_SOURCE_MODULE = "module"
REQUEST_SOURCE_UPSTREAM = "upstream"

ENDPOINT_PERF_TABLE = "endpoint_perf_stats"
ENDPOINT_PERF_DEFAULT_MINUTES = 360          # default aggregation window 6 hours
ENDPOINT_PERF_MAX_MINUTES = 60 * 24 * 30     # single query upper limit 30 days
ENDPOINT_PERF_PERCENTILES = (0.5, 0.95, 0.99)


@dataclass(frozen=True)
class _MetricEvent:
    url: str
    source_type: str
    success: bool


@dataclass(frozen=True)
class _PerfEvent:
    url: str
    http_status: int
    total_ms: int
    service_ms: int
    upstream_ms: int
    render_ms: int
    queue_wait_ms: int
    success: bool
    recorded_at: str
    upstream_count: int = -1
    memory_cache_hits: int = -1
    db_read_hits: int = -1
    upstream_breakdown: str = "{}"


def normalize_request_metric_url(url: str) -> str:
    normalized = str(url or "").strip()
    if not normalized:
        return ""
    try:
        parsed = urlsplit(normalized)
    except Exception:
        return normalized.split("?", 1)[0].strip()
    if not parsed.scheme and not parsed.netloc:
        path = parsed.path or normalized
        return path.split("?", 1)[0].strip()
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")).strip()


def _normalize_metric_row(
    url: str,
    source_type: str,
    total_requests: object,
    successful_requests: object,
    failed_requests: object,
    updated_at: str,
) -> Optional[tuple[str, str, int, int, int, float, str]]:
    normalized_url = normalize_request_metric_url(url)
    normalized_source = str(source_type or "").strip().lower()
    if not normalized_url or normalized_source not in {REQUEST_SOURCE_MODULE, REQUEST_SOURCE_UPSTREAM}:
        return None

    total = max(int(total_requests or 0), 0)
    successful = max(int(successful_requests or 0), 0)
    failed = max(int(failed_requests or 0), 0)
    computed_total = successful + failed
    if total < computed_total:
        total = computed_total
    if total <= 0:
        return None
    success_rate = float(successful) / float(total)
    return (
        normalized_url,
        normalized_source,
        total,
        successful,
        failed,
        success_rate,
        str(updated_at or "").strip() or datetime.now(timezone.utc).isoformat(),
    )


class RequestMetricsRecorder:
    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = Path(db_path or REQUEST_METRICS_DB_PATH)
        self._queue: asyncio.Queue[Optional[_MetricEvent]] = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task[None]] = None
        self._started = False
        self._closed = False

    async def start(self) -> None:
        if self._started or not is_database_write_enabled():
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._initialize_database)
        self._worker_task = asyncio.create_task(self._worker(), name="request-metrics-worker")
        self._started = True

    async def enqueue(self, url: str, source_type: str, success: bool) -> None:
        if not is_database_write_enabled():
            return
        normalized_url = normalize_request_metric_url(url)
        normalized_source = str(source_type or "").strip().lower()
        if not normalized_url or normalized_source not in {REQUEST_SOURCE_MODULE, REQUEST_SOURCE_UPSTREAM}:
            return
        if self._closed:
            return
        if not self._started:
            await self.start()
        await self._queue.put(_MetricEvent(normalized_url, normalized_source, bool(success)))

    async def close(self) -> None:
        if not self._started or self._closed:
            return
        self._closed = True
        await self._queue.join()
        await self._queue.put(None)
        if self._worker_task is not None:
            await self._worker_task
        self._worker_task = None

    def _initialize_database(self) -> None:
        connection = sqlite3.connect(self.db_path)
        try:
            connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {REQUEST_METRICS_TABLE} (
                    url TEXT PRIMARY KEY,
                    source_type TEXT NOT NULL,
                    total_requests INTEGER NOT NULL DEFAULT 0,
                    successful_requests INTEGER NOT NULL DEFAULT 0,
                    failed_requests INTEGER NOT NULL DEFAULT 0,
                    success_rate REAL NOT NULL DEFAULT 0.0,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._normalize_existing_rows(connection)
            connection.commit()
        finally:
            connection.close()

    async def _worker(self) -> None:
        while True:
            event = await self._queue.get()
            if event is None:
                self._queue.task_done()
                return
            try:
                await asyncio.to_thread(self._write_event, event)
            finally:
                self._queue.task_done()

    def _write_event(self, event: _MetricEvent) -> None:
        normalized_row = _normalize_metric_row(
            event.url,
            event.source_type,
            1,
            1 if event.success else 0,
            0 if event.success else 1,
            datetime.now(timezone.utc).isoformat(),
        )
        if normalized_row is None:
            return
        connection = sqlite3.connect(self.db_path)
        try:
            connection.execute(
                f"""
                INSERT INTO {REQUEST_METRICS_TABLE} (
                    url,
                    source_type,
                    total_requests,
                    successful_requests,
                    failed_requests,
                    success_rate,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    source_type = excluded.source_type,
                    total_requests = {REQUEST_METRICS_TABLE}.total_requests + excluded.total_requests,
                    successful_requests = {REQUEST_METRICS_TABLE}.successful_requests + excluded.successful_requests,
                    failed_requests = {REQUEST_METRICS_TABLE}.failed_requests + excluded.failed_requests,
                    success_rate = CAST(
                        {REQUEST_METRICS_TABLE}.successful_requests + excluded.successful_requests AS REAL
                    ) / CAST({REQUEST_METRICS_TABLE}.total_requests + excluded.total_requests AS REAL),
                    updated_at = excluded.updated_at
                """,
                normalized_row,
            )
            connection.commit()
        finally:
            connection.close()

    def read_all_module_stats(self) -> List[Dict[str, Any]]:
        """Read existing request_url_stats for module source_type (for /api/v2/metrics)."""
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                f"""
                SELECT url, source_type, total_requests, successful_requests,
                       failed_requests, success_rate, updated_at
                FROM {REQUEST_METRICS_TABLE}
                WHERE source_type = 'module'
                ORDER BY total_requests DESC
                """
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            connection.close()

    def _normalize_existing_rows(self, connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            f"""
            SELECT
                url,
                source_type,
                total_requests,
                successful_requests,
                failed_requests,
                updated_at
            FROM {REQUEST_METRICS_TABLE}
            """
        ).fetchall()
        if not rows:
            return

        merged_rows: dict[str, list[object]] = {}
        for row in rows:
            normalized_row = _normalize_metric_row(*row)
            if normalized_row is None:
                continue
            url, source_type, total, successful, failed, _success_rate, updated_at = normalized_row
            bucket = merged_rows.get(url)
            if bucket is None:
                merged_rows[url] = [source_type, total, successful, failed, updated_at]
                continue
            bucket[1] = int(bucket[1]) + total
            bucket[2] = int(bucket[2]) + successful
            bucket[3] = int(bucket[3]) + failed
            if str(updated_at) >= str(bucket[4]):
                bucket[0] = source_type
                bucket[4] = updated_at

        normalized_rows = []
        for url, (source_type, total, successful, failed, updated_at) in merged_rows.items():
            total_int = int(total)
            success_int = int(successful)
            normalized_rows.append(
                (
                    url,
                    str(source_type),
                    total_int,
                    success_int,
                    int(failed),
                    float(success_int) / float(total_int),
                    str(updated_at),
                )
            )

        if len(normalized_rows) == len(rows):
            unchanged = True
            original_rows = {
                (
                    str(url),
                    str(source_type),
                    int(total_requests or 0),
                    int(successful_requests or 0),
                    int(failed_requests or 0),
                    str(updated_at or "").strip(),
                )
                for url, source_type, total_requests, successful_requests, failed_requests, updated_at in rows
            }
            normalized_snapshot = {
                (
                    str(url),
                    str(source_type),
                    int(total_requests),
                    int(successful_requests),
                    int(failed_requests),
                    str(updated_at).strip(),
                )
                for url, source_type, total_requests, successful_requests, failed_requests, _success_rate, updated_at in normalized_rows
            }
            unchanged = original_rows == normalized_snapshot
            if unchanged:
                return

        connection.execute(f"DELETE FROM {REQUEST_METRICS_TABLE}")
        connection.executemany(
            f"""
            INSERT INTO {REQUEST_METRICS_TABLE} (
                url,
                source_type,
                total_requests,
                successful_requests,
                failed_requests,
                success_rate,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            normalized_rows,
        )


def _percentiles(samples: List[int], percentiles: Sequence[float]) -> Dict[float, Optional[int]]:
    """Compute linear-interpolation percentiles (matches numpy default)."""
    if not samples:
        return {p: None for p in percentiles}
    n = len(samples)
    out: Dict[float, Optional[int]] = {}
    for p in percentiles:
        if n == 1:
            out[p] = int(samples[0])
            continue
        rank = p * (n - 1)
        lo = int(rank)
        hi = min(lo + 1, n - 1)
        frac = rank - lo
        out[p] = int(round(samples[lo] + (samples[hi] - samples[lo]) * frac))
    return out


def _round_or_none(value: object) -> Optional[float]:
    if value is None:
        return None
    return round(float(value), 2)


class EndpointPerfRecorder:
    """Async background writer for endpoint_perf_stats table."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = Path(db_path or REQUEST_METRICS_DB_PATH)
        self._queue: asyncio.Queue[Optional[_PerfEvent]] = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task[None]] = None
        self._started = False
        self._closed = False

    async def start(self) -> None:
        if self._started or not is_database_write_enabled():
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._initialize_database)
        self._worker_task = asyncio.create_task(self._worker(), name="endpoint-perf-worker")
        self._started = True

    async def enqueue(
        self,
        url: str,
        *,
        http_status: int,
        total_ms: int,
        service_ms: int,
        upstream_ms: int,
        render_ms: int = -1,
        queue_wait_ms: int = -1,
        upstream_count: int = -1,
        memory_cache_hits: int = -1,
        db_read_hits: int = -1,
        upstream_breakdown: str = "{}",
        success: bool,
    ) -> None:
        if not is_database_write_enabled() or self._closed:
            return
        normalized_url = normalize_request_metric_url(url)
        if not normalized_url:
            return
        if not self._started:
            await self.start()
        await self._queue.put(_PerfEvent(
            url=normalized_url,
            http_status=int(http_status),
            total_ms=max(0, int(total_ms)),
            service_ms=max(0, int(service_ms)),
            upstream_ms=max(0, int(upstream_ms)),
            render_ms=int(render_ms),
            queue_wait_ms=int(queue_wait_ms),
            success=bool(success),
            recorded_at=datetime.now(timezone.utc).isoformat(),
            upstream_count=int(upstream_count),
            memory_cache_hits=int(memory_cache_hits),
            db_read_hits=int(db_read_hits),
            upstream_breakdown=str(upstream_breakdown),
        ))

    async def close(self) -> None:
        if not self._started or self._closed:
            return
        self._closed = True
        await self._queue.join()
        await self._queue.put(None)
        if self._worker_task is not None:
            await self._worker_task
        self._worker_task = None

    def _initialize_database(self) -> None:
        connection = sqlite3.connect(self.db_path)
        try:
            # Field semantics:
            #   total_ms     = handler full duration (body parse + service + response write)
            #   service_ms   = async_runner.run(coro) measured time
            #   upstream_ms  = upstream HTTP cumulative (includes worker render round-trip for summary endpoints)
            #   render_ms    = only summary endpoints (worker self-reported RENDER_DONE); others -1
            #   queue_wait_ms= DashenRequestQueue semaphore wait cumulative (non-dashen = -1)
            connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {ENDPOINT_PERF_TABLE} (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    url            TEXT NOT NULL,
                    http_status    INTEGER NOT NULL DEFAULT 0,
                    total_ms       INTEGER NOT NULL DEFAULT 0,
                    service_ms     INTEGER NOT NULL DEFAULT 0,
                    upstream_ms    INTEGER NOT NULL DEFAULT 0,
                    render_ms      INTEGER NOT NULL DEFAULT -1,
                    queue_wait_ms  INTEGER NOT NULL DEFAULT -1,
                    success        INTEGER NOT NULL DEFAULT 1,
                    recorded_at    TEXT NOT NULL
                )
                """
            )
            connection.execute(
                f"CREATE INDEX IF NOT EXISTS idx_perf_url_time "
                f"ON {ENDPOINT_PERF_TABLE}(url, recorded_at)"
            )
            # Schema migration: add count columns if missing (idempotent).
            # db_cache_hit_count is retained for old databases but no longer written.
            for col, col_type, default in [
                ("upstream_count", "INTEGER", -1),
                ("db_cache_hit_count", "INTEGER", -1),
                ("memory_cache_hits", "INTEGER", -1),
                ("db_read_hits", "INTEGER", -1),
                ("upstream_breakdown", "TEXT", "'{}'"),
            ]:
                try:
                    connection.execute(
                        f"ALTER TABLE {ENDPOINT_PERF_TABLE} ADD COLUMN {col} {col_type} NOT NULL DEFAULT {default}"
                    )
                except sqlite3.OperationalError:
                    pass  # column already exists
            connection.commit()
        finally:
            connection.close()

    async def _worker(self) -> None:
        while True:
            event = await self._queue.get()
            if event is None:
                self._queue.task_done()
                return
            try:
                await asyncio.to_thread(self._write_event, event)
            finally:
                self._queue.task_done()

    def _write_event(self, event: _PerfEvent) -> None:
        connection = sqlite3.connect(self.db_path)
        try:
            connection.execute(
                f"""
                INSERT INTO {ENDPOINT_PERF_TABLE} (
                    url, http_status, total_ms, service_ms, upstream_ms,
                    render_ms, queue_wait_ms, success, recorded_at,
                    upstream_count, memory_cache_hits, db_read_hits, upstream_breakdown
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.url, event.http_status, event.total_ms, event.service_ms,
                    event.upstream_ms, event.render_ms, event.queue_wait_ms,
                    1 if event.success else 0, event.recorded_at,
                    event.upstream_count, event.memory_cache_hits,
                    event.db_read_hits, event.upstream_breakdown,
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def read_endpoint_perf_summary(
        self, *, minutes: int = ENDPOINT_PERF_DEFAULT_MINUTES
    ) -> List[Dict[str, Any]]:
        """Aggregated performance stats per endpoint within the time window, including p50/p95/p99."""
        window_minutes = max(1, min(int(minutes or ENDPOINT_PERF_DEFAULT_MINUTES), ENDPOINT_PERF_MAX_MINUTES))
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=window_minutes)).isoformat()

        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        try:
            base_rows = connection.execute(
                f"""
                SELECT url,
                       COUNT(*)                              AS call_count,
                       SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS success_count,
                       AVG(total_ms)                         AS avg_total_ms,
                       MAX(total_ms)                         AS max_total_ms,
                       MIN(total_ms)                         AS min_total_ms,
                       AVG(service_ms)                       AS avg_service_ms,
                       AVG(upstream_ms)                      AS avg_upstream_ms,
                       AVG(CASE WHEN render_ms >= 0 THEN render_ms END)     AS avg_render_ms,
                       AVG(CASE WHEN queue_wait_ms >= 0 THEN queue_wait_ms END) AS avg_queue_wait_ms,
                       AVG(http_status)                      AS avg_http_status,
                       AVG(CASE WHEN upstream_count >= 0 THEN upstream_count END) AS avg_upstream_count,
                       AVG(CASE WHEN memory_cache_hits >= 0 THEN memory_cache_hits END) AS avg_memory_cache_hits,
                       AVG(CASE WHEN db_read_hits >= 0 THEN db_read_hits END) AS avg_db_read_hits,
                       SUM(CASE WHEN upstream_count >= 0 THEN upstream_count ELSE 0 END) AS total_upstream_calls,
                       SUM(CASE WHEN memory_cache_hits >= 0 THEN memory_cache_hits ELSE 0 END) AS total_memory_cache_hits,
                       SUM(CASE WHEN db_read_hits >= 0 THEN db_read_hits ELSE 0 END) AS total_db_read_hits
                FROM {ENDPOINT_PERF_TABLE}
                WHERE recorded_at > ?
                GROUP BY url
                ORDER BY call_count DESC
                """,
                (cutoff,),
            ).fetchall()

            # Return field notes:
            #   avg_upstream_ms (summary endpoints) includes worker render round-trip;
            #     do NOT add avg_render_ms on top of it.
            #   avg_render_ms is non-null only for summary endpoints.
            #   avg_queue_wait_ms is non-null only for dashen-class endpoints.
            result: List[Dict[str, Any]] = []
            for row in base_rows:
                url = row["url"]
                samples = [
                    r[0]
                    for r in connection.execute(
                        f"SELECT total_ms FROM {ENDPOINT_PERF_TABLE} "
                        f"WHERE url = ? AND recorded_at > ? ORDER BY total_ms",
                        (url, cutoff),
                    ).fetchall()
                ]
                pcts = _percentiles(samples, ENDPOINT_PERF_PERCENTILES)
                result.append({
                    "url": url,
                    "call_count": int(row["call_count"]),
                    "success_count": int(row["success_count"]),
                    "success_rate": (
                        int(row["success_count"]) / int(row["call_count"])
                        if row["call_count"]
                        else 0.0
                    ),
                    "avg_total_ms": _round_or_none(row["avg_total_ms"]),
                    "min_total_ms": int(row["min_total_ms"]),
                    "max_total_ms": int(row["max_total_ms"]),
                    "p50_total_ms": pcts.get(0.5),
                    "p95_total_ms": pcts.get(0.95),
                    "p99_total_ms": pcts.get(0.99),
                    "avg_service_ms": _round_or_none(row["avg_service_ms"]),
                    "avg_upstream_ms": _round_or_none(row["avg_upstream_ms"]),
                    "avg_render_ms": _round_or_none(row["avg_render_ms"]),
                    "avg_queue_wait_ms": _round_or_none(row["avg_queue_wait_ms"]),
                    "avg_http_status": _round_or_none(row["avg_http_status"]),
                    "avg_upstream_count": _round_or_none(row["avg_upstream_count"]),
                    "avg_memory_cache_hits": _round_or_none(row["avg_memory_cache_hits"]),
                    "avg_db_read_hits": _round_or_none(row["avg_db_read_hits"]),
                    "total_upstream_calls": int(row["total_upstream_calls"]),
                    "total_memory_cache_hits": int(row["total_memory_cache_hits"]),
                    "total_db_read_hits": int(row["total_db_read_hits"]),
                    "window_minutes": window_minutes,
                })
            return result
        finally:
            connection.close()

    def read_endpoint_perf(self, *, limit: int = 100) -> List[Dict[str, Any]]:
        """Recent raw performance samples (for debugging)."""
        limit = max(1, min(int(limit or 100), 1000))
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                f"""
                SELECT url, http_status, total_ms, service_ms, upstream_ms,
                       render_ms, queue_wait_ms, success, recorded_at,
                       upstream_count, memory_cache_hits, db_read_hits, upstream_breakdown
                FROM {ENDPOINT_PERF_TABLE}
                ORDER BY recorded_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            result: List[Dict[str, Any]] = []
            for row in rows:
                item = dict(row)
                try:
                    item["upstream_breakdown"] = json.loads(str(item.get("upstream_breakdown") or "{}"))
                except Exception:
                    item["upstream_breakdown"] = {}
                result.append(item)
            return result
        finally:
            connection.close()
