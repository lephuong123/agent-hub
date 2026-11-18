"""Proxy loopback chèn token MCP OAuth (docs/dev-notes/mcp-oauth.md §8.1).

Vấn đề của đường cũ: `Authorization: Bearer <access token>` được chèn TĨNH vào mcp.json runtime lúc spawn → (a) run dài hơn hạn access
token (thường 1 h) bị 401 giữa chừng vì `claude` không biết refresh, (b) token nằm trong file runtime (0600, denyRead sandbox, xoá sau
run — nhưng vẫn là file).

Proxy (bật bằng `mcp.oauth_proxy.enabled`):
- HTTP server trong daemon, CHỈ bind `127.0.0.1` (cổng `mcp.oauth_proxy.port`, 0 = OS chọn), khởi động lười ở run đầu tiên cần.
- Mỗi (run, server OAuth) một ROUTE: id ngẫu nhiên 192 bit trong URL + khoá 256 bit trong header `X-Cfharness-Proxy-Key`. mcp.json
  của run trỏ tới `http://127.0.0.1:<cổng>/r/<id><path server>` và chỉ mang khoá — KHÔNG token, KHÔNG header secret của server.
- Mỗi request: kiểm Host loopback đúng cổng, không có `Origin` (chặn trình duyệt/DNS rebinding), route còn sống, khoá đúng
  (`compare_digest`), path ∈ danh sách cho phép của route → `ensure_fresh` (làm mới nếu còn < 5 phút) → lấy header mới nhất từ
  `registry.runtime_entry` (Bearer + header tĩnh đã thay secret) → chuyển tiếp tới ĐÚNG origin server đã cấu hình. Upstream trả 401 →
  làm mới bắt buộc MỘT lần rồi gửi lại (body request đã đệm, có trần).
- Stream: thân response chuyển từng khối (Streamable HTTP trả `text/event-stream`, GET SSE dài). Transport `sse` cũ: event
  `endpoint` được viết lại thành path qua proxy (cùng origin mới nhận; path đó vào danh sách cho phép của route).
- Thu hồi: `revoke_run(run_id)` trong `finally` của `RunManager._run` → route bị xoá, stream đang mở bị đóng. Daemon khởi động
  lại → mọi route mất (RAM).

Không cần ngoại lệ sandbox: sandbox OS của Claude Code chỉ bọc Bash; client MCP do chính tiến trình `claude` chạy NGOÀI sandbox (đã ghi
ở hardening-SEC §1). Bash của agent vẫn không chạm được loopback (trừ agent người mở `allow_localhost`/trust full) — và kể cả chạm được
cũng cần khoá theo run nằm trong `agents/*/runtime` (denyRead + `permissions.deny`).
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hmac
import logging
import secrets as _secrets
import socket
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlsplit

from ..aiotasks import spawn
from ..errors import HubError
from ..redact import redact_str
from .oauth import LOOPBACK_DIRECT_MOUNTS, is_oauth

if TYPE_CHECKING:
    from .config import McpServerConfig

log = logging.getLogger("cfharness.mcp_client.oauth_proxy")

KEY_HEADER = "X-Cfharness-Proxy-Key"
HOST = "127.0.0.1"
ROUTE_PREFIX = "/r/"
DEFAULT_MAX_BODY_MB = 16
# header không chuyển tiếp (hop-by-hop RFC 9110 §7.6.1 + thứ proxy tự đặt/không được lộ)
HOP = {"connection", "keep-alive", "proxy-connection", "transfer-encoding", "te", "trailer", "upgrade", "proxy-authorization",
       "proxy-authenticate", "host", "content-length"}
REQ_DROP = HOP | {"authorization", KEY_HEADER.lower(), "accept-encoding", "origin", "cookie", "referer", "forwarded", "x-forwarded-for",
                  "x-forwarded-host", "x-forwarded-proto"}
# WWW-Authenticate bị gỡ: không để `claude` tự khởi động luồng OAuth của nó với proxy (hub quản lý đăng nhập)
RESP_DROP = HOP | {"content-encoding", "www-authenticate", "set-cookie"}


@dataclass
class Route:
    id: str
    key: str
    run_id: str
    server: str
    upstream: str  # URL server đã cấu hình (lúc đăng ký)
    origin: str  # scheme://host[:port] của upstream
    transport: str
    allowed_paths: set[str] = field(default_factory=set)
    created: float = field(default_factory=time.time)
    responses: set[Any] = field(default_factory=set)
    requests: int = 0


def _origin(url: str) -> tuple[str, str]:
    s = urlsplit(url)
    return f"{s.scheme}://{s.netloc}", (s.path or "/")


def _server_cls() -> type:
    """Lớp con uvicorn.Server dựng lười (không import uvicorn khi proxy tắt)."""
    import uvicorn

    class QuietServer(uvicorn.Server):
        @contextlib.contextmanager
        def capture_signals(self):  # type: ignore[override]
            yield  # KHÔNG chiếm SIGINT/SIGTERM của daemon

    return QuietServer


class OAuthProxy:
    """Proxy loopback trong daemon. `transport` (httpx2) thay được cho test."""

    def __init__(self, hub: Any, *, transport: Any = None) -> None:
        self.hub = hub
        self.transport = transport
        self.routes: dict[str, Route] = {}
        self.port: int | None = None
        self._server: Any = None
        self._task: asyncio.Task | None = None
        self._sock: socket.socket | None = None
        self._start_lock: asyncio.Lock | None = None
        self._http: Any = None
        # đếm theo lý do (không URL/khoá/token) — nghiệm thu + `status()`; từ chối còn ghi log INFO kèm mã lý do
        self.stats: dict[str, int] = {}

    # ------------------------------------------------------------------ cấu hình / vòng đời
    def _cfg(self, key: str, default: Any) -> Any:
        try:
            v = self.hub.config.get(f"mcp.oauth_proxy.{key}")
        except KeyError:
            return default
        return default if v is None else v

    def enabled(self) -> bool:
        return bool(self._cfg("enabled", False))

    @property
    def running(self) -> bool:
        return self._server is not None and bool(getattr(self._server, "started", False)) and self.port is not None

    def needed_for(self, server_names: list[str] | None) -> bool:
        """Agent có server OAuth nào và proxy đang bật."""
        if not self.enabled() or not server_names:
            return False
        reg = getattr(self.hub, "mcp", None)
        for n in server_names:
            row = reg.get_row(n) if reg is not None else None
            if row is not None and is_oauth(row.get("config")):
                return True
        return False

    async def ensure_started(self) -> int:
        if self.running:
            return int(self.port)  # type: ignore[arg-type]
        if self._start_lock is None:
            self._start_lock = asyncio.Lock()
        async with self._start_lock:
            if self.running:
                return int(self.port)  # type: ignore[arg-type]
            import uvicorn

            port = int(self._cfg("port", 0) or 0)
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.bind((HOST, port))  # CHỈ loopback — không bao giờ 0.0.0.0
            except OSError:
                sock.close()
                raise
            sock.setblocking(False)
            config = uvicorn.Config(self, log_config=None, access_log=False, lifespan="off", log_level="warning",
                                    timeout_graceful_shutdown=1, server_header=False, date_header=False)
            server = _server_cls()(config)
            self._sock, self._server = sock, server
            self.port = sock.getsockname()[1]
            # Context RỖNG: khởi động lười trong task của một run → không để mọi log của proxy mang run_id của run đầu tiên
            self._task = asyncio.create_task(server.serve(sockets=[sock]), name="mcp-oauth-proxy", context=contextvars.Context())
            for _ in range(500):
                if server.started:
                    break
                if self._task.done():
                    break
                await asyncio.sleep(0.01)
            if not server.started:
                await self.stop()
                raise HubError("không khởi động được proxy OAuth MCP trên 127.0.0.1")
            log.info("proxy OAuth MCP nghe ở %s:%s", HOST, self.port)
            return int(self.port)

    async def stop(self) -> None:
        for rid in list(self.routes):
            self._drop(rid)
        srv, task = self._server, self._task
        self._server = self._task = None
        if srv is not None:
            srv.should_exit = True
        if task is not None:
            try:
                await asyncio.wait_for(task, 5)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):  # best-effort: chờ vòng proxy dừng; hết giờ/lỗi → huỷ  # noqa: BLE001
                task.cancel()
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.close()
            self._sock = None
        if self._http is not None:
            with contextlib.suppress(Exception):
                await self._http.aclose()
            self._http = None
        self.port = None

    # ------------------------------------------------------------------ route
    def url_for(self, route: Route, path: str | None = None) -> str:
        return f"http://{HOST}:{self.port}{ROUTE_PREFIX}{route.id}{path if path is not None else _origin(route.upstream)[1]}"

    def register(self, run_id: str, cfg: "McpServerConfig") -> dict[str, Any]:
        """Tạo route cho (run, server) → entry mcpServers trỏ tới proxy (chỉ khoá theo run, KHÔNG token)."""
        if not self.running:
            raise HubError("proxy OAuth MCP chưa chạy")
        url = str(cfg.url or "")
        origin, path = _origin(url)
        rid = _secrets.token_urlsafe(24)
        route = Route(id=rid, key=_secrets.token_urlsafe(32), run_id=run_id, server=cfg.name, upstream=url, origin=origin,
                      transport=cfg.transport, allowed_paths={path})
        self.routes[rid] = route
        return {"type": cfg.transport, "url": self.url_for(route), "headers": {KEY_HEADER: route.key}}

    def routes_of(self, run_id: str) -> list[Route]:
        return [r for r in self.routes.values() if r.run_id == run_id]

    def _drop(self, rid: str) -> None:
        r = self.routes.pop(rid, None)
        if r is None:
            return
        for resp in list(r.responses):
            with contextlib.suppress(RuntimeError):
                spawn(_aclose(resp), name="mcp-proxy-aclose")
        r.responses.clear()

    def revoke_run(self, run_id: str) -> int:
        """Run kết thúc → mọi route của run hết hiệu lực ngay (request sau → 403), stream đang mở bị đóng."""
        ids = [r.id for r in self.routes.values() if r.run_id == run_id]
        for rid in ids:
            self._drop(rid)
        return len(ids)

    def status(self) -> dict[str, Any]:
        return {"enabled": self.enabled(), "running": self.running, "port": self.port, "routes": len(self.routes), "stats": dict(self.stats)}

    def _count(self, key: str) -> None:
        self.stats[key] = self.stats.get(key, 0) + 1

    async def _reject(self, send: Any, reason: str, status: int = 403, detail: str = "") -> None:
        """Mọi từ chối: cùng thân trả lời (không lộ lý do cho bên gọi); lý do chỉ vào log/stats của daemon. `detail` chỉ cho
        thứ KHÔNG chứa route id/khoá (vd path ngoài `/r/`)."""
        self._count(f"rejected.{reason}")
        log.info("proxy OAuth MCP từ chối request (%s%s)", reason, f": {detail}" if detail else "")
        await _plain(send, status, "forbidden" if status == 403 else "not found")

    # ------------------------------------------------------------------ http
    def _client(self) -> Any:
        if self._http is None:
            import httpx2

            kw: dict[str, Any] = {"timeout": httpx2.Timeout(connect=15.0, read=None, write=60.0, pool=None),
                                  "follow_redirects": False, "mounts": dict(LOOPBACK_DIRECT_MOUNTS)}
            if self.transport is not None:
                kw["transport"] = self.transport
            self._http = httpx2.AsyncClient(**kw)
        return self._http

    def _upstream_headers(self, cfg: "McpServerConfig") -> tuple[dict[str, str], str]:
        """Header mới nhất của server (Bearer + header tĩnh đã thay secret). Chưa kết nối/hết hạn → HubError."""
        entry = self.hub.mcp.runtime_entry(cfg)
        hdrs = {str(k): str(v) for k, v in (entry.get("headers") or {}).items()}
        auth = next((v for k, v in hdrs.items() if k.lower() == "authorization"), "")
        return hdrs, auth

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            return
        try:
            await self._handle(scope, receive, send)
        except Exception as e:  # noqa: BLE001 — không để lỗi một request làm sập server; không lộ chi tiết
            log.warning("proxy OAuth MCP lỗi: %s", redact_str(f"{type(e).__name__}: {e}"))
            with contextlib.suppress(Exception):
                await _plain(send, 502, "proxy error")

    async def _handle(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
        client = scope.get("client") or ("", 0)
        if client[0] not in ("127.0.0.1", "::1"):
            return await self._reject(send, "peer")
        if headers.get("host") not in (f"{HOST}:{self.port}", f"localhost:{self.port}"):
            return await self._reject(send, "host")  # DNS rebinding: Host lạ
        if "origin" in headers:
            return await self._reject(send, "origin")  # trình duyệt (CSRF) — client MCP của claude không gửi Origin
        path: str = scope.get("path") or ""
        if not path.startswith(ROUTE_PREFIX):
            return await self._reject(send, "no_route", 404, f"{scope.get('method')} {redact_str(path)[:80]}")
        rid, _, rest = path[len(ROUTE_PREFIX):].partition("/")
        route = self.routes.get(rid)
        key = headers.get(KEY_HEADER.lower(), "")
        if route is None:
            return await self._reject(send, "unknown_route")  # route lạ / đã thu hồi
        if not key:
            return await self._reject(send, "missing_key")
        if not hmac.compare_digest(key.encode(), route.key.encode()):
            return await self._reject(send, "bad_key")
        upath = "/" + rest
        if upath not in route.allowed_paths:
            return await self._reject(send, "path")
        reg = self.hub.mcp
        try:
            cfg = reg.config_of(reg.require(route.server))
        except Exception:  # noqa: BLE001 — server bị xoá giữa run
            return await self._reject(send, "server_gone")
        if not is_oauth(cfg) or str(cfg.url or "") != route.upstream:
            return await self._reject(send, "url_changed")  # URL đổi giữa run → không gửi token sang chỗ mới
        # thân request (MCP: JSON-RPC nhỏ) — đệm có trần để gửi lại được sau khi làm mới token
        cap = int(float(self._cfg("max_body_mb", DEFAULT_MAX_BODY_MB)) * 1024 * 1024)
        body = bytearray()
        while True:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                return
            body += msg.get("body", b"")
            if len(body) > cap:
                return await _plain(send, 413, "request too large")
            if not msg.get("more_body"):
                break
        route.requests += 1
        self._count(f"accepted.{scope.get('method') or 'GET'}")
        method = scope.get("method") or "GET"
        qs = (scope.get("query_string") or b"").decode("latin-1")
        url = route.origin + upath + (f"?{qs}" if qs else "")
        fwd = {k: v for k, v in headers.items() if k not in REQ_DROP}
        fwd["accept-encoding"] = "identity"
        oauth = self.hub.mcp_oauth
        await oauth.ensure_fresh(route.server)
        resp = None
        for attempt in range(2):
            try:
                extra, used = self._upstream_headers(cfg)
            except HubError as e:
                log.warning("proxy OAuth MCP %s: %s", route.server, e.message)
                return await _plain(send, 401, "oauth not connected")
            req_headers = {**fwd, **{k.lower(): v for k, v in extra.items()}}
            http = self._client()
            req = http.build_request(method, url, headers=req_headers, content=bytes(body))
            resp = await http.send(req, stream=True)
            if resp.status_code != 401 or attempt == 1:
                break
            await resp.aclose()
            try:
                _, cur = self._upstream_headers(cfg)
            except HubError:
                cur = ""
            self._count("upstream_401")
            if cur == used:  # chưa ai làm mới → làm mới bắt buộc (request khác có thể vừa làm)
                try:
                    await oauth.refresh(route.server)
                    self._count("refreshed_after_401")
                except HubError as e:
                    log.warning("proxy OAuth MCP %s: làm mới sau 401 lỗi: %s", route.server, e.message)
                    return await _plain(send, 401, "oauth refresh failed")
        assert resp is not None
        if route.id not in self.routes:  # bị thu hồi trong lúc chờ upstream
            await resp.aclose()
            return await _plain(send, 403, "forbidden")
        route.responses.add(resp)
        try:
            await self._relay(route, resp, receive, send)
        finally:
            route.responses.discard(resp)
            await _aclose(resp)

    async def _relay(self, route: Route, resp: Any, receive: Any, send: Any) -> None:
        out = [(k.encode("latin-1"), v.encode("latin-1")) for k, v in resp.headers.items() if k.lower() not in RESP_DROP]
        await send({"type": "http.response.start", "status": resp.status_code, "headers": out})
        sse = "text/event-stream" in (resp.headers.get("content-type") or "") and route.transport == "sse"

        async def watch_disconnect() -> None:
            while True:
                m = await receive()
                if m["type"] == "http.disconnect":
                    await _aclose(resp)
                    return

        watcher = asyncio.create_task(watch_disconnect())
        try:
            buf = b""
            async for chunk in resp.aiter_raw():
                if route.id not in self.routes:
                    break
                if sse:
                    buf += chunk
                    chunk, buf = self._rewrite_sse(route, buf)
                    if not chunk:
                        continue
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
            if buf:
                await send({"type": "http.response.body", "body": buf, "more_body": True})
        except Exception as e:  # noqa: BLE001 — upstream đóng / bị thu hồi giữa stream
            log.info("proxy OAuth MCP %s: stream dừng (%s)", route.server, type(e).__name__)
        finally:
            watcher.cancel()
        with contextlib.suppress(Exception):
            await send({"type": "http.response.body", "body": b"", "more_body": False})

    def _rewrite_sse(self, route: Route, buf: bytes) -> tuple[bytes, bytes]:
        """Trả (phần đã xử lý, phần dở). Event `endpoint` của transport SSE cũ → path qua proxy (cùng origin mới nhận)."""
        norm = buf.replace(b"\r\n", b"\n")
        idx = norm.rfind(b"\n\n")
        if idx < 0:
            return b"", buf
        done, rest = norm[: idx + 2], norm[idx + 2:]
        out: list[bytes] = []
        for block in done.split(b"\n\n")[:-1]:
            lines = block.split(b"\n")
            if any(ln.strip() in (b"event: endpoint", b"event:endpoint") for ln in lines):
                lines = [self._endpoint_line(route, ln) if ln.startswith(b"data:") else ln for ln in lines]
            out.append(b"\n".join(lines))
        return b"\n\n".join(out) + b"\n\n", rest

    def _endpoint_line(self, route: Route, line: bytes) -> bytes:
        data = line[5:].decode("utf-8", "replace").strip()
        target = urljoin(route.upstream, data)
        s = urlsplit(target)
        if f"{s.scheme}://{s.netloc}" != route.origin:
            log.warning("proxy OAuth MCP %s: endpoint SSE khác origin — giữ nguyên (client sẽ không có token)", route.server)
            return line
        path = s.path or "/"
        route.allowed_paths.add(path)
        new = f"{ROUTE_PREFIX}{route.id}{path}" + (f"?{s.query}" if s.query else "")
        return b"data: " + new.encode("utf-8")


async def _aclose(resp: Any) -> None:
    with contextlib.suppress(Exception):
        await resp.aclose()


async def _plain(send: Any, status: int, text: str) -> None:
    body = text.encode("utf-8")
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"text/plain; charset=utf-8"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body, "more_body": False})
