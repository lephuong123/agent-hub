"""Hỗ trợ phía hub cho app desktop cfharness (repo `cfharness-desktop`, docs/dev-notes/desktop-app.md).

Hợp đồng (C1–C4, C7):
- C1 `cfharness web --json [--ensure] [--port N]`: in MỘT dòng JSON `{ok, origin, login_url, hub_id, version, name, started}` (lỗi:
  `{ok:false, error, message}` + mã thoát 1). `--ensure` khởi động server web TÁCH tiến trình (sống sau khi CLI thoát; log ở
  `HUB_HOME/logs/web.log`). Không bao giờ in token người hay khoá ký — `login_url` là link đăng nhập MỘT LẦN (hạn 300 s) như `--url`.
- C2 `GET /.well-known/cfharness.json` — công khai, CHỈ `{kind, schema, app_api}` (không phiên bản, không tên). Host vẫn bị kiểm.
- C3 `GET /app/badge` — cần phiên (401 JSON), không gia hạn phiên (nằm trong `GuardMiddleware.API`), chỉ số nguyên/bool/phiên bản.
- C4 cầu nối giọng nói `window.cfhNative.speech` — phía trang (`static/voice.js`), không có code server.
- C5 `GET /.well-known/cfharness-directory?t=<token>` — công khai có token (hub admin): danh mục hub (admin_directory.py); sai → 404 JSON,
  sai liên tục → 429; token chỉ so ở daemon (băm, thời gian hằng).
- C7 User-Agent có hậu tố ` CfharnessDesktop/<ver>` → chỉ ghi log (một lần mỗi phiên bản), KHÔNG đổi giao diện.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ..oscompat import IS_WINDOWS, no_window_kwargs
from ..paths import DEFAULT_HOME_DIRNAME, HubPaths
from ..termsafe import one_line

log = logging.getLogger("cfharness.web")

WELL_KNOWN_PATH = "/.well-known/cfharness.json"
WELL_KNOWN = {"kind": "cfharness.hub", "schema": 1, "app_api": 1}
BADGE_PATH = "/app/badge"
NO_STORE = {"Cache-Control": "no-store"}
DEFAULT_PORT = 8765
# cổng theo hub (home không mặc định): 8770..9769 theo hub_id, dò tiếp nếu bận; bỏ cổng MCP HTTP mặc định
PORT_BASE, PORT_SPAN = 8770, 1000
RESERVED_PORTS = frozenset({8766})
NAME_MAX = 60
# Link tải app desktop (thẻ ở Cài đặt + link ở chân thanh bên). HẰNG SỐ: repo app cố định, tên tệp cố định mà mỗi release
# của app đăng kèm → `releases/latest/download/<tên>` luôn trỏ bản ổn định mới nhất. Không lấy từ cấu hình (không mở đường
# cho ai đổi link tải thành tệp khác).
DESKTOP_REPO = "lephuong123/cfharness-desktop"
DOWNLOADS = {
    "mac": f"https://github.com/{DESKTOP_REPO}/releases/latest/download/Cfharness-Desktop-macOS.dmg",
    "windows": f"https://github.com/{DESKTOP_REPO}/releases/latest/download/Cfharness-Desktop-Windows-setup.exe",
    "page": "https://lephuong123.github.io/cfharness-desktop/",
}

APP_UA_RE = re.compile(r"CfharnessDesktop/([0-9A-Za-z.+\-]{1,32})")


# ============================================================================ C2 + C3: route
async def well_known(request: Request) -> Response:
    return JSONResponse(dict(WELL_KNOWN), headers=NO_STORE)


def badge_from(pending: Any, questions: Any, status: Any, estop: Any) -> dict[str, Any]:
    """THUẦN: dữ liệu RPC → badge CHỈ số nguyên/bool/phiên bản (không bao giờ chữ tự do)."""
    from .. import __version__

    pend = pending if isinstance(pending, list) else []
    qs = questions if isinstance(questions, list) else []
    st = status if isinstance(status, dict) else {}
    es = estop if isinstance(estop, dict) else {}
    running = st.get("running")
    return {
        # approval chưa quyết đang chờ NGƯỜI (approval giao điều phối viên duyệt không tính)
        "approvals": sum(1 for a in pend if isinstance(a, dict) and a.get("routed_to") != "manager"),
        "questions": sum(1 for q in qs if isinstance(q, dict)),
        "running": int(running) if isinstance(running, int) and not isinstance(running, bool) and running >= 0 else 0,
        "estop": bool(es.get("on")) or bool(es.get("admin")),
        "version": str(__version__),
    }


async def app_badge(request: Request) -> Response:
    from ..rpc import DaemonUnavailable, RpcError
    from .app import questions, rpc

    try:
        pend = await rpc(request, "approval.list", {"status": "pending", "limit": 500})
        qs = await questions(request, "open")
        st = await rpc(request, "hub.status")
        es = await rpc(request, "estop.get")
    except (RpcError, DaemonUnavailable, OSError):
        return JSONResponse({"error": "daemon_unavailable"}, status_code=503, headers=NO_STORE)
    return JSONResponse(badge_from(pend, qs, st, es), headers=NO_STORE)


# ============================================================================ C5: directory hub (hub admin)
DIRECTORY_PATH = "/.well-known/cfharness-directory"
NOT_FOUND_BODY = {"error": "not_found"}


def directory_limiter() -> Any:
    """Giới hạn lần SAI token (theo IP + toàn cục + ngày) — riêng với giới hạn đăng nhập; chỉ RAM của tiến trình web."""
    from .public import LoginLimiter

    return LoginLimiter(ip_max_failures=10, global_max_failures=30, window_s=600, lockout_s=600, day_max_failures=200, day_lockout_s=3600)


class SuccessWindow:
    """Trần số lần trả danh mục (token đúng) mỗi phút — theo IP và toàn cục (chống dùng URL bị lộ để dội tải)."""

    def __init__(self, per_ip: int = 30, total: int = 120, window_s: float = 60.0, clock: Any = time.monotonic) -> None:
        self.per_ip, self.total, self.window_s, self.clock = per_ip, total, window_s, clock
        self._ip: dict[str, list[float]] = {}
        self._all: list[float] = []

    def allow(self, ip: str) -> bool:
        now = self.clock()
        cut = now - self.window_s
        self._all = [t for t in self._all if t >= cut]
        q = [t for t in self._ip.get(ip, []) if t >= cut]
        if len(self._all) >= self.total or len(q) >= self.per_ip:
            self._ip[ip] = q
            return False
        q.append(now)
        self._all.append(now)
        self._ip[ip] = q
        if len(self._ip) > 5000:
            self._ip = {k: v for k, v in self._ip.items() if v and v[-1] >= cut}
        return True


async def directory(request: Request) -> Response:
    """`GET /.well-known/cfharness-directory?t=<token>` — công khai nhưng có token. Token sai/thiếu/chưa tạo, hub không phải admin, daemon
    không trả lời → 404 JSON (không phân biệt lý do). Sai liên tục → 429. Token so khớp ở daemon (băm + compare_digest)."""
    from ..rpc import DaemonUnavailable, RpcError
    from .app import _ip, ws

    st = ws(request)
    lim = getattr(st, "dir_limiter", None)
    win = getattr(st, "dir_window", None)
    ip = _ip(request)
    if lim is not None:
        lk = lim.locked(ip)
        if lk is not None:
            return JSONResponse({"error": "rate_limited"}, status_code=429, headers={**NO_STORE, "Retry-After": str(int(lk[0]) + 1)})
    token = request.query_params.get("t") or ""
    try:
        doc = await st.client.call("admin.directory_serve", {"token": token[:256]})
    except (RpcError, DaemonUnavailable, OSError):
        doc = None
    if not isinstance(doc, dict):
        if lim is not None:
            lim.fail(ip)
        return JSONResponse(dict(NOT_FOUND_BODY), status_code=404, headers=NO_STORE)
    if lim is not None:
        lim.success(ip)
    if win is not None and not win.allow(ip):
        return JSONResponse({"error": "rate_limited"}, status_code=429, headers={**NO_STORE, "Retry-After": "60"})
    from ..admin_directory import document

    # lọc lại ở web (phòng daemon cũ/khác phiên bản): đúng hợp đồng C5, không trường nào khác
    return JSONResponse(document(doc.get("name"), doc.get("hubs") if isinstance(doc.get("hubs"), list) else []), headers=NO_STORE)


def routes() -> list[Any]:
    from starlette.routing import Route

    return [Route(WELL_KNOWN_PATH, well_known), Route(BADGE_PATH, app_badge), Route(DIRECTORY_PATH, directory)]


# ============================================================================ C7: nhận diện app (chỉ log)
_SEEN_APP_VERSIONS: set[str] = set()


def note_app_ua(user_agent: str) -> str | None:
    """Phiên bản app desktop trong User-Agent (None nếu không phải app). Ghi log MỘT lần mỗi phiên bản — không đổi giao diện."""
    m = APP_UA_RE.search(user_agent or "")
    if not m:
        return None
    ver = m.group(1)
    if ver not in _SEEN_APP_VERSIONS and len(_SEEN_APP_VERSIONS) < 64:
        _SEEN_APP_VERSIONS.add(ver)
        log.info("app desktop cfharness kết nối (CfharnessDesktop/%s)", ver)
    return ver


# ============================================================================ C1: tên, cổng, khởi động tách tiến trình
def display_name(paths: HubPaths, cfg: dict[str, Any] | None) -> str:
    """`web.display_name` (một dòng ≤ 60) hoặc tên thư mục HUB_HOME."""
    v = (cfg or {}).get("display_name")
    name = one_line(v, NAME_MAX) if isinstance(v, str) else ""
    return name or one_line(paths.home.name, NAME_MAX) or "hub"


def is_default_home(paths: HubPaths) -> bool:
    try:
        return paths.home.resolve() == (Path.home() / DEFAULT_HOME_DIRNAME).resolve()
    except OSError:
        return False


def port_busy(port: int, host: str = "127.0.0.1") -> bool:
    fam = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(fam, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((host.strip("[]") if host != "localhost" else "127.0.0.1", port)) == 0


def preferred_port(paths: HubPaths) -> int:
    """Cổng ưu tiên TẤT ĐỊNH theo hub: home mặc định → 8765; hub khác → 8770 + hub_id mod 1000 (tránh hai hub cùng cổng)."""
    if is_default_home(paths):
        return DEFAULT_PORT
    from ..admin_overview import hub_id

    return PORT_BASE + int(hub_id(paths.home), 16) % PORT_SPAN


def pick_port(paths: HubPaths, host: str = "127.0.0.1", tries: int = 50) -> int | None:
    """Cổng trống đầu tiên từ `preferred_port` (dò tuần tự trong dải theo hub); None nếu không còn."""
    start = preferred_port(paths)
    for i in range(tries):
        if start == DEFAULT_PORT:
            p = DEFAULT_PORT + i if i == 0 else PORT_BASE + (i - 1) % PORT_SPAN
        else:
            p = PORT_BASE + (start - PORT_BASE + i) % PORT_SPAN
        if p in RESERVED_PORTS or port_busy(p, host):
            continue
        return p
    return None


# Windows: không fork — DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP (CREATE_NO_WINDOW do no_window_kwargs gộp thêm, vô hại)
WIN_DETACH_FLAGS = 0x00000008 | 0x00000200


# Nhận diện server do CHÍNH lần `--ensure` này bật: nonce ngẫu nhiên qua env → tiến trình web ghi vào `auth/web.json`. Không so pid vì trên
# Windows `sys.executable` của venv/uv tool là launcher, python thật là tiến trình CON khác pid. Nonce không phải bí mật.
NONCE_ENV = "CFHARNESS_WEB_NONCE"
_NONCE_RE = re.compile(r"^[0-9a-f]{16,64}$")


def take_nonce_env() -> str | None:
    """Nonce do `--ensure` truyền (gỡ khỏi env để tiến trình con của server web không thừa hưởng); sai dạng → None."""
    v = os.environ.pop(NONCE_ENV, None)
    return v if isinstance(v, str) and _NONCE_RE.match(v) else None


def ensure_lock_path(paths: HubPaths) -> Path:
    return paths.home / "auth" / "web.ensure.lock"


def acquire_ensure_lock(paths: HubPaths, wait_s: float) -> Any:
    """Khoá độc quyền `HUB_HOME/auth/web.ensure.lock` (flock / msvcrt) cho đường `--ensure`: hai lần gọi cùng lúc (app mở + người gõ lệnh)
    không bật hai server ghi đè `auth/web.json` của nhau. Giữ handle = giữ khoá; hệ điều hành tự nhả khi tiến trình chết. None nếu hết giờ."""
    d = paths.home / "auth"
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:  # pragma: no cover
        pass
    path = ensure_lock_path(paths)
    deadline = time.monotonic() + max(0.0, wait_s)
    while True:
        f = os.fdopen(os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600), "r+b")
        try:
            if IS_WINDOWS:  # pragma: no cover — chỉ chạy trên Windows
                import msvcrt

                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return f
        except OSError:
            f.close()
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.1)


def release_ensure_lock(handle: Any) -> None:
    if handle is None:
        return
    try:
        if IS_WINDOWS:  # pragma: no cover
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:  # pragma: no cover
        pass
    handle.close()


def spawn_web(paths: HubPaths, *, port: int, host: str, name: str, nonce: str | None = None) -> subprocess.Popen[bytes]:
    """`python -m cfharness web --no-link` tách tiến trình; stdout/stderr → `HUB_HOME/logs/web.log` (0600). Không in link.
    `nonce` → env `CFHARNESS_WEB_NONCE`, server ghi lại vào `auth/web.json` (để `wait_ready` biết state là của server vừa bật)."""
    logs = paths.home / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    logf = logs / "web.log"
    from ..logs import rotate_if_large

    rotate_if_large(logf)  # L1: stdout/stderr thô của web không xoay được khi đang chạy → xoay lúc bật (log JSONL ở web.jsonl)
    fd = os.open(logf, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
    out = os.fdopen(fd, "ab")
    env = dict(os.environ)
    env["CFHARNESS_HOME"] = str(paths.home)
    env.pop(NONCE_ENV, None)
    if nonce:
        env[NONCE_ENV] = nonce
    argv = [sys.executable, "-m", "cfharness", "web", "--no-link", "--host", host, "--port", str(port), "--host-name", name]
    try:
        # server web sống sau khi CLI thoát: POSIX session mới; Windows tách khỏi console + nhóm tiến trình mới
        return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=out, env=env, close_fds=True, start_new_session=not IS_WINDOWS,
                                **no_window_kwargs(WIN_DETACH_FLAGS if IS_WINDOWS else 0))
    finally:
        out.close()


def probe(host: str, port: int, name: str, timeout: float = 1.0) -> bool:
    """Server web trả lời `/.well-known/cfharness.json` với Host đúng tên riêng."""
    try:
        c = http.client.HTTPConnection(host.strip("[]") if host != "localhost" else "127.0.0.1", port, timeout=timeout)
        try:
            c.request("GET", WELL_KNOWN_PATH, headers={"Host": f"{name}:{port}"})
            r = c.getresponse()
            body = r.read(4096)
            return r.status == 200 and json.loads(body or b"{}").get("kind") == WELL_KNOWN["kind"]
        finally:
            c.close()
    except (OSError, ValueError, http.client.HTTPException):
        return False


def wait_ready(paths: HubPaths, proc: subprocess.Popen[bytes] | None, wait_s: float, nonce: str | None = None) -> dict[str, Any] | None:
    """Chờ `auth/web.json` của server đang sống + server trả lời. Trả state; None khi hết giờ / tiến trình thoát mà không ai phục vụ.
    `nonce` (server vừa bật): chỉ tin state mang đúng nonce đó — khoá ký trong state khác (server khác đang khởi động) sẽ cho link đăng nhập
    hỏng; state của server KHÁC chỉ được nhận sau khi tiến trình của mình đã thoát (vd thua cuộc bind cổng) và server kia trả lời."""
    from .security import read_state

    deadline = time.monotonic() + wait_s
    exited_at: float | None = None
    while time.monotonic() < deadline:
        st = read_state(paths)
        mine = nonce is None or (st is not None and st.get("nonce") == nonce) or exited_at is not None
        if st is not None and mine and probe(st["host"], int(st["port"]), st.get("name") or st["host"]):
            return st
        if proc is not None and exited_at is None and proc.poll() is not None:
            exited_at = time.monotonic()
        if exited_at is not None and st is None and time.monotonic() - exited_at > 3.0:
            return None  # tiến trình con thoát và không bản nào khác phục vụ hub này
        time.sleep(0.1)
    return None


def info(paths: HubPaths, st: dict[str, Any], cfg: dict[str, Any] | None, *, started: bool) -> dict[str, Any]:
    """JSON của C1 từ state server đang chạy — link đăng nhập MỚI (một lần, 300 s). Không có token người/khoá ký."""
    from .. import __version__
    from ..admin_overview import hub_id
    from .security import login_url, mint_login_token

    host = st.get("name") or st["host"]
    port = int(st["port"])
    h = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return {"ok": True, "origin": f"http://{h}:{port}", "login_url": login_url(host, port, mint_login_token(bytes.fromhex(st["key"]))),
            "hub_id": hub_id(paths.home), "version": str(__version__), "name": display_name(paths, cfg), "started": started}


def local_web_cfg(paths: HubPaths) -> dict[str, Any]:
    """Nhánh `web` của config.yaml — ĐỌC trực tiếp (không ghi, không cần daemon). Lỗi → {}."""
    try:
        from ..config import HubConfig

        v = HubConfig(paths.config).get("web")
    except Exception:  # noqa: BLE001
        return {}
    return v if isinstance(v, dict) else {}


__all__ = ["DOWNLOADS", "WELL_KNOWN", "WELL_KNOWN_PATH", "BADGE_PATH", "badge_from", "routes", "note_app_ua", "display_name", "preferred_port", "pick_port",
           "spawn_web", "wait_ready", "info", "local_web_cfg", "probe", "acquire_ensure_lock", "release_ensure_lock", "take_nonce_env", "NONCE_ENV"]
