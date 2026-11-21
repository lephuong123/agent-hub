"""Kiểm JWT Cloudflare Access (AUTH-02): header `Cf-Access-Jwt-Assertion`, RS256, `aud` = AUD tag ứng dụng Access, còn hạn.

Khoá công khai lấy từ `https://<team>.cloudflareaccess.com/cdn-cgi/access/certs` (cache `JWKS_TTL_S`). Dùng `cryptography` (đã là
dependency). Không hợp lệ → SAC trả 404 trơn (VIS-01).
"""

from __future__ import annotations

import base64
import json
import threading
import time
import urllib.request
from typing import Any, Callable

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

HEADER = "cf-access-jwt-assertion"
JWKS_TTL_S = 3600
LEEWAY_S = 60


def b64url(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def jwk_to_key(jwk: dict[str, Any]) -> rsa.RSAPublicKey:
    n = int.from_bytes(b64url(jwk["n"]), "big")
    e = int.from_bytes(b64url(jwk["e"]), "big")
    return rsa.RSAPublicNumbers(e, n).public_key()


def _fetch_jwks(team: str) -> dict[str, Any]:  # pragma: no cover — mạng
    url = f"https://{team}.cloudflareaccess.com/cdn-cgi/access/certs"
    with urllib.request.urlopen(url, timeout=10) as r:  # noqa: S310 — URL cố định https
        return json.loads(r.read(1_000_000).decode("utf-8"))


class AccessVerifier:
    def __init__(self, *, team: str, aud: str, fetch: Callable[[str], dict[str, Any]] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.team, self.aud = team, aud
        self.fetch = fetch or _fetch_jwks
        self.clock = clock
        self._keys: dict[str, rsa.RSAPublicKey] = {}
        self._at = 0.0
        self._lock = threading.Lock()

    def _key(self, kid: str) -> rsa.RSAPublicKey | None:
        with self._lock:
            if kid not in self._keys or self.clock() - self._at > JWKS_TTL_S:
                try:
                    data = self.fetch(self.team)
                    self._keys = {k["kid"]: jwk_to_key(k) for k in data.get("keys", []) if k.get("kty") == "RSA" and "kid" in k}
                    self._at = self.clock()
                except Exception:  # noqa: BLE001 — mất mạng: giữ khoá cũ
                    pass
            return self._keys.get(kid)

    def verify(self, token: str | None) -> dict[str, Any] | None:
        """Trả claims nếu hợp lệ; None nếu không."""
        if not token or token.count(".") != 2 or len(token) > 8192:
            return None
        h64, p64, s64 = token.split(".")
        try:
            header = json.loads(b64url(h64))
            claims = json.loads(b64url(p64))
            sig = b64url(s64)
        except (ValueError, TypeError):
            return None
        if header.get("alg") != "RS256" or not isinstance(claims, dict):
            return None
        key = self._key(str(header.get("kid", "")))
        if key is None:
            return None
        try:
            key.verify(sig, f"{h64}.{p64}".encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
        except Exception:  # fail-closed: chữ ký sai → JWT không hợp lệ
            return None
        now = self.clock()
        aud = claims.get("aud")
        auds = aud if isinstance(aud, list) else [aud]
        if self.aud not in auds:
            return None
        if float(claims.get("exp", 0)) < now - LEEWAY_S or float(claims.get("nbf", 0)) > now + LEEWAY_S:
            return None
        iss = str(claims.get("iss", ""))
        if iss and iss != f"https://{self.team}.cloudflareaccess.com":
            return None
        return claims
