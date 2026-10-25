"""`hub.systems` — hệ thống NGOÀI hub (app trong `~/Workspace`) và job launchd (macOS) / task Task Scheduler (Windows) của chúng.

- Nhận diện: mỗi thư mục con trực tiếp của gốc (`systems.roots`, mặc định `workspace.allowed_roots`) có dấu hiệu dự án (.git,
  package.json, pyproject.toml, README/AGENTS) hoặc có job launchd trỏ vào = một hệ thống. Job gán theo `WorkingDirectory` / đường dẫn
  trong `ProgramArguments`. Plist là nguồn sự thật của job (đọc lại mỗi lần), registry chỉ giữ hệ thống NGƯỜI đã đăng ký + khoá.
- Backend job chọn theo hệ điều hành (`make_backend`): `launchd.Launchctl` (macOS) hoặc `taskscheduler.TaskScheduler` (Windows), cùng
  giao diện `jobs/state/disabled/pause/resume/restart/label_ok`. Đường dẫn so sánh theo `os.path.normcase` + `os.sep` (Windows: không
  phân biệt hoa/thường, dấu `\`).
- Registry `hub_state["systems.registry"]` = {id: {path, title, added_at, added_by, locked: [label]}}; tạm dừng do hub ở
  `hub_state["systems.paused"]` = {label: {by, at, reason}}. Không có migration.
- Ghi registry (scan --apply / add / rm / lock) = CHỈ NGƯỜI. Thao tác job (pause|resume|restart) = người hoặc điều phối viên (qua
  MANAGER_ALLOWED + `manager/permissions.check_rpc`), chỉ job thuộc hệ thống ĐÃ ĐĂNG KÝ, không khoá. Hệ thống/job của chính hub
  (repo mã nguồn, HUB_HOME, lệnh cfharness) không bao giờ được nhận diện.
"""

from __future__ import annotations

import asyncio
import os
import re
import unicodedata
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import names as _names
from ..errors import Blocked, HubError, NotFound
from ..events import make_event
from ..store import now_iso
from ..oscompat import IS_WINDOWS
from .launchd import Launchctl, is_abs_token, job_paths

if TYPE_CHECKING:
    from ..hub import Hub

REGISTRY = "systems.registry"
PAUSED = "systems.paused"
ACTIONS = ("pause", "resume", "restart")
MARKERS = (".git", "package.json", "pyproject.toml", "AGENTS.md", "README.md", "CLAUDE.md", "go.mod", "Cargo.toml")
_HUB_TOKEN = re.compile(r"(?<![\w-])" + _names.alt(_names.HUB_CLI_NAMES + _names.HUB_MODULE_NAMES) + r"(?![\w-])")
STATUS_VI = {"running": "đang chạy", "idle": "đã nạp, chờ lịch", "paused": "TẠM DỪNG", "not_loaded": "chưa nạp", "error": "lỗi lần chạy trước"}


def slug(name: str) -> str:
    s = unicodedata.normalize("NFKD", name.replace("đ", "d").replace("Đ", "D"))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9._-]+", "-", s).strip("-.")[:80] or "system"


def _title(d: Path) -> str | None:
    for fn in ("README.md", "AGENTS.md", "CLAUDE.md"):
        try:
            with (d / fn).open(encoding="utf-8", errors="replace") as f:
                for _ in range(40):
                    line = f.readline()
                    if not line:
                        break
                    if line.startswith("# "):
                        return line[2:].strip()[:120] or None
        except OSError:
            continue
    return None


def _under(p: str, root: str) -> bool:
    p, root = os.path.normcase(p), os.path.normcase(root)
    return p == root or p.startswith(root.rstrip(os.sep) + os.sep)


def make_backend(config: Any) -> Any:
    """Backend job theo hệ điều hành: Task Scheduler trên Windows, launchd trên macOS (và mặc định)."""
    timeout = float(config.get("systems.timeout_s"))
    if IS_WINDOWS:
        from .taskscheduler import TaskScheduler

        return TaskScheduler(str(config.get("systems.schtasks") or "schtasks"), str(config.get("systems.powershell") or "powershell"),
                             timeout=timeout)
    return Launchctl(str(config.get("systems.launchctl")), timeout=timeout)


class SystemService:
    def __init__(self, hub: "Hub", launchctl: Any = None) -> None:
        self.hub = hub
        self._lc = launchctl

    # ------------------------------------------------------------------ cấu hình
    @property
    def launchctl(self) -> Any:
        """Backend job (tên giữ từ bản chỉ-macOS): `Launchctl` hoặc `TaskScheduler` — xem `make_backend`."""
        if self._lc is None:
            self._lc = make_backend(self.hub.config)
        return self._lc

    def roots(self) -> list[str]:
        r = self.hub.config.get("systems.roots") or self.hub.config.get("workspace.allowed_roots") or []
        return [os.path.normpath(os.path.expanduser(str(x))) for x in r]

    def _agents_dir(self) -> Path:
        return Path(os.path.expanduser(str(self.hub.config.get("systems.launch_agents_dir"))))

    def _hub_dirs(self) -> list[str]:
        """Thư mục của CHÍNH hub (không bao giờ là hệ thống được quản lý): HUB_HOME + repo/package mã nguồn."""
        import cfharness

        pkg = Path(cfharness.__file__).resolve().parent
        out = [str(Path(self.hub.paths.home).resolve()), str(pkg)]
        for parent in pkg.parents:
            if (parent / "pyproject.toml").exists():
                out.append(str(parent))
                break
        return out

    def _is_hub_path(self, p: str) -> bool:
        rp = os.path.realpath(p)
        return any(_under(rp, h) or _under(h, rp) for h in self._hub_dirs())

    @staticmethod
    def _is_hub_job(job: dict[str, Any]) -> bool:
        return any(_HUB_TOKEN.search(os.path.basename(a) if is_abs_token(a) else a) for a in job.get("program") or [])

    # ------------------------------------------------------------------ registry
    def registry(self) -> dict[str, dict[str, Any]]:
        return dict(self.hub.store.state_get(REGISTRY) or {})

    def _save(self, reg: dict[str, dict[str, Any]]) -> None:
        self.hub.store.state_set(REGISTRY, reg)

    def paused(self) -> dict[str, dict[str, Any]]:
        return dict(self.hub.store.state_get(PAUSED) or {})

    # ------------------------------------------------------------------ nhận diện
    def _system_of(self, path: str) -> str | None:
        """Thư mục hệ thống (con trực tiếp của một gốc) chứa `path`."""
        p = os.path.normpath(path)
        for r in self.roots():
            if _under(p, r) and os.path.normcase(p) != os.path.normcase(r):
                first = p[len(r.rstrip(os.sep)) + 1:].split(os.sep, 1)[0]
                return os.path.join(r, first)
        return None

    def _jobs_by_dir(self) -> dict[str, list[dict[str, Any]]]:
        """Thư mục hệ thống (khoá `normcase`) → job trỏ vào."""
        out: dict[str, list[dict[str, Any]]] = {}
        for j in self.launchctl.jobs(self._agents_dir()):
            if self._is_hub_job(j):
                continue
            for p in job_paths(j):
                d = self._system_of(p)
                if d:
                    out.setdefault(os.path.normcase(d), []).append(j)
                    break
        return out

    def discover(self) -> list[dict[str, Any]]:
        jobs = self._jobs_by_dir()
        found: dict[str, dict[str, Any]] = {}
        for r in self.roots():
            try:
                kids = sorted(Path(r).iterdir())
            except OSError:
                continue
            for d in kids:
                if d.name.startswith((".", "_")) or not d.is_dir() or self._is_hub_path(str(d)):
                    continue
                js = jobs.get(os.path.normcase(str(d)), [])
                if not js and not any((d / m).exists() for m in MARKERS):
                    continue
                sid = slug(d.name)
                while sid in found and found[sid]["path"] != str(d):
                    sid += "-2"
                found[sid] = {"id": sid, "path": str(d), "title": _title(d) or d.name, "jobs": [j["label"] for j in js]}
        return list(found.values())

    def scan(self, *, apply: bool = False, by: str = "user") -> dict[str, Any]:
        from ..auth import require_human

        reg = self.registry()
        known_paths = {v["path"] for v in reg.values()}
        found = self.discover()
        new = [s for s in found if s["path"] not in known_paths]
        found_paths = {s["path"] for s in found}
        gone = [sid for sid, v in reg.items() if v["path"] not in found_paths]
        if apply:
            require_human("đăng ký hệ thống")
            for s in new:
                sid = s["id"]
                while sid in reg:
                    sid += "-2"
                reg[sid] = {"path": s["path"], "title": s["title"], "added_at": now_iso(), "added_by": by, "locked": []}
            self._save(reg)
        return {"roots": self.roots(), "found": found, "new": [s["id"] for s in new], "gone": gone, "applied": apply,
                "registered": len(reg)}

    def add(self, path: str, *, sid: str | None = None, by: str = "user") -> dict[str, Any]:
        from ..auth import require_human

        require_human("đăng ký hệ thống")
        p = os.path.normpath(os.path.realpath(os.path.expanduser(path)))
        if not Path(p).is_dir():
            raise NotFound(f"không có thư mục {p}")
        if self._is_hub_path(p) or os.path.dirname(p) == p or os.path.normcase(p) == os.path.normcase(os.path.expanduser("~")):
            raise Blocked(f"{p} là thư mục của chính hub / quá rộng — không đăng ký làm hệ thống")
        reg = self.registry()
        for k, v in reg.items():
            if v["path"] == p:
                return self.get(k)
        sid = slug(sid or Path(p).name)
        while sid in reg:
            sid += "-2"
        reg[sid] = {"path": p, "title": _title(Path(p)) or Path(p).name, "added_at": now_iso(), "added_by": by, "locked": []}
        self._save(reg)
        return self.get(sid)

    def remove(self, sid: str) -> dict[str, Any]:
        from ..auth import require_human

        require_human("gỡ hệ thống")
        reg = self.registry()
        if sid not in reg:
            raise NotFound(f"không có hệ thống '{sid}'")
        row = reg.pop(sid)
        self._save(reg)
        return {"id": sid, "removed": True, "path": row["path"]}

    def lock(self, label: str, on: bool = True) -> dict[str, Any]:
        """Khoá job: điều phối viên không tạm dừng/bật lại/khởi động lại được (người vẫn làm được)."""
        from ..auth import require_human

        require_human("khoá job hệ thống")
        sid, _ = self._find_job(label)
        reg = self.registry()
        locked = set(reg[sid].get("locked") or [])
        (locked.add if on else locked.discard)(label)
        reg[sid]["locked"] = sorted(locked)
        self._save(reg)
        return {"system": sid, "label": label, "locked": on}

    # ------------------------------------------------------------------ đọc
    def _jobs_for(self, path: str) -> list[dict[str, Any]]:
        return self._jobs_by_dir().get(os.path.normcase(path), [])

    def _status(self, job: dict[str, Any], st: dict[str, dict[str, Any]], dis: set[str], paused: dict[str, Any]) -> dict[str, Any]:
        s = st.get(job["label"])
        if s and (s.get("pid") or s.get("running")):  # Task Scheduler: biết đang chạy nhưng không có pid
            status = "running"
        elif s:
            status = "error" if s.get("last_exit") not in (None, 0) else "idle"
        elif job["label"] in dis or job["label"] in paused:
            status = "paused"
        else:
            status = "not_loaded"
        out = {"status": status, "status_vi": STATUS_VI[status], "pid": (s or {}).get("pid"), "last_exit": (s or {}).get("last_exit"),
               "disabled": job["label"] in dis}
        if job["label"] in paused:
            out["paused_by"] = paused[job["label"]]
        return out

    def _row(self, sid: str, v: dict[str, Any], st: dict[str, Any], dis: set[str], paused: dict[str, Any]) -> dict[str, Any]:
        jobs = []
        for j in self._jobs_for(v["path"]):
            jobs.append({"label": j["label"], "kind": j["kind"], "schedule": j["schedule"], "program": " ".join(j["program"])[:300],
                         "locked": j["label"] in (v.get("locked") or []), **self._status(j, st, dis, paused)})
        return {"id": sid, "path": v["path"], "title": v.get("title"), "added_at": v.get("added_at"), "jobs": jobs}

    def list(self) -> list[dict[str, Any]]:
        reg = self.registry()
        st, dis, paused = self.launchctl.state(), self.launchctl.disabled(), self.paused()
        return [self._row(sid, v, st, dis, paused) for sid, v in sorted(reg.items())]

    def get(self, sid: str, *, log_bytes: int | None = None) -> dict[str, Any]:
        reg = self.registry()
        if sid not in reg:
            raise NotFound(f"không có hệ thống '{sid}' (đã đăng ký: {', '.join(sorted(reg)) or 'chưa có — `cfharness system scan --apply`'})")
        row = self._row(sid, reg[sid], self.launchctl.state(), self.launchctl.disabled(), self.paused())
        if log_bytes:
            by_label = {j["label"]: j for j in self._jobs_for(reg[sid]["path"])}
            for j in row["jobs"]:
                src = by_label.get(j["label"]) or {}
                j["log"] = {k: self._tail(src.get(k), log_bytes) for k in ("stdout", "stderr") if src.get(k)}
                if not src.get("stdout") and not src.get("stderr"):
                    j["log_note"] = getattr(self.launchctl, "no_log_note", None) or "job không khai báo StandardOutPath/StandardErrorPath — không có log để đọc"
                elif not any(j["log"].values()):
                    paths = sorted({p for p in (src.get("stdout"), src.get("stderr")) if p})
                    j["log_note"] = f"chưa có log (file trống hoặc chưa tồn tại): {', '.join(paths)}"
        return row

    @staticmethod
    def _tail(path: str | None, n: int) -> str | None:
        from ..redact import redact_str

        if not path:
            return None
        try:
            p = Path(path)
            if not p.is_file():
                return None
            with p.open("rb") as f:
                size = f.seek(0, 2)
                f.seek(max(0, size - n))
                data = f.read(n).decode("utf-8", errors="replace")
        except OSError:
            return None
        if len(data) >= n and "\n" in data:
            data = data.split("\n", 1)[1]
        return redact_str(data)

    # ------------------------------------------------------------------ thao tác job
    def _find_job(self, label: str) -> tuple[str, dict[str, Any]]:
        if not self.launchctl.label_ok(str(label or "")):
            raise HubError(f"label job không hợp lệ: {label!r}", code=-32602)
        for sid, v in self.registry().items():
            for j in self._jobs_for(v["path"]):
                if j["label"] == label:
                    return sid, j
        raise NotFound(f"job '{label}' không thuộc hệ thống nào đã đăng ký (xem `cfharness system list`)")

    def job_check(self, label: str, action: str, *, manager: bool) -> str | None:
        """Lý do từ chối hoặc None (dùng chung cho service và `manager/permissions.check_rpc`)."""
        if action not in ACTIONS:
            return f"action phải là {'|'.join(ACTIONS)}"
        try:
            sid, _ = self._find_job(label)
        except HubError as e:
            return e.message
        if manager and label in (self.registry()[sid].get("locked") or []):
            return f"job '{label}' bị người khoá — chỉ người thao tác (`cfharness system unlock {label}`)"
        return None

    async def job(self, label: str, action: str, *, by: str = "user", reason: str | None = None) -> dict[str, Any]:
        from ..auth import current_caller

        c = current_caller()
        is_mgr = bool(c is not None and c.manager)
        if c is not None and not c.human and not is_mgr:
            raise Blocked("thao tác job hệ thống: chỉ người hoặc điều phối viên")
        why = self.job_check(label, action, manager=is_mgr)
        if why:
            raise Blocked(why) if "khoá" in why else HubError(why)
        sid, j = self._find_job(label)
        lc = self.launchctl
        if action == "pause":
            ok, msg = await asyncio.to_thread(lc.pause, label)
        elif action == "resume":
            ok, msg = await asyncio.to_thread(lc.resume, label, j["plist"])
        else:
            if label in self.paused():
                raise HubError(f"job '{label}' đang tạm dừng — bật lại (resume) trước khi khởi động lại")
            ok, msg = await asyncio.to_thread(lc.restart, label)
        if ok:
            p = self.paused()
            if action == "pause":
                p[label] = {"by": by, "at": now_iso(), "reason": (reason or "")[:300], "system": sid}
            elif action == "resume":
                p.pop(label, None)
            self.hub.store.state_set(PAUSED, p)
        self.hub.publish(make_event("system.job", None, {"system": sid, "label": label, "action": action, "ok": ok, "by": by,
                                                        "reason": (reason or "")[:300], "detail": msg}))
        if not ok:
            raise HubError(f"{action} {label} thất bại: {msg}")
        st = self._status(j, lc.state(), lc.disabled(), self.paused())
        return {"system": sid, "label": label, "action": action, "ok": True, "detail": msg, **st}
