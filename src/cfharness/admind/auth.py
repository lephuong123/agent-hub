"""Xác thực tài khoản quản trị trong admind (FR-SA-AUTH): mật khẩu scrypt + TOTP, phiên (ticket) trong RAM, step-up, chống dò.

- Mật khẩu: `scrypt$n$r$p$salt_hex$hash_hex` (stdlib `hashlib.scrypt`), so sánh hằng thời gian.
- TOTP: secret trong `vault` (`totp.<admin_id>`), không trong DB; chống dùng lại mã bằng `totp_step` (bước đã dùng gần nhất).
- Ticket: 32 byte ngẫu nhiên; RAM admind giữ bản băm → SAC chỉ giữ chuỗi ticket. admind khởi động lại = mọi phiên hết (an toàn hơn).
  Hết hạn: `IDLE_S` không dùng hoặc `MAX_S` kể từ đăng nhập (AUTH-04). `strong_at` = lần nhập TOTP gần nhất (AUTH-05).
- Kích hoạt tài khoản mới: `admin.create` sinh mã kích hoạt một lần (24 giờ); người được mời tự đặt mật khẩu + quét TOTP.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..web import totp as _totp
from ..web.public import LoginLimiter
from .db import SacDB, loads
from .vault import Vault

IDLE_S = 15 * 60
MAX_S = 8 * 3600
STEP_UP_S = 5 * 60
ENROLL_TTL_S = 24 * 3600
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
ACCOUNT_STATES = ("pending", "active", "locked")


# ============================================================================ mật khẩu
def hash_password(pw: str, *, n: int = SCRYPT_N, r: int = SCRYPT_R, p: int = SCRYPT_P) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=n, r=r, p=p, maxmem=256 * 1024 * 1024, dklen=32)
    return f"scrypt${n}${r}${p}${salt.hex()}${h.hex()}"


def check_password(pw: str, stored: str | None) -> bool:
    try:
        algo, n, r, p, salt, h = str(stored or "").split("$")
        if algo != "scrypt":
            return False
        got = hashlib.scrypt(pw.encode("utf-8"), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), maxmem=256 * 1024 * 1024,
                             dklen=len(bytes.fromhex(h)))
        return hmac.compare_digest(got, bytes.fromhex(h))
    except (ValueError, TypeError):
        return False


_DUMMY = None


def _dummy_hash() -> str:
    global _DUMMY
    if _DUMMY is None:
        _DUMMY = hash_password(secrets.token_hex(8))
    return _DUMMY


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ============================================================================ phiên
@dataclass
class Ticket:
    key: str                # sha256(ticket)
    admin_id: str
    created: float
    last_used: float
    strong_at: float
    ip: str = ""
    ua: str = ""
    sid: str = field(default_factory=lambda: secrets.token_hex(6))   # mã công khai để liệt kê/thu hồi (AUTH-11)


class AuthError(Exception):
    def __init__(self, code: str, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


class AuthService:
    def __init__(self, db: SacDB, vault: Vault, *, clock: Callable[[], float] = time.time, scrypt_n: int = SCRYPT_N) -> None:
        self.db, self.vault, self.clock = db, vault, clock
        self.scrypt_n = scrypt_n
        self.tickets: dict[str, Ticket] = {}
        self.lock = threading.RLock()
        self.limiter = LoginLimiter(clock=clock)
        self.email_limiter = LoginLimiter(clock=clock, global_max_failures=10**9, day_max_failures=10**9)

    # ------------------------------------------------------------------ tài khoản
    def account(self, admin_id: str) -> dict[str, Any] | None:
        a = self.db.one("SELECT * FROM admin_accounts WHERE id=?", (admin_id,))
        return self._pub(a) if a else None

    def account_by_email(self, email: str) -> dict[str, Any] | None:
        a = self.db.one("SELECT * FROM admin_accounts WHERE email=?", (email.lower(),))
        return self._pub(a) if a else None

    @staticmethod
    def _pub(a: dict[str, Any]) -> dict[str, Any]:
        out = {k: a[k] for k in ("id", "email", "name", "role", "state", "created_at", "created_by", "last_login_at", "last_login_ip",
                                 "user_id", "totp_on")}
        out["scope"] = loads(a.get("scope"), {}) or {}
        return out

    def count_super(self, *, active_only: bool = True) -> int:
        sql = "SELECT COUNT(*) AS n FROM admin_accounts WHERE role='super_admin'" + (" AND state='active'" if active_only else "")
        return int((self.db.one(sql) or {"n": 0})["n"])

    def create(self, *, email: str, name: str, role: str, scope: dict[str, Any] | None, created_by: str | None,
               user_id: str | None = None) -> tuple[dict[str, Any], str]:
        """Tạo tài khoản ở trạng thái `pending` + mã kích hoạt một lần (trả về MỘT lần, chỉ lưu băm)."""
        from .db import dumps, new_id

        if self.account_by_email(email):
            raise AuthError("conflict", "email đã có tài khoản quản trị")
        aid, code, now = new_id("adm"), secrets.token_urlsafe(24), self.clock()
        self.db.exec("INSERT INTO admin_accounts(id, email, name, role, scope, state, enroll_hash, enroll_until, created_at, created_by, user_id)"
                     " VALUES (?,?,?,?,?,'pending',?,?,?,?,?)",
                     (aid, email.lower(), name, role, dumps(scope or {}), _sha(code), now + ENROLL_TTL_S, now, created_by, user_id))
        return self.account(aid) or {}, code

    def reissue_enroll(self, admin_id: str) -> str:
        """Đặt lại TOTP + mật khẩu (AUTH-08 'đặt lại TOTP'): về `pending`, huỷ mọi phiên, mã kích hoạt mới."""
        code = secrets.token_urlsafe(24)
        self.db.exec("UPDATE admin_accounts SET state='pending', pw_hash=NULL, totp_on=0, totp_step=-1, enroll_hash=?, enroll_until=? WHERE id=?",
                     (_sha(code), self.clock() + ENROLL_TTL_S, admin_id))
        self.vault.delete(f"totp.{admin_id}")
        self.revoke_all(admin_id)
        return code

    def _by_enroll(self, code: str) -> dict[str, Any]:
        a = self.db.one("SELECT * FROM admin_accounts WHERE enroll_hash=? AND state='pending'", (_sha(code or ""),))
        if not a or float(a["enroll_until"] or 0) < self.clock():
            raise AuthError("not_found", "mã kích hoạt không hợp lệ hoặc đã hết hạn")
        return a

    def enroll_begin(self, code: str) -> dict[str, Any]:
        """Bước 1 kích hoạt: sinh (hoặc giữ) secret TOTP tạm, trả URI otpauth + QR SVG để quét. Chưa bật cho tới `enroll_finish`."""
        a = self._by_enroll(code)
        pend = f"totppend.{a['id']}"
        sec = self.vault.get(pend)
        if not sec:
            sec = _totp.new_secret()
            self.vault.set(pend, sec)
        uri = _totp.otpauth_uri(sec, a["email"], issuer="cfharness SAC")
        return {"email": a["email"], "name": a["name"], "role": a["role"], "otpauth": uri, "qr_svg": _totp.qr_svg(uri), "secret": sec}

    def enroll_finish(self, code: str, password: str, totp_code: str) -> dict[str, Any]:
        a = self._by_enroll(code)
        pend = f"totppend.{a['id']}"
        sec = self.vault.get(pend)
        if not sec:
            raise AuthError("invalid", "chưa bắt đầu kích hoạt (mở lại link)")
        step = _totp.match_step(sec, totp_code, now=self.clock())
        if step is None:
            raise AuthError("invalid", "mã TOTP không đúng")
        self.vault.set(f"totp.{a['id']}", sec)
        self.vault.delete(pend)
        self.db.exec("UPDATE admin_accounts SET pw_hash=?, totp_on=1, totp_step=?, state='active', enroll_hash=NULL, enroll_until=NULL WHERE id=?",
                     (hash_password(password, n=self.scrypt_n), step, a["id"]))
        return self.account(a["id"]) or {}

    def set_state(self, admin_id: str, state: str) -> None:
        if state not in ACCOUNT_STATES:
            raise AuthError("invalid", "trạng thái không hợp lệ")
        self.db.exec("UPDATE admin_accounts SET state=? WHERE id=?", (state, admin_id))
        if state != "active":
            self.revoke_all(admin_id)

    # ------------------------------------------------------------------ đăng nhập
    def _check_totp(self, a: dict[str, Any], code: str) -> bool:
        sec = self.vault.get(f"totp.{a['id']}")
        if not sec or not a.get("totp_on"):
            return False
        step = _totp.match_step(sec, code, now=self.clock(), after_step=int(a.get("totp_step") or -1))
        if step is None:
            return False
        self.db.exec("UPDATE admin_accounts SET totp_step=? WHERE id=?", (step, a["id"]))
        return True

    def login(self, email: str, password: str, totp_code: str, *, ip: str = "", ua: str = "") -> tuple[str, dict[str, Any]]:
        ipk = ip or "?"
        for lim, k in ((self.limiter, ipk), (self.email_limiter, email.lower())):
            lk = lim.locked(k)
            if lk:
                raise AuthError("locked", "tạm khoá do nhập sai nhiều lần", {"retry_s": int(lk[0]) + 1})
        a = self.db.one("SELECT * FROM admin_accounts WHERE email=?", (email.lower(),))
        pw_ok = check_password(password, a["pw_hash"] if a else _dummy_hash())
        ok = bool(a) and pw_ok and a["state"] == "active" and self._check_totp(a, totp_code)
        if not ok:
            scope = self.limiter.fail(ipk)
            self.email_limiter.fail(email.lower())
            raise AuthError("unauthenticated", "email, mật khẩu hoặc mã TOTP không đúng", {"locked": scope} if scope else None)
        self.limiter.success(ipk)
        self.email_limiter.success(email.lower())
        now = self.clock()
        self.db.exec("UPDATE admin_accounts SET last_login_at=?, last_login_ip=? WHERE id=?", (now, ip[:64] or None, a["id"]))
        tok = secrets.token_urlsafe(32)
        with self.lock:
            self.tickets[_sha(tok)] = Ticket(key=_sha(tok), admin_id=a["id"], created=now, last_used=now, strong_at=now, ip=ip[:64], ua=ua[:200])
        return tok, self.account(a["id"]) or {}

    def resolve(self, token: str | None, *, touch: bool = True) -> tuple[Ticket, dict[str, Any]]:
        if not token or not isinstance(token, str):
            raise AuthError("unauthenticated", "chưa đăng nhập")
        now = self.clock()
        with self.lock:
            t = self.tickets.get(_sha(token))
            if t is None or now - t.last_used > IDLE_S or now - t.created > MAX_S:
                if t is not None:
                    self.tickets.pop(t.key, None)
                raise AuthError("unauthenticated", "phiên đã hết hạn")
            a = self.account(t.admin_id)
            if not a or a["state"] != "active":
                self.tickets.pop(t.key, None)
                raise AuthError("unauthenticated", "tài khoản không còn hoạt động")
            if touch:
                t.last_used = now
            return t, a

    def step_up(self, token: str, code: str) -> None:
        t, _a = self.resolve(token)
        raw = self.db.one("SELECT * FROM admin_accounts WHERE id=?", (t.admin_id,)) or {}
        if not self._check_totp(raw, code):
            self.limiter.fail(t.ip or "?")
            raise AuthError("unauthenticated", "mã TOTP không đúng")
        t.strong_at = self.clock()

    def strong(self, t: Ticket) -> bool:
        return self.clock() - t.strong_at <= STEP_UP_S

    def logout(self, token: str | None) -> None:
        if token:
            with self.lock:
                self.tickets.pop(_sha(token), None)

    def sessions(self, admin_id: str | None = None) -> list[dict[str, Any]]:
        now = self.clock()
        with self.lock:
            out = [{"sid": t.sid, "admin_id": t.admin_id, "created": t.created, "last_used": t.last_used, "ip": t.ip, "ua": t.ua}
                   for t in self.tickets.values() if (admin_id is None or t.admin_id == admin_id)
                   and now - t.last_used <= IDLE_S and now - t.created <= MAX_S]
        return sorted(out, key=lambda x: -x["last_used"])

    def revoke_sid(self, sid: str, *, admin_id: str | None = None) -> bool:
        with self.lock:
            for k, t in list(self.tickets.items()):
                if t.sid == sid and (admin_id is None or t.admin_id == admin_id):
                    self.tickets.pop(k, None)
                    return True
        return False

    def revoke_all(self, admin_id: str) -> int:
        with self.lock:
            ks = [k for k, t in self.tickets.items() if t.admin_id == admin_id]
            for k in ks:
                self.tickets.pop(k, None)
        return len(ks)
