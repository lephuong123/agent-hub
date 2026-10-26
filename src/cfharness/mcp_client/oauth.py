"""OAuth 2.1 cho MCP server từ xa (MCP Authorization spec) — "Kết nối" một chạm như connector của Claude.

Luồng (daemon điều khiển, chia 2 bước vì trình duyệt đi vòng qua web UI / listener loopback của CLI):

1. `start(name, redirect_uri)`:
   - gửi `initialize` KHÔNG token tới URL server → 401 + `WWW-Authenticate` (`resource_metadata`, `scope`);
   - Protected Resource Metadata (RFC 9728, `/.well-known/oauth-protected-resource[/<path>]`) → `authorization_servers[0]`,
     `resource` (phải khớp URL server — chống trỏ token sang chỗ khác);
   - Authorization Server Metadata (RFC 8414 / OIDC discovery), `issuer` phải khớp AS mà PRM chỉ tới (chống mix-up);
   - client: `auth.client_id` người nhập (+ `client_secret_ref`) HOẶC Dynamic Client Registration (RFC 7591) theo từng redirect_uri;
   - PKCE S256 + `state` 256 bit + `resource` (RFC 8707) → trả `authorize_url`. Trạng thái chờ (state → verifier, server,
     redirect, hạn ≤ 10 phút) CHỈ nằm trong RAM của daemon.
2. `callback(state, code|error, iss)`: tiêu `state` (một lần), kiểm hạn + `iss` (RFC 9207), đổi code lấy token → secret store.

Lưu trữ: một bản ghi JSON trong secret store (file 0600) tên `secret_name(server)` (`MCP_OAUTH_<TÊN>_<băm>`): metadata AS,
client đã đăng ký, token. KHÔNG BAO GIỜ ghi vào hub.db, event, log, audit, argv. Giá trị token/client_secret được đăng ký vào
bộ che theo giá trị (`redact.register_secret_values`) ngay khi đọc/ghi.

Lúc chạy: `RunManager._run` gọi `ensure_fresh_for(servers)` (làm mới nếu còn < `REFRESH_MARGIN_S`), rồi registry chèn
`headers.Authorization: Bearer …` vào entry mcp.json runtime (file 0600, xoá sau run) qua `bearer_for()`. Chưa kết nối / hết hạn
→ `HubError` → run `failed` với thông báo rõ (fail-closed như secret thiếu).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import logging
import secrets as _secrets
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import quote, urlencode, urlsplit

from ..errors import HubError
from ..events import make_event
from ..redact import redact_str, register_secret_values

if TYPE_CHECKING:
    from ..secrets import SecretStore
    from .config import McpServerConfig
    from .registry import McpRegistry

log = logging.getLogger("cfharness.mcp_client.oauth")

STATE_TTL_S = 600  # trạng thái chờ đăng nhập sống tối đa 10 phút
MAX_PENDING = 32
REFRESH_MARGIN_S = 300  # run/probe: làm mới token nếu còn dưới 5 phút
HTTP_TIMEOUT_S = 15.0
MAX_CLIENTS = 6  # số bản đăng ký DCR giữ lại (mỗi redirect_uri một bản)
CLIENT_NAME = "cfharness"
STATUSES = ("not_connected", "connected", "expired", "error")
SENSITIVE_KEYS = ("access_token", "refresh_token", "client_secret", "id_token")


# ============================================================================ tiện ích thuần
def secret_name(server: str) -> str:
    """Tên secret của bản ghi OAuth. Kèm băm của tên gốc → `a-b` và `a_b` (cùng dạng chữ hoa) không đụng nhau."""
    h = hashlib.sha256(server.encode("utf-8")).hexdigest()[:6].upper()
    base = "".join(ch if ch.isalnum() else "_" for ch in server.upper()).strip("_") or "X"
    return f"MCP_OAUTH_{base[:90]}_{h}"


def is_oauth(cfg: "McpServerConfig | dict[str, Any] | None") -> bool:
    if cfg is None:
        return False
    auth = cfg.get("auth") if isinstance(cfg, dict) else getattr(cfg, "auth", None)
    if auth is None:
        return False
    t = auth.get("type") if isinstance(auth, dict) else getattr(auth, "type", None)
    return t == "oauth"


def _auth_of(cfg: "McpServerConfig") -> Any:
    return cfg.auth


def is_loopback_host(host: str | None) -> bool:
    h = (host or "").strip("[]").lower()
    if h == "localhost" or h.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def require_secure(url: str | None, what: str) -> str:
    """https, hoặc http CHỈ khi host là loopback (token không bao giờ đi qua kênh rõ ra mạng)."""
    s = urlsplit(str(url or ""))
    if s.scheme == "https" and s.netloc:
        return str(url)
    if s.scheme == "http" and is_loopback_host(s.hostname):
        return str(url)
    raise HubError(f"{what} phải là https:// (http chỉ cho máy này): {url!r}", data={"oauth": "insecure_url"})


def pkce_pair() -> tuple[str, str]:
    """(code_verifier, code_challenge S256) — RFC 7636."""
    verifier = _secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode().rstrip("=")
    return verifier, challenge


def _register_record(rec: dict[str, Any] | None) -> None:
    """Đưa mọi giá trị nhạy cảm của bản ghi vào bộ che theo giá trị (H-12)."""
    if not rec:
        return
    vals: list[str] = []
    tok = rec.get("tokens") or {}
    vals += [tok.get(k) for k in SENSITIVE_KEYS if isinstance(tok.get(k), str)]
    for c in (rec.get("clients") or {}).values():
        if isinstance(c, dict) and isinstance(c.get("client_secret"), str):
            vals.append(c["client_secret"])
    register_secret_values(*vals)


def load_record(store: "SecretStore", server: str) -> dict[str, Any] | None:
    raw = store.get(secret_name(server))
    if not raw:
        return None
    try:
        rec = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(rec, dict):
        return None
    _register_record(rec)
    return rec


def save_record(store: "SecretStore", server: str, rec: dict[str, Any]) -> None:
    _register_record(rec)
    store.set(secret_name(server), json.dumps(rec, ensure_ascii=False, separators=(",", ":")))


def delete_record(store: "SecretStore", server: str) -> bool:
    return store.delete(secret_name(server))


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    from datetime import datetime, timezone

    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def status_of(store: "SecretStore", cfg: "McpServerConfig", now: float | None = None) -> dict[str, Any]:
    """Trạng thái OAuth của server — KHÔNG BAO GIỜ chứa token/secret."""
    now = time.time() if now is None else now
    rec = load_record(store, cfg.name)
    auth = _auth_of(cfg)
    out: dict[str, Any] = {
        "status": "not_connected", "expires_at": None, "scope": None, "issuer": None, "refreshable": False,
        "client": "manual" if getattr(auth, "client_id", None) else ("dcr" if rec and rec.get("clients") else None),
        "error": None, "connected_at": None,
    }
    if not rec:
        return out
    out["issuer"] = rec.get("issuer")
    tok = rec.get("tokens") or {}
    err = rec.get("error")
    if err:
        out["error"] = err.get("message") if isinstance(err, dict) else str(err)
    if not tok.get("access_token"):
        if err:
            out["status"] = "error"
        return out
    if rec.get("server_url") != cfg.url:
        out["error"] = "URL server đã đổi sau khi kết nối — kết nối lại"
        return out
    exp = tok.get("expires_at")
    out.update(expires_at=_iso(exp), scope=tok.get("scope"), refreshable=bool(tok.get("refresh_token")),
               connected_at=_iso(tok.get("obtained_at")))
    if err:
        out["status"] = "error"
    elif exp is not None and float(exp) <= now:
        out["status"] = "expired"
    else:
        out["status"] = "connected"
    return out


def bearer_for(store: "SecretStore", cfg: "McpServerConfig", now: float | None = None) -> str:
    """Access token còn hạn cho server (đồng bộ — gọi khi dựng mcp.json/probe). Không có → HubError (fail-closed)."""
    now = time.time() if now is None else now
    rec = load_record(store, cfg.name)
    hint = f"`cfharness mcp login {cfg.name}` hoặc nút Kết nối ở trang MCP trên web"
    tok = (rec or {}).get("tokens") or {}
    if not tok.get("access_token"):
        raise HubError(f"MCP server '{cfg.name}' cần đăng nhập OAuth — {hint}", data={"server": cfg.name, "oauth": "not_connected"})
    if rec.get("server_url") != cfg.url:  # type: ignore[union-attr]
        raise HubError(f"MCP server '{cfg.name}': URL đã đổi sau khi đăng nhập OAuth — đăng nhập lại ({hint})",
                       data={"server": cfg.name, "oauth": "url_changed"})
    exp = tok.get("expires_at")
    if exp is not None and float(exp) <= now:
        raise HubError(f"MCP server '{cfg.name}': token OAuth đã hết hạn và không làm mới được — {hint}",
                       data={"server": cfg.name, "oauth": "expired"})
    return str(tok["access_token"])


def inject_bearer(entry: dict[str, Any], store: "SecretStore", cfg: "McpServerConfig") -> dict[str, Any]:
    """Entry `mcpServers` (đã resolve secret) + `headers.Authorization: Bearer <token>` cho server OAuth. CHỈ dùng trong bộ nhớ /
    file runtime 0600."""
    if not is_oauth(cfg):
        return entry
    tok = bearer_for(store, cfg)
    headers = {k: v for k, v in (entry.get("headers") or {}).items() if k.lower() != "authorization"}
    headers["Authorization"] = f"Bearer {tok}"
    return {**entry, "headers": headers}


def _www_auth_param(header: str | None, key: str) -> str | None:
    import re

    if not header:
        return None
    m = re.search(rf'{key}=(?:"([^"]*)"|([^\s,]+))', header)
    return (m.group(1) or m.group(2)) if m else None


def _resource_allowed(server_url: str, resource: str) -> bool:
    from mcp.shared.auth_utils import check_resource_allowed

    return check_resource_allowed(server_url, resource)


def _resource_of(server_url: str) -> str:
    from mcp.shared.auth_utils import resource_url_from_server_url

    return resource_url_from_server_url(server_url)


def _clean_err(text: Any, n: int = 300) -> str:
    return redact_str(str(text or ""))[:n]


# ============================================================================ trạng thái chờ (RAM)
@dataclass
class Pending:
    state: str
    server: str
    redirect_uri: str
    verifier: str
    created: float
    expires: float
    client_key: str  # "manual" | redirect_uri (bản DCR)
    issuer: str | None
    iss_required: bool = False
    scope: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class OAuthManager:
    """Điều khiển luồng OAuth trong daemon. `transport` (httpx2) thay được cho test."""

    def __init__(self, registry: "McpRegistry", store: "SecretStore", *, publish: Callable[[Any], Any] | None = None,
                 transport: Any = None, clock: Callable[[], float] = time.time) -> None:
        self.registry = registry
        self.store = store
        self.publish = publish or (lambda ev: None)
        self.transport = transport
        self.clock = clock
        self.pending: dict[str, Pending] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------ http
    def _client(self) -> Any:
        import httpx2

        kw: dict[str, Any] = {"timeout": HTTP_TIMEOUT_S, "follow_redirects": False, "headers": {"User-Agent": "cfharness-mcp-oauth"}}
        if self.transport is not None:
            kw["transport"] = self.transport
        return httpx2.AsyncClient(**kw)

    async def _get_json(self, http: Any, url: str) -> tuple[int, Any]:
        from mcp.shared.inbound import MCP_PROTOCOL_VERSION_HEADER
        from mcp_types import LATEST_PROTOCOL_VERSION

        try:
            r = await http.get(url, headers={MCP_PROTOCOL_VERSION_HEADER: LATEST_PROTOCOL_VERSION, "Accept": "application/json"})
        except Exception as e:  # noqa: BLE001 — lỗi mạng của một URL ứng viên → thử URL kế
            log.info("discovery %s lỗi: %s", url, type(e).__name__)
            return 0, None
        if r.status_code != 200:
            return r.status_code, None
        try:
            return 200, r.json()
        except ValueError:
            return 200, None

    # ------------------------------------------------------------------ discovery
    async def discover(self, cfg: "McpServerConfig") -> dict[str, Any]:
        """PRM → AS metadata. Trả {resource, issuer, as:{…}, scope_hint, iss_supported}."""
        from mcp.client.auth.utils import (
            build_oauth_authorization_server_metadata_discovery_urls,
            build_protected_resource_metadata_discovery_urls,
            issuers_match,
        )
        from mcp.shared.auth import OAuthMetadata, ProtectedResourceMetadata

        server_url = require_secure(cfg.url, "URL MCP server dùng OAuth")
        www_meta = www_scope = None
        async with self._client() as http:
            # 1) gọi thử không token → 401 + WWW-Authenticate
            try:
                body = {"jsonrpc": "2.0", "id": 0, "method": "initialize",
                        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": CLIENT_NAME, "version": "1"}}}
                r = await http.post(server_url, json=body, headers={"Accept": "application/json, text/event-stream"})
                if r.status_code in (401, 403):
                    h = r.headers.get("WWW-Authenticate")
                    www_meta, www_scope = _www_auth_param(h, "resource_metadata"), _www_auth_param(h, "scope")
                await r.aclose()
            except Exception as e:  # noqa: BLE001
                raise HubError(f"không kết nối được {server_url}: {type(e).__name__}", data={"oauth": "unreachable"}) from None
            # 2) Protected Resource Metadata (RFC 9728)
            prm = None
            for u in build_protected_resource_metadata_discovery_urls(www_meta, server_url):
                try:
                    require_secure(u, "URL resource metadata")
                except HubError:
                    continue
                code, data = await self._get_json(http, u)
                if code == 200 and isinstance(data, dict):
                    try:
                        prm = ProtectedResourceMetadata.model_validate(data)
                        break
                    except ValueError:
                        continue
            if prm is not None:
                resource = str(prm.resource)
                if not _resource_allowed(server_url, resource):
                    raise HubError(f"resource metadata khai `resource` = {resource!r} không khớp URL server {server_url!r} — từ chối",
                                   data={"oauth": "resource_mismatch"})
                auth_server = str(prm.authorization_servers[0])
                scope_hint = www_scope or (" ".join(prm.scopes_supported) if prm.scopes_supported else None)
            else:  # spec 2025-03-26: AS nằm cùng origin với server
                resource = _resource_of(server_url)
                auth_server = None
                scope_hint = www_scope
            # 3) Authorization Server Metadata (RFC 8414 / OIDC)
            asm = None
            for u in build_oauth_authorization_server_metadata_discovery_urls(auth_server, server_url):
                code, data = await self._get_json(http, u)
                if code == 200 and isinstance(data, dict):
                    try:
                        asm = OAuthMetadata.model_validate(data)
                        break
                    except ValueError:
                        continue
        if asm is not None:
            if auth_server is not None and not issuers_match(str(asm.issuer), auth_server) and not issuers_match(str(asm.issuer), auth_server.rstrip("/")):
                raise HubError(f"issuer của authorization server ({asm.issuer}) không khớp {auth_server} — từ chối (chống mix-up)",
                               data={"oauth": "issuer_mismatch"})
            if asm.code_challenge_methods_supported is not None and "S256" not in asm.code_challenge_methods_supported:
                raise HubError("authorization server không hỗ trợ PKCE S256 — không kết nối", data={"oauth": "no_pkce"})
            meta = {
                "authorization_endpoint": str(asm.authorization_endpoint), "token_endpoint": str(asm.token_endpoint),
                "registration_endpoint": str(asm.registration_endpoint) if asm.registration_endpoint else None,
                "revocation_endpoint": str(asm.revocation_endpoint) if asm.revocation_endpoint else None,
                "token_auth_methods": asm.token_endpoint_auth_methods_supported,
            }
            issuer = str(asm.issuer)
            iss_supported = bool(asm.authorization_response_iss_parameter_supported)
        else:  # dự phòng cũ: endpoint mặc định trên origin của AS/server
            base = auth_server or server_url
            s = urlsplit(base)
            origin = f"{s.scheme}://{s.netloc}"
            meta = {"authorization_endpoint": origin + "/authorize", "token_endpoint": origin + "/token",
                    "registration_endpoint": origin + "/register", "revocation_endpoint": None, "token_auth_methods": None}
            issuer = None
            iss_supported = False
        for k in ("authorization_endpoint", "token_endpoint", "registration_endpoint", "revocation_endpoint"):
            if meta.get(k):
                require_secure(meta[k], k)
        return {"resource": resource, "issuer": issuer, "as": meta, "scope_hint": scope_hint, "iss_supported": iss_supported}

    # ------------------------------------------------------------------ client
    async def _register_client(self, meta: dict[str, Any], redirect_uri: str, scope: str | None) -> dict[str, Any]:
        reg = meta["as"].get("registration_endpoint")
        if not reg:
            raise HubError(
                "authorization server không hỗ trợ đăng ký động (DCR) — đăng ký app với nhà cung cấp rồi nhập client_id: "
                "`cfharness mcp edit <tên> --oauth-client-id <id> [--oauth-client-secret <secret>]` "
                f"(redirect URI cần khai: {redirect_uri})",
                data={"oauth": "no_dcr", "redirect_uri": redirect_uri},
            )
        body: dict[str, Any] = {"redirect_uris": [redirect_uri], "client_name": CLIENT_NAME, "grant_types": ["authorization_code", "refresh_token"],
                                "response_types": ["code"], "token_endpoint_auth_method": "none", "application_type": "native"}
        if scope:
            body["scope"] = scope
        async with self._client() as http:
            try:
                r = await http.post(reg, json=body, headers={"Accept": "application/json"})
            except Exception as e:  # noqa: BLE001
                raise HubError(f"đăng ký client (DCR) lỗi mạng: {type(e).__name__}", data={"oauth": "dcr_failed"}) from None
        if r.status_code not in (200, 201):
            raise HubError(f"đăng ký client (DCR) bị từ chối: HTTP {r.status_code} {_clean_err(r.text)}", data={"oauth": "dcr_failed"})
        try:
            d = r.json()
        except ValueError:
            raise HubError("đăng ký client (DCR): phản hồi không phải JSON", data={"oauth": "dcr_failed"}) from None
        if not isinstance(d, dict) or not d.get("client_id"):
            raise HubError("đăng ký client (DCR): thiếu client_id", data={"oauth": "dcr_failed"})
        method = d.get("token_endpoint_auth_method") or ("client_secret_basic" if d.get("client_secret") else "none")
        if method not in ("none", "client_secret_basic", "client_secret_post"):
            raise HubError(f"đăng ký client (DCR): phương thức xác thực {method!r} không hỗ trợ", data={"oauth": "dcr_failed"})
        if method != "none" and not d.get("client_secret"):
            raise HubError("đăng ký client (DCR): server đòi client_secret nhưng không cấp", data={"oauth": "dcr_failed"})
        client = {"client_id": str(d["client_id"]), "token_endpoint_auth_method": method, "registered": "dcr",
                  "issuer": meta.get("issuer"), "redirect_uri": redirect_uri, "registered_at": self.clock()}
        if d.get("client_secret"):
            client["client_secret"] = str(d["client_secret"])
            if d.get("client_secret_expires_at"):
                client["client_secret_expires_at"] = d["client_secret_expires_at"]
        return client

    def _manual_client(self, cfg: "McpServerConfig", meta_as: dict[str, Any] | None) -> dict[str, Any]:
        auth = _auth_of(cfg)
        client: dict[str, Any] = {"client_id": auth.client_id, "registered": "manual"}
        if auth.client_secret_ref:
            client["client_secret"] = self.store.resolve(auth.client_secret_ref)  # thiếu → SecretMissing (fail-closed)
            register_secret_values(client["client_secret"])
            methods = (meta_as or {}).get("token_auth_methods")
            client["token_endpoint_auth_method"] = "client_secret_basic" if not methods or "client_secret_basic" in methods else "client_secret_post"
        else:
            client["token_endpoint_auth_method"] = "none"
        return client

    def _client_for(self, cfg: "McpServerConfig", rec: dict[str, Any], key: str) -> dict[str, Any]:
        if key == "manual":
            return self._manual_client(cfg, rec.get("as"))
        c = (rec.get("clients") or {}).get(key)
        if not c:
            raise HubError(f"MCP server '{cfg.name}': không còn bản đăng ký client — đăng nhập lại", data={"oauth": "no_client"})
        return c

    # ------------------------------------------------------------------ bước 1
    def _gc(self) -> None:
        now = self.clock()
        for s, p in list(self.pending.items()):
            if p.expires <= now:
                self.pending.pop(s, None)
        while len(self.pending) >= MAX_PENDING:
            self.pending.pop(min(self.pending.values(), key=lambda p: p.created).state, None)

    def _cfg(self, name: str) -> "McpServerConfig":
        cfg = self.registry.config_of(self.registry.require(name))
        if not is_oauth(cfg):
            raise HubError(f"MCP server '{name}' không bật OAuth — `cfharness mcp edit {name} --oauth`", data={"oauth": "not_enabled"})
        if cfg.transport not in ("http", "sse"):
            raise HubError("OAuth chỉ dùng cho transport http/sse")
        return cfg

    async def start(self, name: str, redirect_uri: str) -> dict[str, Any]:
        cfg = self._cfg(name)
        rs = urlsplit(str(redirect_uri or ""))
        if rs.scheme not in ("http", "https") or not rs.netloc or rs.fragment:
            raise HubError(f"redirect_uri không hợp lệ: {redirect_uri!r}")
        if rs.scheme == "http" and not is_loopback_host(rs.hostname):
            raise HubError("redirect_uri http chỉ được trỏ về máy này (loopback / *.localhost)")
        meta = await self.discover(cfg)
        auth = _auth_of(cfg)
        scope = " ".join(auth.scopes) if auth.scopes else meta.get("scope_hint")
        rec = load_record(self.store, name) or {}
        rec.update({"v": 1, "kind": "mcp_oauth", "server": name, "as": meta["as"], "issuer": meta["issuer"], "resource": meta["resource"]})
        if auth.client_id:
            client, key = self._manual_client(cfg, meta["as"]), "manual"
        else:
            clients = dict(rec.get("clients") or {})
            c = clients.get(redirect_uri)
            if not c or c.get("issuer") != meta["issuer"] or (c.get("client_secret_expires_at") and float(c["client_secret_expires_at"]) < self.clock() + 60):
                c = await self._register_client(meta, redirect_uri, scope)
                clients[redirect_uri] = c
                while len(clients) > MAX_CLIENTS:
                    clients.pop(min(clients, key=lambda k: clients[k].get("registered_at") or 0))
            rec["clients"] = clients
            client, key = c, redirect_uri
        save_record(self.store, name, rec)  # metadata + bản đăng ký (token cũ giữ nguyên tới khi callback thành công)
        verifier, challenge = pkce_pair()
        state = _secrets.token_urlsafe(32)
        now = self.clock()
        self._gc()
        self.pending[state] = Pending(state=state, server=name, redirect_uri=redirect_uri, verifier=verifier, created=now, expires=now + STATE_TTL_S,
                                      client_key=key, issuer=meta["issuer"], iss_required=meta["iss_supported"], scope=scope,
                                      extra={"server_url": cfg.url})
        q: dict[str, str] = {"response_type": "code", "client_id": client["client_id"], "redirect_uri": redirect_uri, "state": state,
                             "code_challenge": challenge, "code_challenge_method": "S256", "resource": meta["resource"]}
        if scope:
            q["scope"] = scope
        ep = meta["as"]["authorization_endpoint"]
        url = ep + ("&" if urlsplit(ep).query else "?") + urlencode(q, quote_via=quote)
        self._emit("oauth_started", name, {"client": "manual" if key == "manual" else "dcr"})
        return {"name": name, "authorize_url": url, "expires_at": _iso(now + STATE_TTL_S), "client": "manual" if key == "manual" else "dcr",
                "authorization_host": urlsplit(ep).netloc}

    # ------------------------------------------------------------------ bước 2
    async def callback(self, state: str, code: str | None = None, error: str | None = None, error_description: str | None = None,
                       iss: str | None = None) -> dict[str, Any]:
        if not state or not isinstance(state, str):
            raise HubError("thiếu state", data={"oauth": "bad_state"})
        p = self.pending.pop(state, None)  # MỘT lần: dù thành công hay lỗi
        if p is None:
            raise HubError("state không hợp lệ hoặc đã dùng (daemon khởi động lại / mở lại trang cũ?) — bấm Kết nối lại",
                           data={"oauth": "bad_state"})
        name = p.server
        if p.expires <= self.clock():
            return self._fail(name, "phiên đăng nhập đã hết hạn (quá 10 phút) — bấm Kết nối lại", "expired_state")
        if error:
            return self._fail(name, f"nhà cung cấp từ chối: {_clean_err(error, 80)}" + (f" — {_clean_err(error_description, 200)}" if error_description else ""),
                              "denied")
        if not code:
            return self._fail(name, "thiếu code trong callback", "no_code")
        if iss is not None and p.issuer is not None and iss != p.issuer and iss.rstrip("/") != p.issuer.rstrip("/"):
            return self._fail(name, "iss trong callback không khớp authorization server (RFC 9207) — từ chối", "iss_mismatch")
        if iss is None and p.iss_required:
            return self._fail(name, "callback thiếu iss mà authorization server đã khai báo (RFC 9207) — từ chối", "iss_missing")
        cfg = self._cfg(name)
        if cfg.url != p.extra.get("server_url"):
            return self._fail(name, "URL server đổi trong lúc đăng nhập — kết nối lại", "url_changed")
        rec = load_record(self.store, name) or {}
        client = self._client_for(cfg, rec, p.client_key)
        form = {"grant_type": "authorization_code", "code": code, "redirect_uri": p.redirect_uri, "code_verifier": p.verifier,
                "resource": rec.get("resource") or _resource_of(cfg.url or "")}
        try:
            tok = await self._token_request(rec, client, form)
        except HubError as e:
            return self._fail(name, e.message, "token_failed")
        rec = load_record(self.store, name) or rec
        rec["server_url"] = cfg.url
        rec["tokens"] = {**tok, "client": p.client_key}
        rec["error"] = None
        save_record(self.store, name, rec)
        self._emit("oauth_connected", name, {"expires_at": _iso(tok.get("expires_at")), "refreshable": bool(tok.get("refresh_token"))})
        return {"name": name, "ok": True, **self._public_status(cfg)}

    def _fail(self, name: str, message: str, reason: str) -> dict[str, Any]:
        self._emit("oauth_error", name, {"reason": reason})
        return {"name": name, "ok": False, "status": "error", "oauth_status": None, "error": message, "reason": reason}

    async def _token_request(self, rec: dict[str, Any], client: dict[str, Any], form: dict[str, str]) -> dict[str, Any]:
        url = require_secure((rec.get("as") or {}).get("token_endpoint"), "token endpoint")
        data = dict(form)
        headers = {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}
        method = client.get("token_endpoint_auth_method") or "none"
        if method == "client_secret_basic" and client.get("client_secret"):
            raw = f"{quote(client['client_id'], safe='')}:{quote(client['client_secret'], safe='')}"
            headers["Authorization"] = "Basic " + base64.b64encode(raw.encode()).decode()
        else:
            data["client_id"] = client["client_id"]
            if method == "client_secret_post" and client.get("client_secret"):
                data["client_secret"] = client["client_secret"]
        async with self._client() as http:
            try:
                r = await http.post(url, content=urlencode(data).encode(), headers=headers)
            except Exception as e:  # noqa: BLE001
                raise HubError(f"token endpoint lỗi mạng: {type(e).__name__}", data={"oauth": "token_failed"}) from None
        try:
            d = r.json()
        except ValueError:
            d = None
        if r.status_code != 200 or not isinstance(d, dict):
            err = (d or {}).get("error") if isinstance(d, dict) else None
            desc = (d or {}).get("error_description") if isinstance(d, dict) else None
            raise HubError(f"token endpoint từ chối: HTTP {r.status_code}" + (f" {_clean_err(err, 80)}" if err else "") + (f" — {_clean_err(desc, 200)}" if desc else ""),
                           data={"oauth": "token_failed", "error": _clean_err(err, 80) if err else None})
        at = d.get("access_token")
        if not isinstance(at, str) or not at:
            raise HubError("token endpoint không trả access_token", data={"oauth": "token_failed"})
        tt = str(d.get("token_type") or "Bearer")
        if tt.lower() != "bearer":
            raise HubError(f"token_type {tt!r} không hỗ trợ (chỉ Bearer)", data={"oauth": "token_failed"})
        now = self.clock()
        out: dict[str, Any] = {"access_token": at, "token_type": "Bearer", "obtained_at": now, "scope": d.get("scope"),
                               "expires_at": None, "refresh_token": d.get("refresh_token") if isinstance(d.get("refresh_token"), str) else None}
        try:
            if d.get("expires_in") is not None:
                out["expires_at"] = now + max(0, int(d["expires_in"]))
        except (TypeError, ValueError):
            pass
        register_secret_values(out["access_token"], out.get("refresh_token"))
        return out

    # ------------------------------------------------------------------ làm mới / dùng
    def _lock(self, name: str) -> asyncio.Lock:
        lk = self._locks.get(name)
        if lk is None:
            lk = self._locks[name] = asyncio.Lock()
        return lk

    async def refresh(self, name: str) -> dict[str, Any]:
        cfg = self._cfg(name)
        async with self._lock(name):
            rec = load_record(self.store, name) or {}
            tok = rec.get("tokens") or {}
            if not tok.get("refresh_token"):
                raise HubError(f"MCP server '{name}': không có refresh_token — đăng nhập lại", data={"oauth": "no_refresh"})
            if rec.get("server_url") != cfg.url:
                raise HubError(f"MCP server '{name}': URL đã đổi — đăng nhập lại", data={"oauth": "url_changed"})
            client = self._client_for(cfg, rec, str(tok.get("client") or "manual"))
            form = {"grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "resource": rec.get("resource") or _resource_of(cfg.url or "")}
            try:
                new = await self._token_request(rec, client, form)
            except HubError as e:
                if (e.data or {}).get("error") in ("invalid_grant", "invalid_client", "unauthorized_client"):
                    rec["error"] = {"message": f"làm mới token thất bại ({e.data.get('error')}) — đăng nhập lại", "at": self.clock()}
                    save_record(self.store, name, rec)
                    self._emit("oauth_error", name, {"reason": "refresh_failed"})
                raise
            if not new.get("refresh_token"):
                new["refresh_token"] = tok["refresh_token"]  # server không xoay vòng → giữ refresh_token cũ
            rec["tokens"] = {**new, "client": tok.get("client")}
            rec["error"] = None
            save_record(self.store, name, rec)
        self._emit("oauth_refreshed", name, {"expires_at": _iso(new.get("expires_at"))})
        return self._public_status(cfg)

    async def ensure_fresh(self, name: str, margin_s: float = REFRESH_MARGIN_S) -> None:
        """Làm mới nếu token còn < margin_s. Lỗi làm mới khi token vẫn còn hạn → bỏ qua (dùng token cũ)."""
        cfg = self._cfg(name)
        rec = load_record(self.store, name) or {}
        tok = rec.get("tokens") or {}
        if not tok.get("access_token") or rec.get("server_url") != cfg.url:
            return
        exp = tok.get("expires_at")
        if exp is None or float(exp) - self.clock() > margin_s or not tok.get("refresh_token"):
            return
        try:
            await self.refresh(name)
        except HubError as e:
            log.warning("làm mới token OAuth của MCP %s lỗi: %s", name, e.message)

    async def ensure_fresh_for(self, names: list[str] | None, margin_s: float = REFRESH_MARGIN_S) -> None:
        """Trước khi dựng mcp.json của run: làm mới token các server OAuth của agent. Không ném — dựng mcp.json sẽ báo lỗi rõ."""
        for n in names or []:
            row = self.registry.get_row(n)
            if row is None or not is_oauth(row.get("config")):
                continue
            try:
                await asyncio.wait_for(self.ensure_fresh(n, margin_s), HTTP_TIMEOUT_S * 2)
            except (HubError, asyncio.TimeoutError) as e:
                log.warning("không làm mới được OAuth của MCP %s: %s", n, getattr(e, "message", type(e).__name__))

    # ------------------------------------------------------------------ trạng thái / ngắt kết nối
    def _public_status(self, cfg: "McpServerConfig") -> dict[str, Any]:
        st = status_of(self.store, cfg, self.clock())
        return {"oauth_status": st["status"], "oauth": st, "status": st["status"]}

    def status(self, name: str) -> dict[str, Any]:
        cfg = self.registry.config_of(self.registry.require(name, include_deleted=True))
        if not is_oauth(cfg):
            return {"name": name, "oauth_status": None, "oauth": None, "status": None}
        return {"name": name, **self._public_status(cfg)}

    async def disconnect(self, name: str) -> dict[str, Any]:
        """Thu hồi token (RFC 7009, cố gắng) rồi xoá bản ghi khỏi secret store."""
        row = self.registry.require(name, include_deleted=True)
        cfg = self.registry.config_of(row)
        rec = load_record(self.store, name)
        revoked = False
        if rec:
            ep = (rec.get("as") or {}).get("revocation_endpoint")
            tok = rec.get("tokens") or {}
            if ep and tok:
                try:
                    client = self._client_for(cfg, rec, str(tok.get("client") or "manual")) if is_oauth(cfg) else None
                except HubError:
                    client = None
                if client is not None:
                    revoked = await self._revoke(ep, client, tok)
        for s, p in list(self.pending.items()):
            if p.server == name:
                self.pending.pop(s, None)
        existed = delete_record(self.store, name)
        self._emit("oauth_disconnected", name, {"revoked": revoked})
        return {"name": name, "disconnected": existed, "revoked": revoked, "status": "not_connected", "oauth_status": "not_connected"}

    async def _revoke(self, ep: str, client: dict[str, Any], tok: dict[str, Any]) -> bool:
        try:
            require_secure(ep, "revocation endpoint")
        except HubError:
            return False
        ok = False
        async with self._client() as http:
            for hint in ("refresh_token", "access_token"):
                if not tok.get(hint):
                    continue
                data = {"token": tok[hint], "token_type_hint": hint}
                headers = {"Content-Type": "application/x-www-form-urlencoded"}
                if client.get("token_endpoint_auth_method") == "client_secret_basic" and client.get("client_secret"):
                    raw = f"{quote(client['client_id'], safe='')}:{quote(client['client_secret'], safe='')}"
                    headers["Authorization"] = "Basic " + base64.b64encode(raw.encode()).decode()
                else:
                    data["client_id"] = client["client_id"]
                    if client.get("token_endpoint_auth_method") == "client_secret_post" and client.get("client_secret"):
                        data["client_secret"] = client["client_secret"]
                try:
                    r = await http.post(ep, content=urlencode(data).encode(), headers=headers)
                    ok = ok or r.status_code == 200
                except Exception as e:  # noqa: BLE001 — thu hồi là cố gắng
                    log.info("thu hồi token lỗi: %s", type(e).__name__)
        return ok

    def _emit(self, op: str, name: str, extra: dict[str, Any]) -> None:
        try:
            self.publish(make_event("mcp.changed", None, {"op": op, "name": name, **extra}))
        except Exception:  # pragma: no cover
            log.exception("không phát được mcp.changed")


# ============================================================================ RPC


def rpc_methods(hub: Any) -> dict[str, Callable[[dict[str, Any]], Any]]:
    def _mgr() -> OAuthManager:
        m = getattr(hub, "mcp_oauth", None)
        if m is None:  # pragma: no cover
            raise HubError("OAuth MCP chưa sẵn sàng")
        return m

    def _req(p: dict[str, Any], k: str) -> Any:
        if not p.get(k):
            raise HubError(f"thiếu tham số '{k}'", code=-32602)
        return p[k]

    async def start(p: dict[str, Any]) -> dict[str, Any]:
        return await _mgr().start(str(_req(p, "name")), str(_req(p, "redirect_uri")))

    async def callback(p: dict[str, Any]) -> dict[str, Any]:
        return await _mgr().callback(str(_req(p, "state")), code=p.get("code"), error=p.get("error"),
                                     error_description=p.get("error_description"), iss=p.get("iss"))

    async def disconnect(p: dict[str, Any]) -> dict[str, Any]:
        return await _mgr().disconnect(str(_req(p, "name")))

    async def refresh(p: dict[str, Any]) -> dict[str, Any]:
        return {"name": p.get("name"), **(await _mgr().refresh(str(_req(p, "name"))))}

    return {
        "mcp.oauth_start": start,
        "mcp.oauth_callback": callback,
        "mcp.oauth_status": lambda p: _mgr().status(str(_req(p, "name"))),
        "mcp.oauth_disconnect": disconnect,
        "mcp.oauth_refresh": refresh,
    }
