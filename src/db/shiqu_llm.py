"""是区吗（shiqu）LLM 调用的记录数据库与异步写入器。

仿照本项目 recorder 的写法（见 request_metrics.py / match_detail_recorder.py）：
- 独立的 sqlite 文件 ``shiqu_llm.sqlite3``，不污染 match_stats 库；
- ``ShiquLLMDB`` 负责 schema 初始化与落库（线程内同步执行）；
- ``ShiquLLMRecorder`` 为异步有界队列 worker，主流程仅 ``await enqueue`` 入队即返回，
  实际写盘在后台线程，best-effort，队列满则丢弃并计数；
- 全部写入受 ``is_database_write_enabled`` 开关控制。

存储内容：把「一次 LLM 调用」作为一条遥测记录落库。我们只保留渲染/排查必需的信息：
- ``shiqu_llm_result``：每次分析一条记录
  (target_id / ok / prompt / raw_response / duration_ms / call_count / created_at)。
  ``raw_response`` 是 LLM 原始返回（JSON 文本），渲染时由它解析 score/verdict/summary 等，
  因此不再单独冗余存储这些字段；``customer_token`` 也不需要（target_id 已能标识玩家）。

历史上曾拆分 ``shiqu_llm_match_comment`` / ``shiqu_llm_teammate_comment`` 两张子表，
纯属冗余（同样可由 raw_response 解析），当前代码已不再写入。schema 初始化仅
``CREATE TABLE IF NOT EXISTS`` 主表，文件/表不存在时自动新建。
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
except ModuleNotFoundError:  # pragma: no cover
    from config import is_database_write_enabled


SHIQU_LLM_DB_PATH = Path(__file__).resolve().parent / "shiqu_llm.sqlite3"
SHIQU_LLM_RESULT_TABLE = "shiqu_llm_result"

# 有界队列，避免 SQLite 写盘卡顿（磁盘 IO / 锁竞争 / 满盘）时内存无限增长；
# best-effort 遥测在队列满时直接丢弃。
RECORDER_QUEUE_MAXSIZE = 512


@dataclass(frozen=True)
class _ShiquLLMEvent:
    target_id: str
    prompt: str = ""
    raw_response: str = ""
    ok: bool = False
    duration_ms: int = 0
    call_count: int = 0


def _build_row(event: _ShiquLLMEvent) -> Tuple:
    """将一次事件转换为主记录行。"""
    return (
        str(event.target_id or ""),
        1 if event.ok else 0,
        str(event.prompt or ""),
        str(event.raw_response or ""),
        int(event.duration_ms or 0),
        int(event.call_count or 0),
        int(time.time()),
    )


class ShiquLLMDB:
    """是区吗 LLM 调用的 sqlite 适配器（惰性、容错）。

    与 ``IDPoolDB`` 同风格：数据库文件缺失 / schema 缺失时降级为空结果，不抛异常。
    """

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
            self._warn_once(f"shiqu llm sqlite db not found: {self.db_path}")
            return None
        try:
            connection = sqlite3.connect(str(self.db_path))
            return connection
        except Exception as exc:
            self._warn_once(f"shiqu llm sqlite connection failed: {type(exc).__name__}: {exc}")
            return None

    def _get_write_connection(self) -> Optional[sqlite3.Connection]:
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(str(self.db_path), timeout=30)
            return connection
        except Exception as exc:
            self._warn_once(f"shiqu llm sqlite write connection failed: {type(exc).__name__}: {exc}")
            return None

    def initialize_schema(self) -> None:
        connection = self._get_write_connection()
        if connection is None:
            return
        try:
            connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {SHIQU_LLM_RESULT_TABLE} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target_id TEXT NOT NULL,
                    ok INTEGER NOT NULL DEFAULT 0,
                    prompt TEXT NOT NULL DEFAULT '',
                    raw_response TEXT NOT NULL DEFAULT '',
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    call_count INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_shiqu_result_target
                ON {SHIQU_LLM_RESULT_TABLE}(target_id, created_at)
                """
            )
            connection.commit()
        finally:
            connection.close()

    def insert_result(self, event: _ShiquLLMEvent) -> Optional[int]:
        """写入一条 LLM 调用记录，返回主记录 id。"""
        connection = self._get_write_connection()
        if connection is None:
            return None
        try:
            cursor = connection.cursor()
            cursor.execute(
                f"""
                INSERT INTO {SHIQU_LLM_RESULT_TABLE}
                (target_id, ok, prompt, raw_response, duration_ms, call_count, created_at)
                VALUES (?,?,?,?,?,?,?)
                """,
                _build_row(event),
            )
            result_id = cursor.lastrowid
            connection.commit()
            return result_id
        except Exception as exc:
            self._warn_once(f"shiqu llm insert failed: {type(exc).__name__}: {exc}")
            try:
                connection.rollback()
            except Exception:
                pass
            return None
        finally:
            connection.close()

    def get_recent(self, limit: int = 50) -> List[Dict[str, Any]]:
        """读取最近的分析记录（仅主表，含诊断字段）。"""
        connection = self._get_connection()
        if connection is None:
            return []
        try:
            rows = connection.execute(
                f"""
                SELECT id, target_id, ok, duration_ms, call_count, created_at
                FROM {SHIQU_LLM_RESULT_TABLE}
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
            return [
                {
                    "id": r[0],
                    "target_id": r[1],
                    "ok": bool(r[2]),
                    "duration_ms": r[3],
                    "call_count": r[4],
                    "created_at": r[5],
                }
                for r in rows
            ]
        except Exception:
            return []
        finally:
            connection.close()

    def get_recent_by_target(self, target_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        """按 target_id 读取最近的分析记录。"""
        connection = self._get_connection()
        if connection is None or not str(target_id or "").strip():
            return []
        try:
            rows = connection.execute(
                f"""
                SELECT id, target_id, ok, duration_ms, call_count, created_at
                FROM {SHIQU_LLM_RESULT_TABLE}
                WHERE target_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (str(target_id), int(limit)),
            ).fetchall()
            return [
                {
                    "id": r[0],
                    "target_id": r[1],
                    "ok": bool(r[2]),
                    "duration_ms": r[3],
                    "call_count": r[4],
                    "created_at": r[5],
                }
                for r in rows
            ]
        except Exception:
            return []
        finally:
            connection.close()

    def get_latest_by_target(self, target_id: str) -> Optional[Dict[str, Any]]:
        """读取该 target_id 最近一条判定记录的原始返回（供 use_db 模式直接渲染）。

        返回 ``{"raw_response", "ok", "created_at"}``；无记录或库不可用时返回 ``None``。
        """
        connection = self._get_connection()
        if connection is None or not str(target_id or "").strip():
            return None
        try:
            row = connection.execute(
                f"""
                SELECT raw_response, ok, created_at
                FROM {SHIQU_LLM_RESULT_TABLE}
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
                "ok": bool(row[1]),
                "created_at": row[2],
            }
        except Exception:
            return None
        finally:
            connection.close()


class ShiquLLMRecorder:
    """异步有界队列写入器：将 LLM 调用遥测落库到 shiqu_llm.sqlite3。

    与主流程解耦，主流程仅 ``await enqueue(...)`` 入队即返回，写盘在后台线程完成。
    """

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db = ShiquLLMDB(db_path)
        self._queue: asyncio.Queue[Optional[_ShiquLLMEvent]] = asyncio.Queue(
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
        self._worker_task = asyncio.create_task(self._worker(), name="shiqu-llm-recorder-worker")
        self._started = True

    async def enqueue(
        self,
        *,
        target_id: str,
        prompt: str = "",
        raw_response: str = "",
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
                _ShiquLLMEvent(
                    target_id=str(target_id or ""),
                    prompt=str(prompt or ""),
                    raw_response=str(raw_response or ""),
                    ok=bool(ok),
                    duration_ms=int(duration_ms or 0),
                    call_count=int(call_count or 0),
                )
            )
        except asyncio.QueueFull:
            self._dropped += 1
            if self._dropped == 1 or self._dropped % 10000 == 0:
                print(
                    f"[overstats] shiqu-llm queue full, dropped {self._dropped} events "
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

    def _write_batch(self, batch: Sequence[_ShiquLLMEvent]) -> None:
        for event in batch or []:
            self.db.insert_result(event)


# 模块级单例：service.py 直接 await shiqu_llm_recorder.enqueue(...)
shiqu_llm_recorder = ShiquLLMRecorder()


__all__ = [
    "SHIQU_LLM_DB_PATH",
    "SHIQU_LLM_RESULT_TABLE",
    "ShiquLLMDB",
    "ShiquLLMRecorder",
    "shiqu_llm_recorder",
]
