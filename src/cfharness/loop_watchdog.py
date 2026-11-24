"""Watchdog event loop của daemon: thread riêng, phát hiện loop đứng LÚC ĐANG đứng và dump stack mọi thread vào logs/faults.log.

`Daemon._loop_lag` chỉ đo được độ trễ SAU KHI loop chạy lại, và không biết dòng code nào chặn. Ca thật (23/11, v0.2.27): daemon
đứng ~14,5 phút ngay sau khi spawn một run — không RPC nào được xử lý, tin chat của người dùng hết giờ chờ — nhưng không để
lại dấu vết gì (Windows không có SIGUSR1 để `daemon dump-stacks`). Watchdog này để lần sau có stack.

Cách làm: coroutine trên loop gọi `beat()` mỗi giây; thread kiểm mỗi `check_s`: nhịp cũ hơn `stall_s` → `faulthandler.dump_traceback`
(ghi thẳng vào fd, không qua logging — logging có thể chính là thứ đang chặn) + một dòng WARNING. Trong một lần đứng dump tối đa
`max_dumps` lần, cách nhau `stall_s * 4`; nhịp trở lại → ghi thời gian đứng và lên dây lại."""

from __future__ import annotations

import faulthandler
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

log = logging.getLogger("cfharness.daemon.watchdog")

DEFAULT_STALL_S = 15.0


class LoopWatchdog:
    def __init__(self, fault_file: Callable[[], Any], *, stall_s: Callable[[], float], check_s: float = 1.0,
                 max_dumps: int = 3, clock: Callable[[], float] = time.monotonic) -> None:
        self._fault_file = fault_file  # hàm trả file faults.log hiện tại (None → chỉ ghi log)
        self._stall_s = stall_s  # đọc lại mỗi lần kiểm (đổi cấu hình không cần khởi động lại)
        self._check_s = check_s
        self._max_dumps = max_dumps
        self._clock = clock
        self._last_beat = clock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.stall_started: float | None = None  # mốc nhịp cuối trước khi đứng (None = đang bình thường)
        self.dumps = 0
        self._next_dump = 0.0

    def beat(self) -> None:
        """Gọi TRÊN event loop. Loop vừa đứng xong → ghi một dòng thời gian đứng."""
        now = self._clock()
        if self.stall_started is not None:
            log.warning("loop.stall_end event loop daemon đứng %.1f s (đã dump %d stack vào faults.log)",
                        now - self.stall_started, self.dumps, extra={"event": "loop.stall_end",
                                                                    "duration_ms": round((now - self.stall_started) * 1000.0, 1)})
            self.stall_started = None
            self.dumps = 0
        self._last_beat = now

    def check(self) -> bool:
        """Một lần kiểm (thread gọi định kỳ; test gọi thẳng). True = vừa dump."""
        try:
            stall = float(self._stall_s() or 0)
        except (TypeError, ValueError):
            stall = DEFAULT_STALL_S
        if stall <= 0:
            return False
        now = self._clock()
        idle = now - self._last_beat
        if idle < stall:
            return False
        if self.stall_started is None:
            self.stall_started = self._last_beat
            self.dumps = 0
            self._next_dump = now
        if self.dumps >= self._max_dumps or now < self._next_dump:
            return False
        self.dumps += 1
        self._next_dump = now + stall * 4
        self._dump(idle)
        return True

    def _dump(self, idle: float) -> None:
        f = self._fault_file()
        if f is not None:
            try:
                stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
                f.write(f"\n=== {stamp} event loop daemon đứng {idle:.1f} s — stack mọi thread (lần {self.dumps}/{self._max_dumps}) ===\n")
                f.flush()
                faulthandler.dump_traceback(file=f, all_threads=True)
                f.flush()
            except (OSError, ValueError):  # best-effort
                pass
        # logging có thể đang bị chặn cùng loop (handler giữ lock) — dump đã ghi trước, dòng này chỉ là phụ
        log.warning("loop.stall event loop daemon không phản hồi %.1f s — đã dump stack vào logs/faults.log", idle,
                    extra={"event": "loop.stall", "duration_ms": round(idle * 1000.0, 1)})

    def _run(self) -> None:
        while not self._stop.wait(self._check_s):
            try:
                self.check()
            except Exception:  # pragma: no cover — watchdog không được chết
                pass

    def start(self) -> None:
        if self._thread is None:
            self._last_beat = self._clock()
            self._thread = threading.Thread(target=self._run, name="cfharness-loop-watchdog", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t, self._thread = self._thread, None
        if t is not None:
            t.join(timeout=2)
