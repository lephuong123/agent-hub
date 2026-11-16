"""`hub.filehub` — thư mục dùng chung người ↔ agent trên "Không gian số" (docs/dev-notes/filehub.md, plan-cloud-files.md §3, §8.4).

- Cấu hình ở `config.yaml` nhánh `filehub.*` (∈ `integrity.SECURITY_KEYS`; chỉ daemon ghi theo lệnh người). Danh sách thư mục chung
  `filehub.folders = [{name, root_id, ws_id, local_path}]` (v1 dùng 1, code sẵn cho N).
- Trạng thái đồng bộ đọc từ `HUB_HOME/filehub/status.json` (binary sync ghi nguyên tử) → `idle|syncing|conflict|offline|auth_expired|error`
  (+ `paused`, `not_configured`, `version_mismatch` do hub đặt). Đổi trạng thái / bản xung đột mới / chiều đẩy bị chặn → event.
- Link "Mở trên Không gian số": ánh xạ đường dẫn → id từ `<profile>/index/<rootId>.json` (chỉ đọc) → `<server>/files/<folder_id>?file=<file_id>`.
- Presence: run GHI vào thư mục chung bắt đầu/kết thúc → `control.json` `{"presence": {rootId: {label, ttl_s} | null}}` (binary đẩy lên
  Không gian số — cfharness không cầm token). Làm mới trước khi hết `ttl_s`.
- Steer: `recent` có thay đổi chiều `down` trong khi một run đang giữ khoá gốc thư mục → `steer(by="hub")` câu cố định (tiếng Anh).
  `signals` bỏ qua steer `by=="hub"` (không phải correction của người).
- Sau run trên workspace `filehub:`: xoá → `filehub.deleted` (không hỏi duyệt — §7.3), đổi → `filehub.changed`, tệp mới/sửa chứa GIÁ TRỊ
  secret hub biết (`redact`) → `filehub.secret_warning` (chỉ cảnh báo, tệp đã lên cloud).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..errors import Conflict, HubError, NotFound
from ..events import TERMINAL_RUN_EVENTS, HubEvent, make_event
from ..oscompat import no_window_kwargs
from .supervisor import SyncSupervisor, child_env

if TYPE_CHECKING:
    from ..hub import Hub

log = logging.getLogger("cfharness.filehub")

REF_PREFIX = "filehub:"
STATUS_MAX_BYTES = 4_000_000
STATES = ("idle", "syncing", "conflict", "offline", "auth_expired", "error", "paused", "not_configured", "version_mismatch", "starting")
STEER_TEXT = "[cfharness] The human changed {rel} on Không gian số while you were working. Re-Read it before editing."
IGNORE_BASE = (".git/", "node_modules/", ".DS_Store", "Thumbs.db", "*.tmp", "*.swp", "~$*", ".~lock.*", ".tangthu*",
               # tệp nội bộ của claude CLI trong cwd của run (vd `.claude/.cc-writes`, settings.local.json) — không bao giờ lên cloud
               ".claude/", ".claude.json", ".cc-writes")
DEVICE_NAME_MAX = 40
META_PREFIX = ".tangthu"


def _real(p: str | Path) -> str:
    return os.path.realpath(os.path.expanduser(str(p)))


def _under(p: str, base: str) -> bool:
    p, base = p.rstrip("/"), base.rstrip("/")
    return p == base or p.startswith(base + "/")


def config_folders(config: Any) -> list[dict[str, Any]]:
    try:
        v = config.get("filehub.folders") or []
    except KeyError:
        return []
    return [dict(f) for f in v if isinstance(f, dict) and f.get("name") and f.get("local_path")]


def local_roots(hub: Any) -> list[str]:
    """Gốc bản sao cục bộ (realpath) của mọi thư mục chung đã cấu hình — dùng cho guard/sandbox/khoá/validate_workspace.
    Đọc thẳng cấu hình (không cần `hub.filehub`) để mọi nơi gọi được, kể cả khi service chưa gắn."""
    try:
        if not hub.config.get("filehub.enabled"):
            return []
    except (KeyError, AttributeError):
        return []
    return list(dict.fromkeys(_real(f["local_path"]) for f in config_folders(hub.config)))


def root_of(hub: Any, path: str) -> str | None:
    rp = _real(path)
    for r in local_roots(hub):
        if _under(rp, r):
            return r
    return None


def _is_ip(s: str) -> bool:
    import ipaddress

    try:
        ipaddress.ip_address(s)
        return True
    except ValueError:
        return False


def friendly_host(host: str) -> str:
    """Tên máy từ hostname: IP giữ nguyên (không cắt ở dấu chấm), `may.local`/FQDN → phần đầu; giới hạn độ dài."""
    h = (host or "").strip()
    if not h:
        return "máy"
    if not _is_ip(h):
        h = h.split(".")[0] or h
    return h[:DEVICE_NAME_MAX]


_MACHINE: str | None = None


def machine_name() -> str:
    """Tên máy thân thiện cho thiết bị sync: macOS `scutil --get ComputerName` (vd "MacBook của An"), không có → hostname."""
    global _MACHINE
    if _MACHINE is not None:
        return _MACHINE
    name = ""
    if sys.platform == "darwin":
        try:
            r = subprocess.run(["scutil", "--get", "ComputerName"], capture_output=True, text=True, timeout=3, **no_window_kwargs())
            if r.returncode == 0:
                name = " ".join(r.stdout.split())[:DEVICE_NAME_MAX]
        except (OSError, subprocess.TimeoutExpired):
            name = ""
    _MACHINE = name or friendly_host(socket.gethostname())
    return _MACHINE


def valid_name(name: str) -> bool:
    return (bool(name) and len(name) <= 80 and all(c.isalnum() or c in " -_." for c in name) and name.strip() == name
            and name not in (".", "..") and not name.startswith("."))


class FilehubService:
    def __init__(self, hub: "Hub") -> None:
        self.hub = hub
        self.supervisor = SyncSupervisor(self)
        self._task: asyncio.Task[None] | None = None
        self._last_state: str | None = None
        self._seen_recent: set[str] = set()
        self._recent_primed = False
        self._seen_conflicts: set[str] = set()
        self._blocked: dict[str, str] = {}
        self._steered: set[tuple[str, str]] = set()
        self._presence: dict[str, dict[str, str]] = {}  # root_id → {run_id: label}
        self._presence_written = 0.0
        self._resume_push: list[str] = []
        self.login_state: dict[str, Any] = {"state": "none"}
        self._login_proc: asyncio.subprocess.Process | None = None
        self._bg: set[asyncio.Task[Any]] = set()
        hub.bus.add_listener(self._on_event)

    # ------------------------------------------------------------------ cấu hình / đường dẫn
    def cfg(self, key: str) -> Any:
        try:
            return self.hub.config.get(f"filehub.{key}")
        except KeyError:
            return None

    @property
    def dir(self) -> Path:
        return self.hub.paths.home / "filehub"

    @property
    def profile_dir(self) -> Path:
        return self.dir / "profile"

    @property
    def status_path(self) -> Path:
        return self.dir / "status.json"

    @property
    def control_path(self) -> Path:
        return self.dir / "control.json"

    @property
    def ignore_path(self) -> Path:
        return self.dir / "ignore.txt"

    @property
    def tokens_path(self) -> Path:
        return self.hub.paths.home / "secrets" / "filehub" / "tokens.json"

    def device_name(self) -> str:
        return str(self.cfg("device_name") or f"cfharness · {machine_name()}")[:64]

    def folders(self) -> list[dict[str, Any]]:
        return config_folders(self.hub.config)

    def enabled(self) -> bool:
        return bool(self.cfg("enabled"))

    def configured(self) -> bool:
        return self.enabled() and bool(self.cfg("sync_binary")) and bool(self.cfg("server")) and bool(self.folders()) and not self.paused()

    def paused(self) -> bool:
        return bool(self.hub.store.state_get("filehub.paused"))

    def folder(self, name: str) -> dict[str, Any]:
        for f in self.folders():
            if f["name"] == name:
                return f
        raise NotFound(f"không có thư mục chung '{name}' (cfharness filehub status)")

    def folder_by_root(self, root_id: str | None) -> dict[str, Any] | None:
        return next((f for f in self.folders() if f.get("root_id") == root_id), None)

    def folder_of_path(self, path: str) -> dict[str, Any] | None:
        rp = _real(path)
        for f in self.folders():
            if _under(rp, _real(f["local_path"])):
                return f
        return None

    def pending_add(self) -> dict[str, Any] | None:
        st = self.read_status() or {}
        have = {r.get("id") for r in st.get("roots") or [] if isinstance(r, dict)}
        return next((f for f in self.folders() if f.get("root_id") and f["root_id"] not in have), None)

    # ------------------------------------------------------------------ tệp phụ
    def prepare_files(self) -> None:
        for d in (self.dir, self.profile_dir, self.tokens_path.parent):
            d.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(d, 0o700)
            except OSError:  # pragma: no cover
                pass
        self.write_ignore()
        if not self.control_path.exists():
            self._write_control()

    def ignore_lines(self) -> list[str]:
        """Mẫu bỏ qua chiều đẩy lên (kiểu .gitignore): rác/tạm/.git + tên bí mật của `files.secret_patterns` (trừ `secret_allow`)."""
        from ..fileview import settings as _file_settings

        try:
            st = _file_settings(self.hub.config.get("files"))
        except Exception:  # noqa: BLE001
            st = _file_settings(None)
        out = list(IGNORE_BASE) + [str(p) for p in st["secret_patterns"]] + [f"!{p}" for p in st["secret_allow"]]
        return list(dict.fromkeys(out + [str(x) for x in self.cfg("extra_ignore") or []]))

    def write_ignore(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.ignore_path.with_suffix(".tmp")
        tmp.write_text("# sinh tự động bởi cfharness (filehub/service.py) — sửa qua filehub.extra_ignore\n" + "\n".join(self.ignore_lines()) + "\n",
                       encoding="utf-8")
        os.replace(tmp, self.ignore_path)

    def _write_control(self) -> None:
        ttl = int(self.cfg("presence_ttl_s") or 600)
        pres: dict[str, Any] = {}
        for f in self.folders():
            rid = f.get("root_id")
            if not rid:
                continue
            labels = list((self._presence.get(rid) or {}).values())
            pres[rid] = {"label": (", ".join(dict.fromkeys(labels)))[:80], "ttl_s": ttl} if labels else None
        data = {"presence": pres, "resume_push": list(self._resume_push)}
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.control_path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(tmp, self.control_path)
        self._presence_written = time.time()

    # ------------------------------------------------------------------ đọc status / index
    def read_status(self) -> dict[str, Any] | None:
        try:
            if self.status_path.stat().st_size > STATUS_MAX_BYTES:
                return None
            d = json.loads(self.status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return d if isinstance(d, dict) else None

    def read_index(self, root_id: str) -> dict[str, Any]:
        if not root_id or "/" in root_id or root_id.startswith("."):
            return {}
        p = self.profile_dir / "index" / f"{root_id}.json"
        try:
            if p.stat().st_size > 50_000_000:
                return {}
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return d if isinstance(d, dict) else {}

    def overall_state(self, st: dict[str, Any] | None) -> str:
        if not self.enabled() or not self.folders():
            return "not_configured"
        if self.paused():
            return "paused"
        sv = self.supervisor.state
        if sv in ("version_mismatch",):
            return "version_mismatch"
        if sv in ("error",) or (sv == "backoff" and self.supervisor.restarts >= 3):
            return "error"
        if st is None:
            return "starting" if sv in ("starting", "running", "backoff", "stopped") else "offline"
        s = str(st.get("state") or "idle")
        if s not in STATES:
            s = "error"
        if s in ("idle", "syncing") and any((r or {}).get("conflicts") for r in st.get("roots") or [] if isinstance(r, dict)):
            return "conflict"
        return s

    # ------------------------------------------------------------------ link
    def server(self) -> str:
        return str(self.cfg("server") or "").rstrip("/")

    def link_for(self, path: str) -> dict[str, Any] | None:
        f = self.folder_of_path(path)
        if f is None or not f.get("root_id"):
            return None
        base = _real(f["local_path"])
        rp = _real(path)
        rel = os.path.relpath(rp, base).replace(os.sep, "/")
        idx = self.read_index(f["root_id"])
        items = idx.get("items") if isinstance(idx.get("items"), dict) else {}
        root_folder = idx.get("root_folder_id")
        srv = self.server()
        if rel in (".", ""):
            return {"folder": f["name"], "rel": "", "url": f"{srv}/files/{root_folder}" if root_folder else None}
        it = items.get(rel) if isinstance(items, dict) else None
        if not isinstance(it, dict):
            return {"folder": f["name"], "rel": rel, "url": None}
        if it.get("file_id"):
            url = f"{srv}/files/{it.get('parent_folder_id') or root_folder}?file={it['file_id']}"
        else:
            url = f"{srv}/files/{it.get('folder_id')}"
        return {"folder": f["name"], "rel": rel, "url": url if srv else None}

    def link(self, p: dict[str, Any]) -> dict[str, Any]:
        """RPC `filehub.link {path, run_id?|agent_id?}` — đường dẫn tương đối được hiểu theo cwd/diff của run (như `file.read`)."""
        path = str(p.get("path") or "")
        if not path:
            raise HubError("thiếu tham số 'path'", code=-32602)
        cands = [path] if os.path.isabs(os.path.expanduser(path)) else []
        rid = p.get("run_id")
        if rid and not cands:
            run = self.hub.store.find_run(str(rid))
            if run is None:
                raise NotFound(f"không tìm thấy run '{rid}'")
            for base in ((run.get("diff") or {}).get("root"), run.get("cwd")):
                if base:
                    cands.append(os.path.join(str(base), path))
        for c in cands:
            res = self.link_for(c)
            if res is not None:
                if not res.get("url"):
                    raise NotFound("chưa có ánh xạ id cho tệp này (đồng bộ chưa xong?)")
                return res
        raise NotFound("tệp không nằm trong thư mục chung")

    # ------------------------------------------------------------------ workspace `filehub:<tên>[/<con>]`
    def resolve_ref(self, ref: str, *, must_exist: bool = True) -> tuple[str, dict[str, Any], str]:
        """`filehub:<tên>[/<đường dẫn con>]` → (đường dẫn cục bộ tuyệt đối, thư mục, đường dẫn con). Không `..`, không thoát gốc (symlink)."""
        if not isinstance(ref, str) or not ref.startswith(REF_PREFIX):
            raise HubError(f"workspace phải dạng filehub:<tên>[/<thư mục con>]: {ref!r}")
        body = ref[len(REF_PREFIX):].strip().strip("/")
        name, _, sub = body.partition("/")
        if not self.enabled():
            raise HubError("thư mục chung (filehub) chưa bật — `cfharness filehub setup`")
        f = self.folder(name)
        parts = [x for x in sub.split("/") if x not in ("", ".")]
        if any(x == ".." for x in parts) or any(x.startswith(META_PREFIX) for x in parts):
            raise HubError(f"đường dẫn con không hợp lệ: {sub!r}")
        base = _real(f["local_path"])
        target = os.path.join(base, *parts) if parts else base
        rt = _real(target)
        if not _under(rt, base):
            raise HubError(f"'{ref}' thoát ra ngoài thư mục chung (symlink?)")
        if must_exist and not os.path.isdir(rt):
            raise HubError(f"thư mục {rt} chưa có trên máy (đồng bộ chưa xong, hoặc chưa tạo trên Không gian số)")
        return rt, f, "/".join(parts)

    # ------------------------------------------------------------------ trạng thái
    def status(self) -> dict[str, Any]:
        st = self.read_status()
        roots = {r.get("id"): r for r in (st or {}).get("roots") or [] if isinstance(r, dict)}
        out_f = []
        for f in self.folders():
            r = roots.get(f.get("root_id")) or {}
            idx = self.read_index(f.get("root_id") or "") if f.get("root_id") else {}
            out_f.append({
                "name": f["name"], "root_id": f.get("root_id"), "local_path": f["local_path"], "state": r.get("state") or ("pending" if f.get("root_id") else "no_root"),
                "pending_up": r.get("pending_up"), "pending_down": r.get("pending_down"), "blocked": r.get("blocked"),
                "conflicts": [{"path": str(c.get("path"))[:300], "created_at": c.get("created_at")} for c in (r.get("conflicts") or [])[:50] if isinstance(c, dict)],
                "last_error": r.get("last_error"),
                "url": f"{self.server()}/files/{idx['root_folder_id']}" if idx.get("root_folder_id") and self.server() else None,
                "lock": self.hub.runs.locks.holder(_real(f["local_path"])),
            })
        recent = [{k: e.get(k) for k in ("ts", "root_id", "dir", "op", "path")} for e in ((st or {}).get("recent") or [])[-20:] if isinstance(e, dict)]
        return {
            "enabled": self.enabled(), "server": self.server() or None, "device": self.device_name(), "state": self.overall_state(st),
            "paused": self.paused(), "supervisor": self.supervisor.info(), "sync_version": (st or {}).get("version") or self.supervisor.version,
            "updated_at": (st or {}).get("updated_at"), "folders": out_f, "recent": recent,
            "login": {k: v for k, v in self.login_state.items() if k in ("state", "url", "roots", "error", "started_at", "finished_at")},
            "tokens": self.tokens_path.exists(), "local_parent": str(self.cfg("local_parent") or ""),
            "relogin_after_import": bool(self.hub.store.state_get("import_filehub")),
        }

    def list(self) -> list[dict[str, Any]]:
        """RPC `filehub.list` (đọc — điều phối viên dùng để giao việc): tên thư mục chung + workspace + đường dẫn cục bộ."""
        return [{"name": f["name"], "workspace": f"{REF_PREFIX}{f['name']}", "local_path": f["local_path"], "root_id": f.get("root_id")} for f in self.folders()]

    # ------------------------------------------------------------------ vòng nền
    def start(self) -> None:
        if not self.enabled():
            return
        try:
            self.prepare_files()
            self.supervisor.reconcile()
        except Exception:  # pragma: no cover
            log.exception("filehub: không chuẩn bị được thư mục")
        if self.configured():
            self.supervisor.start()
        if self._task is None or self._task.done():
            try:
                self._task = asyncio.get_running_loop().create_task(self._loop())
            except RuntimeError:
                self._task = None

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        if self._login_proc is not None and self._login_proc.returncode is None:
            try:
                self._login_proc.kill()
            except ProcessLookupError:  # pragma: no cover
                pass
        await self.supervisor.stop()

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(max(0.05, float(self.cfg("tick_s") or 1.0)))
                self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover
                log.exception("filehub tick lỗi")

    def on_supervisor_state(self) -> None:
        try:
            self.tick()
        except Exception:  # pragma: no cover
            log.exception("filehub tick lỗi")

    def tick(self) -> None:
        st = self.read_status()
        state = self.overall_state(st)
        # syncing↔idle là nhịp thường của đồng bộ — không phát/ghi event mỗi vòng; chỉ khi vào/ra trạng thái có ý nghĩa
        # (offline, auth_expired, error, conflict, paused, version_mismatch…). Trạng thái thật vẫn có ở filehub.status.
        state = "idle" if state == "syncing" else state
        if state != self._last_state:
            prev, self._last_state = self._last_state, state
            if prev is not None or state not in ("not_configured", "starting"):
                self.hub.publish(make_event("filehub.state", None, {"state": state, "prev": prev, "error": self.supervisor.last_error if state in ("error", "version_mismatch") else None},
                                            source="hub"))
        if st is None:
            return
        for r in st.get("roots") or []:
            if not isinstance(r, dict):
                continue
            f = self.folder_by_root(r.get("id"))
            fname = f["name"] if f else str(r.get("name") or r.get("id"))[:80]
            for c in r.get("conflicts") or []:
                if not isinstance(c, dict):
                    continue
                key = f"{r.get('id')}|{c.get('path')}|{c.get('created_at')}"
                if key in self._seen_conflicts:
                    continue
                self._seen_conflicts.add(key)
                self.hub.publish(make_event("filehub.conflict", None, {"folder": fname, "root_id": r.get("id"), "path": str(c.get("path"))[:300]}, source="hub"))
            b = r.get("blocked")
            if b and self._blocked.get(str(r.get("id"))) != b:
                self.hub.publish(make_event("filehub.blocked", None, {"folder": fname, "root_id": r.get("id"), "reason": str(b)[:40]}, source="hub"))
            if b:
                self._blocked[str(r.get("id"))] = str(b)
            else:
                self._blocked.pop(str(r.get("id")), None)
                if r.get("id") in self._resume_push:
                    self._resume_push.remove(r.get("id"))
                    self._write_control()
        self._handle_recent(st.get("recent") or [])
        ttl = float(self.cfg("presence_ttl_s") or 600)
        if any(self._presence.values()) and time.time() - self._presence_written > ttl / 2:
            self._write_control()

    def _handle_recent(self, recent: list[Any]) -> None:
        keys = []
        for e in recent:
            if not isinstance(e, dict):
                continue
            k = f"{e.get('ts')}|{e.get('root_id')}|{e.get('dir')}|{e.get('op')}|{e.get('path')}"
            keys.append((k, e))
        if not self._recent_primed:  # lần đầu: thay đổi cũ không phải "trong lúc run chạy"
            self._seen_recent = {k for k, _ in keys}
            self._recent_primed = True
            return
        for k, e in keys:
            if k in self._seen_recent:
                continue
            self._seen_recent.add(k)
            if e.get("dir") == "down":
                self._cloud_change(e)
        if len(self._seen_recent) > 2000:
            self._seen_recent = {k for k, _ in keys}

    def _cloud_change(self, e: dict[str, Any]) -> None:
        if not self.cfg("steer_on_cloud_change"):
            return
        f = self.folder_by_root(e.get("root_id"))
        if f is None:
            return
        holder = self.hub.runs.locks.holder(_real(f["local_path"]))
        rid = (holder or {}).get("run_id")
        if not rid:
            return
        run = self.hub.store.get_run(rid)
        if run is None or run.get("status") != "running":
            return
        from ..termsafe import one_line

        rel = one_line(str(e.get("path") or ""), 200)
        if (rid, rel) in self._steered:
            return
        self._steered.add((rid, rel))
        text = STEER_TEXT.format(rel=rel)
        self._spawn(self._steer(rid, text))

    async def _steer(self, run_id: str, text: str) -> None:
        from ..steer import steer

        try:
            await steer(self.hub, run_id, text, by="hub", channel="hub")
        except HubError as ex:
            log.info("filehub: không steer được run %s: %s", run_id, ex)

    def _spawn(self, coro: Any) -> None:
        try:
            t = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()
            return
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    # ------------------------------------------------------------------ listener bus: presence + báo sau run
    def _run_folder(self, run: dict[str, Any]) -> dict[str, Any] | None:
        name = (run.get("run_opts") or {}).get("filehub")
        if not name:
            return None
        try:
            return self.folder(str(name))
        except NotFound:
            return None

    def _on_event(self, ev: HubEvent) -> None:
        try:
            if ev.type == "hub.started":
                self.start()
            elif ev.type == "run.started" and ev.run_id:
                run = self.hub.store.get_run(ev.run_id) or {}
                f = self._run_folder(run)
                if f is not None and f.get("root_id") and not (run.get("run_opts") or {}).get("read_only"):
                    self._presence.setdefault(f["root_id"], {})[ev.run_id] = f"cfharness · {run.get('agent_id')}"
                    self._write_control()
            elif ev.type in TERMINAL_RUN_EVENTS and ev.source == "hub" and ev.run_id and ev.type != "run.waiting_human":
                changed = False
                for rid, runs in self._presence.items():
                    if runs.pop(ev.run_id, None) is not None:
                        changed = True
                if changed:
                    self._write_control()
                self._steered = {x for x in self._steered if x[0] != ev.run_id}
            elif ev.type == "workspace.diff" and ev.run_id:
                self.after_run(ev.run_id)
        except Exception:  # pragma: no cover — listener không được làm hỏng bus
            log.exception("filehub listener lỗi với %s", ev.type)

    def after_run(self, run_id: str) -> dict[str, Any] | None:
        """Sau diff của run trên workspace `filehub:` → báo xoá / thay đổi / tệp có thể chứa bí mật (chỉ cảnh báo)."""
        run = self.hub.store.get_run(run_id) or {}
        f = self._run_folder(run)
        diff = run.get("diff")
        if f is None or not isinstance(diff, dict) or not diff.get("root"):
            return None
        base = _real(f["local_path"])
        droot = str(diff["root"])
        deleted, changed, secret = [], [], []
        max_b = int(self.cfg("secret_scan_max_bytes") or 5_000_000)
        from ..redact import contains_secret_value

        for item in diff.get("files") or []:
            ap = os.path.join(droot, str(item.get("path") or ""))
            rel = os.path.relpath(_real(ap) if os.path.exists(ap) else os.path.abspath(ap), base).replace(os.sep, "/")
            if rel.startswith(".."):
                continue
            if item.get("status") == "deleted":
                deleted.append(rel)
                continue
            changed.append(rel)
            try:
                if os.path.isfile(ap) and not os.path.islink(ap) and os.path.getsize(ap) <= max_b:
                    with open(ap, "rb") as fh:
                        if contains_secret_value(fh.read()):
                            secret.append(rel)
            except OSError:
                continue
        link = self.link_for(droot) or {}
        url = link.get("url")
        common = {"folder": f["name"], "run_id": run_id, "url": url}
        if changed:
            ev = make_event("filehub.changed", run.get("agent_id"), {**common, "count": len(changed), "paths": changed[:20]}, run_id=run_id, source="hub")
            self.hub.publish(ev)
        if deleted:
            self.hub.publish(make_event("filehub.deleted", run.get("agent_id"), {**common, "count": len(deleted), "paths": deleted[:20]}, run_id=run_id, source="hub"))
        if secret:
            self.hub.publish(make_event("filehub.secret_warning", run.get("agent_id"), {**common, "count": len(secret), "paths": secret[:20]}, run_id=run_id, source="hub"))
        return {"changed": changed, "deleted": deleted, "secret": secret}

    # ------------------------------------------------------------------ thao tác của người (RPC ghi, HUMAN_ONLY)
    def _validate_local(self, local: str, *, exclude: str | None = None) -> str:
        p = Path(os.path.expanduser(local))
        if not p.is_absolute():
            raise HubError(f"đường dẫn bản sao cục bộ phải tuyệt đối: {local!r}")
        rp = _real(p)
        hh = _real(self.hub.paths.home)
        if _under(rp, hh) or _under(hh, rp):
            raise HubError(f"bản sao cục bộ '{rp}' không được nằm trong/chứa HUB_HOME ({hh})", code=-32003)
        uh = _real(os.path.expanduser("~"))
        if rp in (uh, "/") or rp == os.path.dirname(uh):
            raise HubError(f"bản sao cục bộ '{rp}' quá rộng — chọn một thư mục riêng (vd ~/KhongGianSo/<tên>)")
        for f in self.folders():
            if f["name"] == exclude:
                continue
            o = _real(f["local_path"])
            if _under(rp, o) or _under(o, rp):
                raise HubError(f"bản sao cục bộ '{rp}' trùng/chứa thư mục chung '{f['name']}' ({o})")
        from .. import confine as _confine

        conf = _confine.of_hub(self.hub)
        if conf is not None:  # P2: phải trong W
            if not _under(rp, conf.root):
                raise HubError(f"bản sao cục bộ '{rp}' nằm ngoài workspace được giam ({conf.root}) — workspace.confine (chỉ người đổi)", code=-32003)
        from ..guard.runtime import fixed_protected_dirs

        sb = (self.hub.config.get("guard") or {}).get("sandbox") or {}
        for region in fixed_protected_dirs(self.hub.paths.home, self.hub.paths.socket, sb):
            if "*" in region:
                continue
            r = _real(region)
            if _under(rp, r) or _under(r, rp):
                raise HubError(f"bản sao cục bộ '{rp}' trùng/chứa vùng bảo vệ ({region})", code=-32003)
        return rp

    def add(self, root_id: str, name: str | None = None, *, ws_id: str | None = None, local_path: str | None = None) -> dict[str, Any]:
        root_id = str(root_id or "").strip()
        if not root_id or not all(c.isalnum() or c in "-_" for c in root_id):
            raise HubError(f"root_id không hợp lệ: {root_id!r}")
        name = str(name or "").strip() or next((r["name"] for r in self.login_state.get("roots") or [] if r.get("id") == root_id), "") or root_id
        if not valid_name(name):
            raise HubError(f"tên thư mục chung không hợp lệ: {name!r} (chữ, số, khoảng trắng, - _ ., ≤ 80 ký tự)")
        if any(f["name"] == name or f.get("root_id") == root_id for f in self.folders()):
            raise Conflict(f"thư mục chung '{name}' (root {root_id}) đã có")
        ws_id = ws_id or next((r.get("ws_id") for r in self.login_state.get("roots") or [] if r.get("id") == root_id), None)
        parent = os.path.expanduser(str(self.cfg("local_parent") or "~/KhongGianSo"))
        local = self._validate_local(local_path or os.path.join(parent, name))
        if not os.path.isdir(local):
            os.makedirs(local, exist_ok=True)
        folders = self.folders() + [{"name": name, "root_id": root_id, "ws_id": ws_id, "local_path": local}]
        self.hub.config.set("filehub.folders", folders)
        if not self.enabled():
            self.hub.config.set("filehub.enabled", True)
        self._apply_change()
        return {"name": name, "root_id": root_id, "local_path": local, "workspace": f"{REF_PREFIX}{name}"}

    def remove(self, name: str) -> dict[str, Any]:
        f = self.folder(name)
        holder = self.hub.runs.locks.holder(_real(f["local_path"]))
        if holder:
            raise Conflict(f"thư mục chung '{name}' đang có run {holder.get('run_id')} ghi — dừng run trước")
        self.hub.config.set("filehub.folders", [x for x in self.folders() if x["name"] != name])
        self._apply_change()
        return {"removed": name, "local_path": f["local_path"], "note": "tệp trên máy và trên Không gian số được giữ nguyên"}

    def pause(self) -> dict[str, Any]:
        self.hub.store.state_set("filehub.paused", True)
        self._spawn(self.supervisor.stop())
        self.tick()
        return {"paused": True}

    def resume(self, root_id: str | None = None) -> dict[str, Any]:
        if root_id:  # chiều đẩy lên bị chặn (xoá hàng loạt) → báo binary đẩy tiếp
            if self.folder_by_root(root_id) is None:
                raise NotFound(f"không có thư mục chung với root {root_id}")
            if root_id not in self._resume_push:
                self._resume_push.append(root_id)
            self._write_control()
            return {"resume_push": root_id}
        self.hub.store.state_set("filehub.paused", False)
        self._apply_change()
        return {"paused": False, "running": self.configured()}

    def _apply_change(self) -> None:
        if not self.configured():
            self._spawn(self.supervisor.stop())
            self.tick()
            return
        self.prepare_files()
        self._write_control()
        if self.supervisor._task is None or self.supervisor._task.done():
            self.supervisor.start()
        else:
            self.supervisor.restart()
        if self._task is None or self._task.done():
            self.start()

    def setup(self, p: dict[str, Any]) -> dict[str, Any]:
        """Cấu hình + đăng nhập (đồng ý thiết bị, thư mục rỗng tạo ở trang đồng ý) — xong thì tự thêm thư mục chung đầu tiên."""
        server = str(p.get("server") or self.cfg("server") or "").strip().rstrip("/")
        if not (server.startswith("https://") or server.startswith("http://127.0.0.1") or server.startswith("http://localhost")):
            raise HubError("server phải là URL https:// của Không gian số (vd https://thumucai.trungtamdhs.com)")
        binary = os.path.expanduser(str(p.get("binary") or self.cfg("sync_binary") or "").strip())
        if not binary or not os.path.isabs(binary) or not os.path.isfile(binary) or not os.access(binary, os.X_OK):
            raise HubError(f"không tìm thấy binary sync chạy được: {binary!r} (--binary /đường/dẫn/tang-thu-sync-headless)")
        self.hub.config.set("filehub.server", server)
        self.hub.config.set("filehub.sync_binary", binary)
        if p.get("local_parent"):
            self.hub.config.set("filehub.local_parent", str(p["local_parent"]))
        self.hub.config.set("filehub.enabled", True)
        name = str(p.get("name") or "cfharness").strip()
        if not valid_name(name):
            raise HubError(f"tên thư mục không hợp lệ: {name!r}")
        return {"server": server, "binary": binary, "name": name}

    async def login(self, *, root_hint: str | None = None, no_browser: bool = False, auto_add: bool = False,
                    local_path: str | None = None) -> dict[str, Any]:
        """Chạy `<binary> login …`, chờ dòng `LOGIN_URL` rồi trả ngay; tiến trình chạy tiếp tới khi người đồng ý (ROOT …) hoặc lỗi."""
        if self._login_proc is not None and self._login_proc.returncode is None:
            if self.login_state.get("url"):
                return {"login_url": self.login_state["url"], "state": "waiting"}
            raise Conflict("đang có một lượt đăng nhập")
        binary = self.supervisor.binary()
        if not binary or not self.server():
            raise HubError("chưa cấu hình server/binary — chạy `cfharness filehub setup` trước")
        self.prepare_files()
        argv = [binary, "login", "--server", self.server(), "--tokens", str(self.tokens_path), "--device", self.device_name()]
        if root_hint:
            argv += ["--root-hint", root_hint]
        if no_browser:
            argv.append("--no-browser")
        self.login_state = {"state": "starting", "started_at": time.time(), "roots": []}
        proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.DEVNULL, env=child_env(), cwd=str(self.dir), **no_window_kwargs())
        self._login_proc = proc
        timeout = float(self.cfg("login_url_timeout_s") or 30)
        url = None
        try:
            while url is None:
                assert proc.stdout is not None
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
                if not line:
                    break
                s = line.decode("utf-8", "replace").strip()
                if s.startswith("LOGIN_URL "):
                    url = s[len("LOGIN_URL "):].strip()
                elif s.startswith("ERROR"):
                    self.login_state.update(state="error", error=s[5:].strip()[:120] or "unknown", finished_at=time.time())
                    raise HubError(f"đăng nhập Không gian số lỗi: {self.login_state['error']}")
        except asyncio.TimeoutError:
            proc.kill()
            self.login_state.update(state="error", error="timeout", finished_at=time.time())
            raise HubError("binary sync không trả LOGIN_URL kịp") from None
        if url is None:
            rc = await proc.wait()
            self.login_state.update(state="error", error=f"exit {rc}", finished_at=time.time())
            raise HubError(f"binary sync thoát (mã {rc}) trước khi in LOGIN_URL")
        if not (url.startswith("http://") or url.startswith("https://")):
            proc.kill()
            raise HubError("LOGIN_URL không hợp lệ")
        self.login_state.update(state="waiting", url=url)
        self._spawn(self._finish_login(proc, auto_add=auto_add, local_path=local_path))
        return {"login_url": url, "state": "waiting"}

    async def _finish_login(self, proc: asyncio.subprocess.Process, *, auto_add: bool, local_path: str | None) -> None:
        roots: list[dict[str, str]] = []
        err = None
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            s = line.decode("utf-8", "replace").rstrip("\n")
            if s.startswith("ROOT "):
                parts = s[5:].split("\t")
                if len(parts) >= 3:
                    roots.append({"id": parts[0].strip(), "ws_id": parts[1].strip(), "name": parts[2].strip()[:80]})
            elif s.startswith("ERROR"):
                err = s[5:].strip()[:120] or "unknown"
        rc = await proc.wait()
        if rc != 0 or err:
            self.login_state.update(state="error", error=err or f"exit {rc}", finished_at=time.time())
            self.hub.publish(make_event("filehub.state", None, {"state": "auth_error", "prev": self._last_state, "error": self.login_state["error"]}, source="hub"))
            return
        try:
            os.chmod(self.tokens_path, 0o600)
        except OSError:
            pass
        self.login_state.update(state="done", roots=roots, finished_at=time.time())
        if self.hub.store.state_get("import_filehub"):  # hub nhập từ máy khác (hub_transfer): thiết bị máy này đã đăng nhập → hết nhắc
            self.hub.store.state_set("import_filehub", None)
        added = None
        if auto_add and roots and not self.folders():
            r = roots[0]
            try:
                added = self.add(r["id"], r["name"], ws_id=r.get("ws_id"), local_path=local_path)
            except HubError as ex:
                self.login_state.update(error=f"không thêm được thư mục: {ex}"[:200])
        elif self.folders():
            self._apply_change()  # đăng nhập lại (auth_expired) → chạy lại tiến trình sync với token mới
        try:
            from ..approvals import audit_record

            audit_record("filehub.login_done", agent_id=None, run_id=None, data={"roots": [r["id"] for r in roots], "added": (added or {}).get("name")})
        except Exception:  # pragma: no cover
            pass


__all__ = ["FilehubService", "REF_PREFIX", "STEER_TEXT", "local_roots", "root_of", "config_folders", "valid_name"]
