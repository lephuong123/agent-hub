"""Giám sát tiến trình sync `tang-thu-sync-headless` (hợp đồng §8.3, docs/dev-notes/plan-cloud-files.md) — tiến trình con của daemon.

- Khởi động ở `hub.started` (qua `FilehubService.start`), dừng êm khi daemon dừng: SIGTERM → chờ `filehub.stop_grace_s` → SIGKILL.
  Binary hứa dừng êm khi nhận SIGTERM (không bỏ dở commit), nên grace mặc định rộng (15 s).
- Restart khi tiến trình thoát ngoài ý muốn, backoff luỹ thừa `filehub.restart_backoff_s = [min, max]`; chạy ổn > 60 s thì đặt lại backoff.
- Đối soát `(pid, start_time)` khi daemon khởi động lại (như `recovery.py`): tiến trình sync của daemon trước còn sống (spawn với
  `start_new_session`, sống qua lúc daemon chết) và khớp thời điểm khởi động → SIGTERM trước khi chạy bản mới (không có hai tiến trình
  cùng ghi một profile). Ghi ở `HUB_HOME/filehub/supervisor.json`.
- Kiểm phiên bản `--version` ≥ `filehub.sync_min_version` trước khi chạy — sai thì không chạy (`state=version_mismatch`).
- Env TỐI THIỂU (`child_env`): chỉ PATH/HOME/LANG/TMPDIR… — không bao giờ token người của hub (`*.sock.auth` không ở env), không
  biến `CLAUDE*`/`CFHARNESS*`/`ANTHROPIC*`, không secret của hub. Token sync nằm trong tệp `--tokens` do chính binary ghi (0600).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..oscompat import IS_WINDOWS, no_window_kwargs

if TYPE_CHECKING:
    from .service import FilehubService

log = logging.getLogger("cfharness.filehub")

ENV_KEEP = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TZ", "SYSTEMROOT", "TEMP", "TMP", "USERPROFILE",
            "APPDATA", "LOCALAPPDATA")
ENV_DROP_PREFIXES = ("CLAUDE", "CFHARNESS", "AGENTHUB", "AGENT_HUB", "ANTHROPIC")
_SEMVER = re.compile(r"(\d+)\.(\d+)\.(\d+)")
STABLE_S = 60.0
SYNC_LOG = "filehub-sync.log"  # stderr của tiến trình sync (L7), trong HUB_HOME/logs/


def child_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Env tối thiểu cho tiến trình sync. Danh sách TRẮNG — biến mới của daemon không tự lọt sang."""
    src = dict(os.environ if base is None else base)
    out = {k: v for k, v in src.items() if k in ENV_KEEP and not k.upper().startswith(ENV_DROP_PREFIXES)}
    out.setdefault("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    out.setdefault("LANG", "C.UTF-8")
    return out


def parse_version(text: str) -> tuple[int, int, int] | None:
    m = _SEMVER.search(text or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def version_ok(found: str | None, minimum: str | None) -> bool:
    if not minimum:
        return found is not None
    f, m = parse_version(found or ""), parse_version(minimum)
    return f is not None and (m is None or f >= m)


class SyncSupervisor:
    def __init__(self, svc: "FilehubService") -> None:
        self.svc = svc
        self.hub = svc.hub
        self.proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self.restarts = 0
        self.last_exit: int | None = None
        self.last_error: str | None = None
        self.version: str | None = None
        self.state = "stopped"  # stopped | starting | running | backoff | version_mismatch | not_configured | error
        self.started_at: float | None = None
        self._wake = asyncio.Event()

    # ------------------------------------------------------------------ đường dẫn / argv
    @property
    def pidfile(self) -> Path:
        return self.svc.dir / "supervisor.json"

    def binary(self) -> str | None:
        b = self.svc.cfg("sync_binary")
        return os.path.expanduser(str(b)) if b else None

    def base_args(self) -> list[str]:
        s = self.svc
        args = ["--profile", str(s.profile_dir), "--server", str(s.cfg("server") or ""), "--tokens", str(s.tokens_path),
                "--status", str(s.status_path), "--control", str(s.control_path), "--device", s.device_name(),
                "--extra-ignore", str(s.ignore_path), "--max-delete-batch", str(int(s.cfg("max_delete_batch") or 0))]
        if s.cfg("poll_s"):
            args += ["--poll", str(s.cfg("poll_s"))]
        return args

    def argv(self) -> list[str]:
        b = self.binary()
        if not b:
            raise RuntimeError("chưa cấu hình filehub.sync_binary")
        args = [b, *self.base_args()]
        pend = self.svc.pending_add()
        if pend is not None:  # hợp đồng: một --add-root mỗi lần chạy; root đã có trong status thì bỏ cờ pending
            args += ["--add-root", str(pend["root_id"]), "--root-name", str(pend["name"]), "--ws", str(pend.get("ws_id") or ""),
                     "--local-parent", str(Path(pend["local_path"]).parent)]
            if pend.get("kind") == "workspace":  # S5: thiếu = folder; gốc workspace BẮT BUỘC --root-kind workspace
                args += ["--root-kind", "workspace"]
        stale = self.svc.stale_roots()
        if stale:  # root còn trong hồ sơ của binary mà người đã gỡ khỏi cấu hình → binary ngừng đồng bộ (tệp giữ nguyên)
            args += ["--remove-root", ",".join(stale)]
        return args

    def check_version(self) -> str | None:
        b = self.binary()
        if not b or not os.path.isfile(b) or not os.access(b, os.X_OK):
            self.last_error = f"không tìm thấy binary sync (filehub.sync_binary = {b!r})"
            return None
        try:
            r = subprocess.run([b, "--version"], capture_output=True, text=True, timeout=10, env=child_env(), **no_window_kwargs())
        except (OSError, subprocess.TimeoutExpired) as e:
            self.last_error = f"không chạy được `{b} --version`: {e}"
            return None
        out = (r.stdout or "").strip()
        if r.returncode != 0 or parse_version(out) is None:
            self.last_error = f"`--version` không hợp lệ (exit {r.returncode}): {out[:120]!r}"
            return None
        return out

    # ------------------------------------------------------------------ đối soát pid
    def _save_pid(self, pid: int | None, start: float | None) -> None:
        self.svc.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.pidfile.with_suffix(".tmp")
        tmp.write_text(json.dumps({"pid": pid, "start_time": start}), encoding="utf-8")
        os.replace(tmp, self.pidfile)

    def reconcile(self) -> dict[str, Any]:
        """Tiến trình sync còn sót từ daemon trước (đúng pid + start_time) → SIGTERM, chờ, SIGKILL. Trả kết quả để log/test."""
        from ..recovery import kill_group, process_matches

        try:
            rec = json.loads(self.pidfile.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"found": False}
        pid, start = rec.get("pid"), rec.get("start_time")
        if not pid or not process_matches(int(pid), start, float(self.hub.config.get("recovery.pid_start_tolerance_s") or 5)):
            self._save_pid(None, None)
            return {"found": False, "stale": bool(pid)}
        killed = kill_group(int(pid), grace_s=float(self.svc.cfg("stop_grace_s") or 15))
        self._save_pid(None, None)
        log.info("đã dừng tiến trình sync mồ côi pid=%s", pid)
        return {"found": True, "pid": int(pid), "killed": killed}

    # ------------------------------------------------------------------ vòng đời
    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        try:
            self._task = asyncio.get_running_loop().create_task(self._loop())
        except RuntimeError:
            self._task = None

    def restart(self) -> None:
        """Dừng tiến trình hiện tại; vòng tự chạy lại ngay (không backoff) — dùng khi đổi root/cấu hình."""
        self._wake.set()
        if self.proc is not None and self.proc.returncode is None:
            self._signal(signal.SIGTERM)

    def _signal(self, sig: int) -> None:
        p = self.proc
        if p is None or p.returncode is not None:
            return
        try:
            if IS_WINDOWS:  # pragma: no cover
                p.terminate() if sig == signal.SIGTERM else p.kill()
            else:
                os.killpg(p.pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        p = self.proc
        if p is not None and p.returncode is None:
            self._signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(p.wait(), timeout=float(self.svc.cfg("stop_grace_s") or 15))
            except asyncio.TimeoutError:
                self._signal(signal.SIGKILL)
                try:
                    await asyncio.wait_for(p.wait(), timeout=5)
                except asyncio.TimeoutError:  # pragma: no cover
                    pass
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        self.proc = None
        self.state = "stopped"
        if self.pidfile.exists():  # hub chưa từng bật filehub → không tạo HUB_HOME/filehub
            try:
                self._save_pid(None, None)
            except OSError:  # pragma: no cover
                pass

    def _backoff(self) -> float:
        lo, hi = (self.svc.cfg("restart_backoff_s") or [1, 300])[:2]
        return float(min(float(hi), float(lo) * (2 ** max(0, self.restarts - 1))))

    async def _loop(self) -> None:
        from ..recovery import pid_start_time

        while not self._stopping:
            if not self.svc.configured():
                self.state = "not_configured"
                return
            ver = await asyncio.to_thread(self.check_version)
            self.version = ver
            if ver is None or not version_ok(ver, self.svc.cfg("sync_min_version")):
                self.state = "version_mismatch" if ver is not None else "error"
                if ver is not None:
                    self.last_error = f"phiên bản sync {ver} < tối thiểu {self.svc.cfg('sync_min_version')}"
                self.svc.on_supervisor_state()
                return
            try:
                self.svc.prepare_files()
                argv = self.argv()
                self.state = "starting"
                self.proc = await asyncio.create_subprocess_exec(
                    *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
                    env=child_env(), cwd=str(self.svc.dir), start_new_session=not IS_WINDOWS, **no_window_kwargs())
            except (OSError, RuntimeError, ValueError) as e:
                self.state, self.last_error = "error", str(e)[:300]
                self.svc.on_supervisor_state()
                return
            self.started_at = time.time()
            self._save_pid(self.proc.pid, pid_start_time(self.proc.pid))
            self.state = "running"
            self.svc.on_supervisor_state()
            err_tail = await self._drain_stderr(self.proc)
            rc = await self.proc.wait()
            self.last_exit = rc
            ran = time.time() - (self.started_at or time.time())
            self._save_pid(None, None)
            if self._stopping:
                break
            woke = self._wake.is_set()
            self._wake.clear()
            if woke:  # restart có chủ đích (đổi root) — chạy lại ngay
                continue
            if rc != 0:
                self.last_error = f"tiến trình sync thoát mã {rc}" + (f": {err_tail}" if err_tail else "")
            if ran > STABLE_S:
                self.restarts = 0
            self.restarts += 1
            self.state = "backoff"
            self.svc.on_supervisor_state()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._backoff())
                self._wake.clear()
            except asyncio.TimeoutError:
                pass
        self.state = "stopped"

    @property
    def log_path(self) -> Path:
        return self.hub.paths.logs_dir / SYNC_LOG

    def _open_log(self) -> int | None:
        """`HUB_HOME/logs/filehub-sync.log` (0600, O_APPEND, không theo symlink), xoay lúc spawn theo `logs.max_bytes`. Lỗi IO → None
        (vẫn giữ đuôi stderr trong RAM để báo lỗi)."""
        from ..logs import DEFAULT_BACKUP_COUNT, DEFAULT_MAX_BYTES, rotate_if_large

        p = self.log_path
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            ls = self.hub.config.get("logs") or {}
            rotate_if_large(p, int(ls.get("max_bytes", DEFAULT_MAX_BYTES) or 0), int(ls.get("backup_count", DEFAULT_BACKUP_COUNT) or 0))
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                os.chmod(p, 0o600)
            except OSError:  # pragma: no cover — Windows
                pass
            return fd
        except OSError as e:
            log.warning("không mở được %s: %s", p.name, type(e).__name__)
            return None

    async def _drain_stderr(self, p: asyncio.subprocess.Process) -> str:
        """Đọc stderr tới khi tiến trình đóng (tránh đầy pipe). Ghi từng khúc (đã che theo GIÁ TRỊ secret) vào `logs/filehub-sync.log`
        (L7 — gói chẩn đoán lấy file này); vẫn giữ 300 ký tự cuối (đã che) để báo lỗi trong `last_error`."""
        from ..redact import mask_bytes, redact_str

        tail = b""
        if p.stderr is None:
            return ""
        fd = self._open_log()
        try:
            if fd is not None:
                os.write(fd, f"--- {time.strftime('%Y-%m-%dT%H:%M:%S%z')} sync pid {p.pid} bắt đầu ---\n".encode())
            while True:
                chunk = await p.stderr.read(4096)
                if not chunk:
                    break
                tail = (tail + chunk)[-2000:]
                if fd is not None:
                    try:
                        os.write(fd, mask_bytes(chunk))
                    except OSError:  # đĩa đầy… → thôi ghi file, vẫn đọc pipe
                        os.close(fd)
                        fd = None
        finally:
            if fd is not None:
                os.close(fd)
        return redact_str(tail.decode("utf-8", "replace").strip()[-300:])

    def info(self) -> dict[str, Any]:
        p = self.proc
        return {"state": self.state, "pid": p.pid if p is not None and p.returncode is None else None, "version": self.version,
                "restarts": self.restarts, "last_exit": self.last_exit, "last_error": self.last_error, "started_at": self.started_at}


__all__ = ["SyncSupervisor", "child_env", "parse_version", "version_ok", "ENV_KEEP"]
