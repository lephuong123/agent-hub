"""Web tự chạy lại khi bản cài cfharness trên đĩa đổi phiên bản (sau `cfharness update` / nút "Cập nhật hệ thống").

Vì sao: trên macOS/Linux `uv tool install --force` thay venv khi tiến trình vẫn chạy, và helper cập nhật chỉ khởi động lại
DAEMON (`update_helper.stop_venv_holders` chỉ dừng tiến trình trên Windows). Tiến trình `cfharness web` cũ vẫn chạy code cũ
nhưng Jinja đọc template MỚI từ đĩa → template dùng biến mà code cũ không truyền → lỗi 500 (sự cố trang Cài đặt sau v0.2.12).

Cách làm: luồng nền so phiên bản trong metadata gói đã cài (`importlib.metadata`, đọc lại từ đĩa) với phiên bản ĐÃ CÀI lúc web
khởi động — không so với `__version__` trong RAM (bản cài editable chưa cài lại sẽ lệch mãi → vòng lặp chạy lại). Thấy bản mới
ổn định qua `STABLE_POLLS` lần dò liên tiếp (uv có thể đang ghi dở venv) và trình thông dịch còn đó → yêu cầu uvicorn dừng êm,
rồi `cmd_web` gọi `exec_self()` (`os.execv`, giữ pid → launchd `KeepAlive` không coi là chết; chạy tay cũng tự lên lại).
Phiên web nằm trong RAM nên người dùng đăng nhập lại sau cập nhật (như mọi lần khởi động lại web).
"""

from __future__ import annotations

import importlib
import importlib.metadata
import logging
import os
import sys
import threading
from collections.abc import Callable

log = logging.getLogger("cfharness.web.reload")

DIST = "cfharness"
POLL_S = 15.0
STABLE_POLLS = 2


def installed_version() -> str | None:
    """Phiên bản gói cfharness đang CÀI trên đĩa (không phải bản đã nạp vào RAM); None nếu không đọc được."""
    try:
        importlib.invalidate_caches()
        return importlib.metadata.version(DIST)
    except Exception:  # noqa: BLE001 — thiếu/đang ghi dở metadata: coi như chưa biết
        return None


class InstallWatcher:
    """Dò bản cài định kỳ; bản cài đổi (ổn định) → gọi `on_change(new_version)` MỘT lần rồi dừng."""

    def __init__(self, on_change: Callable[[str], None], *, poll_s: float = POLL_S, stable_polls: int = STABLE_POLLS,
                 probe: Callable[[], str | None] = installed_version, executable: str | None = None) -> None:
        self.on_change = on_change
        self.poll_s = poll_s
        self.stable_polls = max(1, stable_polls)
        self.probe = probe
        self.executable = executable or sys.executable
        self.baseline = probe()
        self.changed_to: str | None = None
        self._seen: str | None = None
        self._count = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def check(self) -> bool:
        """Một lần dò (thuần, test gọi trực tiếp). True khi vừa phát hiện thay đổi và đã gọi `on_change`."""
        if self.baseline is None or self.changed_to is not None:
            return False
        v = self.probe()
        if v is None or v == self.baseline or not os.path.exists(self.executable):
            self._seen, self._count = None, 0
            return False
        if v != self._seen:
            self._seen, self._count = v, 0
        self._count += 1
        if self._count < self.stable_polls:
            return False
        self.changed_to = v
        log.warning("bản cài cfharness đổi %s → %s: chạy lại web", self.baseline, v)
        try:
            self.on_change(v)
        except Exception:  # noqa: BLE001
            log.exception("không yêu cầu dừng web được")
        return True

    def _loop(self) -> None:
        while not self._stop.wait(self.poll_s):
            if self.check():
                return

    def start(self) -> InstallWatcher:
        if self.baseline is None:
            log.info("không đọc được phiên bản bản cài — bỏ tự chạy lại web sau cập nhật")
            return self
        self._thread = threading.Thread(target=self._loop, name="cfharness-web-reload", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()


def restart_argv(orig: list[str] | None = None, executable: str | None = None) -> list[str]:
    """argv chạy lại đúng lệnh hiện tại bằng trình thông dịch hiện tại; bỏ `--open` (không bật thêm tab trình duyệt)."""
    orig = list(orig if orig is not None else getattr(sys, "orig_argv", None) or [sys.executable, *sys.argv])
    exe = executable or sys.executable
    return [exe, *[x for x in orig[1:] if x != "--open"]]


def exec_self() -> None:  # pragma: no cover — thay tiến trình; test kiểm `restart_argv`
    argv = restart_argv()
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass
    os.execv(argv[0], argv)




# ============================================================================ phía DAEMON: dọn web còn chạy code cũ
# Web khởi động TRƯỚC bản sửa này không có `InstallWatcher` → cập nhật xong vẫn chạy code cũ mãi (đúng sự cố lamlinh). Daemon bản mới
# (luôn được helper khởi động lại sau cập nhật) dò `auth/web.json`: pid sống + `version` khác của daemon (thiếu = web cũ) → SIGTERM.
# launchd `KeepAlive` bật lại web bằng code mới; không ai bật lại sau `RESPAWN_WAIT_S` (web chạy tay/tách tiến trình) → daemon tự chạy
# lại đúng dòng lệnh cũ (tách session, log `logs/web.log`). Chờ `GRACE_S` trước để `InstallWatcher` của web mới tự exec trước.
# Windows: helper đã dừng + chạy lại web (`update_helper.stop_venv_holders`) → bỏ qua.
GRACE_S = 45.0
RESPAWN_WAIT_S = 35.0


def process_cmdline(pid: int) -> str | None:
    """Dòng lệnh của pid (POSIX, `ps`); None nếu không đọc được."""
    import subprocess

    try:
        r = subprocess.run(["ps", "-o", "command=", "-p", str(int(pid))], capture_output=True, text=True, timeout=5)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    out = r.stdout.strip()
    return out if r.returncode == 0 and out else None


def web_argv_from_cmdline(cmd: str) -> list[str] | None:
    """`<python> [-m cfharness | <launcher cfharness>] web [args]` → argv chạy lại (bỏ `--open`); không phải web cfharness → None."""
    import shlex

    try:
        parts = shlex.split(cmd)
    except ValueError:
        return None
    for i, tok in enumerate(parts):
        if tok != "web" or i < 1:
            continue
        prev = parts[i - 1]
        if (prev == "cfharness" and i >= 2 and parts[i - 2] == "-m") or "cfharness" in os.path.basename(prev).lower():
            return [*parts[:i + 1], *[x for x in parts[i + 1:] if x != "--open"]]
    return None


def stale_web(paths, version: str, *, cmdline: Callable[[int], str | None] = process_cmdline) -> tuple[dict, list[str]] | None:
    """Web đang chạy code KHÁC phiên bản `version` → (state, argv chạy lại); không thì None. Chỉ nhận tiến trình có dòng lệnh là
    `cfharness … web` (không bao giờ đụng pid lạ / chính mình — state cũ có thể trỏ pid đã bị tái dùng)."""
    from .security import read_state

    st = read_state(paths)
    if not st or st.get("version") == version:
        return None
    pid = int(st["pid"])
    if pid in (os.getpid(), os.getppid()):
        return None
    cmd = cmdline(pid)
    argv = web_argv_from_cmdline(cmd) if cmd else None
    return (st, argv) if argv else None


def _wait_gone(pid: int, timeout: float) -> bool:
    import time

    from ..oscompat import pid_alive

    def gone() -> bool:
        try:  # tiến trình con của chính mình đã thoát = zombie (pid_alive vẫn True) → dọn
            os.waitpid(pid, os.WNOHANG)
        except (ChildProcessError, OSError):
            pass
        return not pid_alive(pid)

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if gone():
            return True
        time.sleep(0.2)
    return gone()


def spawn_detached(paths, argv: list[str]) -> None:
    import subprocess

    logs = paths.home / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    fd = os.open(logs / "web.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
    env = dict(os.environ)
    env["CFHARNESS_HOME"] = str(paths.home)
    env.pop("CFHARNESS_WEB_NONCE", None)
    with os.fdopen(fd, "ab") as out:
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=out, env=env, close_fds=True, start_new_session=True)


def restart_stale_web(paths, version: str, *, respawn_wait_s: float = RESPAWN_WAIT_S,
                      cmdline: Callable[[int], str | None] = process_cmdline) -> str:
    """Đồng bộ (chạy trong thread). Trả kết quả: none|killed|respawned|failed."""
    import signal

    from .security import read_state

    found = stale_web(paths, version, cmdline=cmdline)
    if not found:
        return "none"
    st, argv = found
    pid = int(st["pid"])
    log.warning("web (pid %s, bản %s) còn chạy code cũ, daemon bản %s → dừng để chạy lại", pid, st.get("version") or "cũ", version)
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return "failed"
    if not _wait_gone(pid, 15.0):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        if not _wait_gone(pid, 5.0):
            return "failed"
    if _wait_respawn(paths, respawn_wait_s, read_state):
        return "killed"  # launchd (KeepAlive) đã bật lại
    log.warning("không có ai bật lại web → daemon chạy lại: %s", " ".join(argv[:3]))
    try:
        spawn_detached(paths, argv)
    except OSError:
        log.exception("không chạy lại được web")
        return "failed"
    return "respawned"


def _wait_respawn(paths, timeout: float, read_state) -> bool:
    import time

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if read_state(paths):
            return True
        time.sleep(0.5)
    return bool(read_state(paths))


async def stale_web_loop(hub, *, grace_s: float = GRACE_S) -> None:  # pragma: no cover — bọc mỏng, test gọi restart_stale_web
    import asyncio

    from .. import __version__

    if os.name == "nt":
        return
    await asyncio.sleep(grace_s)
    try:
        await asyncio.to_thread(restart_stale_web, hub.paths, __version__)
    except Exception:  # noqa: BLE001
        log.exception("không kiểm được web còn chạy code cũ")


__all__ = ["InstallWatcher", "installed_version", "restart_argv", "exec_self", "POLL_S", "STABLE_POLLS", "stale_web", "restart_stale_web",
           "stale_web_loop", "web_argv_from_cmdline", "process_cmdline", "GRACE_S", "RESPAWN_WAIT_S"]
