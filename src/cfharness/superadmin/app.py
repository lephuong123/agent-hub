"""Ứng dụng web SAC (Starlette) — plan-superadmin SA1-2.

Lớp bảo vệ theo thứ tự cho MỌI request:
1. Host phải là `sac.hostname` (Cloudflare Tunnel) — khác → 421. Khi Access CHƯA cấu hình (lúc dựng máy), chỉ nhận Host loopback.
2. JWT Cloudflare Access hợp lệ (AUTH-02) khi đã cấu hình — thiếu/sai → 404 trơn (VIS-01: không tên sản phẩm, không "Forbidden").
3. Phiên SAC (cookie `__Host-sac`, Secure, HttpOnly, SameSite=Strict) giữ ticket của admind. Không phiên → trang đăng nhập.
4. POST: CSRF của phiên + Origin cùng nguồn. Thao tác đi qua `/op/<mã>` → admind (admind kiểm quyền/step-up/four-eyes thật).

Menu dựng từ `admind.rbac.MATRIX` theo vai trò (VIS-08): mục không có quyền bị ẩn hẳn; trang gõ tay mà admind từ chối → 404.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from ..admind import rbac
from ..admind.client import AdmindClient, AdmindError, AdmindUnavailable
from .access import HEADER as ACCESS_HEADER
from .access import AccessVerifier
from . import views

log = logging.getLogger("cfharness.superadmin")

HERE = Path(__file__).parent
COOKIE = "__Host-sac"
IDLE_S = 15 * 60
MAX_S = 8 * 3600
NOT_FOUND = PlainTextResponse("Not Found", status_code=404)


@dataclass
class Sess:
    sid: str
    ticket: str
    account: dict[str, Any]
    nav: list[str]
    csrf: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    created: float = field(default_factory=time.time)
    last: float = field(default_factory=time.time)
    flashes: list[tuple[str, str]] = field(default_factory=list)
    reveal: list[dict[str, Any]] = field(default_factory=list)   # secret hiện MỘT lần (link kích hoạt, link TOTP)
    pending: dict[str, Any] | None = None                         # thao tác chờ step-up

    def flash(self, kind: str, text: str) -> None:
        self.flashes.append((kind, text))
        del self.flashes[:-8]


@dataclass
class Config:
    hostname: str
    access: AccessVerifier | None
    loopback_ok: bool = False      # chưa cấu hình Access: chỉ nhận Host loopback (dựng máy)
    port: int = 8700


class SacApp:
    def __init__(self, client: AdmindClient, config: Config, *, clock: Callable[[], float] = time.time) -> None:
        self.client = client
        self.cfg = config
        self.clock = clock
        self.sessions: dict[str, Sess] = {}
        self.templates = Jinja2Templates(directory=str(HERE / "templates"))
        env = self.templates.env
        env.autoescape = True
        env.globals.update(views.GLOBALS)
        env.filters.update(views.FILTERS)

    # ------------------------------------------------------------------ cổng vào
    def _host_ok(self, request: Request) -> bool:
        host = (request.headers.get("host") or "").lower()
        if self.cfg.access is not None:
            return host == self.cfg.hostname.lower()
        if self.cfg.loopback_ok:  # chưa có Access: KHÔNG nhận tên miền công khai (đi qua tunnel mà không có lớp Access)
            name = host.rsplit(":", 1)[0]
            return name in ("127.0.0.1", "localhost", "[::1]")
        return False

    def _access(self, request: Request) -> dict[str, Any] | None:
        if self.cfg.access is None:
            return {"email": None, "local": True} if self.cfg.loopback_ok else None
        return self.cfg.access.verify(request.headers.get(ACCESS_HEADER))

    @staticmethod
    def client_ip(request: Request) -> str:
        return (request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "") or "")[:64]

    def session(self, request: Request) -> Sess | None:
        sid = request.cookies.get(COOKIE)
        s = self.sessions.get(sid or "")
        now = self.clock()
        if s and (now - s.last > IDLE_S or now - s.created > MAX_S):
            self.sessions.pop(s.sid, None)
            return None
        return s

    def _origin_ok(self, request: Request) -> bool:
        origin = request.headers.get("origin") or request.headers.get("referer") or ""
        if not origin:
            return False
        u = urlsplit(origin)
        return (u.netloc or "").lower() == (request.headers.get("host") or "").lower()

    # ------------------------------------------------------------------ admind
    async def call(self, request: Request, op: str, params: dict[str, Any] | None = None, *, sess: Sess | None = None) -> Any:
        s = sess if sess is not None else self.session(request)
        return await asyncio.to_thread(self.client.call, op, params or {}, ticket=s.ticket if s else None, ip=self.client_ip(request),
                                       ua=(request.headers.get("user-agent") or "")[:200])

    # ------------------------------------------------------------------ hiển thị
    def render(self, request: Request, template: str, sess: Sess | None, status: int = 200, **ctx: Any) -> Response:
        flashes = list(sess.flashes) if sess else []
        reveal = list(sess.reveal) if sess else []
        if sess:
            sess.flashes.clear()
            sess.reveal.clear()
        nav = views.nav_for(sess.account["role"]) if sess else []
        resp = self.templates.TemplateResponse(request, template, {"sess": sess, "flashes": flashes, "reveal": reveal, "nav": nav,
                                                                   "csrf": sess.csrf if sess else "", "path": request.url.path,
                                                                   "loopback_mode": self.cfg.access is None, **ctx}, status_code=status)
        return resp

    def login_page(self, request: Request, error: str = "", status: int = 200) -> Response:
        return self.render(request, "login.html", None, status=status, error=error)

    # ------------------------------------------------------------------ middleware chung
    async def guard(self, request: Request, handler: Callable[..., Any]) -> Response:
        if not self._host_ok(request):
            return PlainTextResponse("Misdirected Request", status_code=421)
        claims = self._access(request)
        if claims is None:
            return NOT_FOUND
        request.state.access = claims
        try:
            resp = await handler(request)
        except AdmindUnavailable:
            resp = self.render(request, "error.html", self.session(request), status=503, message="admind không chạy — liên hệ người trực máy")
        except AdmindError as e:
            if e.code in ("forbidden", "not_found"):
                return NOT_FOUND
            if e.code == "unauthenticated":
                s = self.session(request)
                if s:
                    self.sessions.pop(s.sid, None)
                return RedirectResponse("/login", status_code=303)
            resp = self.render(request, "error.html", self.session(request), status=400, message=e.message)
        for k, v in SECURITY_HEADERS.items():
            resp.headers.setdefault(k, v)
        return resp


SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; frame-ancestors 'none'; "
                               "base-uri 'none'; form-action 'self'",
    "Referrer-Policy": "same-origin",  # no-referrer làm Chrome/Safari gửi `Origin: null` cho form POST → "Bad Origin"
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
    "Strict-Transport-Security": "max-age=31536000",
}


# ============================================================================ chuyển form → tham số
def form_params(form: Any) -> dict[str, Any]:
    """Trường `p.<tên>[:kiểu]` → tham số. Kiểu: str (mặc định), int, float, bool, json, list (phân cách dòng/dấu phẩy), multi.
    Chuỗi rỗng bị bỏ (trừ `:keep`). Tên có dấu chấm tạo object lồng (`p.scope.teams:list`)."""
    out: dict[str, Any] = {}
    keys = list(dict.fromkeys(k for k in form.keys() if k.startswith("p.")))
    for k in keys:
        name, _, kind = k[2:].partition(":")
        vals = form.getlist(k)
        v: Any = vals[-1] if vals else ""
        if kind == "multi":
            v = [x for x in vals if x]
        elif kind == "bool":
            v = str(v).lower() in ("1", "true", "on", "yes")
        elif v == "" and kind != "keep":
            continue
        elif kind == "int":
            try:
                v = int(v)
            except ValueError:
                pass
        elif kind == "float":
            try:
                v = float(v)
            except ValueError:
                pass
        elif kind == "json":
            try:
                v = json.loads(v)
            except ValueError:
                pass
        elif kind == "list":
            v = [x.strip() for x in str(v).replace(",", "\n").splitlines() if x.strip()]
        cur = out
        parts = name.split(".")
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = v
    return out


def safe_next(v: Any, default: str = "/") -> str:
    s = str(v or "")
    if s.startswith("/") and not s.startswith("//") and "\\" not in s and len(s) < 500:
        return s
    return default


# ============================================================================ route
def build(app: SacApp) -> Starlette:
    async def login(request: Request) -> Response:
        if request.method == "GET":
            if app.session(request):
                return RedirectResponse("/", status_code=303)
            return app.login_page(request)
        if not app._origin_ok(request):
            return PlainTextResponse("Bad Origin", status_code=403)
        f = await request.form()
        email = str(f.get("email") or "").strip().lower()
        claims = request.state.access
        if claims.get("email") and claims["email"].lower() != email:
            return app.login_page(request, "Email đăng nhập phải trùng email đã qua Cloudflare Access", 400)
        try:
            r = await asyncio.to_thread(app.client.call, "auth.login", {"email": email, "password": str(f.get("password") or ""),
                                                                         "totp": str(f.get("totp") or "").strip()},
                                        ip=app.client_ip(request), ua=(request.headers.get("user-agent") or "")[:200])
        except AdmindError as e:
            if e.code in ("forbidden", "not_found", "unavailable"):
                raise
            return app.login_page(request, e.message, 401)
        s = Sess(sid=secrets.token_urlsafe(32), ticket=r["ticket"], account=r["account"], nav=r["nav"], created=app.clock(), last=app.clock())
        app.sessions[s.sid] = s
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(COOKIE, s.sid, httponly=True, secure=True, samesite="strict", path="/", max_age=MAX_S)
        return resp

    async def logout(request: Request) -> Response:
        s = app.session(request)
        if request.method != "POST" or not s or not app._origin_ok(request):
            return RedirectResponse("/login", status_code=303)
        f = await request.form()
        if f.get("csrf") == s.csrf:
            try:
                await app.call(request, "auth.logout", sess=s)
            except AdmindError:
                pass
            app.sessions.pop(s.sid, None)
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="strict")
        return resp

    async def enroll(request: Request) -> Response:
        code = request.path_params["code"]
        if request.method == "GET":
            try:
                info = await asyncio.to_thread(app.client.call, "auth.enroll_begin", {"code": code}, ip=app.client_ip(request))
            except AdmindError as e:
                if e.code == "unavailable":
                    raise
                return NOT_FOUND
            qr = info.get("qr_svg")
            if qr and "xmlns=" not in qr.split(">", 1)[0]:  # SVG dạng ảnh (data:) bắt buộc có xmlns, không thì trình duyệt không vẽ
                qr = qr.replace("<svg ", '<svg xmlns="http://www.w3.org/2000/svg" ', 1)
            return app.render(request, "enroll.html", None, info=info, code=code,
                              qr=("data:image/svg+xml;base64," + base64.b64encode(qr.encode()).decode()) if qr else None)
        if not app._origin_ok(request):
            return PlainTextResponse("Bad Origin", status_code=403)
        f = await request.form()
        if str(f.get("password") or "") != str(f.get("password2") or ""):
            return app.render(request, "error.html", None, status=400, message="Hai lần nhập mật khẩu không khớp — mở lại link để làm lại")
        try:
            await asyncio.to_thread(app.client.call, "auth.enroll_finish", {"code": code, "password": str(f.get("password") or ""),
                                                                             "totp": str(f.get("totp") or "").strip()}, ip=app.client_ip(request))
        except AdmindError as e:
            if e.code == "unavailable":
                raise
            return app.render(request, "error.html", None, status=400, message=f"Không kích hoạt được: {e.message}")
        return app.render(request, "login.html", None, info="Đã kích hoạt — đăng nhập bằng email, mật khẩu và mã TOTP.")

    async def do_op(request: Request) -> Response:
        """POST /op/<mã>: chạy một thao tác admind từ form. step_up_required → trang nhập TOTP rồi chạy lại."""
        s = app.session(request)
        if not s:
            return RedirectResponse("/login", status_code=303)
        if not app._origin_ok(request):
            return PlainTextResponse("Bad Origin", status_code=403)
        f = await request.form()
        if f.get("csrf") != s.csrf:
            return PlainTextResponse("Bad CSRF", status_code=403)
        s.last = app.clock()
        name = request.path_params["name"]
        nxt = safe_next(f.get("next"), request.headers.get("referer") and urlsplit(request.headers["referer"]).path or "/")
        params = form_params(f)
        return await run_op(request, s, name, params, nxt, label=str(f.get("label") or ""))

    async def run_op(request: Request, s: Sess, name: str, params: dict[str, Any], nxt: str, label: str = "") -> Response:
        try:
            r = await app.call(request, name, params, sess=s)
        except AdmindError as e:
            if e.code == "step_up_required":
                s.pending = {"op": name, "params": params, "next": nxt, "label": label}
                return RedirectResponse("/step-up", status_code=303)
            if e.code == "approval_required":
                d = e.data or {}
                s.flash("warn", f"Đã tạo yêu cầu {d.get('approval_id')} — " + ("hệ chỉ có 1 Super Admin: tự chạy sau 1 giờ nếu không huỷ."
                                                                                if d.get("solo") else "chờ Super Admin khác duyệt (24 giờ)."))
                return RedirectResponse(nxt, status_code=303)
            if e.code in ("unauthenticated",):
                raise
            detail = ""
            if isinstance(e.data, list):
                detail = " — " + "; ".join(f"{x.get('field')}: {x.get('error')}" for x in e.data[:3] if isinstance(x, dict))
            s.flash("err", f"{label or name}: {e.message}{detail}")
            return RedirectResponse(nxt, status_code=303)
        views.after_op(s, name, r)
        s.flash("ok", f"{label or views.OP_LABEL.get(name, name)}: xong")
        jid = r.get("job_id") if isinstance(r, dict) else None
        if jid:
            return RedirectResponse(f"/jobs/{jid}", status_code=303)
        target = r.get("redirect") if isinstance(r, dict) else None
        return RedirectResponse(safe_next(target, nxt), status_code=303)

    async def step_up(request: Request) -> Response:
        s = app.session(request)
        if not s:
            return RedirectResponse("/login", status_code=303)
        if request.method == "GET":
            return app.render(request, "step_up.html", s, pending=s.pending)
        if not app._origin_ok(request):
            return PlainTextResponse("Bad Origin", status_code=403)
        f = await request.form()
        if f.get("csrf") != s.csrf:
            return PlainTextResponse("Bad CSRF", status_code=403)
        try:
            await app.call(request, "auth.step_up", {"totp": str(f.get("totp") or "").strip()}, sess=s)
        except AdmindError as e:
            if e.code == "unauthenticated" and "TOTP" in e.message:
                s.flash("err", e.message)
                return RedirectResponse("/step-up", status_code=303)
            raise
        p, s.pending = s.pending, None
        if not p:
            return RedirectResponse("/", status_code=303)
        return await run_op(request, s, p["op"], p["params"], p["next"], p.get("label", ""))

    async def page(request: Request) -> Response:
        s = app.session(request)
        if not s:
            return RedirectResponse("/login", status_code=303) if request.url.path == "/" else NOT_FOUND
        s.last = app.clock()
        return await views.dispatch(app, request, s)

    async def api(request: Request) -> Response:
        s = app.session(request)
        if not s:
            return JSONResponse({"error": "unauthenticated"}, status_code=401)
        data = await views.api(app, request, s)
        return JSONResponse(data) if data is not None else NOT_FOUND

    def wrap(fn: Callable[[Request], Any]) -> Callable[[Request], Any]:
        async def inner(request: Request) -> Response:
            return await app.guard(request, fn)
        return inner

    async def notfound(request: Request, exc: Exception) -> Response:
        return NOT_FOUND

    routes = [
        Route("/login", wrap(login), methods=["GET", "POST"]),
        Route("/logout", wrap(logout), methods=["GET", "POST"]),
        Route("/enroll/{code}", wrap(enroll), methods=["GET", "POST"]),
        Route("/op/{name}", wrap(do_op), methods=["POST"]),
        Route("/step-up", wrap(step_up), methods=["GET", "POST"]),
        Route("/api/{name}", wrap(api), methods=["GET"]),
        Mount("/static", app=StaticFiles(directory=str(HERE / "static")), name="static"),
        Route("/{path:path}", wrap(page), methods=["GET"]),
    ]
    return Starlette(routes=routes, exception_handlers={404: notfound, 405: notfound})


def create_app(client: AdmindClient | None = None, *, hostname: str | None = None, access: AccessVerifier | None = None,
               loopback_ok: bool | None = None, port: int = 8700, clock: Callable[[], float] = time.time) -> Starlette:
    cl = client or AdmindClient()
    if hostname is None:
        cfg = cl.call("sys.sac_config")
        hostname = cfg["hostname"]
        if access is None and cfg.get("access_aud") and cfg.get("access_team"):
            access = AccessVerifier(team=cfg["access_team"], aud=cfg["access_aud"])
    sac = SacApp(cl, Config(hostname=hostname, access=access, loopback_ok=(access is None) if loopback_ok is None else loopback_ok, port=port),
                 clock=clock)
    star = build(sac)
    star.state.sac = sac
    return star


def serve(*, host: str = "127.0.0.1", port: int = 8700) -> int:  # pragma: no cover — chạy thật
    import uvicorn

    from ..web.security import require_loopback

    require_loopback(host)
    uvicorn.run(create_app(port=port), host=host, port=port, log_level="info", proxy_headers=False, server_header=False)
    return 0
