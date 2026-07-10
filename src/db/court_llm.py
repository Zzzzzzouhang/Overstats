"""电竞法庭（court）LLM 调用的数据库与异步写入器。

与是区吗（shiqu）共用同一个 sqlite 文件 ``shiqu_llm.sqlite3``，但使用独立表
``court_llm_result``，避免污染 shiqu 的表。court 的 LLM 输出为纯文本判决书，
渲染时直接由 ``raw_response`` 解析，因此不再冗余存储 score/verdict 等字段，也不需要
``customer_token`` / ``bnet_id``（target_id 已能标识玩家）。

除渲染所需元数据（match_index / map_name / game_mode）外，额外落库 LLM 调用的诊断信息：
``ok``（是否成功）、``prompt``（本次提示词）、``duration_ms``（总耗时）、``call_count``
（实际调用次数），便于排查大模型调用质量。

schema 初始化仅 ``CREATE TABLE IF NOT EXISTS``，文件/表不存在时自动新建。
写入受 ``is_database_write_enabled`` 开关控制。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import sqlite3

try:
    from overstats.config import is_database_write_enabled
    from overstats.src.db.shiqu_llm import SHIQU_LLM_DB_PATH
except ModuleNotFoundError:  # pragma: no cover
    from config import is_database_write_enabled
    from src.db.shiqu_llm import SHIQU_LLM_DB_PATH


COURT_LLM_TABLE = "court_llm_result"
RECORDER_QUEUE_MAXSIZE = 512


@dataclass(frozen=True)
class _CourtLLMEvent:
    target_id: str
    match_index: int = 0
    map_name: str = ""
    game_mode: str = ""
    prompt: str = ""
    raw_response: str = ""
    ok: bool = False
    duration_ms: int = 0
    call_count: int = 0


def _build_row(event: _CourtLLMEvent) -> Tuple:
    return (
        str(event.target_id or ""),
        int(event.match_index or 0),
        str(event.map_name or ""),
        str(event.game_mode or ""),
        str(event.prompt or ""),
        str(event.raw_response or ""),
        1 if event.ok else 0,
        int(event.duration_ms or 0),
        int(event.call_count or 0),
        int(time.time()),
    )


class CourtLLMDB:
    """court LLM 结果的 sqlite 适配器（惰性、容错）。"""

    _warned_messages: set[str] = set()

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = Path(db_path or SHIQU_LLM_DB_PATH)

    @classmethod
    def _warn_once(cls, message: str) -> None:
        if message in cls._warned_messages:
            return
        cls._warned_messages.add(message)
        print(f"[overstats] {message}")

    def _get_connection(self) -> Optional[sqlite3.Connection]:
        if not self.db_path.exists():
            self._warn_once(f"court llm sqlite db not found: {self.db_path}")
            return None
        try:
            return sqlite3.connect(str(self.db_path), timeout=10)
        except Exception as exc:
            self._warn_once(f"court llm sqlite connection failed: {type(exc).__name__}: {exc}")
            return None

    def _get_write_connection(self) -> Optional[sqlite3.Connection]:
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.db_path), timeout=30)
            conn.execute("PRAGMA journal_mode=WAL")
            return conn
        except Exception as exc:
            self._warn_once(f"court llm sqlite write connection failed: {type(exc).__name__}: {exc}")
            return None

    def initialize_schema(self) -> None:
        connection = self._get_write_connection()
        if connection is None:
            return
        try:
            connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {COURT_LLM_TABLE} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target_id TEXT NOT NULL,
                    match_index INTEGER NOT NULL DEFAULT 0,
                    map_name TEXT NOT NULL DEFAULT '',
                    game_mode TEXT NOT NULL DEFAULT '',
                    prompt TEXT NOT NULL DEFAULT '',
                    raw_response TEXT NOT NULL DEFAULT '',
                    ok INTEGER NOT NULL DEFAULT 0,
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    call_count INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_court_target
                ON {COURT_LLM_TABLE}(target_id, created_at)
                """
            )
            connection.commit()
        finally:
            connection.close()

    def insert_result(self, event: _CourtLLMEvent) -> Optional[int]:
        connection = self._get_write_connection()
        if connection is None:
            return None
        try:
            cursor = connection.cursor()
            cursor.execute(
                f"""
                INSERT INTO {COURT_LLM_TABLE}
                (target_id, match_index, map_name, game_mode, prompt,
                 raw_response, ok, duration_ms, call_count, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                _build_row(event),
            )
            result_id = cursor.lastrowid
            connection.commit()
            return result_id
        except Exception as exc:
            self._warn_once(f"court llm insert failed: {type(exc).__name__}: {exc}")
            try:
                connection.rollback()
            except Exception:
                pass
            return None
        finally:
            connection.close()

    def get_recent(self, limit: int = 50) -> List[Dict[str, Any]]:
        connection = self._get_connection()
        if connection is None:
            return []
        try:
            rows = connection.execute(
                f"""
                SELECT id, target_id, match_index, map_name, game_mode,
                       ok, duration_ms, call_count, created_at
                FROM {COURT_LLM_TABLE}
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
            return [
                {
                    "id": r[0],
                    "target_id": r[1],
                    "match_index": r[2],
                    "map_name": r[3],
                    "game_mode": r[4],
                    "ok": bool(r[5]),
                    "duration_ms": r[6],
                    "call_count": r[7],
                    "created_at": r[8],
                }
                for r in rows
            ]
        except Exception:
            return []
        finally:
            connection.close()

    def get_latest_by_target(
        self, target_id: str, match_index: Optional[int] = None
    ) -> Optional[Dict[str, Any]]:
        """读取该 target_id（可选指定对局序号）最近一条判决书原始返回与渲染元数据。

        返回 ``{"raw_response", "match_index", "map_name", "game_mode", "ok", "created_at"}``；
        无记录或库不可用时返回 ``None``。
        """
        connection = self._get_connection()
        if connection is None or not str(target_id or "").strip():
            return None
        try:
            if match_index is not None:
                row = connection.execute(
                    f"""
                    SELECT raw_response, match_index, map_name, game_mode, ok, created_at
                    FROM {COURT_LLM_TABLE}
                    WHERE target_id = ? AND match_index = ?
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                    """,
                    (str(target_id), int(match_index)),
                ).fetchone()
            else:
                row = connection.execute(
                    f"""
                    SELECT raw_response, match_index, map_name, game_mode, ok, created_at
                    FROM {COURT_LLM_TABLE}
                    WHERE target_id = ?
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                    """,
                    (str(target_id),),
                ).fetchone()
            if row is None:
                return None
            return {
                "raw_response": str(row[0] or ""),
                "match_index": row[1],
                "map_name": str(row[2] or ""),
                "game_mode": str(row[3] or ""),
                "ok": bool(row[4]),
                "created_at": row[5],
            }
        except Exception:
            return None
        finally:
            connection.close()


class CourtLLMRecorder:
    """异步有界队列写入器：将 court 判决书落库到 shiqu_llm.sqlite3。"""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db = CourtLLMDB(db_path)
        self._queue: asyncio.Queue[Optional[_CourtLLMEvent]] = asyncio.Queue(
            maxsize=RECORDER_QUEUE_MAXSIZE
        )
        self._worker_task: Optional[asyncio.Task[None]] = None
        self._started = False
        self._closed = False
        self._dropped = 0

    async def start(self) -> None:
        if self._started or not is_database_write_enabled():
            return
        await asyncio.to_thread(self.db.initialize_schema)
        self._worker_task = asyncio.create_task(self._worker(), name="court-llm-recorder-worker")
        self._started = True

    async def enqueue(
        self,
        *,
        target_id: str,
        raw_response: str = "",
        match_index: int = 0,
        map_name: str = "",
        game_mode: str = "",
        prompt: str = "",
        ok: bool = False,
        duration_ms: int = 0,
        call_count: int = 0,
    ) -> None:
        if not is_database_write_enabled():
            return
        if self._closed:
            return
        if not self._started:
            await self.start()
        try:
            self._queue.put_nowait(
                _CourtLLMEvent(
                    target_id=str(target_id or ""),
                    raw_response=str(raw_response or ""),
                    match_index=int(match_index or 0),
                    map_name=str(map_name or ""),
                    game_mode=str(game_mode or ""),
                    prompt=str(prompt or ""),
                    ok=bool(ok),
                    duration_ms=int(duration_ms or 0),
                    call_count=int(call_count or 0),
                )
            )
        except asyncio.QueueFull:
            self._dropped += 1
            if self._dropped == 1 or self._dropped % 10000 == 0:
                print(
                    f"[overstats] court-llm queue full, dropped {self._dropped} events "
                    f"(maxsize={RECORDER_QUEUE_MAXSIZE})"
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

    def _write_batch(self, batch: Sequence[_CourtLLMEvent]) -> None:
        for event in batch or []:
            self.db.insert_result(event)


# 模块级单例：service.py 直接 await court_llm_recorder.enqueue(...)
court_llm_recorder = CourtLLMRecorder()


__all__ = [
    "COURT_LLM_TABLE",
    "CourtLLMDB",
    "CourtLLMRecorder",
    "court_llm_recorder",
]
