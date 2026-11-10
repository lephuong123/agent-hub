"""Đăng nhập TOTP cho web public (RFC 6238 / RFC 4226, chỉ stdlib `hmac`) — docs/dev-notes/web-public.md.

Người dùng không có phiên trên máy chạy hub (không đăng nhập màn hình, không SSH) nên không mở được link một lần của
`cfharness web --open`. Thay vào đó: admin chạy `sudo -u <user> cfharness web totp enroll` qua SSH → in link đăng ký MỘT LẦN
(URI `otpauth://` chứa bí mật chỉ in khi `--show-uri`); người dùng mở link trên điện thoại, bấm "Hiện mã" (một lần duy nhất — GET không tiêu link nên bộ xem
trước link của Slack/Telegram không làm hỏng nó), thêm vào app xác thực, nhập mã để kích hoạt → có phiên web.

File (thư mục `HUB_HOME/auth/` 0700 — sandbox run `denyRead`/`denyWrite`, `permissions.deny` Read/Grep/Glob):
- `auth/totp.json` (0600): `{secret, last_step, created_at}` — bí mật ĐANG dùng; `last_step` chống dùng lại mã.
- `auth/totp_enroll.json` (0600): `{secret, token_sha256, expires, viewed}` — đăng ký đang chờ; link chỉ lưu dạng băm.
- Tắt (`disable`) / kích hoạt bí mật mới (`confirm`, `enroll(activate=True)`) → xoá mọi thiết bị tin cậy (`web/devices.py`).
Thông số: SHA-1, 6 chữ số, bước 30 s, chấp nhận lệch ±1 bước (mã cũ hơn/bằng bước đã dùng bị từ chối).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from ..oscompat import owned_by_me
from ..paths import HubPaths

STEP = 30
DIGITS = 6
WINDOW = 1
ISSUER = "cfharness"
ACTIVE = "totp.json"
PENDING = "totp_enroll.json"
ENROLL_TTL_S = 24 * 3600


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _key(secret_b32: str) -> bytes:
    s = secret_b32.strip().replace(" ", "").upper()
    return base64.b32decode(s + "=" * (-len(s) % 8))


def hotp(secret_b32: str, counter: int, digits: int = DIGITS) -> str:
    mac = hmac.new(_key(secret_b32), struct.pack(">Q", counter), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def totp(secret_b32: str, at: float | None = None) -> str:
    return hotp(secret_b32, int((time.time() if at is None else at) // STEP))


def match_step(secret_b32: str, code: Any, *, now: float | None = None, after_step: int = -1, window: int = WINDOW) -> int | None:
    """Bước thời gian khớp `code` (> `after_step`, trong ±window); None nếu sai. So sánh hằng thời gian."""
    c = str(code or "").strip().replace(" ", "")
    if len(c) != DIGITS or not c.isdigit():
        return None
    cur = int((time.time() if now is None else now) // STEP)
    hit = None
    for st in range(cur - window, cur + window + 1):
        if hmac.compare_digest(hotp(secret_b32, st), c) and st > after_step:
            hit = st if hit is None else hit
    return hit


def otpauth_uri(secret_b32: str, account: str, issuer: str = ISSUER) -> str:
    label = quote(f"{issuer}:{account}", safe="")
    return f"otpauth://totp/{label}?" + urlencode({"secret": secret_b32, "issuer": issuer, "algorithm": "SHA1", "digits": DIGITS, "period": STEP})


def qr_svg(uri: str) -> str | None:
    """Mã QR (SVG nội tuyến, chỉ `<svg>/<path>` thuộc tính trình bày — không `style`/script, hợp CSP `'self'`) cho URI
    `otpauth://`, để quét bằng Google Authenticator/Authy/1Password. Sinh ở server (`segno`, Python thuần, không CDN, bí mật
    không rời trang). Không có `segno` → None (trang chỉ hiện khoá nhập tay)."""
    try:
        import segno
    except ImportError:  # pragma: no cover
        return None
    svg = segno.make(uri, error="m").svg_inline(scale=1, border=4, dark="#000", light="#fff", omitsize=True)
    return svg.replace("<svg ", '<svg role="img" aria-label="Mã QR đăng ký ứng dụng xác thực" ', 1)


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


class TotpStore:
    """Đọc/ghi bí mật TOTP trong `HUB_HOME/auth/`. Dùng bởi tiến trình web (đăng nhập) và CLI của người (đăng ký)."""

    def __init__(self, paths: HubPaths) -> None:
        self.paths = paths
        self.dir: Path = paths.home / "auth"
        self.devices_forgotten = 0  # số thiết bị tin cậy vừa bị xoá ở lần disable/confirm/enroll(activate) gần nhất

    def _forget_devices(self) -> None:
        """Bí mật TOTP đổi / bị tắt → mọi thiết bị tin cậy hết hiệu lực (web/devices.py; `totp_fp` là lớp dự phòng)."""
        from .devices import forget_all

        self.devices_forgotten = forget_all(self.paths)

    # ------------------------------------------------------------------ file
    def _write(self, name: str, data: dict[str, Any]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        tmp = self.dir / f".{name}.{os.getpid()}.tmp"
        tmp.unlink(missing_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, self.dir / name)

    def _read(self, name: str) -> dict[str, Any] | None:
        p = self.dir / name
        try:
            st = os.lstat(p)
            if p.is_symlink() or not owned_by_me(st):
                return None
            d = json.loads(p.read_text(encoding="utf-8"))
            _key(str(d["secret"]))
            return d if isinstance(d, dict) else None
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return None

    # ------------------------------------------------------------------ trạng thái
    def enrolled(self) -> bool:
        return self._read(ACTIVE) is not None

    def fingerprint(self) -> str | None:
        """`sha256(bí mật đang dùng)[:16]` — thiết bị tin cậy gắn với giá trị này (đổi bí mật = mọi thiết bị vô hiệu). None nếu chưa bật."""
        a = self._read(ACTIVE)
        return _sha(str(a["secret"]))[:16] if a else None

    def status(self, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        a, p = self._read(ACTIVE), self._read(PENDING)
        pend = bool(p and float(p.get("expires") or 0) > now)
        return {"enrolled": a is not None, "enrolled_at": (a or {}).get("created_at"), "pending": pend,
                "pending_expires": (p or {}).get("expires") if pend else None, "pending_viewed": bool(p and p.get("viewed")) if pend else None}

    def disable(self) -> bool:
        gone = False
        for n in (ACTIVE, PENDING):
            try:
                (self.dir / n).unlink()
                gone = True
            except FileNotFoundError:
                pass
        self._forget_devices()
        return gone

    # ------------------------------------------------------------------ đăng ký
    def enroll(self, *, ttl_s: int = ENROLL_TTL_S, activate: bool = False, now: float | None = None) -> tuple[str, str]:
        """Bí mật mới + link đăng ký một lần (trả `(secret, token)`). `activate` = dùng ngay (admin đã trao URI tận tay)."""
        now = time.time() if now is None else now
        secret, token = new_secret(), secrets.token_urlsafe(24)
        self._write(PENDING, {"v": 1, "secret": secret, "token_sha256": _sha(token), "expires": now + ttl_s, "viewed": False, "created_at": now})
        if activate:
            self._write(ACTIVE, {"v": 1, "secret": secret, "last_step": -1, "created_at": now})
            self._forget_devices()
        return secret, token

    def pending(self, token: Any, now: float | None = None) -> dict[str, Any] | None:
        now = time.time() if now is None else now
        p = self._read(PENDING)
        if not p or not isinstance(token, str) or not token or float(p.get("expires") or 0) <= now:
            return None
        if not hmac.compare_digest(str(p.get("token_sha256") or ""), _sha(token)):
            return None
        return p

    def reveal(self, token: Any, now: float | None = None) -> str | None:
        """Bí mật đang chờ cho link `token` — CHỈ MỘT LẦN (lần sau None). GET trang đăng ký không gọi hàm này."""
        p = self.pending(token, now)
        if p is None or p.get("viewed"):
            return None
        p["viewed"] = True
        self._write(PENDING, p)
        return str(p["secret"])

    def confirm(self, token: Any, code: Any, now: float | None = None) -> bool:
        """Mã đúng với bí mật đang chờ (đã hiện) → thành bí mật đang dùng, link bị tiêu."""
        now = time.time() if now is None else now
        p = self.pending(token, now)
        if p is None or not p.get("viewed"):
            return False
        st = match_step(str(p["secret"]), code, now=now)
        if st is None:
            return False
        self._write(ACTIVE, {"v": 1, "secret": p["secret"], "last_step": st, "created_at": now})
        (self.dir / PENDING).unlink(missing_ok=True)
        self._forget_devices()
        return True

    # ------------------------------------------------------------------ đăng nhập
    def verify(self, code: Any, now: float | None = None) -> bool:
        """Mã đăng nhập đúng và CHƯA dùng (bước > last_step) → ghi last_step, True."""
        a = self._read(ACTIVE)
        if a is None:
            return False
        st = match_step(str(a["secret"]), code, now=now, after_step=int(a.get("last_step", -1)))
        if st is None:
            return False
        a["last_step"] = st
        self._write(ACTIVE, a)
        return True
