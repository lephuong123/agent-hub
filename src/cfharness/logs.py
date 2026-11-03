"""Log có cấu trúc JSON lines ở `HUB_HOME/logs/hub.log`, xoay vòng theo dung lượng + dọn bản cũ theo tuổi (FR-OB-05, NFR-21).

Xoay vòng an toàn trên Windows (`SafeRotatingFileHandler`): file đang mở ở tiến trình khác (`daemon logs`, trình xem log,
antivirus) KHÔNG đổi tên được → chép sang `.1` rồi cắt file gốc (copytruncate) thay vì `handleError` và để file lớn mãi.
`logs.*` trong config: `max_bytes`, `backup_count`, `max_age_days` (0/null = không dọn theo tuổi).
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import shutil
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from .events import iso

# hardening H-20 (NFR-21): ngữ cảnh run hiện tại — RunManager đặt trong task asyncio của run, nên MỌI log phát ra trong task
# đó (runtime, adapter, workspace…) tự có run_id/agent_id kể cả khi gọi `logging.getLogger(...)` trực tiếp.
_RUN_CTX: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar("cfharness_run_ctx", default=None)


def set_run_context(run_id: str | None, agent_id: str | None) -> contextvars.Token:
    return _RUN_CTX.set({"run_id": run_id or "", "agent_id": agent_id or ""})


class RunLogger(logging.LoggerAdapter):
    """`LoggerAdapter` gắn `run_id`/`agent_id` (+ `task_id`) vào mọi bản ghi — dùng `run_logger(log, run_id, agent_id)`."""

    def process(self, msg: Any, kwargs: Any) -> tuple[Any, Any]:
        extra = dict(kwargs.get("extra") or {})
        for k, v in (self.extra or {}).items():
            extra.setdefault(k, v)
        kwargs["extra"] = extra
        return msg, kwargs


def run_logger(logger: logging.Logger, run_id: str | None, agent_id: str | None = None, **extra: Any) -> RunLogger:
    return RunLogger(logger, {"run_id": run_id, "agent_id": agent_id, **extra})


class RunContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        ctx = _RUN_CTX.get()
        if ctx:
            for k, v in ctx.items():
                if v and not getattr(record, k, None):
                    setattr(record, k, v)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        d = {
            "ts": iso(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for k in ("run_id", "agent_id", "task_id", "team_id"):
            v = getattr(record, k, None)
            if v:
                d[k] = v
        if record.exc_info:
            d["exc"] = self.formatException(record.exc_info)
        return json.dumps(d, ensure_ascii=False)


DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 5
DEFAULT_MAX_AGE_DAYS = 30
_RETRY_AFTER_S = 60.0  # xoay vòng thất bại hẳn → không thử lại ở MỖI dòng log


def _move(src: str, dst: str) -> bool:
    """Đổi tên `src` → `dst` (ghi đè). Windows: một trong hai file đang mở ở tiến trình khác → chép + cắt file nguồn.
    Trả False nếu không làm được (giữ nguyên, không ném lỗi)."""
    if not os.path.exists(src):
        return True
    try:
        os.replace(src, dst)
        return True
    except OSError:
        pass
    try:
        shutil.copyfile(src, dst)
        with open(src, "r+b") as f:
            f.truncate(0)
        return True
    except OSError:
        return False


def rotate_file(path: Path, backup_count: int = DEFAULT_BACKUP_COUNT) -> bool:
    """Đẩy `path` → `path.1` → … → `path.<backup_count>` (bản cuối bị bỏ). Dùng cho `daemon.out` lúc daemon khởi động
    (stdout/stderr của daemon trỏ thẳng vào file nên không xoay được khi đang chạy)."""
    if backup_count <= 0:
        try:
            with open(path, "r+b") as f:
                f.truncate(0)
            return True
        except OSError:
            return False
    base = str(path)
    for i in range(backup_count - 1, 0, -1):
        _move(f"{base}.{i}", f"{base}.{i + 1}")
    return _move(base, f"{base}.1")


def rotate_if_large(path: Path, max_bytes: int = DEFAULT_MAX_BYTES, backup_count: int = DEFAULT_BACKUP_COUNT) -> bool:
    try:
        if not max_bytes or path.stat().st_size < max_bytes:
            return False
    except OSError:
        return False
    return rotate_file(path, backup_count)


def purge_old_logs(logs_dir: Path, max_age_days: float | None, now: float | None = None) -> list[str]:
    """Xoá bản xoay vòng (`*.log.N`, `*.out.N`) cũ hơn `max_age_days` theo mtime. File đang ghi không bao giờ bị xoá."""
    if not max_age_days:
        return []
    cutoff = (now if now is not None else time.time()) - float(max_age_days) * 86400
    out: list[str] = []
    for f in logs_dir.glob("*.*"):
        if not f.suffix[1:].isdigit() or not f.stem.endswith((".log", ".out")):
            continue
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                out.append(f.name)
        except OSError:  # Windows: đang mở ở nơi khác → để lần sau
            continue
    return out


class SafeRotatingFileHandler(RotatingFileHandler):
    """`RotatingFileHandler` không hỏng trên Windows: xoay bằng `_move` (rename, lỗi thì copytruncate), xoay thất bại hẳn
    thì nghỉ `_RETRY_AFTER_S` rồi mới thử lại; sau mỗi lần xoay dọn bản cũ quá `max_age_days`."""

    def __init__(self, filename: str | Path, maxBytes: int = DEFAULT_MAX_BYTES, backupCount: int = DEFAULT_BACKUP_COUNT,
                 max_age_days: float | None = DEFAULT_MAX_AGE_DAYS, encoding: str = "utf-8") -> None:
        super().__init__(filename, maxBytes=maxBytes, backupCount=backupCount, encoding=encoding)
        self.max_age_days = max_age_days
        self._retry_at = 0.0
        purge_old_logs(Path(self.baseFilename).parent, max_age_days)

    def shouldRollover(self, record: logging.LogRecord) -> int:
        if self._retry_at and time.monotonic() < self._retry_at:
            return False
        return super().shouldRollover(record)

    def doRollover(self) -> None:
        if self.stream:
            self.stream.close()
            self.stream = None  # type: ignore[assignment]
        ok = rotate_file(Path(self.baseFilename), self.backupCount)
        self._retry_at = 0.0 if ok else time.monotonic() + _RETRY_AFTER_S
        if not self.delay:
            self.stream = self._open()
        purge_old_logs(Path(self.baseFilename).parent, self.max_age_days)


def setup_logging(logs_dir: Path, level: int = logging.INFO, *, max_bytes: int = DEFAULT_MAX_BYTES,
                  backup_count: int = DEFAULT_BACKUP_COUNT, max_age_days: float | None = DEFAULT_MAX_AGE_DAYS) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("cfharness")
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    h = SafeRotatingFileHandler(logs_dir / "hub.log", maxBytes=int(max_bytes or 0), backupCount=int(backup_count or 0),
                                max_age_days=max_age_days)
    h.setFormatter(JsonFormatter())
    h.addFilter(RunContextFilter())
    root.addHandler(h)
