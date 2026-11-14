"""Web public qua tunnel TLS (Cloudflare Tunnel / Tailscale Funnel / reverse proxy bất kỳ) — docs/dev-notes/web-public.md.

Bind VẪN loopback (`security.require_loopback` không đổi): tunnel chạy trên CHÍNH máy này và proxy về `127.0.0.1:<cổng>`.
`web.public_origin` (vd `https://an.example.com`, chỉ người đặt — nhánh `web` ∈ integrity.SECURITY_KEYS, `config.set` ∈ HUMAN_ONLY,
không có trong danh mục Cài đặt của web) thêm ĐÚNG một Host + một Origin vào danh sách hợp lệ; cookie `Secure` trên host đó.

- `client_ip`: IP thật của người truy cập chỉ lấy từ header của proxy KHI có public_origin VÀ kết nối tới từ loopback (tunnel cục bộ);
  ngoài ra luôn là địa chỉ peer — header client tự gửi không bao giờ được tin.
- `LoginLimiter`: chống dò mã TOTP — theo IP, toàn cục (mọi IP gộp lại: kẻ đổi IP liên tục vẫn bị chặn) và trần theo ngày. Chỉ RAM
  của tiến trình web (khởi động lại web = xoá khoá; kẻ tấn công từ ngoài không khởi động lại được).
"""

from __future__ import annotations

import ipaddress
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlsplit

from .security import is_loopback

IP_HEADERS = ("x-forwarded-for", "cf-connecting-ip", "none")
LOGIN_KEYS = ("ip_max_failures", "global_max_failures", "window_s", "lockout_s", "day_max_failures", "day_lockout_s")
_HOST_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")


def parse_public_origin(value: Any) -> str | None:
    """Chuẩn hoá `web.public_origin` → `https://host[:port]` (chữ thường); None nếu rỗng. Sai dạng → ValueError.

    Chỉ https (tunnel có TLS — cookie phiên phải `Secure`), không path/query/fragment/userinfo, host là tên miền (không IP,
    không `*.localhost` — đã có host cục bộ riêng)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str):
        raise ValueError("web.public_origin phải là chuỗi dạng https://ten.mien")
    v = value.strip()
    u = urlsplit(v)
    if u.scheme.lower() != "https":
        raise ValueError("web.public_origin phải là https://… (tunnel có TLS: Cloudflare Tunnel / Tailscale Funnel)")
    if u.username or u.password or (u.path not in ("", "/")) or u.query or u.fragment or "@" in u.netloc:
        raise ValueError("web.public_origin chỉ gồm https://host[:cổng] — không path, query, fragment hay user")
    host = (u.hostname or "").lower()
    if not _HOST_RE.match(host) or host.endswith(".localhost") or is_loopback(host):
        raise ValueError(f"web.public_origin: host không hợp lệ '{host}' (cần tên miền công khai, vd an.example.com)")
    if re.fullmatch(r"[0-9.]+", host):
        raise ValueError("web.public_origin: dùng tên miền, không dùng địa chỉ IP")
    try:
        port = u.port
    except ValueError:
        raise ValueError("web.public_origin: cổng không hợp lệ") from None
    return f"https://{host}" + (f":{port}" if port and port != 443 else "")


def public_hosts(origin: str | None) -> set[str]:
    """Các giá trị Host header chấp nhận cho origin công khai (`host`, và `host:443` khi cổng mặc định)."""
    if not origin:
        return set()
    u = urlsplit(origin)
    host = (u.hostname or "").lower()
    if u.port and u.port != 443:
        return {f"{host}:{u.port}"}
    return {host, f"{host}:443"}


def _int(key: str, value: Any, lo: int, hi: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
        raise ValueError(f"{key} phải là số nguyên {lo}..{hi}")


def validate_setting(key: str, value: Any) -> None:
    """Kiểm `config set web[.…]` (gọi từ `config.validate_setting`) — sai → ValueError, không ghi."""
    if key == "web":
        if not isinstance(value, dict):
            raise ValueError("web phải là mapping")
        for k, v in value.items():
            validate_setting(f"web.{k}", v)
        return
    if key == "web.public_origin":
        parse_public_origin(value)
    elif key == "web.session_max_age_s":
        _int(key, value, 300, 30 * 86400)
    elif key == "web.session_idle_s":
        _int(key, value, 60, 30 * 86400)
    elif key == "web.client_ip_header":
        if value not in IP_HEADERS:
            raise ValueError(f"web.client_ip_header phải là {'|'.join(IP_HEADERS)}")
    elif key == "web.login":
        if not isinstance(value, dict):
            raise ValueError("web.login phải là mapping")
        for k, v in value.items():
            validate_setting(f"web.login.{k}", v)
    elif key == "web.trusted_devices" or key.startswith("web.trusted_devices."):
        from .devices import validate_setting as _dev

        _dev(key, value)
    elif key.startswith("web.login."):
        sub = key.split(".", 2)[2]
        if sub not in LOGIN_KEYS:
            raise ValueError(f"khoá lạ {key} (có: {', '.join(LOGIN_KEYS)})")
        _int(key, value, 1, 30 * 86400)
    else:
        raise ValueError(f"khoá lạ {key} (web.public_origin | session_max_age_s | session_idle_s | client_ip_header | login.* | trusted_devices.*)")


# ============================================================================ IP thật của client
def client_ip(scope: dict[str, Any], headers: dict[str, str], *, public: bool, header: str = "x-forwarded-for") -> str:
    """IP dùng cho giới hạn tốc độ/audit. Header proxy CHỈ được tin khi có public_origin (`public`) VÀ peer là loopback."""
    peer = str((scope.get("client") or ("", 0))[0] or "")
    if not public or header == "none" or not is_loopback(peer):
        return peer or "?"
    if header == "cf-connecting-ip":
        raw = headers.get("cf-connecting-ip", "")
    else:  # phần tử CUỐI do proxy gần nhất (tunnel) thêm — phần đầu client tự đặt được
        parts = [p.strip() for p in headers.get("x-forwarded-for", "").split(",") if p.strip()]
        raw = parts[-1] if parts else ""
    try:
        return str(ipaddress.ip_address(raw.strip().strip("[]")))
    except ValueError:
        return peer or "?"


# ============================================================================ giới hạn tốc độ đăng nhập
@dataclass
class LoginLimiter:
    ip_max_failures: int = 5
    global_max_failures: int = 10
    window_s: int = 900
    lockout_s: int = 900
    day_max_failures: int = 50
    day_lockout_s: int = 86400
    clock: Callable[[], float] = time.time
    _ip: dict[str, deque] = field(default_factory=dict)
    _all: deque = field(default_factory=deque)
    _day: deque = field(default_factory=deque)
    _ip_until: dict[str, float] = field(default_factory=dict)
    _all_until: float = 0.0

    @classmethod
    def from_config(cls, cfg: dict[str, Any] | None, clock: Callable[[], float] = time.time) -> "LoginLimiter":
        c = {k: int(v) for k, v in (cfg or {}).items() if k in LOGIN_KEYS and isinstance(v, int) and not isinstance(v, bool) and v > 0}
        return cls(**c, clock=clock)

    @staticmethod
    def _trim(q: deque, since: float) -> None:
        while q and q[0] < since:
            q.popleft()

    def locked(self, ip: str) -> tuple[float, str] | None:
        """(giây còn lại, phạm vi 'all'|'ip') nếu đang khoá; None nếu được thử."""
        now = self.clock()
        if self._all_until > now:
            return self._all_until - now, "all"
        u = self._ip_until.get(ip, 0.0)
        if u > now:
            return u - now, "ip"
        return None

    def fail(self, ip: str) -> str | None:
        """Ghi một lần sai. Trả phạm vi vừa bị khoá ('ip'|'all'|'day') hoặc None."""
        now = self.clock()
        q = self._ip.setdefault(ip, deque())
        for d in (q, self._all):
            d.append(now)
            self._trim(d, now - self.window_s)
        self._day.append(now)
        self._trim(self._day, now - 86400)
        if len(self._ip) > 10_000:  # chặn phình bộ nhớ khi bị quét từ rất nhiều IP
            for k in [k for k, v in self._ip.items() if not v][:5000]:
                self._ip.pop(k, None)
        if len(self._day) >= self.day_max_failures:
            self._all_until = max(self._all_until, now + self.day_lockout_s)
            return "day"
        if len(self._all) >= self.global_max_failures:
            self._all_until = max(self._all_until, now + self.lockout_s)
            self._all.clear()
            return "all"
        if len(q) >= self.ip_max_failures:
            self._ip_until[ip] = now + self.lockout_s
            q.clear()
            return "ip"
        return None

    def success(self, ip: str) -> None:
        self._ip.pop(ip, None)
        self._ip_until.pop(ip, None)
