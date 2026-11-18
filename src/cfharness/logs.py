"""Log có cấu trúc JSON lines ở `HUB_HOME/logs/hub.log`, xoay vòng theo dung lượng + dọn bản cũ theo tuổi (FR-OB-05, NFR-21).

Xoay vòng an toàn trên Windows (`SafeRotatingFileHandler`): file đang mở ở tiến trình khác (`daemon logs`, trình xem log,
antivirus) KHÔNG đổi tên được → chép sang `.1` rồi cắt file gốc (copytruncate) thay vì `handleError` và để file lớn mãi.
`logs.*` trong config: `max_bytes`, `backup_count`, `max_age_days` (0/null = không dọn theo tuổi), `level` + `levels`
(đổi lúc chạy — `LevelController`, docs/dev-notes/plan-logging.md L1).

Mỗi tiến trình một file JSONL: daemon `hub.log`, web `web.jsonl`, MCP stdio `mcp.jsonl` (`setup_logging(proc=…)`). Bản ghi thư
viện ngoài (asyncio, mcp, uvicorn…) cũng vào cùng file (handler ở root, chỉ khi logger đó phát tới mức của handler). Log là
METADATA: không bao giờ ghi prompt, nội dung trả lời, input/output tool hay giá trị secret — kể cả ở DEBUG.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
from datetime import datetime, timezone
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


_PROC = "daemon"  # tên tiến trình ghi log (daemon|web|mcp|cli|helper) — đặt trong setup_logging
CONTEXT_FIELDS = ("run_id", "agent_id", "task_id", "team_id", "req_id", "parent_req_id", "channel")
# L2: trường METADATA của access log RPC (`cfharness.rpc`, daemon._access_log) — chỉ id/tên/số, không bao giờ params/kết quả
EXTRA_FIELDS = ("event", "method", "via", "duration_ms", "outcome", "code", "reason")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        d = {
            "ts": iso(datetime.fromtimestamp(record.created, timezone.utc)),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
            "proc": _PROC,
            "pid": record.process,
            "src": f"{record.module}:{record.lineno}",
        }
        for k in CONTEXT_FIELDS:
            v = getattr(record, k, None)
            if v:
                d[k] = v
        for k in EXTRA_FIELDS:
            v = getattr(record, k, None)
            if v is not None and isinstance(v, (str, int, float)):
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
    """Xoá bản xoay vòng (`*.log.N`, `*.out.N`, `*.jsonl.N`) cũ hơn `max_age_days` theo mtime. File đang ghi không bao giờ bị xoá."""
    if not max_age_days:
        return []
    cutoff = (now if now is not None else time.time()) - float(max_age_days) * 86400
    out: list[str] = []
    for f in logs_dir.glob("*.*"):
        if not f.suffix[1:].isdigit() or not f.stem.endswith((".log", ".out", ".jsonl")):
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


LIB_LOGGERS = ("asyncio", "mcp", "uvicorn", "starlette", "websockets")  # thư viện được phép đổi level (không httpx/urllib3: URL có token)
LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR, "critical": logging.CRITICAL}
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z0-9_]+){0,8}$")
_FILE_MARK = "_cfharness_jsonl"


class _NotCfharness(logging.Filter):
    """Handler ở root: bản ghi `cfharness.*` đã có handler riêng (propagate lên root) → không ghi lần hai."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not (record.name == "cfharness" or record.name.startswith("cfharness."))


def _file_handler(path: Path, proc: str, *, max_bytes: int, backup_count: int, max_age_days: float | None,
                  shared: bool) -> logging.Handler:
    """`shared=True` (nhiều tiến trình cùng ghi, vd MCP stdio): FileHandler append + xoay lúc mở — không xoay giữa chừng (đua nhau
    đổi tên). Ngược lại `SafeRotatingFileHandler`."""
    if shared:
        rotate_if_large(path, max_bytes, backup_count)
        purge_old_logs(path.parent, max_age_days)
        h: logging.Handler = logging.FileHandler(path, encoding="utf-8")
    else:
        h = SafeRotatingFileHandler(path, maxBytes=int(max_bytes or 0), backupCount=int(backup_count or 0), max_age_days=max_age_days)
    try:
        os.chmod(path, 0o600)
    except OSError:  # best-effort: Windows / file của người khác
        pass
    return h


def setup_logging(logs_dir: Path, level: int = logging.INFO, *, max_bytes: int = DEFAULT_MAX_BYTES,
                  backup_count: int = DEFAULT_BACKUP_COUNT, max_age_days: float | None = DEFAULT_MAX_AGE_DAYS,
                  proc: str = "daemon", filename: str = "hub.log", shared: bool = False) -> None:
    """Cấu hình log JSONL cho tiến trình `proc`: handler cho `cfharness` (mọi level ≥ level của logger) + handler ở root cho thư
    viện ngoài (không lặp bản ghi `cfharness`). Cả hai che secret (`SecretLogFilter`). Gọi lại được (thay handler cũ)."""
    global _PROC
    _PROC = proc
    logs_dir.mkdir(parents=True, exist_ok=True)
    from .redact import SecretLogFilter
    from .trace import TraceFilter

    path = logs_dir / filename
    lg = logging.getLogger("cfharness")
    lg.setLevel(level)
    for h in list(lg.handlers):
        lg.removeHandler(h)
        h.close()
    h = _file_handler(path, proc, max_bytes=max_bytes, backup_count=backup_count, max_age_days=max_age_days, shared=shared)
    h.setFormatter(JsonFormatter())
    h.addFilter(RunContextFilter())
    h.addFilter(TraceFilter())  # L2: req_id/channel của lượt xử lý hiện tại (trace.py)
    h.addFilter(SecretLogFilter())
    lg.addHandler(h)
    root = logging.getLogger()
    for old in [x for x in root.handlers if getattr(x, _FILE_MARK, False)]:
        root.removeHandler(old)
        old.close()
    rh: logging.Handler = _SharedStream(h)
    rh.addFilter(_NotCfharness())
    setattr(rh, _FILE_MARK, True)
    root.addHandler(rh)
    setattr(lg, _FILE_MARK, True)
    from .log_query import ensure_error_counter

    ensure_error_counter()  # L6 (B5): đếm WARNING/ERROR theo logger trong RAM cho hub.status / thẻ Sức khoẻ


class _SharedStream(logging.Handler):
    """Handler ở root ghi QUA handler file của `cfharness` (một file, một luồng xoay vòng) — tránh hai handler xoay cùng file."""

    def __init__(self, target: logging.Handler) -> None:
        super().__init__()
        self.target = target

    def emit(self, record: logging.LogRecord) -> None:
        self.target.handle(record)  # filter (ngữ cảnh run, che secret) + formatter + xoay vòng của handler đích


def file_logging_active() -> bool:
    """True khi tiến trình đã `setup_logging` (log thư viện vào file JSONL) — stderr khỏi ghi lặp."""
    return bool(getattr(logging.getLogger("cfharness"), _FILE_MARK, False))


def setup_process_logging(logs_dir: Path, proc: str, filename: str, settings: dict[str, Any] | None = None, *,
                          shared: bool = False) -> bool:
    """Tiến trình phụ (web, MCP stdio): như daemon nhưng lỗi IO (sandbox chặn ghi, đĩa đầy…) chỉ bỏ qua — trả False."""
    s = settings or {}
    try:
        setup_logging(logs_dir, LEVELS.get(str(s.get("level") or "info").lower(), logging.INFO),
                      max_bytes=int(s.get("max_bytes", DEFAULT_MAX_BYTES) or 0), backup_count=int(s.get("backup_count", DEFAULT_BACKUP_COUNT) or 0),
                      max_age_days=s.get("max_age_days", DEFAULT_MAX_AGE_DAYS), proc=proc, filename=filename, shared=shared)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------- level đổi lúc chạy (L1)
def valid_logger_name(name: str) -> bool:
    """Chỉ `cfharness[.x]` và thư viện trong `LIB_LOGGERS[.x]` — không root, không httpx/urllib3 (DEBUG của chúng in URL có token)."""
    if not isinstance(name, str) or not _NAME_RE.match(name):
        return False
    head = name.split(".", 1)[0]
    return head == "cfharness" or head in LIB_LOGGERS


def parse_level(v: Any) -> int | None:
    return LEVELS.get(str(v or "").strip().lower())


class LevelController:
    """Áp `logs.level` (logger `cfharness`) + `logs.levels` {logger: level} + level tạm (hub_state `logs.temp_levels`
    {logger: {level, until}}, ưu tiên cao nhất). Logger thôi được cấu hình → về NOTSET (thừa kế). Thuần với `now`."""

    def __init__(self) -> None:
        self.managed: set[str] = set()

    @staticmethod
    def effective(cfg: dict[str, Any] | None, temp: dict[str, Any] | None, now: float) -> tuple[dict[str, int], list[str]]:
        cfg = cfg if isinstance(cfg, dict) else {}
        out: dict[str, int] = {"cfharness": parse_level(cfg.get("level")) or logging.INFO}
        per = cfg.get("levels")
        for name, lv in (per.items() if isinstance(per, dict) else []):
            n = parse_level(lv)
            if n is not None and valid_logger_name(str(name)):
                out[str(name)] = n
        expired: list[str] = []
        for name, t in ((temp or {}).items() if isinstance(temp, dict) else []):
            n = parse_level((t or {}).get("level")) if isinstance(t, dict) else None
            try:
                until = float((t or {}).get("until") or 0)
            except (TypeError, ValueError):
                until = 0.0
            if n is None or not valid_logger_name(str(name)) or until <= now:
                expired.append(str(name))
                continue
            out[str(name)] = n
        return out, expired

    def apply(self, levels: dict[str, int]) -> None:
        for name in self.managed - set(levels):
            logging.getLogger(name).setLevel(logging.NOTSET if name != "cfharness" else logging.INFO)
        for name, n in levels.items():
            lg = logging.getLogger(name)
            if lg.level != n:
                lg.setLevel(n)
        self.managed = set(levels)


TEMP_KEY = "logs.temp_levels"
MAX_TEMP_S = 24 * 3600


def refresh_levels(hub: Any, ctl: LevelController, now: float | None = None) -> dict[str, int]:
    """Đọc config + hub_state, dọn level tạm hết hạn, áp. Chỉ khi tiến trình đã `setup_logging` (test in-process không đổi
    level toàn cục của pytest)."""
    now = time.time() if now is None else now
    temp = hub.store.state_get(TEMP_KEY, {}) or {}
    levels, expired = ctl.effective(hub.config.get("logs") or {}, temp, now)
    if expired and isinstance(temp, dict):
        for name in expired:
            temp.pop(name, None)
        hub.store.state_set(TEMP_KEY, temp)
        logging.getLogger("cfharness.logs").info("level log tạm đã hết hạn: %s", ", ".join(sorted(expired)))
    if file_logging_active():
        ctl.apply(levels)
    return levels


async def level_loop(hub: Any, interval_s: float = 5.0) -> None:
    import asyncio

    ctl = hub.log_levels
    while True:
        try:
            refresh_levels(hub, ctl)
        except Exception:  # best-effort: cấu hình hỏng không được giết vòng
            logging.getLogger("cfharness.logs").exception("không áp được level log")
        await asyncio.sleep(interval_s)


# ---------------------------------------------------------------------------- lỗi không ai bắt (L1)
_FAULT_FILE: Any = None
_crash_log = logging.getLogger("cfharness.crash")
ERROR_COUNTS: dict[str, int] = {"asyncio": 0, "thread": 0, "uncaught": 0, "task": 0}


def install_crash_handlers(logs_dir: Path | None = None) -> None:
    """`sys.excepthook` + `threading.excepthook` → log CRITICAL/ERROR (rồi gọi hook cũ); `faulthandler` → `logs/faults.log`
    (segfault/treo); POSIX: `SIGUSR1` dump stack mọi thread vào faults.log (`cfharness daemon dump-stacks`). Idempotent."""
    global _FAULT_FILE
    prev_sys = sys.excepthook
    if not getattr(prev_sys, "_cfharness", False):
        def _sys_hook(tp: Any, val: Any, tb: Any) -> None:
            if not issubclass(tp, KeyboardInterrupt):
                ERROR_COUNTS["uncaught"] += 1
                _crash_log.critical("lỗi không bắt được: %s", tp.__name__, exc_info=(tp, val, tb))
            prev_sys(tp, val, tb)

        _sys_hook._cfharness = True  # type: ignore[attr-defined]
        sys.excepthook = _sys_hook
    prev_th = threading.excepthook
    if not getattr(prev_th, "_cfharness", False):
        def _th_hook(args: Any) -> None:
            if args.exc_type is not SystemExit:
                ERROR_COUNTS["thread"] += 1
                _crash_log.error("thread %s lỗi: %s", getattr(args.thread, "name", "?"), args.exc_type.__name__,
                                 exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
            prev_th(args)

        _th_hook._cfharness = True  # type: ignore[attr-defined]
        threading.excepthook = _th_hook
    if logs_dir is not None and _FAULT_FILE is None:
        import faulthandler

        try:
            logs_dir.mkdir(parents=True, exist_ok=True)
            fd = os.open(logs_dir / "faults.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            _FAULT_FILE = os.fdopen(fd, "a", encoding="utf-8")
            faulthandler.enable(file=_FAULT_FILE, all_threads=True)
            if hasattr(faulthandler, "register") and hasattr(__import__("signal"), "SIGUSR1"):
                import signal

                faulthandler.register(signal.SIGUSR1, file=_FAULT_FILE, all_threads=True, chain=False)
        except (OSError, ValueError, RuntimeError):  # best-effort: không có faults.log vẫn chạy
            _FAULT_FILE = None


def asyncio_exception_handler(loop: Any, context: dict[str, Any]) -> None:
    """`loop.set_exception_handler`: task chết không ai đợi, callback lỗi… → log ERROR (thay vì chỉ in stderr)."""
    ERROR_COUNTS["asyncio"] += 1
    exc = context.get("exception")
    where = context.get("task") or context.get("future") or context.get("handle")
    name = where.get_name() if hasattr(where, "get_name") else type(where).__name__ if where is not None else "-"
    _crash_log.error("asyncio: %s (%s)", context.get("message") or "lỗi", name,
                     exc_info=(type(exc), exc, exc.__traceback__) if isinstance(exc, BaseException) else None)


def logs_settings(cfg: Any) -> dict[str, Any]:
    c = cfg if isinstance(cfg, dict) else {}
    return {"max_bytes": c.get("max_bytes", DEFAULT_MAX_BYTES), "backup_count": c.get("backup_count", DEFAULT_BACKUP_COUNT),
            "max_age_days": c.get("max_age_days", DEFAULT_MAX_AGE_DAYS), "level": c.get("level") or "info"}


__all__ = ["setup_logging", "setup_process_logging", "file_logging_active", "LevelController", "refresh_levels", "level_loop",
           "install_crash_handlers", "asyncio_exception_handler", "valid_logger_name", "parse_level", "logs_settings",
           "JsonFormatter", "RunContextFilter", "SafeRotatingFileHandler", "run_logger", "set_run_context", "rotate_file",
           "rotate_if_large", "purge_old_logs", "ERROR_COUNTS", "LEVELS", "LIB_LOGGERS", "TEMP_KEY", "MAX_TEMP_S"]
