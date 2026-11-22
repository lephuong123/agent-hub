"""Đo hiệu năng phía web (perf.py, docs/dev-notes/perf-monitor.md) — BẬT mặc định, chi phí gần bằng 0.

- `PerfMiddleware` (ASGI thuần, ngoài cùng — không đệm response nên SSE vẫn chạy): mỗi request HTTP đo tổng ms, route TEMPLATE
  (`scope["route"].path`, không bao giờ đường dẫn thô có id), status, phân rã RPC tới daemon (số lần + ms theo method) và render
  template; thêm header `Server-Timing` (devtools → Network → Timing); chậm hơn `perf.slow_ms` → WARNING `http.slow` (web.jsonl).
  Không tính vào tổng hợp: `/sse/*` (kết nối dài), `/static/*`, beacon.
- `POST /perf/beacon`: trình duyệt (`static/perf.js`) gửi lô mẫu bằng `navigator.sendBeacon` — thân `text/plain` chứa JSON
  `{csrf, k, s: [{m, r, d}]}` (sendBeacon không đặt được header → CSRF + khoá phiên NGOÀI cookie nằm trong thân, kiểm như `post_guard`);
  trần byte/số mẫu; `m` ∈ `FE_METRICS`; `r` là đường dẫn → ánh xạ về route template phía server.
- `/perf` "Hiệu năng": bảng "chậm nhất" FE / BE / RPC daemon; `/perf.json` cùng dữ liệu (JSON). Terminal: `cfharness perf`.
- Web đẩy ảnh chụp tổng hợp lên daemon (`perf.report`, tối đa mỗi `PUSH_EVERY_S`) để CLI/agent của người đọc qua `perf.stats`;
  phản hồi mang cấu hình `perf.*` (web không đọc config.yaml).
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .. import aiotasks
from .. import perf as _perf
from ..rpc import DaemonUnavailable, RpcError

log = logging.getLogger("cfharness.web.perf")

BEACON_PATH = "/perf/beacon"
MAX_BEACON_BYTES = 32 * 1024
MAX_SAMPLES = 200
MAX_SAMPLE_MS = 10 * 60 * 1000.0
PUSH_EVERY_S = 30.0
# metric trình duyệt được nhận (static/perf.js) — khoá tổng hợp = "<metric> <route template>"
FE_METRICS = frozenset({"nav.ttfb", "nav.dcl", "nav.load", "htmx", "switch.tab", "switch.ws", "sse.first", "sse.lag", "longtask"})
SKIP_PREFIXES = ("/sse/", "/static/", BEACON_PATH)
_ROUTE_CACHE_MAX = 1024


@dataclass
class WebPerf:
    """Tổng hợp của tiến trình web (RAM) + cấu hình nhận từ daemon qua `perf.report`."""

    backend: _perf.Rolling = field(default_factory=_perf.Rolling)
    frontend: _perf.Rolling = field(default_factory=_perf.Rolling)
    web_rpc: _perf.Rolling = field(default_factory=_perf.Rolling)
    enabled: bool = True
    slow_ms: float = float(_perf.DEFAULT_SLOW_MS)
    window_min: float = float(_perf.DEFAULT_WINDOW_MIN)
    last_push: float = 0.0
    pushing: bool = False
    _routes: dict[str, str] = field(default_factory=dict)

    def apply_config(self, cfg: Any) -> None:
        if not isinstance(cfg, dict):
            return
        self.enabled = bool(cfg.get("enabled", True))
        try:
            self.slow_ms = float(cfg.get("slow_ms") or 0)
            self.window_min = max(1.0, float(cfg.get("window_min") or _perf.DEFAULT_WINDOW_MIN))
        except (TypeError, ValueError):
            pass

    def snapshot(self, window_min: float | None = None, limit: int = 50) -> dict[str, Any]:
        win = max(1.0, min(float(window_min or self.window_min), _perf.KEEP_S / 60)) * 60
        return {"window_min": round(win / 60, 1), "backend": self.backend.snapshot(win, limit=limit),
                "frontend": self.frontend.snapshot(win, limit=limit), "web_rpc": self.web_rpc.snapshot(win, limit=limit)}


def route_template(app: Any, path: str) -> str:
    """Đường dẫn thô → route template (`/runs/{id}`) bằng chính router của app; không khớp → `(khác)`. Có cache giới hạn."""
    st: WebPerf | None = getattr(getattr(getattr(app, "state", None), "web", None), "perf", None)
    cache = st._routes if st is not None else {}
    hit = cache.get(path)
    if hit is not None:
        return hit
    from starlette.routing import Match

    label = _perf.OTHER_KEY
    scope = {"type": "http", "path": path, "method": "GET", "root_path": "", "headers": [], "query_string": b""}
    for r in getattr(getattr(app, "router", None), "routes", None) or []:
        try:
            m, _ = r.matches(scope)
        except Exception:  # best-effort: route lạ không khớp được → coi như không khớp
            continue
        if m != Match.NONE:
            label = getattr(r, "path", None) or _perf.OTHER_KEY
            break
    if len(cache) >= _ROUTE_CACHE_MAX:
        cache.clear()
    cache[path] = label
    return label


class PerfMiddleware:
    """ASGI thuần, đặt NGOÀI `GuardMiddleware` — xem docstring module."""

    def __init__(self, app: Any, state: Any) -> None:
        self.app = app
        self.state = state

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        wp: WebPerf = self.state.perf
        if scope["type"] != "http" or not wp.enabled:
            await self.app(scope, receive, send)
            return
        path = scope.get("path") or "/"
        t = _perf.ReqTiming()
        tok = _perf.CURRENT.set(t)
        status = 500
        sent_end = False

        async def send_timed(msg: dict) -> None:
            nonlocal status, sent_end
            if msg["type"] == "http.response.start":
                status = int(msg.get("status") or 0)
                hdr = _perf.server_timing(t, (time.perf_counter() - t.t0) * 1000.0)
                msg = {**msg, "headers": [*(msg.get("headers") or []), (b"server-timing", hdr.encode("latin-1", "replace"))]}
            elif msg["type"] == "http.response.body" and not msg.get("more_body"):
                sent_end = True
            await send(msg)

        try:
            await self.app(scope, receive, send_timed)
        finally:
            _perf.CURRENT.reset(tok)
            if not path.startswith(SKIP_PREFIXES):
                self._record(scope, path, t, status, sent_end)

    def _record(self, scope: dict, path: str, t: _perf.ReqTiming, status: int, done: bool) -> None:
        wp: WebPerf = self.state.perf
        dur = (time.perf_counter() - t.t0) * 1000.0
        route = getattr(scope.get("route"), "path", None) or _perf.OTHER_KEY
        method = scope.get("method") or "GET"
        key = f"{method} {route}"
        wp.backend.add(key, dur)
        for m, ms in t.calls:
            wp.web_rpc.add(m, ms)
        if wp.slow_ms and dur >= wp.slow_ms:
            log.warning("http.slow %s %s %s %.0fms (rpc %d/%.0fms, tpl %.0fms)", method, route, status, dur, t.rpc_n, t.rpc_ms, t.render_ms,
                        extra={"event": "http.slow", "method": method, "route": route[:120], "status": status, "duration_ms": round(dur, 1),
                               "rpc_n": t.rpc_n, "rpc_ms": round(t.rpc_ms, 1), "render_ms": round(t.render_ms, 1)})
        self._maybe_push()

    def _maybe_push(self) -> None:
        wp: WebPerf = self.state.perf
        now = time.monotonic()
        if wp.pushing or now - wp.last_push < PUSH_EVERY_S:
            return
        wp.last_push = now
        wp.pushing = True
        try:
            aiotasks.spawn(push_snapshot(self.state), name="perf-push")
        except RuntimeError:  # best-effort: không có event loop đang chạy (test đồng bộ) → bỏ lượt đẩy
            wp.pushing = False


async def push_snapshot(state: Any) -> None:
    """Ảnh chụp → daemon (`perf.report`); nhận lại cấu hình `perf.*`. Lỗi chỉ log DEBUG (daemon tắt / bản cũ chưa có method)."""
    wp: WebPerf = state.perf
    tok = _perf.CURRENT.set(None)  # không tính lời gọi này vào request vừa xong
    try:
        snap = wp.snapshot(limit=200)
        cfg = await state.client.call("perf.report", {k: snap[k] for k in _perf.WEB_SECTIONS}, timeout=5.0)
        wp.apply_config(cfg)
    except (RpcError, DaemonUnavailable, OSError, TimeoutError) as e:
        log.debug("không đẩy được ảnh chụp perf: %s", type(e).__name__)
    finally:
        wp.pushing = False
        _perf.CURRENT.reset(tok)


# ============================================================================ beacon
def parse_samples(app: Any, items: Any) -> list[tuple[str, float]]:
    """Lô mẫu → [(khoá, ms)]. Sai kiểu/ngoài khoảng → bỏ mẫu đó (không lỗi cả lô); quá `MAX_SAMPLES` → 413 ở tầng gọi."""
    out: list[tuple[str, float]] = []
    for s in items:
        if not isinstance(s, dict):
            continue
        m, r, d = s.get("m"), s.get("r"), s.get("d")
        if m not in FE_METRICS or not isinstance(r, str) or not r.startswith("/") or len(r) > 300:
            continue
        if not isinstance(d, (int, float)) or isinstance(d, bool) or not math.isfinite(d) or not 0 <= d <= MAX_SAMPLE_MS:
            continue
        out.append((f"{m} {route_template(app, r.split('?', 1)[0])}", float(d)))
    return out


async def beacon(request: Request) -> Response:
    from .app import origin_guard, session_of, ws
    from .security import KEY_FIELD, WebAuth

    sess = session_of(request)
    if sess is None:
        raise HTTPException(401, "Chưa đăng nhập")
    origin_guard(request)
    try:
        if int(request.headers.get("content-length") or 0) > MAX_BEACON_BYTES:
            raise HTTPException(413, "Lô quá lớn")
    except ValueError:
        raise HTTPException(400, "Content-Length sai") from None
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BEACON_BYTES:
            raise HTTPException(413, "Lô quá lớn")
    try:
        data = json.loads(bytes(body).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise HTTPException(400, "Thân không phải JSON") from None
    if not isinstance(data, dict):
        raise HTTPException(400, "Thân phải là object")
    if not WebAuth.csrf_ok(sess, data.get("csrf")):
        raise HTTPException(403, "Token CSRF sai hoặc thiếu")
    if not WebAuth.key_ok(sess, data.get(KEY_FIELD)):
        raise HTTPException(403, "Thiếu khoá phiên")
    items = data.get("s")
    if not isinstance(items, list):
        raise HTTPException(400, "Thiếu danh sách mẫu 's'")
    if len(items) > MAX_SAMPLES:
        raise HTTPException(413, f"Tối đa {MAX_SAMPLES} mẫu mỗi lô")
    wp: WebPerf = ws(request).perf
    if wp.enabled:
        for key, ms in parse_samples(request.app, items):
            wp.frontend.add(key, ms)
    return Response(status_code=204)


# ============================================================================ trang /perf + /perf.json
def _window(request: Request) -> float | None:
    try:
        v = float(request.query_params.get("w") or 0)
    except ValueError:
        return None
    return v if v > 0 else None


async def _data(request: Request) -> dict[str, Any]:
    from .app import rpc, ws

    wp: WebPerf = ws(request).perf
    win = _window(request)
    out = wp.snapshot(win, limit=50)
    out["daemon_rpc"] = []
    out["daemon_error"] = None
    try:
        res = await rpc(request, "perf.stats", {"window_min": out["window_min"], "limit": 50}) or {}
        out["daemon_rpc"] = res.get("rpc") or []
    except (RpcError, DaemonUnavailable) as e:
        out["daemon_error"] = type(e).__name__
    out["config"] = {"enabled": wp.enabled, "slow_ms": wp.slow_ms, "window_min": wp.window_min}
    return out


async def perf_json(request: Request) -> Response:
    return JSONResponse(await _data(request))


async def perf_page(request: Request) -> Response:
    from .app import render

    d = await _data(request)
    web_rpc = {r["key"]: r for r in d["web_rpc"]}
    return await render(request, "perf.html", {"nav": "perf", "d": d, "web_rpc": web_rpc, "windows": (5, 15, 30, 60),
                                               "split": split_key})


_KEY_RE = re.compile(r"^(\S+) (.*)$")


def split_key(key: str) -> tuple[str, str]:
    m = _KEY_RE.match(key or "")
    return (m.group(1), m.group(2)) if m else ("", key or "")


def routes() -> list[Any]:
    from starlette.routing import Route

    return [Route("/perf", perf_page), Route("/perf.json", perf_json), Route(BEACON_PATH, beacon, methods=["POST"])]


__all__ = ["PerfMiddleware", "WebPerf", "routes", "route_template", "parse_samples", "push_snapshot", "FE_METRICS", "MAX_BEACON_BYTES",
           "MAX_SAMPLES", "BEACON_PATH"]
