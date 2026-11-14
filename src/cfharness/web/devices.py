"""Thiết bị tin cậy cho đăng nhập web TOTP ("Tin cậy thiết bị này") — docs/dev-notes/plan-trusted-device.md, web-public.md.

Cookie thiết bị hợp lệ thay ĐÚNG bước nhập mã TOTP: đổi thành phiên mới bình thường (cookie phiên + khoá `k` mới qua `#k=`).
Link một lần không cấp cookie thiết bị. CHỈ bật trên host public https (cookie `__Host-…`, Secure); trên `http://cfharness.localhost`
cookie không tách theo cổng — dev server agent viết ở cổng khác cùng host sẽ nhận cookie → đổi được thành phiên kèm `k` mới → phá lớp 2
của WT-1, nên không bao giờ bật ở đó.

Token = `<did>.<secret>` (did 9 byte, secret 32 byte ngẫu nhiên); file chỉ lưu `sha256(secret)` — so `hmac.compare_digest`.
- Hạn TUYỆT ĐỐI (`expires_at`, mặc định 30 ngày, trần 90) — không trượt khi dùng.
- Xoay mỗi lần dùng: hash cũ còn hợp lệ `rotate_grace_s` (60 s, hai tab cùng lúc); token cũ sau đó → thu hồi thiết bị + `reuse`.
- Gắn `totp_fp = sha256(bí mật TOTP)[:16]`: tắt/đăng ký lại TOTP → mọi thiết bị vô hiệu (ngoài việc `TotpStore` xoá file).
- Tối đa `max_devices` (5, trần cứng 20): vượt → loại thiết bị lâu không dùng nhất.
- Không ràng buộc IP/UA (tunnel/di động đổi IP) — chỉ lưu `ua_label` + IP để audit/thông báo.

File `HUB_HOME/auth/trusted_devices.json` (0600, thư mục 0700 — sandbox run denyRead/denyWrite `auth/`); ghi nguyên tử
(`O_EXCL|O_NOFOLLOW` + `os.replace`), khoá `auth/.devices.lock` (web + CLI cùng ghi). File là symlink / không thuộc user → coi như rỗng.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Iterator

from ..oscompat import owned_by_me
from ..paths import HubPaths

FILE = "trusted_devices.json"
LOCK = ".devices.lock"
DEFAULTS: dict[str, Any] = {"enabled": True, "ttl_days": 30, "max_devices": 5, "rotate_grace_s": 60, "allow_port": False}
LIMITS: dict[str, tuple[int, int]] = {"ttl_days": (1, 90), "max_devices": (1, 20), "rotate_grace_s": (0, 300)}
KEYS = tuple(DEFAULTS)
DID_RE = re.compile(r"^[A-Za-z0-9_-]{8,24}$")
_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{20,64}$")


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def settings(cfg: dict[str, Any] | None) -> dict[str, Any]:
    """`web.trusted_devices` đã kẹp giới hạn (giá trị sai → mặc định)."""
    out = dict(DEFAULTS)
    for k, v in (cfg or {}).items():
        if k not in DEFAULTS:
            continue
        if isinstance(DEFAULTS[k], bool):
            if isinstance(v, bool):
                out[k] = v
        elif isinstance(v, int) and not isinstance(v, bool):
            lo, hi = LIMITS[k]
            out[k] = min(hi, max(lo, v))
    return out


def validate_setting(key: str, value: Any) -> None:
    """`config set web.trusted_devices[.…]` — sai → ValueError (gọi từ `public.validate_setting`)."""
    if key == "web.trusted_devices":
        if not isinstance(value, dict):
            raise ValueError("web.trusted_devices phải là mapping")
        for k, v in value.items():
            validate_setting(f"web.trusted_devices.{k}", v)
        return
    sub = key.split(".", 2)[2] if key.count(".") >= 2 else ""
    if sub not in DEFAULTS:
        raise ValueError(f"khoá lạ {key} (có: {', '.join(KEYS)})")
    if isinstance(DEFAULTS[sub], bool):
        if not isinstance(value, bool):
            raise ValueError(f"{key} phải là true/false")
        return
    lo, hi = LIMITS[sub]
    if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
        raise ValueError(f"{key} phải là số nguyên {lo}..{hi}")


# ============================================================================ nhãn trình duyệt (không lưu UA nguyên văn)
_BROWSERS = (("Edg/", "Edge"), ("OPR/", "Opera"), ("Firefox/", "Firefox"), ("FxiOS/", "Firefox"), ("CriOS/", "Chrome"),
             ("Chrome/", "Chrome"), ("Safari/", "Safari"))
_OSES = (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("Mac OS X", "macOS"), ("Macintosh", "macOS"),
         ("Windows", "Windows"), ("CrOS", "ChromeOS"), ("Linux", "Linux"))


def ua_label(ua: Any) -> str:
    """User-Agent → nhãn ngắn từ DANH SÁCH CỐ ĐỊNH ("Chrome · macOS"). Không bao giờ trả chữ do client tự đặt."""
    s = str(ua or "")[:512]
    b = next((name for k, name in _BROWSERS if k in s), "Trình duyệt khác")
    o = next((name for k, name in _OSES if k in s), "")
    return f"{b} · {o}" if o else b


def _clean_ip(ip: Any) -> str:
    import ipaddress

    try:
        return str(ipaddress.ip_address(str(ip or "").strip().strip("[]")))
    except ValueError:
        return "?"


# ============================================================================ kho
class DeviceStore:
    """Đọc/ghi `auth/trusted_devices.json`. Dùng bởi tiến trình web (tin cậy/đổi token) và CLI của người (liệt kê/thu hồi)."""

    def __init__(self, paths: HubPaths, cfg: dict[str, Any] | None = None) -> None:
        self.paths = paths
        self.dir: Path = paths.home / "auth"
        self.cfg = settings(cfg)
        self._cache: tuple[Any, dict[str, Any]] | None = None

    @property
    def path(self) -> Path:
        return self.dir / FILE

    # ------------------------------------------------------------------ file
    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        fd = os.open(self.dir / LOCK, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            try:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX)
            except ImportError:  # pragma: no cover — Windows: một tiến trình web + CLI hiếm khi trùng
                pass
            yield
        finally:
            os.close(fd)

    def _read(self) -> dict[str, Any]:
        p = self.path
        try:
            st = os.lstat(p)
            if p.is_symlink() or not owned_by_me(st):
                return {}
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return {}
        devs = d.get("devices") if isinstance(d, dict) else None
        if not isinstance(devs, dict):
            return {}
        return {k: v for k, v in devs.items() if isinstance(v, dict) and DID_RE.match(str(k))}

    def _write(self, devs: dict[str, Any]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        tmp = self.dir / f".{FILE}.{os.getpid()}.tmp"
        tmp.unlink(missing_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"v": 1, "devices": devs}, f)
        os.replace(tmp, self.path)
        self._cache = None

    def _totp_fp(self) -> str | None:
        from .totp import TotpStore

        return TotpStore(self.paths).fingerprint()

    # ------------------------------------------------------------------ đọc
    def list(self, now: float | None = None) -> list[dict[str, Any]]:
        """Thiết bị còn hạn (KHÔNG có hash), mới dùng trước."""
        now = time.time() if now is None else now
        fp = self._totp_fp()
        out = []
        for did, r in self._read().items():
            if float(r.get("expires_at") or 0) <= now or (fp is not None and r.get("totp_fp") != fp):
                continue
            out.append({"did": did, "ua_label": str(r.get("ua_label") or ""), "created_at": r.get("created_at"), "expires_at": r.get("expires_at"),
                        "last_used_at": r.get("last_used_at"), "last_ip": r.get("last_ip"), "first_ip": r.get("first_ip")})
        return sorted(out, key=lambda x: -float(x.get("last_used_at") or x.get("created_at") or 0))

    def valid(self, did: str, now: float | None = None) -> bool:
        """Thiết bị `did` còn được tin cậy (còn trong file, chưa hết hạn, khớp TOTP hiện tại). Cache theo mtime hai file —
        `WebAuth.get` gọi mỗi request để phiên gắn thiết bị bị thu hồi chết ngay."""
        now = time.time() if now is None else now
        try:
            m1 = os.stat(self.path).st_mtime_ns
        except OSError:
            return False
        try:
            m2 = os.stat(self.dir / "totp.json").st_mtime_ns
        except OSError:
            m2 = None
        key = (m1, m2)
        if self._cache is None or self._cache[0] != key:
            fp = self._totp_fp()
            self._cache = (key, {k: v for k, v in self._read().items() if fp is not None and v.get("totp_fp") == fp})
        r = self._cache[1].get(did)
        return bool(r) and float(r.get("expires_at") or 0) > now

    # ------------------------------------------------------------------ ghi
    def trust(self, *, ip: Any, ua: Any, now: float | None = None) -> tuple[str, dict[str, Any], list[str]]:
        """Tin cậy thiết bị mới → (token cookie, bản ghi công khai, các did bị loại vì vượt `max_devices`)."""
        now = time.time() if now is None else now
        fp = self._totp_fp()
        if fp is None:
            raise ValueError("TOTP chưa bật — không tin cậy thiết bị được")
        did, sec = "d" + secrets.token_urlsafe(9), secrets.token_urlsafe(32)  # tiền tố: id không bắt đầu bằng "-" (CLI `revoke <id>`)
        ipc = _clean_ip(ip)
        rec = {"sha256": _sha(sec), "prev_sha256": None, "prev_until": 0, "created_at": now,
               "expires_at": now + int(self.cfg["ttl_days"]) * 86400, "last_used_at": now, "last_ip": ipc, "first_ip": ipc,
               "ua_label": ua_label(ua), "totp_fp": fp}
        with self._locked():
            devs = {k: v for k, v in self._read().items() if float(v.get("expires_at") or 0) > now and v.get("totp_fp") == fp}
            evicted: list[str] = []
            while len(devs) >= int(self.cfg["max_devices"]):
                old = min(devs, key=lambda k: float(devs[k].get("last_used_at") or 0))
                devs.pop(old)
                evicted.append(old)
            devs[did] = rec
            self._write(devs)
        return f"{did}.{sec}", self._public(did, rec), evicted

    def redeem_and_rotate(self, token: Any, *, ip: Any, ua: Any, now: float | None = None) -> tuple[str, str | None, dict[str, Any] | None]:
        """Đổi token thiết bị → (trạng thái, token MỚI, bản ghi công khai).

        Trạng thái: `ok` (đã xoay) | `malformed` | `unknown` | `expired` | `totp_changed` | `bad_secret` | `reuse` (token cũ sau grace →
        thiết bị bị THU HỒI). Token sai không bao giờ làm lộ thiết bị có tồn tại hay không qua thời gian chạy đáng kể (so hash hằng thời gian)."""
        now = time.time() if now is None else now
        did, _, sec = str(token or "").partition(".")
        if not DID_RE.match(did) or not _SECRET_RE.match(sec):
            return "malformed", None, None
        h = _sha(sec)
        with self._locked():
            devs = self._read()
            r = devs.get(did)
            if r is None:
                return "unknown", None, None
            fp = self._totp_fp()
            if fp is None or r.get("totp_fp") != fp:
                devs.pop(did, None)
                self._write(devs)
                return "totp_changed", None, None
            if float(r.get("expires_at") or 0) <= now:
                devs.pop(did, None)
                self._write(devs)
                return "expired", None, None
            cur = hmac.compare_digest(str(r.get("sha256") or ""), h)
            prev = bool(r.get("prev_sha256")) and hmac.compare_digest(str(r.get("prev_sha256")), h)
            if not cur and prev and float(r.get("prev_until") or 0) < now:
                devs.pop(did, None)  # token đã xoay bị dùng lại sau cửa sổ grace → có thể bị đánh cắp: thu hồi
                self._write(devs)
                return "reuse", None, self._public(did, r)
            if not cur and not prev:
                return "bad_secret", None, None
            new = secrets.token_urlsafe(32)
            ua_new = ua_label(ua)
            changed = ua_new != r.get("ua_label")
            r.update({"prev_sha256": r.get("sha256") if cur else r.get("prev_sha256"),
                      "prev_until": now + int(self.cfg["rotate_grace_s"]) if cur else r.get("prev_until"),
                      "sha256": _sha(new), "last_used_at": now, "last_ip": _clean_ip(ip)})
            pub = self._public(did, r)
            pub["ua_changed_from"] = r.get("ua_label") if changed else None
            if changed:
                r["ua_label"] = ua_new
                pub["ua_label"] = ua_new
            devs[did] = r
            self._write(devs)
        return "ok", f"{did}.{new}", pub

    def revoke(self, did: str) -> dict[str, Any] | None:
        with self._locked():
            devs = self._read()
            r = devs.pop(str(did), None)
            if r is None:
                return None
            self._write(devs)
        return self._public(str(did), r)

    def revoke_all(self) -> int:
        with self._locked():
            n = len(self._read())
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            self._cache = None
        return n

    def purge_expired(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        with self._locked():
            devs = self._read()
            keep = {k: v for k, v in devs.items() if float(v.get("expires_at") or 0) > now}
            if len(keep) != len(devs):
                self._write(keep)
        return len(devs) - len(keep)

    @staticmethod
    def did_of(token: Any) -> str | None:
        did = str(token or "").partition(".")[0]
        return did if DID_RE.match(did) else None

    @staticmethod
    def _public(did: str, r: dict[str, Any]) -> dict[str, Any]:
        return {"did": did, "ua_label": str(r.get("ua_label") or ""), "created_at": r.get("created_at"), "expires_at": r.get("expires_at"),
                "last_used_at": r.get("last_used_at"), "last_ip": r.get("last_ip"), "first_ip": r.get("first_ip")}


def forget_all(paths: HubPaths) -> int:
    """Xoá mọi thiết bị (gọi khi tắt / đăng ký lại TOTP). Trả số thiết bị đã xoá."""
    return DeviceStore(paths).revoke_all()


__all__ = ["DeviceStore", "DEFAULTS", "KEYS", "LIMITS", "FILE", "forget_all", "settings", "ua_label", "validate_setting"]
