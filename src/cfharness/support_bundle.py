"""Gói chẩn đoán (support bundle) — L7, `docs/dev-notes/plan-logging.md` B7.

`create(paths, since=…, run_ids=…, include_raw=…, dry_run=…)`:
- `dry_run=True` → KẾ HOẠCH: danh sách file + cỡ (SAU khi lọc + che) + số chỗ đã che + file bị bỏ vì trần. Không ghi gì.
- `dry_run=False` → ghi `HUB_HOME/exports/support-<ts>.tar.gz` (0600, thư mục 0700; `.part` O_EXCL rồi rename).

Nội dung (danh sách TRẮNG theo nguồn — không bao giờ quét cả HUB_HOME):
- `meta/versions.json` (cfharness, python, OS, claude — lấy từ `hub_state.cli_compat` đã kiểm lúc daemon khởi động, KHÔNG spawn `claude`),
  `meta/doctor.json` (`hub_doctor.run`), `meta/status.json` (`hub.status` do daemon truyền vào; ngoại tuyến = "daemon không chạy"),
  `meta/config.json` (config.yaml ĐÃ LỌC: khoá nhạy cảm bị che, nhánh `web` chỉ giữ giá trị bool, che theo giá trị secret).
- `logs/<tên>`: file trong `HUB_HOME/logs/` (KHÔNG thư mục con) theo `LOG_JSONL_RE` (lọc dòng theo `ts` ≥ `since`, `.gz` được giải nén) và
  `LOG_TEXT_RE` (lấy nguyên file, phần ĐUÔI trong trần). File có mtime trước `since` bị bỏ. Không theo symlink.
- `runs/<id>/run.json` (metadata của dòng `runs` — không prompt/summary/kết quả/diff/argv) + `runs/<id>/trace.json` (`trace_view.build`:
  audit metadata, event TÓM TẮT, guard verdict, approval — không payload/input) cho các run được chọn.
- `runs/<id>/raw.jsonl` + `runs/<id>/stderr.log` CHỈ khi `include_raw` (nội dung hội thoại; Q5 — RPC đòi thêm `confirm_raw`).
- `MANIFEST.json` (phần tử cuối): danh sách file + sha256 + cỡ, `redaction_report` (CHỈ số đếm theo file), file bị bỏ, phiên bản.

KHÔNG BAO GIỜ (theo cấu tạo — chỉ đọc đúng các nguồn trên): `hub.db`, `secrets/`, `auth/`, `mcp_server/tokens.json`, file token socket
(`auth.all_token_paths`), session Claude, `checkpoints/`, `backups/`, `exports/`, `imports/`, `audit-archive/`, tri thức agent (memory/skill).

Mọi byte chữ đưa vào gói đi qua `redact.redact_count` (theo GIÁ TRỊ secret đã đăng ký + theo mẫu token) NGAY TRƯỚC khi ghi; sha256 trong
manifest là của nội dung đã che. Trần: tổng `support.max_mb` (mặc định 200), mỗi file `support.max_file_mb` (mặc định 50).
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import logging
import os
import platform
import re
import sqlite3
import stat
import sys
import tarfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterator

from . import __version__
from .errors import Conflict, HubError, NotFound

if TYPE_CHECKING:  # pragma: no cover
    from .daemon import Daemon
    from .paths import HubPaths

log = logging.getLogger("cfharness.support")

FORMAT = "cfharness-support"
SCHEMA = 1
MANIFEST = "MANIFEST.json"
EXPORTS_DIRNAME = "exports"
NAME_RE = re.compile(r"^support-\d{8}T\d{6}(?:-\d{1,3})?\.tar\.gz$")
RUN_ID_RE = re.compile(r"^r_[A-Za-z0-9_\-]{3,62}$")
DEFAULT_SINCE = "24h"
DEFAULT_MAX_MB = 200
DEFAULT_MAX_FILE_MB = 50
MAX_RUNS = 50
GZ_READ_MAX = 512 * 1024 * 1024  # trần giải nén một file .gz
MUTATING_SUPPORT = ("support.create",)
READ_SUPPORT = ("support.plan", "support.list")
# file log được lấy (tên trong HUB_HOME/logs/, không thư mục con)
LOG_JSONL_RE = re.compile(r"^(?:hub\.log|[A-Za-z0-9_\-]+\.jsonl)(?:\.\d{1,3})?(?:\.gz)?$")
LOG_TEXT_RE = re.compile(r"^(?:faults\.log|daemon\.out|web\.log|web\.out|update\.log|update\.out|import-job\.out|filehub-sync\.log)"
                         r"(?:\.\d{1,3})?$")
# cột `runs` được vào gói — metadata (id/trạng thái/thời gian/số); KHÔNG prompt, summary, error, result_json, diff, argv, usage chi tiết
RUN_COLUMNS = ("id", "task_id", "team_id", "agent_id", "agent_config_revision", "kind", "session_id", "status", "outcome", "queued_at",
               "started_at", "ended_at", "cost_usd", "cost_basis", "duration_ms", "num_turns", "exit_code", "terminal_reason", "cli_version",
               "model", "parent_run_id", "resume_reason", "continued_in_run_id", "steer_count", "human_wait_s", "req_id", "account_id")
# để manifest nói rõ cái KHÔNG có (theo cấu tạo, không phải bộ lọc)
NEVER_INCLUDED = ("hub.db", "secrets/", "auth/", "mcp_server/tokens.json", "<socket>.auth (token người/web/mcp)", "session Claude",
                  "checkpoints/", "backups/", "exports/", "imports/", "audit-archive/", "claude-config/",
                  "tri thức agent (memory/skill)")
# config: khoá có giá trị nhạy cảm (ngoài mẫu chung của redact: secret/token/password/api_key/authorization/cookie/credential)
_CFG_SENSITIVE = re.compile(r"(secret|token|passw|api[_-]?key|authorization|cookie|credential|private|webhook|chat_ids?$|user_ids?$|"
                            r"security_chats|email|phone|origin|url$|urls$|host$|headers|env$|passphrase|salt|signing|nonce)", re.I)
_CFG_ID_KEY = re.compile(r"(chat|user)_?ids?$|security_chats|phone", re.I)
MASKED = "***"

_LOCK = threading.Lock()


# ============================================================================ tiện ích
def exports_dir(paths: "HubPaths") -> Path:
    return paths.home / EXPORTS_DIRNAME


def _ensure_dir(paths: "HubPaths") -> Path:
    d = exports_dir(paths)
    d.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(d, 0o700)
    return d


def _cfg(cfg: dict[str, Any] | None, key: str, default: Any) -> Any:
    try:
        v = ((cfg or {}).get("support") or {}).get(key)
    except AttributeError:
        v = None
    return default if v in (None, "") else v


def _since_epoch(since: Any) -> tuple[str, float]:
    from .log_query import _epoch, parse_time

    try:
        iso = parse_time(since if since not in (None, "") else DEFAULT_SINCE)
    except ValueError as e:
        raise HubError(str(e), code=-32602) from None
    ep = _epoch(iso)
    if iso is None or ep is None:  # pragma: no cover — parse_time luôn trả ISO hợp lệ
        raise HubError("since không hợp lệ", code=-32602)
    return iso, ep


def _ts_epoch(ts: Any) -> float | None:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _open_regular(path: Path) -> int | None:
    """fd của file THƯỜNG (không theo symlink). None nếu không phải/không mở được."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return None
    except OSError:  # pragma: no cover
        os.close(fd)
        return None
    return fd


def _read_tail(path: Path, cap: int) -> tuple[bytes, bool] | None:
    fd = _open_regular(path)
    if fd is None:
        return None
    with os.fdopen(fd, "rb") as f:
        size = os.fstat(f.fileno()).st_size
        cut = size > cap
        if cut:
            f.seek(size - cap)
        data = f.read(cap)
    if cut:  # bỏ dòng dở đầu
        nl = data.find(b"\n")
        data = data[nl + 1:] if nl >= 0 else data
    return data, cut


def _read_all(path: Path, cap: int) -> tuple[bytes, bool] | None:
    """Toàn bộ file (giải nén nếu `.gz`), tối đa `cap` byte — vượt thì giữ phần ĐUÔI (file thường) / phần đầu (.gz)."""
    fd = _open_regular(path)
    if fd is None:
        return None
    with os.fdopen(fd, "rb") as raw:
        if path.name.endswith(".gz"):
            try:
                with gzip.GzipFile(fileobj=raw) as g:
                    data = g.read(cap + 1)
            except (OSError, EOFError, ValueError):
                return None
            return data[:cap], len(data) > cap
        size = os.fstat(raw.fileno()).st_size
        if size > cap:
            raw.seek(size - cap)
            return raw.read(cap), True
        return raw.read(), False


def scrub(data: bytes) -> tuple[bytes, int]:
    """Che theo giá trị secret + theo mẫu token. Không mất byte nào ngoài chỗ che (surrogateescape)."""
    from .redact import redact_count

    text, n = redact_count(data.decode("utf-8", "surrogateescape"))
    return text.encode("utf-8", "surrogateescape"), n


def _json_bytes(obj: Any) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, indent=1, default=str) + "\n").encode("utf-8")


# ============================================================================ nguồn
class _Item:
    __slots__ = ("path", "data", "source", "note")

    def __init__(self, path: str, data: bytes, source: str, note: dict[str, Any] | None = None) -> None:
        self.path, self.data, self.source, self.note = path, data, source, note or {}


def filter_config(cfg: Any, *, _in_web: bool = False) -> tuple[Any, int]:
    """config.yaml → bản đã lọc + số chỗ đã che. Khoá nhạy cảm → `***`; nhánh `web` chỉ giữ bool (mọi giá trị khác → `***`)."""
    from .redact import redact_count

    if isinstance(cfg, dict):
        out: dict[str, Any] = {}
        n = 0
        for k, v in cfg.items():
            ks = str(k)
            web = _in_web or ks == "web"
            numeric = isinstance(v, (int, float)) and not isinstance(v, bool)
            hit = bool(_CFG_SENSITIVE.search(ks)) and (not numeric or bool(_CFG_ID_KEY.search(ks)))  # số chỉ nhạy cảm khi là id chat/user
            if not isinstance(v, (dict, list)) and not isinstance(v, bool) and v not in (None, "") and (hit or web):
                out[ks] = MASKED
                n += 1
                continue
            if isinstance(v, (dict, list)) and _CFG_SENSITIVE.search(ks) and not web and v:
                out[ks] = MASKED if isinstance(v, dict) else [MASKED] * len(v)
                n += 1
                continue
            out[ks], m = filter_config(v, _in_web=web)
            n += m
        return out, n
    if isinstance(cfg, list):
        res, n = [], 0
        for x in cfg:
            y, m = filter_config(x, _in_web=_in_web)
            res.append(y)
            n += m
        return res, n
    if isinstance(cfg, str):
        if _in_web and cfg:
            return MASKED, 1
        return redact_count(cfg)
    return cfg, 0


def _ro(db: Path) -> sqlite3.Connection | None:
    if not db.is_file():
        return None
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        c.row_factory = sqlite3.Row
        return c
    except sqlite3.Error:
        return None


def _versions(conn: sqlite3.Connection | None) -> dict[str, Any]:
    out: dict[str, Any] = {"cfharness": __version__, "python": sys.version.split()[0], "python_impl": platform.python_implementation(),
                           "os": platform.platform(), "machine": platform.machine()}
    cc: dict[str, Any] = {}
    if conn is not None:
        try:
            r = conn.execute("SELECT value FROM hub_state WHERE key='cli_compat'").fetchone()
            cc = json.loads(r[0]) if r and r[0] else {}
        except (sqlite3.Error, ValueError, TypeError):
            cc = {}
    out["claude"] = {k: cc.get(k) for k in ("version", "status", "checked_at") if isinstance(cc, dict)} if cc else None
    try:
        if conn is not None:
            out["db_user_version"] = int(conn.execute("PRAGMA user_version").fetchone()[0])
    except sqlite3.Error:  # pragma: no cover
        pass
    return out


def _meta_items(paths: "HubPaths", conn: sqlite3.Connection | None, status: dict[str, Any] | None,
                doctor: Callable[[], dict[str, Any]] | None) -> Iterator[_Item]:
    yield _Item("meta/versions.json", _json_bytes(_versions(conn)), "versions")
    try:
        if doctor is not None:
            doc = doctor()
        else:
            from .hub_doctor import run as _doctor

            doc = _doctor(paths)
    except Exception as e:  # noqa: BLE001 — chẩn đoán không được làm hỏng gói
        log.warning("gói chẩn đoán: hub doctor lỗi: %s", type(e).__name__)
        doc = {"error": f"hub doctor lỗi: {type(e).__name__}"}
    yield _Item("meta/doctor.json", _json_bytes(doc), "doctor")
    yield _Item("meta/status.json", _json_bytes(status if status is not None else {"daemon": "not_running"}), "status")
    try:
        import yaml

        raw = yaml.safe_load(paths.config.read_text(encoding="utf-8")) if paths.config.is_file() else {}
        cfg, masked = filter_config(raw or {})
        yield _Item("meta/config.json", _json_bytes(cfg), "config", {"masked_keys": masked})
    except Exception as e:  # best-effort: lỗi ghi vào meta/config.json (chỉ tên lớp lỗi, không nội dung config)  # noqa: BLE001
        yield _Item("meta/config.json", _json_bytes({"error": f"không đọc được config.yaml: {type(e).__name__}"}), "config")


def check_runs(conn: sqlite3.Connection | None, run_ids: list[str]) -> list[str]:
    ids: list[str] = []
    for r in run_ids or []:
        r = str(r or "").strip()
        if not r:
            continue
        if not RUN_ID_RE.match(r):
            raise HubError(f"run_id không hợp lệ: {r[:40]!r}", code=-32602)
        if r not in ids:
            ids.append(r)
    if len(ids) > MAX_RUNS:
        raise HubError(f"tối đa {MAX_RUNS} run mỗi gói", code=-32602)
    if ids:
        if conn is None:
            raise NotFound("không mở được hub.db để đọc run")
        have = {row[0] for row in conn.execute(f"SELECT id FROM runs WHERE id IN ({','.join('?' * len(ids))})", ids)}
        missing = [i for i in ids if i not in have]
        if missing:
            raise NotFound(f"không có run: {', '.join(missing)}")
    return ids


def _run_items(paths: "HubPaths", conn: sqlite3.Connection, run_ids: list[str], include_raw: bool, file_cap: int) -> Iterator[_Item]:
    from .trace_view import build

    cols = [c[1] for c in conn.execute("PRAGMA table_info(runs)")]
    sel = [c for c in RUN_COLUMNS if c in cols]
    for rid in run_ids:
        row = conn.execute(f"SELECT {', '.join(sel)} FROM runs WHERE id=?", (rid,)).fetchone()
        yield _Item(f"runs/{rid}/run.json", _json_bytes(dict(row) if row else {"id": rid}), "run")
        try:
            tr = build(conn, None, rid, limit=2000, include_logs=False)
        except (sqlite3.Error, ValueError) as e:
            tr = {"error": f"không dựng được dòng thời gian: {type(e).__name__}"}
        yield _Item(f"runs/{rid}/trace.json", _json_bytes(tr), "trace")
        if not include_raw:
            continue
        # bản .gz (run đã xong) trước, .jsonl (run đang chạy) sau — trùng tên `raw.jsonl` bị `_collect` bỏ
        for src, arc in ((f"{rid}.jsonl.gz", "raw.jsonl"), (f"{rid}.jsonl", "raw.jsonl"), (f"{rid}.stderr.log", "stderr.log")):
            p = paths.runs_dir / src
            if p.is_symlink() or not p.is_file():
                continue
            got = _read_all(p, file_cap)
            if got is None:
                continue
            data, cut = got
            yield _Item(f"runs/{rid}/{arc}", data, "raw", {"truncated": True} if cut else None)


def _log_files(logs_dir: Path, since_ep: float) -> list[tuple[Path, str]]:
    out: list[tuple[float, Path, str]] = []
    try:
        entries = list(os.scandir(logs_dir))
    except OSError:
        return []
    for e in entries:
        kind = "jsonl" if LOG_JSONL_RE.match(e.name) else "text" if LOG_TEXT_RE.match(e.name) else None
        if kind is None:
            continue
        try:
            if not e.is_file(follow_symlinks=False):
                continue
            mt = e.stat(follow_symlinks=False).st_mtime
        except OSError:
            continue
        if mt < since_ep - 60:  # không ghi gì trong khoảng → không cần
            continue
        out.append((mt, Path(e.path), kind))
    out.sort(key=lambda x: x[0], reverse=True)  # mới nhất trước (trần tổng: bỏ file cũ trước)
    return [(p, k) for _, p, k in out]


def _filter_jsonl(data: bytes, since_ep: float, cap: int) -> tuple[bytes, dict[str, Any]]:
    keep: list[bytes] = []
    dropped_old = dropped_bad = 0
    for line in data.split(b"\n"):
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except ValueError:
            dropped_bad += 1
            continue
        ep = _ts_epoch(d.get("ts")) if isinstance(d, dict) else None
        if ep is None:
            dropped_bad += 1
            continue
        if ep < since_ep:
            dropped_old += 1
            continue
        keep.append(line)
    out = b"\n".join(keep) + (b"\n" if keep else b"")
    note: dict[str, Any] = {}
    if len(out) > cap:  # giữ dòng MỚI NHẤT trong trần
        tail = out[-cap:]
        nl = tail.find(b"\n")
        out = tail[nl + 1:] if nl >= 0 else tail
        note["truncated"] = True
    if dropped_old:
        note["lines_before_since"] = dropped_old
    if dropped_bad:
        note["lines_not_json"] = dropped_bad
    return out, note


def _log_items(paths: "HubPaths", since_ep: float, file_cap: int) -> Iterator[_Item]:
    logs_dir = paths.logs_dir
    for p, kind in _log_files(logs_dir, since_ep):
        arc = "logs/" + (p.name[:-3] if p.name.endswith(".gz") else p.name)
        if kind == "jsonl":
            got = _read_all(p, GZ_READ_MAX if p.name.endswith(".gz") else max(file_cap * 4, file_cap))
            if got is None:
                continue
            data, cut = got
            out, note = _filter_jsonl(data, since_ep, file_cap)
            if cut:
                note["truncated"] = True
            if not out:
                continue
            yield _Item(arc, out, "log", note)
        else:
            got = _read_tail(p, file_cap)
            if got is None:
                continue
            data, cut = got
            yield _Item(arc, data, "log", {"truncated": True} if cut else None)


# ============================================================================ dựng gói
def _collect(paths: "HubPaths", *, since_ep: float, run_ids: list[str], include_raw: bool, status: dict[str, Any] | None,
             doctor: Callable[[], dict[str, Any]] | None, max_total: int, file_cap: int,
             sink: Callable[[str, bytes], None] | None) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    report: dict[str, int] = {}
    total = 0
    seen: set[str] = set()
    conn = _ro(paths.db)
    try:
        ids = check_runs(conn, run_ids)
        sources: list[Iterator[_Item]] = [_meta_items(paths, conn, status, doctor)]
        if ids and conn is not None:
            sources.append(_run_items(paths, conn, ids, include_raw, file_cap))
        sources.append(_log_items(paths, since_ep, file_cap))
        for src in sources:
            for it in src:
                if it.path in seen:
                    continue
                data, n = scrub(it.data)  # QUÉT LẠI mọi byte chữ ngay trước khi vào gói
                n += int(it.note.get("masked_keys") or 0)  # config: khoá nhạy cảm đã che lúc lọc
                if len(data) > file_cap:
                    data = data[-file_cap:]
                    it.note["truncated"] = True
                if total + len(data) > max_total:
                    skipped.append({"path": it.path, "size": len(data), "reason": "total_cap"})
                    continue
                seen.add(it.path)
                total += len(data)
                if n:
                    report[it.path] = n
                ent = {"path": it.path, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "source": it.source,
                       **{k: v for k, v in it.note.items() if v}}
                files.append(ent)
                if sink is not None:
                    sink(it.path, data)
    finally:
        if conn is not None:
            conn.close()
    return {"files": files, "skipped": skipped, "total": total, "runs": ids,
            "redaction_report": {"total": sum(report.values()), "files": report}}


def _tarinfo(name: str, size: int) -> tarfile.TarInfo:
    ti = tarfile.TarInfo(name)
    ti.size, ti.mode, ti.mtime, ti.type = size, 0o600, int(time.time()), tarfile.REGTYPE
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = ""
    return ti


def _new_name(d: Path) -> str:
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    name = f"support-{stamp}.tar.gz"
    i = 1
    while (d / name).exists() or (d / f".{name}.part").exists():
        i += 1
        name = f"support-{stamp}-{i}.tar.gz"
    return name


def create(paths: "HubPaths", *, since: Any = DEFAULT_SINCE, run_ids: list[str] | None = None, include_raw: bool = False,
           dry_run: bool = False, status: dict[str, Any] | None = None, cfg: dict[str, Any] | None = None,
           doctor: Callable[[], dict[str, Any]] | None = None) -> dict[str, Any]:
    """Kế hoạch (`dry_run`) hoặc ghi gói. Chạy được trong daemon (to_thread) lẫn ngoại tuyến. Xem docstring module."""
    since_iso, since_ep = _since_epoch(since)
    max_total = int(float(_cfg(cfg, "max_mb", DEFAULT_MAX_MB)) * 1024 * 1024)
    file_cap = min(max_total, int(float(_cfg(cfg, "max_file_mb", DEFAULT_MAX_FILE_MB)) * 1024 * 1024))
    if max_total <= 0 or file_cap <= 0:
        raise HubError("support.max_mb / support.max_file_mb phải > 0", code=-32602)
    head = {"format": FORMAT, "schema": SCHEMA, "cfharness_version": __version__, "since": since_iso, "include_raw": bool(include_raw),
            "max_bytes": max_total, "max_file_bytes": file_cap, "never_included": list(NEVER_INCLUDED)}
    if dry_run:
        res = _collect(paths, since_ep=since_ep, run_ids=list(run_ids or []), include_raw=include_raw, status=status, doctor=doctor,
                       max_total=max_total, file_cap=file_cap, sink=None)
        return {**head, **res, "dry_run": True}
    if not _LOCK.acquire(blocking=False):
        raise Conflict("đang tạo một gói chẩn đoán khác — chờ xong rồi thử lại")
    try:
        d = _ensure_dir(paths)
        name = _new_name(d)
        part = d / f".{name}.part"
        final = d / name
        t0 = time.monotonic()
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            with os.fdopen(fd, "wb") as raw:
                with tarfile.open(fileobj=raw, mode="w:gz", compresslevel=6) as tf:
                    def sink(arc: str, data: bytes) -> None:
                        tf.addfile(_tarinfo(arc, len(data)), io.BytesIO(data))

                    res = _collect(paths, since_ep=since_ep, run_ids=list(run_ids or []), include_raw=include_raw, status=status,
                                   doctor=doctor, max_total=max_total, file_cap=file_cap, sink=sink)
                    manifest = {**head, "name": name, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **res}
                    mb, _ = scrub(_json_bytes(manifest))
                    tf.addfile(_tarinfo(MANIFEST, len(mb)), io.BytesIO(mb))
                raw.flush()
                os.fsync(raw.fileno())
            os.replace(part, final)
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        size = final.stat().st_size
        log.info("đã tạo gói chẩn đoán %s (%d file, %d byte, che %d chỗ, %.1f s)", name, len(res["files"]), size,
                 res["redaction_report"]["total"], time.monotonic() - t0)
        return {**head, **res, "dry_run": False, "name": name, "path": str(final), "size": size,
                "sha256": _sha_file(final), "duration_s": round(time.monotonic() - t0, 2)}
    finally:
        _LOCK.release()


def _sha_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def list_bundles(paths: "HubPaths") -> list[dict[str, Any]]:
    d = exports_dir(paths)
    out: list[dict[str, Any]] = []
    try:
        entries = list(os.scandir(d))
    except OSError:
        return []
    for e in entries:
        if not NAME_RE.match(e.name):
            continue
        try:
            if not e.is_file(follow_symlinks=False):
                continue
            st = e.stat(follow_symlinks=False)
        except OSError:
            continue
        out.append({"name": e.name, "size": st.st_size,
                    "created_at": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(timespec="seconds")})
    out.sort(key=lambda x: x["name"], reverse=True)
    return out


def bundle_path(paths: "HubPaths", name: str) -> Path:
    if not NAME_RE.match(str(name or "")):
        raise HubError("tên gói chẩn đoán không hợp lệ", code=-32602)
    return exports_dir(paths) / name


# ============================================================================ tham số dùng chung RPC / CLI
def params_of(p: dict[str, Any]) -> dict[str, Any]:
    runs = p.get("runs") or p.get("run_ids") or []
    if isinstance(runs, str):
        runs = [x for x in re.split(r"[\s,]+", runs) if x]
    if not isinstance(runs, list):
        raise HubError("runs phải là danh sách run_id", code=-32602)
    return {"since": p.get("since") or DEFAULT_SINCE, "run_ids": [str(x) for x in runs], "include_raw": p.get("include_raw") is True}


def require_human_process(what: str = "gói chẩn đoán") -> None:
    from .auth import ENV_RUN_ID

    if os.environ.get(ENV_RUN_ID):
        raise HubError(f"{what} chỉ người làm được — không chạy trong run của agent", code=-32003)


# ============================================================================ RPC
def register_rpc(daemon: "Daemon") -> None:
    """`support.plan {since?, runs?, include_raw?}` (HUMAN_ONLY, chỉ đọc) → kế hoạch; `support.create {…, confirm_raw?}` (HUMAN_ONLY, tự audit)
    → ghi gói; `include_raw` mà thiếu `confirm_raw: true` → từ chối (Q5: raw cần xác nhận bước 2). `support.list` (HUMAN_ONLY) → gói đã có."""
    import asyncio

    from .mcp_server.rpc import wrap_with_audit

    hub = daemon.hub

    def _status() -> dict[str, Any] | None:
        try:
            return hub.status()
        except Exception:  # noqa: BLE001 — trạng thái chỉ là phụ lục
            log.exception("gói chẩn đoán: không lấy được hub.status")
            return {"error": "hub.status lỗi"}

    async def _plan(p: dict[str, Any]) -> dict[str, Any]:
        kw = params_of(p)
        return await asyncio.to_thread(create, hub.paths, dry_run=True, status=_status(), cfg=hub.config.data, **kw)

    async def _create(p: dict[str, Any]) -> dict[str, Any]:
        kw = params_of(p)
        if kw["include_raw"] and p.get("confirm_raw") is not True:
            raise HubError("include_raw cần xác nhận bước 2 (confirm_raw: true) — raw transcript chứa nội dung hội thoại", code=-32602)
        res = await asyncio.to_thread(create, hub.paths, dry_run=False, status=_status(), cfg=hub.config.data, **kw)
        return {k: res[k] for k in ("name", "path", "size", "sha256", "since", "include_raw", "runs", "total", "duration_s")} | {
            "files": len(res["files"]), "skipped": len(res["skipped"]), "redaction_report": res["redaction_report"]}

    async def _list(p: dict[str, Any]) -> dict[str, Any]:
        return {"bundles": await asyncio.to_thread(list_bundles, hub.paths), "dir": str(exports_dir(hub.paths)),
                "max_mb": _cfg(hub.config.data, "max_mb", DEFAULT_MAX_MB)}

    daemon.methods.update({"support.plan": _plan, "support.create": wrap_with_audit(hub, "support.create", _create),
                           "support.list": _list})


__all__ = ["create", "list_bundles", "bundle_path", "filter_config", "scrub", "register_rpc", "params_of", "MUTATING_SUPPORT",
           "READ_SUPPORT", "NAME_RE", "NEVER_INCLUDED", "exports_dir"]
