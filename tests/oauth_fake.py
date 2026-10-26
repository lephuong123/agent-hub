"""Authorization server OAuth 2.1 GIẢ + MCP endpoint được bảo vệ (Streamable HTTP, JSON) cho test OAuth MCP.

Chạy bằng uvicorn trên 127.0.0.1 (loopback → http được phép). Kiểm thật: PKCE S256, redirect_uri, client_id, `resource`
(RFC 8707), refresh, thu hồi. Không gọi mạng ngoài.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import socket
import threading
import time
from typing import Any
from urllib.parse import urlencode

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeOAuth:
    def __init__(self, *, dcr: bool = True, prm: bool = True, token_ttl: int = 3600, iss_param: bool = False,
                 manual_client: tuple[str, str] | None = None) -> None:
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.dcr, self.prm, self.token_ttl, self.iss_param = dcr, prm, token_ttl, iss_param
        self.clients: dict[str, dict[str, Any]] = {}
        if manual_client:
            self.clients[manual_client[0]] = {"client_secret": manual_client[1], "redirect_uris": None, "method": "client_secret_basic"}
        self.codes: dict[str, dict[str, Any]] = {}
        self.access: dict[str, float] = {}
        self.refresh: dict[str, str] = {}
        self.revoked: list[str] = []
        self.token_calls: list[dict[str, Any]] = []
        self.mcp_auth_seen: list[str | None] = []
        self.issued_access: list[str] = []
        self.issued_refresh: list[str] = []
        self.app = Starlette(routes=[
            Route("/mcp", self.mcp, methods=["POST", "GET", "DELETE"]),
            Route("/.well-known/oauth-protected-resource/mcp", self.prm_doc),
            Route("/.well-known/oauth-authorization-server/as", self.as_doc),
            Route("/as/register", self.register, methods=["POST"]),
            Route("/as/authorize", self.authorize),
            Route("/as/token", self.token, methods=["POST"]),
            Route("/as/revoke", self.revoke, methods=["POST"]),
        ])
        self._server: uvicorn.Server | None = None

    @property
    def mcp_url(self) -> str:
        return self.base + "/mcp"

    @property
    def issuer(self) -> str:
        return self.base + "/as"

    # ---------------------------------------------------------------- vòng đời
    def start(self) -> "FakeOAuth":
        cfg = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="error", access_log=False, lifespan="off")
        self._server = uvicorn.Server(cfg)
        t = threading.Thread(target=self._server.run, daemon=True)
        t.start()
        for _ in range(200):
            if self._server.started:
                break
            time.sleep(0.02)
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True

    # ---------------------------------------------------------------- MCP được bảo vệ
    async def mcp(self, request: Request) -> Response:
        auth = request.headers.get("authorization")
        self.mcp_auth_seen.append(auth)
        tok = auth[7:] if auth and auth.startswith("Bearer ") else None
        if not tok or self.access.get(tok, 0) < time.time() or tok in self.revoked:
            return Response(status_code=401, headers={
                "WWW-Authenticate": f'Bearer resource_metadata="{self.base}/.well-known/oauth-protected-resource/mcp", scope="mcp.read"'})
        if request.method != "POST":
            return Response(status_code=405)
        msg = await request.json()
        if "id" not in msg:
            return Response(status_code=202)
        if msg["method"] == "initialize":
            res = {"protocolVersion": msg["params"]["protocolVersion"], "capabilities": {"tools": {}}, "serverInfo": {"name": "fake-oauth-mcp", "version": "1"}}
        elif msg["method"] == "tools/list":
            res = {"tools": [{"name": "doc_ghi_chu", "description": "đọc ghi chú", "inputSchema": {"type": "object"}, "annotations": {"readOnlyHint": True}}]}
        else:
            return JSONResponse({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "no"}})
        return JSONResponse({"jsonrpc": "2.0", "id": msg["id"], "result": res})

    async def prm_doc(self, request: Request) -> Response:
        if not self.prm:
            return Response(status_code=404)
        return JSONResponse({"resource": self.mcp_url, "authorization_servers": [self.issuer], "scopes_supported": ["mcp.read"]})

    async def as_doc(self, request: Request) -> Response:
        d = {"issuer": self.issuer, "authorization_endpoint": self.issuer + "/authorize", "token_endpoint": self.issuer + "/token",
             "revocation_endpoint": self.issuer + "/revoke", "code_challenge_methods_supported": ["S256"],
             "response_types_supported": ["code"], "grant_types_supported": ["authorization_code", "refresh_token"]}
        if self.dcr:
            d["registration_endpoint"] = self.issuer + "/register"
        if self.iss_param:
            d["authorization_response_iss_parameter_supported"] = True
        return JSONResponse(d)

    async def register(self, request: Request) -> Response:
        body = await request.json()
        cid = "cid_" + secrets.token_hex(6)
        self.clients[cid] = {"redirect_uris": body["redirect_uris"], "method": "none"}
        return JSONResponse({"client_id": cid, "redirect_uris": body["redirect_uris"], "token_endpoint_auth_method": "none",
                             "grant_types": body.get("grant_types")}, status_code=201)

    async def authorize(self, request: Request) -> Response:
        """Mô phỏng người dùng bấm "Cho phép": 302 về redirect_uri với code + state."""
        q = request.query_params
        c = self.clients.get(q.get("client_id") or "")
        if c is None or (c["redirect_uris"] is not None and q.get("redirect_uri") not in c["redirect_uris"]):
            return Response("client/redirect sai", status_code=400)
        if q.get("code_challenge_method") != "S256" or not q.get("code_challenge") or not q.get("state"):
            return Response("thiếu PKCE/state", status_code=400)
        code = "code_" + secrets.token_urlsafe(12)
        self.codes[code] = {"client_id": q["client_id"], "redirect_uri": q["redirect_uri"], "challenge": q["code_challenge"],
                            "resource": q.get("resource"), "scope": q.get("scope")}
        params = {"code": code, "state": q["state"]}
        if self.iss_param:
            params["iss"] = self.issuer
        return RedirectResponse(q["redirect_uri"] + "?" + urlencode(params), status_code=302)

    def _client_auth(self, request: Request, form: dict[str, str]) -> str | None:
        auth = request.headers.get("authorization")
        if auth and auth.startswith("Basic "):
            cid, _, sec = base64.b64decode(auth[6:]).decode().partition(":")
            c = self.clients.get(cid)
            return cid if c and c.get("client_secret") == sec else None
        cid = form.get("client_id") or ""
        c = self.clients.get(cid)
        if c is None or c.get("client_secret"):
            return None
        return cid

    def _issue(self, cid: str, scope: str | None) -> dict[str, Any]:
        at, rt = "at_" + secrets.token_urlsafe(24), "rt_" + secrets.token_urlsafe(24)
        self.access[at] = time.time() + self.token_ttl
        self.refresh[rt] = cid
        self.issued_access.append(at)
        self.issued_refresh.append(rt)
        return {"access_token": at, "token_type": "bearer", "expires_in": self.token_ttl, "refresh_token": rt, "scope": scope or "mcp.read"}

    async def token(self, request: Request) -> Response:
        form = dict(await request.form())
        self.token_calls.append({k: v for k, v in form.items() if k not in ("code", "code_verifier", "refresh_token")} | {"_basic": "authorization" in request.headers})
        cid = self._client_auth(request, form)
        if cid is None:
            return JSONResponse({"error": "invalid_client"}, status_code=401)
        if form.get("resource") != self.mcp_url:
            return JSONResponse({"error": "invalid_target"}, status_code=400)
        if form.get("grant_type") == "authorization_code":
            rec = self.codes.pop(form.get("code") or "", None)
            if rec is None or rec["client_id"] != cid or rec["redirect_uri"] != form.get("redirect_uri"):
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            ver = form.get("code_verifier") or ""
            chal = base64.urlsafe_b64encode(hashlib.sha256(ver.encode()).digest()).decode().rstrip("=")
            if chal != rec["challenge"]:
                return JSONResponse({"error": "invalid_grant", "error_description": "PKCE sai"}, status_code=400)
            return JSONResponse(self._issue(cid, rec["scope"]))
        if form.get("grant_type") == "refresh_token":
            rt = form.get("refresh_token") or ""
            if self.refresh.get(rt) != cid or rt in self.revoked:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            self.refresh.pop(rt)
            return JSONResponse(self._issue(cid, None))
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    async def revoke(self, request: Request) -> Response:
        form = dict(await request.form())
        self.revoked.append(form.get("token") or "")
        return Response(status_code=200)
