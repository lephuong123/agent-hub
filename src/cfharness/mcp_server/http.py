"""Streamable HTTP cho MCP server của hub (FR-MS-01b, FR-MS-06, NFR-14).

- CHỈ bind loopback (127.0.0.1 / ::1 / localhost) — Q-11 một người dùng/một máy. Host khác → từ chối.
- Token cục bộ sinh lần đầu, lưu `HUB_HOME/mcp_server/tokens.json` (0600): hai mức `read` và `control`.
  Thiếu/sai token → 401. Token `read` chỉ gọi được tool readOnly (kiểm trong từng tool).
"""

from __future__ import annotations

import json
import os
import secrets as _secrets
from pathlib import Path
from typing import Any

from ..events import iso
from ..paths import HubPaths

LOOPBACK = ("127.0.0.1", "::1", "localhost")


def tokens_path(paths: HubPaths) -> Path:
    return paths.home / "mcp_server" / "tokens.json"


def ensure_tokens(paths: HubPaths, *, rotate: bool = False) -> dict[str, str]:
    p = tokens_path(paths)
    if p.is_file() and not rotate:
        data = json.loads(p.read_text(encoding="utf-8"))
        if data.get("read") and data.get("control"):
            return {"read": data["read"], "control": data["control"]}
    p.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(p.parent, 0o700)
    data = {"read": "ahr_" + _secrets.token_urlsafe(32), "control": "ahc_" + _secrets.token_urlsafe(32), "created_at": iso()}
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return {"read": data["read"], "control": data["control"]}


class BearerAuth:
    """ASGI middleware: yêu cầu `Authorization: Bearer <token read|control>` cho mọi request HTTP."""

    def __init__(self, app: Any, tokens: dict[str, str]) -> None:
        self.app = app
        self.valid = {v for v in tokens.values() if v}

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http":
            auth = ""
            for k, v in scope.get("headers") or []:
                if k.lower() == b"authorization":
                    auth = v.decode("latin-1")
                    break
            tok = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
            if tok not in self.valid:
                body = json.dumps({"error": "unauthorized", "message": "thiếu hoặc sai token (cfharness mcp token)"}, ensure_ascii=False).encode()
                await send({"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


def build_http_app(caller: Any, tokens: dict[str, str], *, host: str = "127.0.0.1") -> Any:
    from .server import HubMcp

    if host not in LOOPBACK:
        raise ValueError(f"MCP HTTP chỉ được bind loopback (127.0.0.1) — không chấp nhận '{host}' (Q-11, NFR-14)")
    srv = HubMcp(caller, default_level="read", tokens=tokens, transport="http").build()
    app = srv.streamable_http_app(host=host if host != "localhost" else "127.0.0.1")
    return BearerAuth(app, tokens)


def serve_http(caller: Any, paths: HubPaths, *, host: str = "127.0.0.1", port: int = 8765) -> None:
    import uvicorn

    tokens = ensure_tokens(paths)
    app = build_http_app(caller, tokens, host=host)
    uvicorn.run(app, host=host, port=port, log_level="warning")
