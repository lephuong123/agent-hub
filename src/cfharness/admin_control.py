"""Quản trị máy — kênh admin → user (plan-admin-users P1; chi tiết: `docs/dev-notes/admin-users.md`).

Thành phần:
- Bố cục thư mục chung + nhóm (`ADMIN_GROUP`, `USERS_GROUP`, `shared_root()`, `control_dir()`, `policy_dir()`, `admin_log_dir()`).
  Đường dẫn `control/`, `policy/`, `admin-log/` là HẰNG SỐ trong code (không cấu hình — user không trỏ được đi nơi khác). Ngoại lệ
  duy nhất: test (`CFHARNESS_TESTING=1` + `CFHARNESS_TEST_SHARED_ROOT`, tests/conftest.py) để máy dev có thư mục thật không ảnh hưởng test.
- `is_admin()`: gid nhóm `cfharness-admin` ∈ `os.getgroups()` của tiến trình daemon (quyền file thật mới là ranh giới; đây là cổng UI/RPC).
- ESTOP admin: file cờ `control/estop` (mọi user) và `control/estop.<user>`. CÓ MẶT = BẬT (kể cả nội dung hỏng / symlink / thư mục).
  Nội dung chỉ `reason_code` ∈ `REASON_CODES` (không chữ tự do admin → agent). Hub user kiểm mỗi `CHECK_S` giây (`AdminControl`),
  lưu `hub_state.admin_estop`, phát `hub.admin_estop`, dừng run đang chạy; `RunManager.estop` gộp cờ này (user `estop off` không gỡ được);
  `RunManager.submit` kiểm lại ngay (`submit_gate`).
- admin-log: `admin-log/<user>.jsonl` (+ `_all.jsonl` cho thao tác mọi user) — user ĐỌC được thao tác admin trên máy của mình.
  Dòng chỉ gồm khoá trong `ADMIN_LOG_SPEC` (enum/regex, không chữ tự do).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import stat
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .errors import Blocked, HubError

if TYPE_CHECKING:
    from .events import HubEvent
    from .hub import Hub

log = logging.getLogger("cfharness.admin_control")

SHARED_ROOT = "/Users/Shared/cfharness"
ADMIN_GROUP = "cfharness-admin"
USERS_GROUP = "cfharness-users"
CHECK_S = 5.0
FLAG_MAX = 4096
ADMIN_LOG_MAX_LINES = 500
REASON_CODES = ("maintenance", "budget", "security", "policy", "revoked", "incident", "other")
ADMIN_LOG_ACTIONS = ("estop_on", "estop_off", "user_add", "user_revoke", "policy_set")
USER_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$")

# Dòng admin-log: danh sách trắng (cả khi ghi lẫn khi đọc)
ADMIN_LOG_SPEC: dict[str, Any] = {"ts": "iso", "by": "user", "action": "enum:" + ",".join(ADMIN_LOG_ACTIONS), "scope": "enum:all,user",
                                  "user": "user", "reason_code": "enum:" + ",".join(REASON_CODES)}

# Hook thay được trong test (không gọi dscacheutil/getgroups thật theo nhóm của máy dev)
_GETGROUPS: Callable[[], list[int]] = os.getgroups if hasattr(os, "getgroups") else (lambda: [])


def _getgrnam(name: str) -> Any:
    import grp

    return grp.getgrnam(name)


_GETGRNAM: Callable[[str], Any] = _getgrnam


# ============================================================================ đường dẫn
def shared_root() -> Path:
    """Gốc thư mục chung. HẰNG SỐ; chỉ test (CFHARNESS_TESTING=1) đổi được qua CFHARNESS_TEST_SHARED_ROOT."""
    if os.environ.get("CFHARNESS_TESTING") == "1" and os.environ.get("CFHARNESS_TEST_SHARED_ROOT"):
        return Path(os.environ["CFHARNESS_TEST_SHARED_ROOT"])
    return Path(SHARED_ROOT)


def control_dir() -> Path:
    return shared_root() / "control"


def policy_dir() -> Path:
    return shared_root() / "policy"


def admin_log_dir() -> Path:
    return shared_root() / "admin-log"


def os_user() -> str:
    from .admin_overview import os_user as _u

    return _u()


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ============================================================================ nhóm
def group_gid(name: str) -> int | None:
    try:
        return int(_GETGRNAM(name).gr_gid)
    except (KeyError, ImportError, AttributeError, OSError):
        return None


def group_members(name: str) -> list[str]:
    try:
        return sorted(str(m) for m in (_GETGRNAM(name).gr_mem or []) if USER_RE.match(str(m)))
    except (KeyError, ImportError, AttributeError, OSError):
        return []


def is_admin() -> bool:
    """Tiến trình này (daemon/CLI) thuộc nhóm `cfharness-admin`. Cổng UI/RPC — quyền file mới là ranh giới thật."""
    gid = group_gid(ADMIN_GROUP)
    if gid is None:
        return False
    try:
        return gid in set(_GETGROUPS())
    except OSError:
        return False


def require_admin(what: str) -> None:
    if not is_admin():
        raise Blocked(f"{what}: chỉ thành viên nhóm {ADMIN_GROUP} (người quản trị máy). Xem docs/dev-notes/admin-overview.md",
                      data={"reason": "not_admin"})


# ============================================================================ lọc dữ liệu (dùng chung admin-log/audit)
def valid(kind: str, v: Any) -> Any:
    if kind == "iso":
        return v if isinstance(v, str) and _ISO_RE.match(v) else None
    if kind == "user":
        return v if isinstance(v, str) and USER_RE.match(v) else None
    if kind.startswith("enum:"):
        return v if isinstance(v, str) and v in kind[5:].split(",") else None
    if kind.startswith("re:"):
        return v if isinstance(v, str) and re.match(kind[3:], v) else None
    if kind == "int":
        return v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 10**12 else None
    return None


def filter_flat(obj: Any, spec: dict[str, str]) -> dict[str, Any]:
    if not isinstance(obj, dict):
        return {}
    out = {}
    for k, kind in spec.items():
        if k in obj:
            v = valid(kind, obj[k])
            if v is not None:
                out[k] = v
    return out


def read_small(path: Path, limit: int) -> tuple[bytes | None, os.stat_result | None, str | None]:
    """Đọc file KHÔNG theo symlink, chỉ file thường ≤ limit. Trả (dữ liệu, stat, mã lỗi)."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None, None, "missing"
    except OSError as e:
        return None, None, "symlink" if os.path.islink(path) else f"open_failed:{e.errno or 0}"
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None, st, "not_regular"
        if st.st_size > limit:
            return None, st, "too_large"
        return os.read(fd, limit + 1), st, None
    except OSError as e:
        return None, None, f"read_failed:{e.errno or 0}"
    finally:
        os.close(fd)


# ============================================================================ ESTOP admin — đọc (hub user)
def flag_path(user: str | None) -> Path:
    if user is None:
        return control_dir() / "estop"
    if not USER_RE.match(user):
        raise HubError("tên user không hợp lệ")
    return control_dir() / f"estop.{user}"


def _read_flag(p: Path) -> dict[str, Any] | None:
    try:
        st = os.lstat(p)
    except FileNotFoundError:
        return None
    except OSError:  # không stat được (quyền thư mục sai) → không kết luận được; coi như không có, báo lỗi
        return {"on": False, "error": "stat_failed"}
    code = "unknown"
    raw, _st, err = read_small(p, FLAG_MAX)
    if raw is not None:
        try:
            obj = json.loads(raw.decode("utf-8"))
            c = obj.get("reason_code") if isinstance(obj, dict) else None
            if isinstance(c, str) and c in REASON_CODES:
                code = c
        except (UnicodeDecodeError, ValueError):
            pass
    since = datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    return {"on": True, "reason_code": code, "since": since, "malformed": code == "unknown"}


def read_flags(user: str | None = None) -> dict[str, Any]:
    """Cờ ESTOP admin có hiệu lực cho `user` (mặc định user OS hiện tại): mọi user (`estop`) thắng cờ riêng (`estop.<u>`)."""
    user = user or os_user()
    out: dict[str, Any] = {"on": False, "scope": None, "reason_code": None, "since": None}
    for scope, p in (("all", flag_path(None)), ("user", flag_path(user) if USER_RE.match(user) else None)):
        if p is None:
            continue
        f = _read_flag(p)
        if f is None:
            continue
        if f.get("error"):
            out.setdefault("errors", []).append(f"{scope}:{f['error']}")
            continue
        out.update({"on": True, "scope": scope, "reason_code": f["reason_code"], "since": f["since"]})
        break
    return out


def merge_estop(es: dict[str, Any] | None, adm: dict[str, Any] | None) -> dict[str, Any]:
    """`RunManager.estop`: ESTOP của user gộp với ESTOP admin (admin bật → on, user không gỡ được)."""
    es = dict(es or {"on": False})
    if not (adm or {}).get("on"):
        return es
    return {**es, "on": True, "own_on": bool(es.get("on")), "admin": dict(adm or {}),
            "reason": f"ESTOP quản trị máy ({(adm or {}).get('reason_code') or 'unknown'}) — chỉ người quản trị máy gỡ được"}


# ============================================================================ ESTOP admin — ghi (hub admin)
def set_flag(on: bool, user: str | None, *, reason_code: str = "other", by: str | None = None) -> dict[str, Any]:
    """Bật/gỡ cờ (cần quyền ghi `control/` = nhóm cfharness-admin). Ghi nguyên tử 0644; gỡ = xoá file. Trả trạng thái mới."""
    if reason_code not in REASON_CODES:
        raise HubError(f"reason_code phải là một trong {', '.join(REASON_CODES)}")
    d = control_dir()
    if not d.is_dir():
        raise HubError(f"thư mục {d} chưa có — chạy `cfharness admin setup check` để xem lệnh tạo")
    p = flag_path(user)
    by = by or os_user()
    if on:
        payload = json.dumps({"reason_code": reason_code, "at": iso_now(), "by": by if USER_RE.match(by) else None},
                             ensure_ascii=True, sort_keys=True).encode("ascii")
        fd, tmp = tempfile.mkstemp(prefix=".estop.", suffix=".tmp", dir=str(d))
        try:
            os.fchmod(fd, 0o644)
            os.write(fd, payload)
            os.fsync(fd)
            os.close(fd)
            fd = -1
            os.replace(tmp, p)
        except BaseException:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    else:
        try:
            if os.path.isdir(p) and not os.path.islink(p):
                os.rmdir(p)
            else:
                os.unlink(p)
        except FileNotFoundError:
            pass
        except OSError as e:
            raise HubError(f"không gỡ được {p}: {e}") from None
    admin_log_append(user, "estop_on" if on else "estop_off", by=by, reason_code=reason_code if on else None)
    return {"on": on, "scope": "user" if user else "all", "user": user, "reason_code": reason_code if on else None}


def list_flags() -> list[dict[str, Any]]:
    d = control_dir()
    out: list[dict[str, Any]] = []
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return out
    for n in names:
        if n == "estop":
            user = None
        elif n.startswith("estop.") and USER_RE.match(n[6:]):
            user = n[6:]
        else:
            continue
        f = _read_flag(d / n)
        if f and f.get("on"):
            out.append({"scope": "user" if user else "all", "user": user, "reason_code": f["reason_code"], "since": f["since"]})
    return out


# ============================================================================ admin-log
def admin_log_append(user: str | None, action: str, *, by: str | None = None, reason_code: str | None = None) -> bool:
    """Ghi một dòng (best-effort — thiếu thư mục/quyền chỉ log cảnh báo). `user=None` → `_all.jsonl`."""
    d = admin_log_dir()
    rec = filter_flat({"ts": iso_now(), "by": by or os_user(), "action": action, "scope": "user" if user else "all", "user": user,
                       "reason_code": reason_code}, ADMIN_LOG_SPEC)
    if not d.is_dir():
        log.warning("admin-log: thư mục %s chưa có — bỏ qua dòng %s", d, action)
        return False
    p = d / (f"{user}.jsonl" if user else "_all.jsonl")
    try:
        fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o664)
        try:
            os.write(fd, (json.dumps(rec, ensure_ascii=True, sort_keys=True) + "\n").encode("ascii"))
        finally:
            os.close(fd)
        return True
    except OSError as e:
        log.warning("admin-log: không ghi được %s: %s", p, e)
        return False


def admin_log_read(user: str | None, *, limit: int = 100) -> list[dict[str, Any]]:
    """Dòng admin-log liên quan tới `user` (file riêng + `_all.jsonl`), mới nhất trước, đã lọc lại qua ADMIN_LOG_SPEC."""
    d = admin_log_dir()
    rows: list[dict[str, Any]] = []
    names = ["_all.jsonl"] + ([f"{user}.jsonl"] if user and USER_RE.match(user) else [])
    for n in names:
        raw, _st, err = read_small(d / n, 1024 * 1024)
        if raw is None:
            continue
        for line in raw.decode("utf-8", "replace").splitlines()[-ADMIN_LOG_MAX_LINES:]:
            try:
                r = filter_flat(json.loads(line), ADMIN_LOG_SPEC)
            except ValueError:
                continue
            if r.get("ts") and r.get("action"):
                rows.append(r)
    rows.reverse()  # dòng sau trong file = mới hơn; sort ổn định giữ thứ tự này khi trùng giây
    rows.sort(key=lambda r: r["ts"], reverse=True)
    return rows[:limit]


# ============================================================================ hub user: theo dõi cờ
class AdminControl:
    """`hub.admin_control` — kiểm cờ ESTOP admin mỗi `CHECK_S` giây + trước mỗi submit."""

    def __init__(self, hub: "Hub") -> None:
        self.hub = hub
        self._task: asyncio.Task[Any] | None = None
        self.last_check: float = 0.0
        hub.bus.add_listener(self._on_event)

    @property
    def state(self) -> dict[str, Any]:
        return dict(self.hub.store.state_get("admin_estop") or {"on": False})

    def check(self) -> dict[str, Any]:
        """Đọc cờ; đổi trạng thái → lưu hub_state, phát `hub.admin_estop`, bật: dừng run đang chạy; tắt: tick hàng đợi."""
        from .events import make_event

        cur = read_flags(os_user())
        self.last_check = time.time()
        prev = self.state
        keep = {k: cur.get(k) for k in ("on", "scope", "reason_code", "since")}
        if bool(prev.get("on")) == bool(keep["on"]) and (not keep["on"] or (prev.get("scope"), prev.get("reason_code")) == (keep["scope"], keep["reason_code"])):
            return prev
        st = {**keep, "at": iso_now()}
        self.hub.store.state_set("admin_estop", st)
        self.hub.publish(make_event("hub.admin_estop", None, {k: st.get(k) for k in ("on", "scope", "reason_code", "since")}))
        runs = self.hub.runs
        if st["on"] and not prev.get("on"):
            log.warning("ESTOP quản trị máy BẬT (%s, %s) — dừng run đang chạy", st.get("scope"), st.get("reason_code"))
            try:
                grace = float(self.hub.config.get("runs.stop_grace_s"))
                for ar in list(runs.active.values()):
                    if ar.handle is not None and ar.runtime is not None:
                        asyncio.ensure_future(ar.runtime.stop(ar.handle, grace_s=grace))
            except RuntimeError:  # không có event loop (Hub dùng đồng bộ trong test/CLI) — run sẽ bị chặn ở submit/tick
                pass
        elif not st["on"]:
            log.warning("ESTOP quản trị máy đã gỡ")
            try:
                runs.tick()
            except RuntimeError:
                pass
        return st

    def _on_event(self, ev: "HubEvent") -> None:
        try:
            if ev.type == "hub.started":
                self.check()
                self.start()
            elif ev.type == "hub.stopping":
                self.stop()
        except Exception:  # pragma: no cover
            log.exception("admin_control listener lỗi với %s", ev.type)

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
                await asyncio.sleep(CHECK_S)
                self.check()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover
                log.exception("admin_control lỗi")
                await asyncio.sleep(30)


def attach(hub: "Hub") -> None:
    hub.admin_control = AdminControl(hub)  # type: ignore[attr-defined]


def submit_gate(hub: "Hub", agent: dict[str, Any], model: str | None) -> None:
    """Gọi từ `RunManager.submit` TRƯỚC ESTOP của user: ESTOP admin (đọc lại cờ ngay) rồi chính sách root (admin_policy.py)."""
    ctl: AdminControl | None = getattr(hub, "admin_control", None)
    st = ctl.check() if ctl is not None else read_flags()
    if st.get("on"):
        raise Blocked(f"ESTOP quản trị máy đang bật (mã {st.get('reason_code') or 'unknown'}) — không khởi chạy run mới; chỉ người quản trị máy gỡ được",
                      data={"admin_estop": {k: st.get(k) for k in ("on", "scope", "reason_code", "since")}})
    from .admin_policy import submit_gate as _policy_gate

    _policy_gate(hub, agent, model)


__all__ = ["ADMIN_GROUP", "USERS_GROUP", "SHARED_ROOT", "REASON_CODES", "AdminControl", "attach", "admin_log_append", "admin_log_read",
           "control_dir", "flag_path", "is_admin", "list_flags", "merge_estop", "policy_dir", "read_flags", "require_admin", "set_flag",
           "shared_root", "submit_gate"]
