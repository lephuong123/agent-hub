"""Tác vụ asyncio nền "bắn rồi quên" có log lỗi (L1, docs/dev-notes/plan-logging.md B4).

`track(task, keep)`: giữ tham chiếu trong `keep` (tránh bị GC giữa chừng) và khi task xong thì bỏ khỏi `keep`; task kết thúc bằng
exception (không phải huỷ) → log ERROR kèm tên task + traceback. Trước đây lỗi của các task này chỉ lộ thành thông báo
"Task exception was never retrieved" trên stderr (hoặc mất hẳn).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Coroutine

log = logging.getLogger("cfharness.tasks")
_BG: set[asyncio.Task[Any]] = set()


def _done(keep: set[asyncio.Task[Any]]) -> Any:
    def cb(t: asyncio.Task[Any]) -> None:
        keep.discard(t)
        if t.cancelled():
            return
        exc = t.exception()  # đánh dấu đã lấy → asyncio không in "never retrieved" lần nữa
        if exc is not None:
            from .logs import ERROR_COUNTS

            ERROR_COUNTS["task"] = ERROR_COUNTS.get("task", 0) + 1
            log.error("tác vụ nền %s lỗi: %s", t.get_name(), type(exc).__name__, exc_info=(type(exc), exc, exc.__traceback__))

    return cb


def track(task: asyncio.Task[Any], keep: set[asyncio.Task[Any]] | None = None) -> asyncio.Task[Any]:
    s = _BG if keep is None else keep
    s.add(task)
    task.add_done_callback(_done(s))
    return task


def spawn(coro: Coroutine[Any, Any, Any], *, name: str | None = None, keep: set[asyncio.Task[Any]] | None = None) -> asyncio.Task[Any]:
    """`create_task` + `track`. Cần event loop đang chạy (RuntimeError như create_task)."""
    return track(asyncio.get_running_loop().create_task(coro, name=name), keep)


__all__ = ["spawn", "track"]
