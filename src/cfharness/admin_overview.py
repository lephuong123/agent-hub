"""Tổng quan máy cho người quản trị (Q6, `docs/dev-notes/plan-multi-instance.md` §6b; hướng dẫn: `docs/dev-notes/admin-overview.md`).

Một Mac mini, mỗi người một user macOS + một hub riêng (HUB_HOME 0700). Người quản trị cần MỘT màn tổng quan mọi hub mà không đọc được
HUB_HOME của người khác. Cách làm: mỗi hub tự XUẤT bản tóm tắt CHỈ SỐ LIỆU ra thư mục chung, hub của admin ĐỌC thư mục đó.

Xuất (`hub.admin_export`, `AdminExporter`):
- Tắt mặc định (`admin_export.enabled=false`). Vòng nền khởi động ở `hub.started`, chu kỳ `admin_export.interval_s` (mặc định 60 s);
  `hub.stopping` → ghi lần cuối `daemon.up=false` (admin thấy "đã dừng" thay vì chờ tới khi quá hạn).
- Ghi `<admin_export.dir>/<user-os>.json` NGUYÊN TỬ (mkstemp O_EXCL trong cùng thư mục → fchown nhóm → fchmod 0640 → fsync → rename),
  nhóm `admin_export.group` (mặc định `cfharness-admin`; nhóm không tồn tại / không đổi được → giữ nhóm hiện có, 0640, log cảnh báo).
- Nội dung = `SPEC`: CHỈ số, bool, id/enum đã kiểm bằng regex (phiên bản, băm HUB_HOME, mốc thời gian ISO). KHÔNG bao giờ có chữ tự do:
  prompt, summary, tiêu đề task, mô tả agent, đường dẫn, hostname, id agent. Bộ xuất cũng đi qua `sanitize(SPEC)` nên khoá ngoài SPEC
  không thể lọt ra file.
- Nguồn số liệu dùng lại service sẵn có: `Hub.status()`, `Store.cost_today/cost_since`, `reports.outcome_counts`,
  `ApprovalService.list`, `QuestionService.counts`.

Đọc (`read_dir`, RPC `admin.overview` ∈ HUMAN_ONLY — lộ hoạt động của người khác; CLI `cfharness admin overview`; web `/admin/overview`):
- File của người khác là DỮ LIỆU KHÔNG TIN CẬY: mở `O_NOFOLLOW|O_NONBLOCK`, chỉ file thường, ≤ `MAX_FILE`; JSON hỏng / schema lạ →
  `malformed` (không ném); giá trị lọc lại qua `SPEC` (khoá lạ/chữ tự do bị bỏ); chủ file (uid) phải trùng tên file và trường `user`
  → không thì `owner_mismatch` (không hiện số liệu).
- `stale` khi file cũ hơn 3× chu kỳ (chu kỳ do hub đó khai, kẹp 10..3600 s; thiếu → chu kỳ của hub admin).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import stat
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from . import __version__

if TYPE_CHECKING:
    from .daemon import Daemon
    from .events import HubEvent
    from .hub import Hub

log = logging.getLogger("cfharness.admin_overview")

SCHEMA = 1
DEFAULT_DIR = "/Users/Shared/cfharness/status"
SHARED_ROOT = "/Users/Shared/cfharness"  # agent của hub không đọc/ghi được (sandbox — guard/runtime.sandbox_settings)
DEFAULT_GROUP = "cfharness-admin"
STALE_FACTOR = 3
MAX_FILE = 64 * 1024
MAX_FILES = 500
FAILED_OUTCOMES = ("failed", "crashed", "timed_out", "budget_exceeded", "rate_limited", "protocol_violation")

USER_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")
GROUP_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
_RE = {
    "user": USER_RE,
    "hex": re.compile(r"^[0-9a-f]{8,64}$"),
    "version": re.compile(r"^\d{1,4}\.\d{1,4}\.\d{1,4}([.+-][0-9A-Za-z.]{1,32})?$"),
    "iso": re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$"),
}
_INT_MAX = 10**12

# Danh sách trắng DUY NHẤT của nội dung file (cả khi xuất lẫn khi đọc). Thêm trường → chỉ kiểu số/bool/regex ở trên, kèm ca trong
# tests/test_admin_overview.py. Không bao giờ thêm kiểu "chuỗi tự do".
SPEC: dict[str, Any] = {
    "schema": "int",
    "user": "user",
    "hub_id": "hex",
    "version": "version",
    "generated_at": "iso",
    "generated_ts": "num",
    "interval_s": "int",
    "daemon": {"up": "bool", "started_at": "iso"},
    "estop": {"on": "bool", "teams": "int"},
    "cost_usd": {"today": "num", "week": "num"},
    "runs": {"running": "int", "queued": "int", "max_concurrent": "int", "today_total": "int", "today_completed": "int",
             "today_failed": "int", "today_cancelled": "int"},
    "approvals": {"open": "int", "waiting_human": "int"},
    "questions": {"open": "int", "with_manager": "int"},
    "agents": {"total": "int"},
    "flags": {"auth_block": "bool", "rate_limited": "bool", "sandbox_enabled": "bool"},
}


# ============================================================================ thuần
def _valid(kind: str, v: Any) -> Any:
    if kind == "int":
        return v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= _INT_MAX else None
    if kind == "num":
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= _INT_MAX:
            return None
        return round(float(v), 6)
    if kind == "bool":
        return v if isinstance(v, bool) else None
    rx = _RE.get(kind)
    return v if rx is not None and isinstance(v, str) and rx.match(v) else None


def sanitize(obj: Any, spec: dict[str, Any] = SPEC) -> dict[str, Any]:
    """Chỉ giữ khoá trong `spec` có giá trị đúng kiểu; mọi thứ khác (khoá lạ, chữ tự do, kiểu sai) bị bỏ."""
    out: dict[str, Any] = {}
    if not isinstance(obj, dict):
        return out
    for k, kind in spec.items():
        if k not in obj:
            continue
        if isinstance(kind, dict):
            sub = sanitize(obj[k], kind)
            if sub:
                out[k] = sub
        else:
            v = _valid(kind, obj[k])
            if v is not None:
                out[k] = v
    return out


def os_user() -> str:
    try:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, KeyError, AttributeError):  # Windows / uid không có trong passwd
        import getpass

        return getpass.getuser()


def _owner_name(uid: int) -> str | None:
    try:
        import pwd

        return pwd.getpwuid(uid).pw_name
    except (ImportError, KeyError):
        return None


def hub_id(home: Path) -> str:
    """Băm HUB_HOME (không lộ đường dẫn): phân biệt 2 hub của cùng một user."""
    return hashlib.sha256(os.path.realpath(home).encode("utf-8")).hexdigest()[:16]


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _clamp_interval(v: Any, default: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return float(default)
    return min(3600.0, max(10.0, f)) if math.isfinite(f) else float(default)


# ============================================================================ xuất
def collect(hub: "Hub", *, up: bool = True, interval_s: float = 60, now: datetime | None = None) -> dict[str, Any]:
    """Bản tóm tắt CHỈ SỐ LIỆU của hub này (đã qua `sanitize`)."""
    from .reports import outcome_counts

    now = (now or datetime.now()).astimezone()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    st = hub.status()
    store = hub.store
    oc = outcome_counts(store.conn, midnight.astimezone(timezone.utc))
    try:
        pend = hub.approvals.list("pending", limit=100_000)
    except Exception:  # pragma: no cover — bảng chưa có
        pend = []
    qs = getattr(hub, "questions", None)
    qc = qs.counts() if qs is not None else {}
    estop = st.get("estop") or {}
    raw = {
        "schema": SCHEMA,
        "user": os_user(),
        "hub_id": hub_id(hub.paths.home),
        "version": __version__,
        "generated_at": _iso(now),
        "generated_ts": time.time(),
        "interval_s": int(interval_s),
        "daemon": {"up": bool(up), "started_at": getattr(hub, "admin_started_at", None)},
        "estop": {"on": bool(estop.get("on")) if isinstance(estop, dict) else bool(estop), "teams": len(st.get("teams_estop") or [])},
        "cost_usd": {"today": float(store.cost_since(midnight.astimezone(timezone.utc))),
                     "week": float(store.cost_since((now - timedelta(days=7)).astimezone(timezone.utc)))},
        "runs": {"running": int(st.get("running") or 0), "queued": int(st.get("queued") or 0),
                 "max_concurrent": int(st.get("max_concurrent_runs") or 0), "today_total": sum(oc.values()),
                 "today_completed": int(oc.get("completed", 0)), "today_failed": sum(int(oc.get(k, 0)) for k in FAILED_OUTCOMES),
                 "today_cancelled": int(oc.get("cancelled", 0))},
        "approvals": {"open": len(pend), "waiting_human": sum(1 for a in pend if a.get("status") == "waiting_human")},
        "questions": {"open": int(qc.get("open", 0)) + int(qc.get("with_manager", 0)), "with_manager": int(qc.get("with_manager", 0))},
        "agents": {"total": int(st.get("agents") or 0)},
        "flags": {"auth_block": bool(st.get("auth_block")), "rate_limited": bool(st.get("rate_limit")), "sandbox_enabled": bool(st.get("sandbox_enabled"))},
    }
    return sanitize(raw)


def write_atomic(directory: Path, name: str, data: dict[str, Any], *, group: str | None = DEFAULT_GROUP, mode: int = 0o640) -> list[str]:
    """Ghi `directory/name` nguyên tử. Trả danh sách mã cảnh báo (`group_missing`, `group_chown_failed`) — không ném vì nhóm."""
    warnings: list[str] = []
    payload = json.dumps(data, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")
    fd, tmp = tempfile.mkstemp(prefix=f".{name}.", suffix=".tmp", dir=str(directory))  # O_CREAT|O_EXCL — không theo symlink cài sẵn
    try:
        gid = None
        if group:
            try:
                import grp

                gid = grp.getgrnam(group).gr_gid
            except (ImportError, KeyError):
                warnings.append("group_missing")
        if gid is not None and os.fstat(fd).st_gid != gid:
            try:
                os.fchown(fd, -1, gid)
            except OSError:
                warnings.append("group_chown_failed")
        os.fchmod(fd, mode)
        view = memoryview(payload)
        while view:
            n = os.write(fd, view)
            view = view[n:]
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(tmp, directory / name)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return warnings


class AdminExporter:
    """`hub.admin_export` — vòng nền xuất bản tóm tắt cho tổng quan máy. `export_once()` gọi trực tiếp được (test)."""

    def __init__(self, hub: "Hub") -> None:
        self.hub = hub
        self._task: asyncio.Task[Any] | None = None
        self.last_ok: str | None = None
        self.last_error: str | None = None
        self.last_warnings: list[str] = []
        self.writes = 0
        self._logged: set[str] = set()
        hub.bus.add_listener(self._on_event)

    def cfg(self, key: str) -> Any:
        try:
            return (self.hub.config.get("admin_export") or {}).get(key)
        except KeyError:
            return None

    @property
    def enabled(self) -> bool:
        return bool(self.cfg("enabled"))

    @property
    def interval_s(self) -> float:
        return _clamp_interval(self.cfg("interval_s"), 60)

    @property
    def directory(self) -> Path:
        return Path(str(self.cfg("dir") or DEFAULT_DIR)).expanduser()

    @property
    def path(self) -> Path:
        return self.directory / f"{os_user()}.json"

    def _warn_once(self, code: str, msg: str, *args: Any) -> None:
        if code not in self._logged:
            self._logged.add(code)
            log.warning(msg, *args)

    def export_once(self, *, up: bool = True) -> Path | None:
        if not self.enabled:
            return None
        user = os_user()
        if not USER_RE.match(user):
            self.last_error = "bad_user"
            self._warn_once("bad_user", "admin_export: tên user OS không hợp lệ cho tên file — bỏ qua")
            return None
        d = self.directory
        if not d.is_dir():
            self.last_error = "dir_missing"
            self._warn_once("dir_missing", "admin_export: thư mục %s chưa có — admin cần tạo (docs/dev-notes/admin-overview.md)", d)
            return None
        try:
            data = collect(self.hub, up=up, interval_s=self.interval_s)
            group = str(self.cfg("group") or "") or None
            warns = write_atomic(d, f"{user}.json", data, group=group)
        except OSError as e:
            self.last_error = f"write_failed:{e.errno or 0}"
            self._warn_once(f"write_{e.errno}", "admin_export: không ghi được %s: %s", d, e)
            return None
        for w in warns:
            self._warn_once(w, "admin_export: nhóm '%s' %s — file giữ quyền 0640 với nhóm hiện có", self.cfg("group"),
                            "không tồn tại" if w == "group_missing" else "không đổi được (user chưa thuộc nhóm?)")
        self.last_warnings = warns
        self.last_error = None
        self.last_ok = _iso(datetime.now())
        self.writes += 1
        return d / f"{user}.json"

    def status(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "dir": str(self.directory), "interval_s": self.interval_s, "last_ok": self.last_ok,
                "last_error": self.last_error, "warnings": list(self.last_warnings), "writes": self.writes}

    # ------------------------------------------------------------------ vòng nền
    def _on_event(self, ev: "HubEvent") -> None:
        try:
            if ev.type == "hub.started":
                self.hub.admin_started_at = _iso(datetime.now())  # type: ignore[attr-defined]
                self.start()
            elif ev.type == "hub.stopping":
                self.stop()
                self.export_once(up=False)
        except Exception:  # pragma: no cover — listener không được làm hỏng bus
            log.exception("admin_export listener lỗi với %s", ev.type)

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        try:
            self._task = asyncio.get_running_loop().create_task(self._loop())
        except RuntimeError:
            self._task = None

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                self.export_once()  # chạy trên loop: kết nối SQLite của hub gắn với luồng này; ghi file nhỏ (< 2 KB)
                await asyncio.sleep(self.interval_s)
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover
                log.exception("admin_export lỗi")
                await asyncio.sleep(60)


def attach(hub: "Hub") -> None:
    hub.admin_export = AdminExporter(hub)  # type: ignore[attr-defined]


# ============================================================================ đọc
def read_file(path: Path, *, default_interval: float = 60, now: float | None = None) -> dict[str, Any]:
    """Một file trạng thái → `{user, state, age_s, stale, data?, error?}`. Không bao giờ ném vì nội dung file."""
    now = time.time() if now is None else now
    user = path.stem
    out: dict[str, Any] = {"user": user, "state": "malformed", "age_s": None, "stale": False, "data": None, "error": None}
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as e:
        out["error"] = "symlink" if os.path.islink(path) else f"open_failed:{e.errno or 0}"
        return out
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            out["error"] = "not_regular"
            return out
        if st.st_size > MAX_FILE:
            out["error"] = "too_large"
            return out
        raw = os.read(fd, MAX_FILE + 1)
    except OSError as e:
        out["error"] = f"read_failed:{e.errno or 0}"
        return out
    finally:
        os.close(fd)
    out["age_s"] = round(max(0.0, now - st.st_mtime), 1)
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        out["error"] = "bad_json"
        return out
    if not isinstance(obj, dict):
        out["error"] = "not_object"
        return out
    data = sanitize(obj)
    if data.get("schema") != SCHEMA:
        out["error"] = "unsupported_schema"
        return out
    owner = _owner_name(st.st_uid)
    if data.get("user") != user or (owner is not None and owner != user):
        out["state"], out["error"] = "owner_mismatch", "owner_mismatch"
        return out
    interval = _clamp_interval(data.get("interval_s"), default_interval)
    stale = out["age_s"] > STALE_FACTOR * interval
    out["stale"] = stale
    out["data"] = data
    out["state"] = "stale" if stale else ("stopped" if (data.get("daemon") or {}).get("up") is False else "ok")
    return out


def _total(hubs: list[dict[str, Any]]) -> dict[str, Any]:
    t = {"hubs": len(hubs), "ok": 0, "stale": 0, "stopped": 0, "bad": 0, "cost_today_usd": 0.0, "cost_week_usd": 0.0, "running": 0,
         "queued": 0, "failed_today": 0, "approvals_open": 0, "questions_open": 0, "agents": 0, "estop_on": 0}
    for h in hubs:
        st = h["state"]
        t[st if st in ("ok", "stale", "stopped") else "bad"] += 1
        d = h.get("data") or {}
        if not d:
            continue
        c, r = d.get("cost_usd") or {}, d.get("runs") or {}
        t["cost_today_usd"] += float(c.get("today") or 0)
        t["cost_week_usd"] += float(c.get("week") or 0)
        t["failed_today"] += int(r.get("today_failed") or 0)
        t["approvals_open"] += int((d.get("approvals") or {}).get("open") or 0)
        t["questions_open"] += int((d.get("questions") or {}).get("open") or 0)
        t["agents"] += int((d.get("agents") or {}).get("total") or 0)
        t["estop_on"] += 1 if (d.get("estop") or {}).get("on") else 0
        if st == "ok":  # run đang chạy/chờ chỉ tính khi dữ liệu còn mới
            t["running"] += int(r.get("running") or 0)
            t["queued"] += int(r.get("queued") or 0)
    t["cost_today_usd"] = round(t["cost_today_usd"], 6)
    t["cost_week_usd"] = round(t["cost_week_usd"], 6)
    return t


def read_dir(directory: str | Path, *, default_interval: float = 60, now: float | None = None) -> dict[str, Any]:
    """Đọc mọi `<user>.json` trong thư mục chung. Thiếu thư mục → `exists=false`, không ném."""
    d = Path(directory).expanduser()
    out: dict[str, Any] = {"dir": str(d), "exists": d.is_dir(), "read_at": _iso(datetime.now()), "stale_after_factor": STALE_FACTOR,
                           "hubs": [], "totals": _total([]), "error": None}
    if not out["exists"]:
        return out
    try:
        names = sorted(e.name for e in os.scandir(d))
    except OSError as e:
        out["error"] = f"list_failed:{e.errno or 0}"
        return out
    files = [n for n in names if n.endswith(".json") and not n.startswith(".") and USER_RE.match(n[:-5])][:MAX_FILES]
    out["hubs"] = [read_file(d / n, default_interval=default_interval, now=now) for n in files]
    out["totals"] = _total(out["hubs"])
    return out


# ============================================================================ RPC
def register_rpc(daemon: "Daemon") -> None:
    """`admin.overview {}` — CHỈ ĐỌC, ∈ HUMAN_ONLY (lộ hoạt động của người khác trên máy). Không ghi trạng thái → không audit."""
    hub = daemon.hub
    exp: AdminExporter | None = getattr(hub, "admin_export", None)

    async def _overview(p: dict[str, Any]) -> dict[str, Any]:
        cfg = hub.config.get("admin_export") or {}
        directory = str(cfg.get("dir") or DEFAULT_DIR)
        res = await asyncio.to_thread(read_dir, directory, default_interval=_clamp_interval(cfg.get("interval_s"), 60))
        res["self"] = {**(exp.status() if exp else {}), "user": os_user()}
        return res

    methods: dict[str, Callable[[dict[str, Any]], Any]] = {"admin.overview": _overview}
    daemon.methods.update(methods)


__all__ = ["AdminExporter", "SCHEMA", "SPEC", "attach", "collect", "read_dir", "read_file", "register_rpc", "sanitize", "write_atomic",
           "DEFAULT_DIR", "SHARED_ROOT", "DEFAULT_GROUP", "USER_RE", "GROUP_RE"]
