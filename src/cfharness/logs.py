"""Log có cấu trúc JSON lines ở `HUB_HOME/logs/hub.log`, xoay vòng theo dung lượng (FR-OB-05, NFR-21)."""

from __future__ import annotations

import contextvars
import json
import logging
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


def setup_logging(logs_dir: Path, level: int = logging.INFO) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("cfharness")
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    h = RotatingFileHandler(logs_dir / "hub.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
    h.setFormatter(JsonFormatter())
    h.addFilter(RunContextFilter())
    root.addHandler(h)
