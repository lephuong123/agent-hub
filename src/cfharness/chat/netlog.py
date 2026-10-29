"""Mức log cho lỗi mạng tạm thời của vòng nhận tin chat (Telegram `getUpdates`, Slack Socket Mode).

Lỗi mạng chập chờn (ReadTimeout, ConnectError…) tự thử lại → log DEBUG; chỉ WARNING MỘT lần khi lỗi kéo dài (≥ `warn_after`
lần liên tiếp hoặc ≥ `warn_after_s` giây không nhận được thành công), rồi INFO một lần khi kết nối lại. Lỗi không phải mạng
(token sai, 401/403, lỗi API) vẫn WARNING mỗi lần như cũ.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable

TRANSIENT_NAMES = re.compile(
    r"\b(\w*Timeout\w*|ConnectError|ConnectionError|ReadError|WriteError|NetworkError|RemoteProtocolError|ProtocolError|"
    r"ConnectionClosed\w*|ConnectionReset\w*|ConnectionRefused\w*|ServerDisconnected\w*|gaierror|OSError|TimeoutError)\b")
TRANSIENT_CODES = {429, 500, 502, 503, 504, "ratelimited"}


def is_transient(e: BaseException) -> bool:
    """Lỗi mạng/máy chủ tạm thời (tự hết khi thử lại). API bọc lỗi mạng thành `<method>: <TênLớp>: …` → nhận theo tên lớp."""
    if isinstance(e, (TimeoutError, ConnectionError)):
        return True
    if getattr(e, "code", None) in TRANSIENT_CODES:
        return True
    if getattr(e, "code", None) is not None:
        return False
    return bool(TRANSIENT_NAMES.search(f"{type(e).__name__}: {e}"))


class TransientLog:
    def __init__(self, log: logging.Logger, label: str, *, warn_after: int = 5, warn_after_s: float = 300.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.log, self.label = log, label
        self.warn_after, self.warn_after_s = warn_after, warn_after_s
        self.clock = clock
        self.fails = 0
        self.since: float | None = None  # lúc lỗi đầu của chuỗi hiện tại
        self.warned = False

    def ok(self) -> None:
        if self.warned:
            self.log.info("%s: đã kết nối lại sau %d lỗi mạng (%.0f s)", self.label, self.fails, self.clock() - (self.since or self.clock()))
        self.fails, self.since, self.warned = 0, None, False

    def error(self, e: BaseException, text: Any) -> None:
        if not is_transient(e):
            self.log.warning("%s: %s", self.label, text)
            return
        now = self.clock()
        self.fails += 1
        if self.since is None:
            self.since = now
        if not self.warned and (self.fails >= self.warn_after or now - self.since >= self.warn_after_s):
            self.warned = True
            self.log.warning("%s: lỗi mạng kéo dài (%d lần liên tiếp, %.0f s), vẫn tự thử lại: %s", self.label, self.fails, now - self.since, text)
        else:
            self.log.debug("%s: lỗi mạng tạm thời (lần %d), thử lại: %s", self.label, self.fails, text)
