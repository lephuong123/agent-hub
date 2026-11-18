"""Đọc log JSONL của hub theo TRƯỜNG + bộ đếm lỗi trong RAM + "lỗi gần đây" (L6, docs/dev-notes/plan-logging.md B5/B6).

- `query(logs_dir, …)`: đọc mọi `*.jsonl` + `hub.log` trong `HUB_HOME/logs/` (kể cả bản xoay `.N` và `.gz`), lọc theo trường JSON (không
  theo chuỗi con — chuỗi con chỉ là bộ lọc thô TRƯỚC khi parse cho nhanh), gộp theo `ts`. Dòng không phải JSON bị bỏ qua. Trần: số dòng trả
  về (`limit` ≤ `MAX_LIMIT`) + số byte quét (`max_bytes` ≤ `MAX_SCAN_BYTES`; file mới nhất quét trước, file lớn chỉ đọc phần đuôi).
  Dùng chung cho RPC `logs.query` (logs_rpc.py), fallback của CLI khi daemon dừng, trang web `/logs` và `trace.get` (trace_view.py).
- `ErrorCounter`: `logging.Handler` mức WARNING gắn trong `logs.setup_logging` — đếm WARNING/ERROR theo logger trong thùng 1 phút (giữ
  24 h) + số `rpc.slow`. Không IO, không giữ nội dung.
- `recent_errors(logs_dir)`: top N thông điệp ERROR 24 h gom theo `logger` + mẫu thông điệp (số/id thay bằng chỗ trống), có req_id mẫu —
  cho `hub doctor`.

Log là METADATA (logs.py) nhưng vẫn là DỮ LIỆU khi hiển thị: web autoescape, terminal qua `termsafe.clean`.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import re
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

LEVEL_RANK = {"debug": 10, "info": 20, "warning": 30, "error": 40, "critical": 50}
DEFAULT_LIMIT = 200
MAX_LIMIT = 2000
DEFAULT_SCAN_BYTES = 64 * 1024 * 1024
MAX_SCAN_BYTES = 512 * 1024 * 1024
FIELD_MAX = 4000  # trần mỗi chuỗi trong dòng trả về (traceback dài)
# tên file log có cấu trúc: <tên>.jsonl[.N][.gz] hoặc hub.log[.N][.gz]
_FILE_RE = re.compile(r"^(?P<base>[A-Za-z0-9_\-]+\.jsonl|hub\.log)(?:\.(?P<n>\d+))?(?P<gz>\.gz)?$")
# file → tiến trình ghi (để bỏ qua file khi lọc `proc`; file lạ vẫn đọc)
FILE_PROC = {"hub.log": "daemon", "web.jsonl": "web", "mcp.jsonl": "mcp", "cli.jsonl": "cli"}
_ID_RE = re.compile(r"^[A-Za-z0-9_\-:.]{1,80}$")
_REL_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhdw])\s*$")


# ---------------------------------------------------------------------------- thời gian
def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_time(v: Any, now: datetime | None = None) -> str | None:
    """`2h`/`30m`/`1d`/`90s`/`1w` (lùi từ bây giờ) hoặc ISO 8601 (không múi giờ = giờ máy) → ISO UTC cùng dạng `ts` của log. Rỗng → None."""
    if v is None or v == "":
        return None
    s = str(v).strip()
    now = now or datetime.now(timezone.utc)
    m = _REL_RE.match(s)
    if m:
        n = float(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}[m.group(2)]
        return iso_utc(now - timedelta(seconds=n))
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"thời điểm không hợp lệ: {s!r} (vd 2h, 30m, 1d, 2026-11-18T10:00)") from None
    if dt.tzinfo is None:
        dt = dt.astimezone()  # giờ máy
    return iso_utc(dt)


def _epoch(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# ---------------------------------------------------------------------------- file
def log_files(logs_dir: Path) -> list[Path]:
    """File log có cấu trúc, MỚI NHẤT trước (theo mtime). Không theo symlink."""
    out: list[tuple[float, Path]] = []
    try:
        entries = list(os.scandir(logs_dir))
    except OSError:
        return []
    for e in entries:
        if not _FILE_RE.match(e.name):
            continue
        try:
            if not e.is_file(follow_symlinks=False):
                continue
            out.append((e.stat(follow_symlinks=False).st_mtime, Path(e.path)))
        except OSError:
            continue
    out.sort(key=lambda x: x[0], reverse=True)
    return [p for _, p in out]


def file_proc(name: str) -> str | None:
    m = _FILE_RE.match(name)
    return FILE_PROC.get(m.group("base")) if m else None


def _read_lines(path: Path, budget: int) -> tuple[list[bytes], int, bool]:
    """Dòng (bytes) của file, tối đa `budget` byte (file thường: đọc PHẦN ĐUÔI; .gz: đọc từ đầu tới trần). → (dòng, byte đã đọc, bị cắt)."""
    if budget <= 0:
        return [], 0, True
    try:
        if path.name.endswith(".gz"):
            with gzip.open(path, "rb") as f:
                data = f.read(budget + 1)
            cut = len(data) > budget
            data = data[:budget]
            lines = data.split(b"\n")
            if cut and lines:
                lines.pop()  # dòng dở cuối
            return lines, len(data), cut
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            cut = size > budget
            if cut:
                f.seek(size - budget)
            data = f.read(budget)
        lines = data.split(b"\n")
        if cut and lines:
            lines.pop(0)  # dòng dở đầu
        return lines, len(data), cut
    except (OSError, EOFError, gzip.BadGzipFile, ValueError):
        return [], 0, False


def _clip(v: Any) -> Any:
    if isinstance(v, str) and len(v) > FIELD_MAX:
        return v[:FIELD_MAX] + f"… (+{len(v) - FIELD_MAX} ký tự)"
    return v


def parse_line(raw: bytes | str) -> dict[str, Any] | None:
    """Một dòng JSON object có `ts` → dict (chuỗi đã cắt trần); dòng hỏng/không phải object → None."""
    try:
        d = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(d, dict) or not isinstance(d.get("ts"), str):
        return None
    return {str(k): _clip(v) for k, v in d.items() if isinstance(v, (str, int, float, bool)) or v is None}


# ---------------------------------------------------------------------------- lọc
class Filters:
    """Bộ lọc theo trường. `logger` khớp tiền tố theo dấu chấm (`cfharness.guard` ⊇ `cfharness.guard.service`); `level` = tối thiểu."""

    def __init__(self, *, since: str | None = None, until: str | None = None, level: str | None = None, logger: str | None = None,
                 req: str | None = None, run: str | None = None, agent: str | None = None, task: str | None = None,
                 proc: str | None = None, any_of: dict[str, set[str]] | None = None) -> None:
        self.since, self.until = since, until
        lv = (level or "").strip().lower()
        if lv and lv not in LEVEL_RANK:
            raise ValueError("level phải là debug|info|warning|error|critical")
        self.min_rank = LEVEL_RANK.get(lv, 0)
        for name, v in (("logger", logger), ("req", req), ("run", run), ("agent", agent), ("task", task), ("proc", proc)):
            if v is not None and v != "" and not _ID_RE.match(str(v)):
                raise ValueError(f"{name} không hợp lệ")
        self.logger = logger or None
        self.eq = {k: v for k, v in (("req_id", req), ("run_id", run), ("agent_id", agent), ("task_id", task), ("proc", proc)) if v}
        # any_of: {trường: {giá trị…}} — dòng khớp nếu MỘT trong các trường có giá trị thuộc tập (trace: req_id ∈ … hoặc run_id ∈ …)
        self.any_of = {k: set(v) for k, v in (any_of or {}).items() if v}
        needles = [v for k, v in self.eq.items() if k != "proc"]
        self.needle = needles[0].encode() if needles else None
        self.needles_any = [x.encode() for vs in self.any_of.values() for x in vs][:200]

    def prefilter(self, raw: bytes) -> bool:
        if self.needle is not None and self.needle not in raw:
            return False
        if self.needles_any and not any(n in raw for n in self.needles_any):
            return False
        return True

    def match(self, d: dict[str, Any]) -> bool:
        ts = d.get("ts") or ""
        if self.since and ts < self.since:
            return False
        if self.until and ts > self.until:
            return False
        if self.min_rank and LEVEL_RANK.get(str(d.get("level") or "").lower(), 0) < self.min_rank:
            return False
        if self.logger:
            lg = str(d.get("logger") or "")
            if lg != self.logger and not lg.startswith(self.logger + "."):
                return False
        for k, v in self.eq.items():
            if d.get(k) != v:
                return False
        if self.any_of and not any(d.get(k) in vs for k, vs in self.any_of.items()):
            return False
        return True


def query(logs_dir: Path, filters: Filters | None = None, *, limit: int = DEFAULT_LIMIT, max_bytes: int = DEFAULT_SCAN_BYTES) -> dict[str, Any]:
    """Dòng log khớp, cũ → mới, tối đa `limit` dòng MỚI NHẤT. Kết quả: `{rows, files, scanned_bytes, truncated (chạm trần byte —
    dữ liệu cũ hơn không được quét), more (có nhiều dòng khớp hơn limit)}`."""
    f = filters or Filters()
    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    budget = max(1, min(int(max_bytes or DEFAULT_SCAN_BYTES), MAX_SCAN_BYTES))
    since_epoch = _epoch(f.since)
    proc = f.eq.get("proc")
    rows: list[dict[str, Any]] = []
    files: list[str] = []
    scanned, truncated = 0, False
    for path in log_files(logs_dir):
        if proc and (fp := file_proc(path.name)) is not None and fp != proc:
            continue
        if since_epoch is not None:
            try:
                if path.stat().st_mtime < since_epoch - 1:  # lần ghi cuối trước mốc → không có dòng nào sau mốc
                    continue
            except OSError:
                continue
        left = budget - scanned
        if left <= 0:
            truncated = True
            break
        lines, n, cut = _read_lines(path, left)
        scanned += n
        truncated = truncated or cut
        files.append(path.name)
        for raw in lines:
            if not raw or not f.prefilter(raw):
                continue
            d = parse_line(raw)
            if d is None or not f.match(d):
                continue
            d["_file"] = path.name
            rows.append(d)
    rows.sort(key=lambda d: d.get("ts") or "")
    more = len(rows) > limit
    return {"rows": rows[-limit:], "files": files, "scanned_bytes": scanned, "truncated": truncated, "more": more}


def follow(logs_dir: Path, filters: Filters | None = None, *, poll_s: float = 0.5, stop: Any = None) -> Iterator[dict[str, Any]]:
    """`logs -f` (cục bộ): theo dõi file ĐANG GHI (`hub.log`, `*.jsonl`) từ cuối; xoay vòng (inode đổi/file ngắn lại) → mở lại từ đầu.
    `stop()` trả True thì dừng (test)."""
    f = filters or Filters()
    pos: dict[str, tuple[int, int]] = {}  # tên → (inode, offset)
    buf: dict[str, bytes] = {}
    first = True
    while not (stop and stop()):
        try:
            names = sorted(e.name for e in os.scandir(logs_dir) if (m := _FILE_RE.match(e.name)) and not m.group("n") and not m.group("gz"))
        except OSError:
            names = []
        batch: list[dict[str, Any]] = []
        for name in names:
            p = logs_dir / name
            try:
                st = p.stat()
            except OSError:
                continue
            ino, off = pos.get(name, (st.st_ino, st.st_size if first else 0))
            if ino != st.st_ino or st.st_size < off:
                ino, off = st.st_ino, 0
                buf.pop(name, None)
            if st.st_size > off:
                try:
                    with open(p, "rb") as fh:
                        fh.seek(off)
                        data = fh.read(min(st.st_size - off, 8 * 1024 * 1024))
                except OSError:
                    continue
                off += len(data)
                data = buf.pop(name, b"") + data
                parts = data.split(b"\n")
                if parts and parts[-1]:
                    buf[name] = parts.pop()
                for raw in parts:
                    if raw and f.prefilter(raw):
                        d = parse_line(raw)
                        if d is not None and f.match(d):
                            d["_file"] = name
                            batch.append(d)
            pos[name] = (ino, off)
        first = False
        batch.sort(key=lambda d: d.get("ts") or "")
        yield from batch
        time.sleep(poll_s)


# ---------------------------------------------------------------------------- bộ đếm lỗi (B5)
BUCKET_S = 60
_COUNTER_MARK = "_cfharness_error_counter"
KEEP_S = 24 * 3600
MAX_LOGGERS = 200


class ErrorCounter(logging.Handler):
    """Đếm WARNING/ERROR theo logger trong thùng `BUCKET_S` (giữ `KEEP_S`) + `rpc.slow`. Chỉ con số — không giữ thông điệp."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self._lock_ = threading.Lock()
        self.buckets: dict[int, Counter[tuple[str, str]]] = {}
        self.total: Counter[str] = Counter()
        setattr(self, _COUNTER_MARK, True)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            lv = "error" if record.levelno >= logging.ERROR else "warning"
            name = record.name if len(record.name) <= 80 else record.name[:80]
            b = int(record.created // BUCKET_S)
            slow = getattr(record, "event", None) == "rpc.slow"
            with self._lock_:
                c = self.buckets.get(b)
                if c is None:
                    c = self.buckets[b] = Counter()
                    cutoff = b - KEEP_S // BUCKET_S
                    for k in [k for k in self.buckets if k < cutoff]:
                        del self.buckets[k]
                if (lv, name) not in c and len(c) >= MAX_LOGGERS:
                    name = "(khác)"
                c[(lv, name)] += 1
                if slow:
                    c[("rpc_slow", "")] += 1
                self.total[lv] += 1
        except Exception:  # best-effort: bộ đếm không bao giờ được làm hỏng việc ghi log
            pass

    def snapshot(self, now: float | None = None, top: int = 8) -> dict[str, Any]:
        now = time.time() if now is None else now
        cur = int(now // BUCKET_S)
        out: dict[str, Any] = {}
        with self._lock_:
            items = [(b, dict(c)) for b, c in self.buckets.items()]
        for label, span in (("1h", 3600), ("24h", KEEP_S)):
            lo = cur - span // BUCKET_S
            agg: Counter[tuple[str, str]] = Counter()
            for b, c in items:
                if b > lo:
                    agg.update(c)
            by_logger: dict[str, dict[str, int]] = {}
            for (lv, name), n in agg.items():
                if lv in ("error", "warning"):
                    by_logger.setdefault(name, {"error": 0, "warning": 0})[lv] += n
            ranked = sorted(by_logger.items(), key=lambda kv: (-kv[1]["error"], -kv[1]["warning"], kv[0]))[:top]
            out[label] = {"error": sum(n for (lv, _), n in agg.items() if lv == "error"),
                          "warning": sum(n for (lv, _), n in agg.items() if lv == "warning"),
                          "rpc_slow": agg.get(("rpc_slow", ""), 0),
                          "by_logger": [{"logger": k, **v} for k, v in ranked]}
        return out


COUNTER = ErrorCounter()


def ensure_error_counter() -> ErrorCounter:
    """Gắn `COUNTER` vào logger `cfharness` (mọi bản ghi của hub) và root (thư viện ngoài — không đếm lặp bản ghi `cfharness`). Idempotent."""
    lg = logging.getLogger("cfharness")
    if not any(getattr(h, _COUNTER_MARK, False) for h in lg.handlers):
        lg.addHandler(COUNTER)
    root = logging.getLogger()
    if not any(getattr(h, _COUNTER_MARK, False) for h in root.handlers):
        rh = _RootCounter()
        root.addHandler(rh)
    return COUNTER


class _RootCounter(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        setattr(self, _COUNTER_MARK, True)

    def emit(self, record: logging.LogRecord) -> None:
        if record.name == "cfharness" or record.name.startswith("cfharness."):
            return
        COUNTER.emit(record)


def error_stats(hub: Any = None) -> dict[str, Any]:
    """Cho `hub.status` / thẻ Sức khoẻ: đếm từ RAM (từ khi daemon chạy) + lỗi không ai bắt + listener lỗi."""
    from .logs import ERROR_COUNTS

    snap = COUNTER.snapshot()
    return {**snap, "uncaught": dict(ERROR_COUNTS), "bus_listener_errors": int(getattr(getattr(hub, "bus", None), "listener_errors", 0) or 0)}


# ---------------------------------------------------------------------------- lỗi gần đây (doctor)
_TPL_SUBS = (
    (re.compile(r"r_[0-7][0-9A-HJKMNP-TV-Z]{25}"), "<req>"),
    (re.compile(r"\b[a-z]{1,3}_[0-9a-f]{8,32}\b"), "<id>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\b[0-9a-f]{12,}\b", re.I), "<hex>"),
    (re.compile(r"(?<![A-Za-z])\d+(?:[.,]\d+)*"), "<n>"),
)


def msg_template(msg: str) -> str:
    """Mẫu thông điệp: id/req_id/uuid/hex/số → chỗ trống — để gom các lỗi cùng loại."""
    s = (msg or "").splitlines()[0][:300] if msg else ""
    for rx, rep in _TPL_SUBS:
        s = rx.sub(rep, s)
    return s


def recent_errors(logs_dir: Path, *, since_s: float = 24 * 3600, top: int = 5, max_bytes: int = 32 * 1024 * 1024,
                  now: datetime | None = None) -> dict[str, Any]:
    """Top `top` nhóm ERROR/CRITICAL trong `since_s` giây gần nhất: `{groups[{logger, template, count, last_ts, req_id}], total, truncated}`."""
    now = now or datetime.now(timezone.utc)
    f = Filters(since=iso_utc(now - timedelta(seconds=since_s)), level="error")
    res = query(logs_dir, f, limit=MAX_LIMIT, max_bytes=max_bytes)
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for d in res["rows"]:
        key = (str(d.get("logger") or "?"), msg_template(str(d.get("msg") or "")))
        g = groups.get(key)
        if g is None:
            g = groups[key] = {"logger": key[0], "template": key[1], "count": 0, "last_ts": None, "req_id": None}
        g["count"] += 1
        g["last_ts"] = d.get("ts")
        if d.get("req_id"):
            g["req_id"] = d["req_id"]
    ranked = sorted(groups.values(), key=lambda g: (-g["count"], g["last_ts"] or ""))
    return {"groups": ranked[:top], "total": len(res["rows"]), "distinct": len(groups), "truncated": res["truncated"] or res["more"]}


__all__ = ["query", "follow", "Filters", "parse_time", "log_files", "parse_line", "ErrorCounter", "COUNTER", "ensure_error_counter",
           "error_stats", "recent_errors", "msg_template", "LEVEL_RANK", "MAX_LIMIT", "DEFAULT_SCAN_BYTES"]
