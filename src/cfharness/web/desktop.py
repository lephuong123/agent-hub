"""Hỗ trợ phía hub cho app desktop cfharness (repo `cfharness-desktop`, docs/dev-notes/desktop-app.md).

Hợp đồng (C1–C4, C7):
- C1 `cfharness web --json [--ensure] [--port N]`: in MỘT dòng JSON `{ok, origin, login_url, hub_id, version, name, started}` (lỗi:
  `{ok:false, error, message}` + mã thoát 1). `--ensure` khởi động server web TÁCH tiến trình (sống sau khi CLI thoát; log ở
  `HUB_HOME/logs/web.log`). Không bao giờ in token người hay khoá ký — `login_url` là link đăng nhập MỘT LẦN (hạn 300 s) như `--url`.
- C2 `GET /.well-known/cfharness.json` — công khai, CHỈ `{kind, schema, app_api}` (không phiên bản, không tên). Host vẫn bị kiểm.
- C3 `GET /app/badge` — cần phiên (401 JSON), không gia hạn phiên (nằm trong `GuardMiddleware.API`), chỉ số nguyên/bool/phiên bản.
- C4 cầu nối giọng nói `window.cfhNative.speech` — phía trang (`static/voice.js`), không có code server.
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


def routes() -> list[Any]:
    from starlette.routing import Route

    return [Route(WELL_KNOWN_PATH, well_known), Route(BADGE_PATH, app_badge)]


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


def spawn_web(paths: HubPaths, *, port: int, host: str, name: str) -> subprocess.Popen[bytes]:
    """`python -m cfharness web --no-link` tách tiến trình; stdout/stderr → `HUB_HOME/logs/web.log` (0600). Không in link."""
    logs = paths.home / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    logf = logs / "web.log"
    fd = os.open(logf, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
    out = os.fdopen(fd, "ab")
    env = dict(os.environ)
    env["CFHARNESS_HOME"] = str(paths.home)
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


def wait_ready(paths: HubPaths, proc: subprocess.Popen[bytes] | None, wait_s: float) -> dict[str, Any] | None:
    """Chờ `auth/web.json` của server đang sống + server trả lời. Trả state; None khi hết giờ / tiến trình thoát mà không ai phục vụ."""
    from .security import read_state

    deadline = time.monotonic() + wait_s
    exited_at: float | None = None
    while time.monotonic() < deadline:
        st = read_state(paths)
        if st is not None and probe(st["host"], int(st["port"]), st.get("name") or st["host"]):
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


__all__ = ["WELL_KNOWN", "WELL_KNOWN_PATH", "BADGE_PATH", "badge_from", "routes", "note_app_ua", "display_name", "preferred_port", "pick_port",
           "spawn_web", "wait_ready", "info", "local_web_cfg", "probe"]
