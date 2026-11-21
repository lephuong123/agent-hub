"""Client Cloudflare API v4 tối thiểu (stdlib urllib) cho admind: zone, DNS, Access (C4: token quyền tối thiểu trong vault).

Mọi hàm nhận/ trả dict thuần. `CloudflareAPI(token, transport=...)`: test tiêm `transport(method, path, body) -> (status, json)`.
Tạo CNAME luôn theo **zone_id đúng zone của hostname** (sửa lỗi `cert.pem` tạo nhầm zone: `lamlinh.trungtamdhs.com.cchchn.com`).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable

API = "https://api.cloudflare.com/client/v4"

Transport = Callable[[str, str, dict[str, Any] | None], tuple[int, dict[str, Any]]]


class CloudflareError(RuntimeError):
    def __init__(self, message: str, status: int = 0, errors: Any = None) -> None:
        super().__init__(message)
        self.status, self.errors = status, errors


class CloudflareAPI:
    def __init__(self, token: str, *, transport: Transport | None = None, timeout: float = 20.0) -> None:
        self.token = token
        self.timeout = timeout
        self.transport = transport or self._http

    def _http(self, method: str, path: str, body: dict[str, Any] | None) -> tuple[int, dict[str, Any]]:  # pragma: no cover — mạng
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(API + path, data=data, method=method,
                                     headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
                                              "User-Agent": "cfharness-admind"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:  # noqa: S310 — URL cố định https
                return r.status, json.loads(r.read(5_000_000).decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read(1_000_000).decode("utf-8") or "{}")
            except ValueError:
                return e.code, {}

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        status, data = self.transport(method, path, body)
        if status >= 400 or not data.get("success", status < 400):
            errs = data.get("errors") or []
            msg = "; ".join(str(e.get("message")) for e in errs if isinstance(e, dict)) or f"HTTP {status}"
            raise CloudflareError(f"Cloudflare: {msg}", status, errs)
        return data.get("result")

    # ------------------------------------------------------------------ token + zone
    def verify_token(self) -> dict[str, Any]:
        return self._call("GET", "/user/tokens/verify") or {}

    def zones(self) -> list[dict[str, Any]]:
        return [{"id": z["id"], "name": z["name"], "status": z.get("status")} for z in (self._call("GET", "/zones?per_page=50") or [])]

    def zone_id(self, name: str) -> str | None:
        r = self._call("GET", f"/zones?name={name}") or []
        return r[0]["id"] if r else None

    # ------------------------------------------------------------------ DNS
    def dns_find(self, zone_id: str, fqdn: str) -> list[dict[str, Any]]:
        return self._call("GET", f"/zones/{zone_id}/dns_records?name={fqdn}") or []

    def cname_upsert(self, zone_id: str, fqdn: str, target: str) -> dict[str, Any]:
        """CNAME proxied `fqdn → target`. Có bản ghi cùng tên: đúng đích → giữ; khác → sửa (chỉ khi là CNAME)."""
        cur = self.dns_find(zone_id, fqdn)
        body = {"type": "CNAME", "name": fqdn, "content": target, "proxied": True, "ttl": 1, "comment": "cfharness SAC"}
        for r in cur:
            if r.get("type") == "CNAME":
                if r.get("content") == target and r.get("proxied"):
                    return r
                return self._call("PUT", f"/zones/{zone_id}/dns_records/{r['id']}", body)
            raise CloudflareError(f"{fqdn} đã có bản ghi {r.get('type')} — không ghi đè")
        return self._call("POST", f"/zones/{zone_id}/dns_records", body)

    def dns_delete(self, zone_id: str, record_id: str) -> None:
        self._call("DELETE", f"/zones/{zone_id}/dns_records/{record_id}")

    # ------------------------------------------------------------------ tunnel + Access
    def tunnel_connections(self, account_id: str, tunnel_id: str) -> list[dict[str, Any]]:
        return self._call("GET", f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/connections") or []

    def access_app_upsert(self, account_id: str, domain: str, emails: list[str], *, app_id: str | None = None,
                          name: str | None = None) -> dict[str, Any]:
        body = {"name": name or domain, "domain": domain, "type": "self_hosted", "session_duration": "24h",
                "policies": [{"name": "allow", "decision": "allow", "include": [{"email": {"email": e}} for e in emails]}]}
        if app_id:
            return self._call("PUT", f"/accounts/{account_id}/access/apps/{app_id}", body)
        return self._call("POST", f"/accounts/{account_id}/access/apps", body)

    def access_app_delete(self, account_id: str, app_id: str) -> None:
        self._call("DELETE", f"/accounts/{account_id}/access/apps/{app_id}")


def from_state(state: Any) -> CloudflareAPI:
    """API từ token trong vault (hoặc bản giả do test tiêm vào `state.cloudflare`)."""
    if state.cloudflare is not None:
        return state.cloudflare
    tok = state.vault.get("cf.token")
    if not tok:
        from .ops import OpError

        raise OpError("failed", "chưa nạp token Cloudflare (Cấu hình → Secret → cf.token)")
    return CloudflareAPI(tok)
