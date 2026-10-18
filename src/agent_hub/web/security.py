"""Bảo mật của giao diện web (Q-11: một người dùng / một máy, chỉ localhost).

Mô hình:
- Server CHỈ bind loopback (`127.0.0.1`, `::1`, `localhost`); host khác → từ chối khi khởi động (`require_loopback`).
- WT-1: trình duyệt dùng host riêng `agent-hub.localhost:<port>` (cookie host-only, không gửi tới 127.0.0.1:*/localhost:* nơi dev
  server agent viết chạy); 127.0.0.1/localhost → 303 sang host riêng, không đặt cookie. Cookie KHÔNG tách theo cổng nên còn lớp 2:
  mọi POST cần `Session.key` (bí mật ngoài cookie, trao qua fragment `#k=` lúc đăng nhập, JS giữ trong localStorage, không bao giờ
  nằm trong HTML). Phiên ngắn: idle 1 h (polling/SSE không gia hạn), tối đa 12 h.
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
import re
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..paths import HubPaths

COOKIE = "ahub_web"
KEY_FIELD = "k"  # bí mật phiên NGOÀI cookie (WT-1): form field / header X-Ahub-Key, trình duyệt giữ trong localStorage của origin web
KEY_HEADER = "x-ahub-key"
LOGIN_TTL_S = 300
SESSION_IDLE_S = 3600  # WT-1: phiên ngắn, gia hạn khi người dùng thao tác (không gia hạn bởi polling/SSE)
SESSION_MAX_S = 12 * 3600
LOOPBACK_NAMES = ("localhost",)
# WT-1: tên host riêng → cookie host-only KHÔNG được gửi tới 127.0.0.1:* / localhost:* (dev server agent viết).
# Chrome/Firefox/Safari + resolver macOS/glibc phân giải *.localhost về loopback (RFC 6761).
WEB_HOST = "agent-hub.localhost"


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


def valid_web_name(name: str) -> bool:
    """Tên host công khai của web: `*.localhost` hoặc loopback (chế độ dự phòng kém an toàn hơn — xem web.md)."""
    n = (name or "").strip().lower()
    return n.endswith(".localhost") and re.fullmatch(r"[a-z0-9.-]+", n) is not None or is_loopback(n)


def allowed_hosts(name: str, port: int) -> set[str]:
    """Host header DUY NHẤT được phục vụ (chống DNS rebinding + không đặt cookie trên 127.0.0.1)."""
    n = name.strip("[]").lower()
    n = f"[{n}]" if ":" in n else n
    return {f"{n}:{port}"} | ({n} if port == 80 else set())


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
    key: str = ""  # bí mật NGOÀI cookie, không bao giờ nằm trong HTML (WT-1)
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
        s = Session(sid=secrets.token_urlsafe(32), csrf=secrets.token_urlsafe(24), created=now, last=now, key=secrets.token_urlsafe(32))
        self.sessions[s.sid] = s
        return s

    def get(self, sid: str | None, now: float | None = None, *, touch: bool = True) -> Session | None:
        if not sid:
            return None
        s = self.sessions.get(sid)
        if s is None:
            return None
        now = now or time.time()
        if now - s.last > SESSION_IDLE_S or now - s.created > SESSION_MAX_S:
            self.sessions.pop(sid, None)
            return None
        if touch:
            s.last = now
        return s

    def logout(self, sid: str | None) -> None:
        if sid:
            self.sessions.pop(sid, None)

    @staticmethod
    def key_ok(session: Session, given: Any) -> bool:
        return isinstance(given, str) and bool(given) and bool(session.key) and hmac.compare_digest(given.encode(), session.key.encode())

    @staticmethod
    def csrf_ok(session: Session, given: Any) -> bool:
        return isinstance(given, str) and bool(given) and hmac.compare_digest(given.encode(), session.csrf.encode())


# ============================================================================ file trạng thái HUB_HOME/auth/web.json
def state_path(paths: HubPaths) -> Path:
    return paths.home / "auth" / "web.json"


def write_state(paths: HubPaths, *, host: str, port: int, key: bytes, name: str = WEB_HOST) -> Path:
    d = paths.home / "auth"
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    p = state_path(paths)
    tmp = d / f".web.{os.getpid()}.tmp"
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"v": 1, "pid": os.getpid(), "host": host, "name": name, "port": port, "key": key.hex(), "started_at": time.time()}, f)
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
