"""是区吗（shiqu）与电竞法庭（court）独立 LLM 调用的状态跟踪与定时打印。

- ``LLMCallStatus``：线程/协程安全的调用状态计数器（进行中数量、累计、失败、最近目标等）。
- 模块级单例 ``shiqu_llm_status`` / ``court_llm_status``：由各自 service.py 的 analyze 调用。
- ``start_llm_status_reporter``：协程，在 AsyncRunner 事件循环上每 10 秒打印一次两个模块的状态。
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class LLMCallStatus:
    """单个模块的 LLM 调用状态；所有字段通过 ``lock`` 保护，可在主流程与后台线程/协程并发访问。"""

    name: str
    lock: threading.Lock = field(default_factory=threading.Lock)
    in_progress: int = 0
    total_calls: int = 0
    failed_calls: int = 0
    last_target: str = ""
    last_started_at: float = 0.0
    last_finished_at: float = 0.0
    last_error: str = ""

    def mark_call_start(self, target_id: str) -> None:
        with self.lock:
            self.in_progress += 1
            self.total_calls += 1
            self.last_target = str(target_id or "")
            self.last_started_at = time.time()

    def mark_call_done(self, success: bool, error: str = "") -> None:
        with self.lock:
            self.in_progress = max(0, self.in_progress - 1)
            self.last_finished_at = time.time()
            if not success:
                self.failed_calls += 1
            if error:
                self.last_error = str(error)
            elif success:
                self.last_error = ""

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "name": self.name,
                "in_progress": self.in_progress,
                "total_calls": self.total_calls,
                "failed_calls": self.failed_calls,
                "last_target": self.last_target,
                "last_started_at": self.last_started_at,
                "last_finished_at": self.last_finished_at,
                "last_error": self.last_error,
            }


# 模块级单例：service.py 直接引用，reporter 直接读取。name 用英文，便于状态日志输出。
shiqu_llm_status = LLMCallStatus("shiqu")
court_llm_status = LLMCallStatus("court")


def _queue_size(recorder: Optional[Any]) -> int:
    if recorder is None:
        return 0
    try:
        return int(recorder._queue.qsize())
    except Exception:
        return -1


def _print_one(status: LLMCallStatus, queue_size: int) -> None:
    s = status.snapshot()
    # 仅在 LLM 调用进行中才打印，空闲时不输出，避免无意义的周期刷屏。
    if s["in_progress"] <= 0:
        return
    now = time.time()
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    elapsed = int(now - s["last_started_at"]) if s["last_started_at"] else 0
    target = s["last_target"] or "-"
    print(
        f"[{ts}] [{status.name}][{elapsed}s][calling][{target}] "
        f"in_progress={s['in_progress']} | total={s['total_calls']} | "
        f"failed={s['failed_calls']} | queue={queue_size}",
        flush=True,
    )


async def start_llm_status_reporter(
    shiqu_status: LLMCallStatus,
    court_status: LLMCallStatus,
    shiqu_recorder: Optional[Any] = None,
    court_recorder: Optional[Any] = None,
    interval: float = 20.0,
) -> None:
    """每隔 ``interval`` 秒检查一次 shiqu / court 的 LLM 调用状态；仅在某模块 LLM 调用进行中时才打印（在 AsyncRunner 事件循环上运行）。"""
    while True:
        await asyncio.sleep(interval)
        try:
            _print_one(shiqu_status, _queue_size(shiqu_recorder))
            _print_one(court_status, _queue_size(court_recorder))
        except Exception as exc:  # 状态打印绝不应当影响主流程
            print(f"[overstats] LLM 状态打印异常: {type(exc).__name__}: {exc}", flush=True)


__all__ = [
    "LLMCallStatus",
    "shiqu_llm_status",
    "court_llm_status",
    "start_llm_status_reporter",
]
