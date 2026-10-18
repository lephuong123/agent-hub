"""Bảo mật của giao diện web (Q-11: một người dùng / một máy, chỉ localhost).

Mô hình:
- Server CHỈ bind loopback (`127.0.0.1`, `::1`, `localhost`); host khác → từ chối khi khởi động (`require_loopback`).
- Đăng nhập: `agent-hub web` in/mở URL `/login?t=<link một lần>`. Link = HMAC(khoá phiên server, nonce|hạn) — dùng
  ĐÚNG MỘT lần, hết hạn sau `LOGIN_TTL_S`. Đổi sang cookie phiên ngẫu nhiên (HttpOnly, SameSite=Strict, Path=/) rồi
  chuyển hướng về `/` (link không ở lại thanh địa chỉ; `Referrer-Policy: no-referrer`).
- Khoá ký link nằm trong RAM của server + `HUB_HOME/auth/web.json` (0600, thư mục 0700) để `agent-hub web --open` lần
  sau xin link mới mà không mở server thứ hai. Thư mục `HUB_HOME/auth` nằm trong `denyRead`/`denyWrite` của sandbox run
  (guard/runtime.py) và `permissions.deny` của tool Read/Grep/Glob — agent trong run không đọc được.
- Token NGƯỜI của daemon (`<socket>.auth`) KHÔNG dùng làm mật khẩu web và không bao giờ vào HTML/cookie/log: tiến trình
  web tự đọc file đó và gửi kèm RPC (như CLI/TUI).
- Mọi POST cần token CSRF của phiên (form ẩn hoặc header `X-CSRF-Token`) + Origin/Referer cùng nguồn; mọi request cần
  Host là loopback:port (chống DNS rebinding). Không gửi header CORS nào.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..paths import HubPaths

COOKIE = "ahub_web"
LOGIN_TTL_S = 300
SESSION_IDLE_S = 12 * 3600
SESSION_MAX_S = 7 * 24 * 3600
LOOPBACK_NAMES = ("localhost",)


class NotLoopback(ValueError):
    pass


def is_loopback(host: str) -> bool:
    h = (host or "").strip().strip("[]").lower()
    if h in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def require_loopback(host: str) -> str:
    if not is_loopback(host):
        raise NotLoopback(
            f"web chỉ được bind loopback (127.0.0.1 / ::1 / localhost) — từ chối '{host}'. "
            "Hub là một người dùng/một máy (Q-11); muốn xem từ máy khác hãy dùng SSH tunnel: ssh -L 8765:127.0.0.1:8765 <máy>"
        )
    return host


def allowed_hosts(host: str, port: int) -> set[str]:
    names = {"127.0.0.1", "localhost", "[::1]"}
    h = host.strip("[]")
    names.add(f"[{h}]" if ":" in h else h)
    out = {f"{n}:{port}" for n in names}
    if port == 80:
        out |= names
    return out


# ============================================================================ link đăng nhập một lần
def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def mint_login_token(key: bytes, ttl_s: int = LOGIN_TTL_S, now: float | None = None) -> str:
    nonce = secrets.token_urlsafe(12)
    exp = int((now or time.time()) + ttl_s)
    msg = f"{nonce}.{exp}"
    sig = _b64(hmac.new(key, msg.encode(), hashlib.sha256).digest())
    return f"{msg}.{sig}"


def check_login_token(key: bytes, token: str, now: float | None = None) -> tuple[str, int] | None:
    """(nonce, hạn) nếu chữ ký đúng và chưa hết hạn; None nếu sai. KHÔNG đánh dấu đã dùng (xem `WebAuth.redeem`)."""
    try:
        nonce, exp_s, sig = str(token).split(".")
        exp = int(exp_s)
    except (ValueError, AttributeError):
        return None
    want = _b64(hmac.new(key, f"{nonce}.{exp}".encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(want.encode(), sig.encode()):
        return None
    if exp < (now or time.time()):
        return None
    return nonce, exp


# ============================================================================ phiên
@dataclass
class Session:
    sid: str
    csrf: str
    created: float
    last: float
    flashes: list[tuple[str, str]] = field(default_factory=list)

    def flash(self, kind: str, text: str) -> None:
        self.flashes.append((kind, text))
        del self.flashes[:-8]

    def pop_flashes(self) -> list[tuple[str, str]]:
        out, self.flashes = self.flashes, []
        return out


class WebAuth:
    """Khoá ký link + phiên trong RAM (server khởi động lại → đăng nhập lại bằng `agent-hub web --open`)."""

    def __init__(self, key: bytes | None = None) -> None:
        self.key = key or secrets.token_bytes(32)
        self.sessions: dict[str, Session] = {}
        self._used: dict[str, int] = {}

    def mint(self) -> str:
        return mint_login_token(self.key)

    def redeem(self, token: str, now: float | None = None) -> Session | None:
        now = now or time.time()
        ok = check_login_token(self.key, token, now)
        if ok is None:
            return None
        nonce, exp = ok
        for n, e in list(self._used.items()):
            if e < now:
                del self._used[n]
        if nonce in self._used:
            return None  # link một lần đã dùng
        self._used[nonce] = exp
        return self.new_session(now)

    def new_session(self, now: float | None = None) -> Session:
        now = now or time.time()
        s = Session(sid=secrets.token_urlsafe(32), csrf=secrets.token_urlsafe(24), created=now, last=now)
        self.sessions[s.sid] = s
        return s

    def get(self, sid: str | None, now: float | None = None) -> Session | None:
        if not sid:
            return None
        s = self.sessions.get(sid)
        if s is None:
            return None
        now = now or time.time()
        if now - s.last > SESSION_IDLE_S or now - s.created > SESSION_MAX_S:
            self.sessions.pop(sid, None)
            return None
        s.last = now
        return s

    def logout(self, sid: str | None) -> None:
        if sid:
            self.sessions.pop(sid, None)

    @staticmethod
    def csrf_ok(session: Session, given: Any) -> bool:
        return isinstance(given, str) and bool(given) and hmac.compare_digest(given.encode(), session.csrf.encode())


# ============================================================================ file trạng thái HUB_HOME/auth/web.json
def state_path(paths: HubPaths) -> Path:
    return paths.home / "auth" / "web.json"


def write_state(paths: HubPaths, *, host: str, port: int, key: bytes) -> Path:
    d = paths.home / "auth"
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    p = state_path(paths)
    tmp = d / f".web.{os.getpid()}.tmp"
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"v": 1, "pid": os.getpid(), "host": host, "port": port, "key": key.hex(), "started_at": time.time()}, f)
    os.replace(tmp, p)
    return p


def read_state(paths: HubPaths) -> dict[str, Any] | None:
    """Trạng thái server web đang chạy (pid còn sống, file của chính user, không phải symlink); None nếu không có."""
    p = state_path(paths)
    try:
        st = os.lstat(p)
        if p.is_symlink() or st.st_uid != os.getuid():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        pid = int(data["pid"])
        os.kill(pid, 0)
        bytes.fromhex(data["key"])
        return data
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def clear_state(paths: HubPaths, pid: int | None = None) -> None:
    p = state_path(paths)
    try:
        if pid is not None:
            data = json.loads(p.read_text(encoding="utf-8"))
            if int(data.get("pid") or 0) != pid:
                return
        p.unlink()
    except (OSError, ValueError, json.JSONDecodeError):
        pass


def login_url(host: str, port: int, token: str) -> str:
    h = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"http://{h}:{port}/login?t={token}"
