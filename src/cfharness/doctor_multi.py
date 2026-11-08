"""`cfharness doctor multi` — kiểm sẵn sàng chạy NHIỀU hub trên một máy (đa instance P1, `docs/dev-notes/multi-instance.md`).

CHỈ ĐỌC: không cần daemon, không gọi model, không sửa gì. Không bao giờ in token — bot Telegram/Slack chỉ hiện "đã cấu hình" kèm
vân tay sha256 8 ký tự (so giữa các hub để phát hiện hai hub dùng chung một bot — C7) .
Mã thoát: 0 = không có lỗi (có thể có cảnh báo), 1 = có lỗi.
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Any

from . import __version__
from .oscompat import IS_WINDOWS
from .paths import HubPaths, fallback_socket_dir

WEB_DEFAULT_PORT = 8765


def _mode(p: Path) -> int | None:
    try:
        return stat.S_IMODE(os.lstat(p).st_mode)
    except OSError:
        return None


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def _read_secret(paths: HubPaths, name: str) -> str | None:
    """Đọc file secret của CHÍNH hub (không theo symlink, không lấy env) — chỉ để tính vân tay, không in giá trị."""
    if not name or "/" in name or name.startswith("."):
        return None
    p = paths.home / "secrets" / name
    try:
        if p.is_symlink() or not p.is_file():
            return None
        return p.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def collect(paths: HubPaths) -> list[dict[str, Any]]:
    from .config import MCP_HTTP_DEFAULT_PORT, MCP_OAUTH_CALLBACK_DEFAULT_PORT, HubConfig
    from .web.cli import _port_busy
    from .web.security import cookie_name, read_state

    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str, level: str = "error") -> None:
        checks.append({"check": name, "ok": ok, "detail": detail, "level": "ok" if ok else level})

    home = paths.home
    add("phiên bản cfharness", True, __version__)
    # 1. quyền HUB_HOME + mục nhạy cảm
    if not home.exists():
        add("HUB_HOME", False, f"{home} chưa tồn tại (chạy `cfharness daemon start`)", "warning")
    elif not IS_WINDOWS:
        st = os.lstat(home)
        m = stat.S_IMODE(st.st_mode)
        add("HUB_HOME thuộc user này", st.st_uid == os.getuid(), f"{home} (uid {st.st_uid})")
        add("HUB_HOME riêng tư (0700)", not m & 0o077, f"{home} mode {m:o}" + ("" if not m & 0o077 else
            " — user khác đọc được; khởi động lại daemon (tự siết) hoặc `chmod 700` thư mục này"))
        loose = [f"{n} ({_mode(home / n):o})" for n in ("hub.db", "logs", "runs", "prompts", "secrets", "auth")
                 if _mode(home / n) is not None and _mode(home / n) & 0o077]  # type: ignore[operator]
        add("mục trong HUB_HOME riêng tư", not loose, ", ".join(loose) or "hub.db/logs/runs/prompts/secrets/auth không mở cho group/other",
            "warning")
    # 2. socket điều khiển
    sock = paths.socket
    where = "trong HUB_HOME" if sock.parent == home else ("thư mục dự phòng riêng" if sock.parent == fallback_socket_dir()
                                                          else "đặt qua CFHARNESS_SOCKET")
    if IS_WINDOWS:
        add("socket điều khiển", True, f"{sock} (Windows: file endpoint TCP loopback)")
    else:
        try:
            st = os.lstat(sock)
            ok = stat.S_ISSOCK(st.st_mode) and st.st_uid == os.getuid()
            add("socket điều khiển", ok, f"{sock} ({where})" + ("" if ok else
                f" — {'không phải socket' if not stat.S_ISSOCK(st.st_mode) else f'thuộc uid {st.st_uid}'}: client sẽ KHÔNG gửi token"))
        except FileNotFoundError:
            add("socket điều khiển", True, f"{sock} ({where}; daemon chưa chạy)")
        except OSError as e:
            add("socket điều khiển", False, f"{sock}: {e}")
        if sock.parent == fallback_socket_dir() and sock.parent.exists():
            st = os.lstat(sock.parent)
            ok = not stat.S_ISLNK(st.st_mode) and st.st_uid == os.getuid() and not stat.S_IMODE(st.st_mode) & 0o077
            add("thư mục socket dự phòng", ok, f"{sock.parent} mode {stat.S_IMODE(st.st_mode):o} uid {st.st_uid}")
    # 3. cổng web / MCP
    try:
        cfg = HubConfig(paths.config)
    except Exception as e:  # config hỏng: vẫn báo phần còn lại
        add("config.yaml", False, f"không đọc được: {e}")
        cfg = None

    def cget(key: str, default: Any) -> Any:
        try:
            v = cfg.get(key) if cfg is not None else None
        except KeyError:
            v = None
        return default if v is None else v

    web = read_state(paths)
    web_port = int(web["port"]) if web else WEB_DEFAULT_PORT
    if web:
        add("web", True, f"đang chạy (pid {web['pid']}) tại http://{web.get('name') or web['host']}:{web_port}/")
    else:
        busy = _port_busy("127.0.0.1", web_port)
        add("cổng web", not busy, f"{web_port} " + ("đang bị tiến trình KHÁC chiếm (hub khác?) — chạy `cfharness web --port <cổng riêng>`"
                                               if busy else "trống (web chưa chạy)"), "warning")
    add("cookie web theo hub", True, cookie_name(home))
    mcp_port = int(cget("mcp_server.http_port", MCP_HTTP_DEFAULT_PORT))
    add("cổng MCP HTTP khác cổng web", mcp_port != web_port, f"MCP {mcp_port}, web {web_port}" + (
        "" if mcp_port != web_port else f" — `cfharness config set mcp_server.http_port {web_port + 1}`"))
    if _port_busy("127.0.0.1", mcp_port):
        add("cổng MCP HTTP", False, f"{mcp_port} đang bận (có thể là `mcp serve --http` của chính hub này, hoặc hub khác)", "warning")
    else:
        add("cổng MCP HTTP", True, f"{mcp_port} trống")
    cb = int(cget("mcp_client.oauth_callback_port", MCP_OAUTH_CALLBACK_DEFAULT_PORT))
    add("cổng callback OAuth (mcp login)", True, "OS chọn (0)" if cb == 0 else str(cb))
    # 4. bot chat — chỉ "đã cấu hình" + vân tay
    for label, key in (("Telegram bot", "notify.telegram.bot_token_secret"), ("Slack bot", "notify.slack.bot_token_secret"),
                       ("Slack app (Socket Mode)", "notify.slack.app_token_secret")):
        name = str(cget(key, "") or "")
        val = _read_secret(paths, name)
        add(f"token {label}", True, f"đã cấu hình (vân tay sha256:{_fingerprint(val)} — hai hub cùng vân tay = dùng chung bot, sẽ va nhau)"
            if val else "chưa cấu hình")
    # 5. gốc workspace
    roots = [str(r) for r in (cget("workspace.allowed_roots", []) or [])] or ["~/Workspace"]
    broad = [r for r in roots if Path(os.path.expanduser(r)).resolve() in (Path.home().resolve(), (Path.home() / "Workspace").resolve(), Path("/"))]
    add("workspace.allowed_roots", not broad, ", ".join(roots), "warning")
    if broad:
        checks[-1]["detail"] += " — gốc rộng; nhiều người/dự án trên một máy nên thu hẹp: " \
                                "`cfharness config set workspace.allowed_roots '[\"~/Workspace/<dự-án>\"]'`"
    if os.environ.get("CFHARNESS_EXTRA_WORKSPACE_ROOTS") and os.environ.get("CFHARNESS_TESTING") != "1":
        add("CFHARNESS_EXTRA_WORKSPACE_ROOTS", False, "đang đặt nhưng BỊ BỎ QUA (chỉ dùng trong test) — gỡ khỏi môi trường", "warning")
    return checks


def cmd_doctor_multi(ctx: Any) -> int:
    from .ui import views as V

    checks = collect(ctx.paths)
    bad = [c for c in checks if c["level"] == "error"]
    text = "\n".join(f"{'✓' if c['ok'] else ('!' if c['level'] == 'warning' else '✗')} {c['check']}: {c['detail']}" for c in checks)
    ctx.out({"ok": not bad, "checks": checks}, text, lambda: V.doctor(checks, ctx.mode, home=str(ctx.paths.home), width=ctx.width))
    return 0 if not bad else 1
