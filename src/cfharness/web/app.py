"""Ứng dụng Starlette của `cfharness web` — client thuần của daemon (RPC qua Unix socket, như TUI).

Luồng hằng ngày: Tổng quan (agent nào đang làm gì, chờ duyệt gì) → bấm vào run xem live (SSE) → duyệt / chỉ đạo (steer) /
dừng → tạo agent từ template + gán MCP bằng checkbox → giao việc bằng form → xem kết quả & chi phí.

Thao tác ghi = form POST `/do/<action>` (bảng `ACTIONS`) hoặc form riêng (tạo/sửa agent): kiểm CSRF + Origin, thao tác
nhạy cảm cần `confirm=yes` (JS hiện hộp xác nhận; không có JS → trang xác nhận phía server). Mọi lời gọi ghi gửi
`_audit.channel=web` (approval: `channel=web`) → `audit_log` kênh `web`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Callable
from urllib.parse import parse_qs, urlencode, urlsplit

from markupsafe import Markup
from starlette.applications import Starlette
from starlette.datastructures import FormData
from starlette.exceptions import HTTPException
from starlette.requests import Request, cookie_parser
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from .. import auth as _auth
from ..errors import BLOCKED
from ..paths import HubPaths
from ..rpc import DaemonUnavailable, RpcError
from ..tui.client import AsyncHubClient, subscribe
from ..tui.model import STATUS_ORDER
from . import present as P
from .security import COOKIE, KEY_FIELD, KEY_HEADER, WEB_HOST, Session, WebAuth, allowed_hosts, is_loopback

log = logging.getLogger("cfharness.web")
HERE = Path(__file__).parent
HTMX = "htmx-2.0.4.min.js"
HEARTBEAT_S = 15.0
CHANNEL = "web"
MODELS = ("sonnet", "opus", "haiku", "fable")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
PERMISSION_MODES = ("default", "acceptEdits", "plan", "auto", "dontAsk", "manual")
# Cấu hình an toàn cho phép sửa từ web (khoá, nhãn, gợi ý). Phần còn lại: `cfharness config set` (vẫn chỉ người).
SAFE_CONFIG: tuple[tuple[str, str, str], ...] = (
    ("hub.max_concurrent_runs", "Số run chạy đồng thời tối đa (toàn hub)", "số nguyên, vd 4"),
    ("runs.default_max_budget_usd", "Ngân sách mặc định mỗi run ($)", "để trống = không giới hạn; vd 1.5"),
    ("runs.stop_grace_s", "Thời gian chờ dừng êm (giây)", "vd 10"),
    ("approval.timeout_s", "Giữ chờ duyệt trước khi chuyển 'chờ người' (giây)", "vd 300 — không bao giờ tự từ chối"),
    ("digest.enabled", "Bật bản tin học hằng ngày", "true / false"),
    ("digest.schedule", "Giờ gửi bản tin (giờ máy)", "HH:MM, vd 08:00"),
    ("notify.slack.enabled", "Bật thông báo Slack", "true / false"),
    ("notify.telegram.enabled", "Bật thông báo Telegram", "true / false"),
    ("retention.soft_delete_days", "Giữ thùng rác (ngày)", "vd 30"),
)
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; font-src 'self'; "
       "frame-ancestors 'none'; form-action 'self'; base-uri 'none'; object-src 'none'")
SECURITY_HEADERS = [
    (b"content-security-policy", CSP.encode()),
    (b"x-frame-options", b"DENY"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"same-origin"),  # no-referrer làm Chrome gửi `Origin: null` cho form POST
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
]


# ============================================================================ trạng thái + middleware
@dataclass
class WebState:
    paths: HubPaths
    auth: WebAuth
    host: str
    port: int
    client: AsyncHubClient
    templates: Jinja2Templates
    name: str = WEB_HOST  # host công khai (Host header + cookie host-only) — khác bind host (WT-1)

    @property
    def hosts(self) -> set[str]:
        return allowed_hosts(self.name, self.port)

    @property
    def base_url(self) -> str:
        n = f"[{self.name}]" if ":" in self.name and not self.name.startswith("[") else self.name
        return f"http://{n}:{self.port}"

    @property
    def origins(self) -> set[str]:
        return {f"http://{h}" for h in self.hosts}


class GuardMiddleware:
    """ASGI thuần (không đệm response → SSE chạy được): kiểm Host, gắn phiên, chặn khi chưa đăng nhập, thêm header bảo mật."""

    PUBLIC = ("/login", "/favicon.ico")

    def __init__(self, app: Any, state: WebState) -> None:
        self.app = app
        self.state = state

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
        path = scope.get("path") or "/"

        async def send_headers(msg: dict) -> None:
            if msg["type"] == "http.response.start":
                h = list(msg.get("headers") or [])
                names = {k.lower() for k, _ in h}
                h += [(k, v) for k, v in SECURITY_HEADERS if k not in names]
                if not path.startswith("/static/") and b"cache-control" not in names:
                    h.append((b"cache-control", b"no-store"))
                msg = {**msg, "headers": h}
            await send(msg)

        host = headers.get("host", "").lower()
        if host not in self.state.hosts:
            hn, _, hp = host.rpartition(":")
            if scope.get("method") == "GET" and hp == str(self.state.port) and is_loopback(hn):
                # 127.0.0.1/localhost:port → chuyển sang host riêng (không bao giờ đặt cookie trên 127.0.0.1 — WT-1)
                qs = scope.get("query_string") or b""
                loc = self.state.base_url + path + (("?" + qs.decode("latin-1")) if qs else "")
                await RedirectResponse(loc, status_code=303)(scope, receive, send_headers)
                return
            # chống DNS rebinding: chỉ phục vụ đúng Host đã định
            await PlainTextResponse(f"Host không hợp lệ — mở {self.state.base_url}/", status_code=421)(scope, receive, send_headers)
            return
        sid = cookie_parser(headers.get("cookie", "")).get(COOKIE)
        # polling/SSE không gia hạn phiên — chỉ thao tác của người (trang, POST) mới gia hạn
        sess = self.state.auth.get(sid, touch=not path.startswith(("/sse/", "/partials/")))
        scope.setdefault("state", {})["session"] = sess
        public = path in self.PUBLIC or path.startswith("/static/")
        if sess is None and not public:
            if scope.get("method") == "GET" and not path.startswith(("/sse/", "/partials/")) and "hx-request" not in headers:
                resp: Response = RedirectResponse("/login", status_code=303)
            else:
                resp = PlainTextResponse("Chưa đăng nhập — chạy `cfharness web --open` trong terminal.", status_code=401)
            await resp(scope, receive, send_headers)
            return
        await self.app(scope, receive, send_headers)


def with_wait(rows: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """approval.list không tính thời gian đã chờ (chỉ tui.snapshot có) → tính từ opened_at."""
    import time as _t

    out = []
    for a in rows or []:
        if a.get("wait_s") is None:
            t0 = P.parse_ts(a.get("opened_at"))
            a["wait_s"] = round(_t.time() - t0, 1) if t0 else None
        out.append(a)
    return out


def ws(request: Request) -> WebState:
    return request.app.state.web


def session_of(request: Request) -> Session | None:
    return request.scope.get("state", {}).get("session")


_NEXT_RE = re.compile(r"^/(?![/\\])[A-Za-z0-9/_\-.~?=&%#+,:]*$")


def safe_next(v: Any, default: str = "/") -> str:
    """Chỉ đường dẫn nội bộ: bắt đầu `/`, không `//`, `/\\`, không ký tự điều khiển/khoảng trắng (WT-7)."""
    s = str(v or "")
    if any(ord(ch) < 0x21 or ord(ch) == 0x7F for ch in s) or not _NEXT_RE.match(s):
        return default
    return s


async def post_guard(request: Request, *, need_key: bool = True) -> tuple[Session, FormData]:
    """CSRF + cùng nguồn + bí mật phiên NGOÀI cookie (WT-1) cho mọi POST đã đăng nhập.

    `k` (form) / `X-Ahub-Key` (header) = `Session.key`: chỉ trao cho trình duyệt MỘT lần qua fragment `#k=` lúc đăng nhập, JS giữ trong
    localStorage của origin web và gắn vào mọi form POST. HTML không bao giờ chứa nó → kẻ có cookie (vd dev server ở cổng khác cùng
    host nhận cookie) đọc HTML lấy CSRF vẫn không POST được."""
    st = ws(request)
    sess = session_of(request)
    if sess is None:
        raise HTTPException(401, "Chưa đăng nhập")
    origin = request.headers.get("origin")
    if origin and origin not in st.origins:
        raise HTTPException(403, "Origin không hợp lệ")
    if not origin:
        ref = request.headers.get("referer")
        if ref and urlsplit(ref).netloc.lower() not in st.hosts:
            raise HTTPException(403, "Referer không hợp lệ")
    sfs = request.headers.get("sec-fetch-site")
    if sfs and sfs not in ("same-origin", "none"):
        raise HTTPException(403, "Yêu cầu khác nguồn bị từ chối")
    form = await request.form()
    if not WebAuth.csrf_ok(sess, form.get("csrf") or request.headers.get("x-csrf-token")):
        raise HTTPException(403, "Token CSRF sai hoặc thiếu — tải lại trang rồi thử lại")
    if need_key and not WebAuth.key_ok(sess, form.get(KEY_FIELD) or request.headers.get(KEY_HEADER)):
        raise HTTPException(403, "Trình duyệt này thiếu khoá phiên (cần JavaScript; hoặc phiên mở ở tab/trình duyệt khác) — "
                                 "chạy `cfharness web --open` để đăng nhập lại")
    return sess, form


# ============================================================================ RPC
_UNAUDITED_HUMAN = None


def _needs_extra_audit(method: str) -> bool:
    """Method chỉ-người mà daemon KHÔNG tự ghi audit theo `_audit` (approval.* tự ghi theo `channel`)."""
    global _UNAUDITED_HUMAN
    if _UNAUDITED_HUMAN is None:
        _UNAUDITED_HUMAN = frozenset(m for m in _auth.HUMAN_ONLY - _auth.all_mutating() if not m.startswith("approval.") and m != "agent.purge")
    return method in _UNAUDITED_HUMAN


async def rpc(request: Request, method: str, params: dict[str, Any] | None = None, *, audit: bool = False) -> Any:
    st = ws(request)
    res = await st.client.call(method, params or {}, audit=audit)
    if audit and _needs_extra_audit(method):
        try:
            await st.client.call("audit.record", {"kind": method, "actor": "user", "channel": CHANNEL, "summary": method,
                                                  "target": str((params or {}).get("id") or (params or {}).get("name") or "")})
        except Exception:  # pragma: no cover
            log.exception("không ghi được audit web cho %s", method)
    return res


def err_text(e: Exception) -> str:
    if isinstance(e, RpcError):
        msg = e.message
        fields = (e.data or {}).get("fields") if isinstance(e.data, dict) else None
        if fields:
            msg += " — " + "; ".join(f"{f.get('field')}: {f.get('error')}" for f in fields if isinstance(f, dict))
        if e.code == BLOCKED:
            msg = "Bị chặn: " + msg
        return msg
    if isinstance(e, DaemonUnavailable):
        return "Daemon cfharness không chạy — chạy `cfharness daemon start` rồi tải lại trang."
    return str(e)


async def render(request: Request, name: str, ctx: dict[str, Any] | None = None, status: int = 200) -> Response:
    st = ws(request)
    sess = session_of(request)
    badge: dict[str, Any] = {}
    if sess is not None:
        try:
            pend = await rpc(request, "approval.list", {"limit": 500})
            es = await rpc(request, "estop.get")
            badge = {"approvals": len(pend or []), "waiting_human": sum(1 for a in pend or [] if a.get("status") == "waiting_human"),
                     "estop": bool((es or {}).get("on")), "questions": len(await questions(request, "open"))}
        except (RpcError, DaemonUnavailable, OSError):
            badge = {"down": True}
    base = {"csrf": sess.csrf if sess else "", "flashes": sess.pop_flashes() if sess else [], "badge": badge, "nav": "", "htmx": HTMX,
            "path": request.url.path}
    return st.templates.TemplateResponse(request, name, {**base, **(ctx or {})}, status_code=status)


def flash_redirect(request: Request, url: str, kind: str | None = None, text: str | None = None) -> RedirectResponse:
    sess = session_of(request)
    if sess is not None and kind and text:
        sess.flash(kind, text)
    return RedirectResponse(url, status_code=303)


# ============================================================================ đăng nhập
def _token_from(v: str) -> str:
    v = (v or "").strip()
    if "t=" in v:
        q = parse_qs(urlsplit(v).query or v.split("?", 1)[-1])
        v = (q.get("t") or [""])[0]
    return v


def _login_ok(request: Request, sess: Session) -> Response:
    # khoá phiên đi trong FRAGMENT (không gửi lên server, không vào log/Referer); app.js lưu vào localStorage rồi xoá khỏi URL
    resp = RedirectResponse(f"/#{KEY_FIELD}={sess.key}", status_code=303)
    resp.set_cookie(COOKIE, sess.sid, httponly=True, samesite="strict", path="/", secure=False)
    return resp


async def login(request: Request) -> Response:
    st = ws(request)
    err = None
    if request.method == "POST":
        origin = request.headers.get("origin")
        if (origin and origin not in st.origins) or (request.headers.get("sec-fetch-site") not in (None, "same-origin", "none")):
            raise HTTPException(403, "Origin không hợp lệ")
        form = await request.form()
        tok = _token_from(str(form.get("link") or ""))
    else:
        tok = request.query_params.get("t") or ""
        if not tok and session_of(request) is not None:
            return RedirectResponse("/", status_code=303)
    if tok:
        sess = st.auth.redeem(tok)
        if sess is not None:
            return _login_ok(request, sess)
        err = "Link đăng nhập không hợp lệ, đã dùng rồi hoặc đã hết hạn. Chạy lại `cfharness web --open` để lấy link mới."
    return st.templates.TemplateResponse(request, "login.html", {"error": err, "htmx": HTMX, "badge": {}, "csrf": "", "flashes": [], "nav": ""},
                                         status_code=401 if err or request.method == "POST" else 200)


async def logout(request: Request) -> Response:
    await post_guard(request, need_key=False)
    ws(request).auth.logout(request.cookies.get(COOKIE))
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE, path="/")
    return resp


# ============================================================================ câu hỏi (UX)
async def questions(request: Request, status: str = "pending", **flt: Any) -> list[dict[str, Any]]:
    """`question.list` — rỗng nếu daemon chưa có `question.*` (bản cũ)."""
    try:
        return await rpc(request, "question.list", {"status": status, "limit": 200, **{k: v for k, v in flt.items() if v}}) or []
    except RpcError:
        return []


# ============================================================================ trang
def _sort_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda r: (STATUS_ORDER.get(r.get("status") or "", 9), r.get("team") or "~", r["id"]))


async def overview_data(request: Request) -> dict[str, Any]:
    snap = await rpc(request, "tui.snapshot")
    qs = await questions(request, "pending")
    asking = {q["agent_id"] for q in qs}
    rows = snap.get("agents") or []
    for r in rows:  # agent chờ câu trả lời (task bị chặn) hiện "Chờ bạn trả lời" thay vì "Rảnh"
        if r["id"] in asking and r.get("status") in ("idle", "error"):
            r["status"] = "asking"
    rows = _sort_rows(rows)
    appr = sorted(snap.get("approvals") or [], key=lambda a: a.get("opened_at") or "")
    return {"hub": snap.get("hub") or {}, "rows": rows, "approvals": appr, "questions": [q for q in qs if q["status"] == "open"],
            "questions_mgr": [q for q in qs if q["status"] == "with_manager"]}


async def overview(request: Request) -> Response:
    data = await overview_data(request)
    agents = [r["id"] for r in data["rows"]]
    from .manager import _web_convs, mgr_state

    try:
        mgr = await mgr_state(request)
        if mgr.get("agent_id"):
            mgr["web_conv"] = next((c["id"] for c in await _web_convs(request)), None)
    except RpcError:
        mgr = {"available": False, "agent_id": None}
    return await render(request, "overview.html", {**data, "nav": "overview", "agents": agents, "mgr": mgr})


async def partial_overview(request: Request) -> Response:
    data = await overview_data(request)
    sess = session_of(request)
    return ws(request).templates.TemplateResponse(request, "_overview.html", {**data, "csrf": sess.csrf if sess else ""})


async def agents_page(request: Request) -> Response:
    rows = await rpc(request, "agent.list", {})
    snap = await rpc(request, "tui.snapshot")
    live = {r["id"]: r for r in snap.get("agents") or []}
    trash = [t for t in (await rpc(request, "trash.list") or []) if t.get("kind") in (None, "agent")]
    return await render(request, "agents.html", {"nav": "agents", "rows": rows, "live": live, "trash": trash})


async def _agent_form_ctx(request: Request, agent: dict[str, Any] | None, values: dict[str, Any] | None = None, error: str | None = None) -> dict[str, Any]:
    templates_ = await rpc(request, "template.list")
    mcp = [m for m in (await rpc(request, "mcp.list") or [])]
    v: dict[str, Any] = values or {}
    if agent and not values:
        tp = agent.get("tool_policy") or {}
        lim = agent.get("limits") or {}
        v = {
            "id": agent["id"], "name": agent.get("name"), "role": agent.get("role"), "description": agent.get("description"),
            "model": agent.get("model"), "effort": agent.get("effort") or "", "permission_mode": tp.get("permission_mode") or "",
            "tools": ", ".join(tp.get("tools") or []), "mcp": list(agent.get("mcp_servers") or []),
            "max_budget_usd_per_run": lim.get("max_budget_usd_per_run") or "", "budget_usd_per_day": lim.get("budget_usd_per_day") or "",
            "max_runtime_s": lim.get("max_runtime_s") or "", "max_concurrent_runs": lim.get("max_concurrent_runs") or 1,
            "workspace_default": agent.get("workspace_default") or "", "soul": agent.get("soul") or "",
        }
    v.setdefault("mcp", [])
    return {"nav": "agents", "agent": agent, "templates": templates_, "mcp": mcp, "v": v, "error": error, "models": MODELS, "efforts": EFFORTS,
            "modes": PERMISSION_MODES}


async def agent_new(request: Request) -> Response:
    ctx = await _agent_form_ctx(request, None, {"template": request.query_params.get("template") or "researcher", "mcp": []})
    return await render(request, "agent_form.html", ctx)


async def agent_edit(request: Request) -> Response:
    agent = await rpc(request, "agent.get", {"id": request.path_params["id"]})
    return await render(request, "agent_form.html", await _agent_form_ctx(request, agent))


def _num(v: Any, kind: type, label: str) -> Any:
    s = str(v or "").strip().replace(",", ".")
    if not s:
        return None
    try:
        return kind(float(s)) if kind is int else kind(s)
    except ValueError:
        raise ValueError(f"{label}: '{s}' không phải số") from None


def agent_changes(form: FormData, *, creating: bool, from_template: bool) -> dict[str, Any]:
    """Form → `changes` cho agent.update / overrides cho agent.create_from_template. Ô trống khi tạo từ template = theo template."""
    sparse = creating and from_template
    ch: dict[str, Any] = {}
    for k in ("name", "role", "description", "model", "workspace_default"):
        val = str(form.get(k) or "").strip()
        if val or not sparse:
            if k == "workspace_default":
                ch[k] = val or None
            elif k == "model":
                if val:
                    ch[k] = val
            elif k == "name" and not val:
                continue
            else:
                ch[k] = val
    eff = str(form.get("effort") or "").strip()
    if eff or not sparse:
        ch["effort"] = eff or None
    tp: dict[str, Any] = {}
    pm = str(form.get("permission_mode") or "").strip()
    if pm and pm not in PERMISSION_MODES:
        raise ValueError(f"chế độ quyền '{pm}' không được phép từ web (chỉ {', '.join(PERMISSION_MODES)})")
    if pm or not sparse:
        tp["permission_mode"] = pm or None
    tools = [t.strip() for t in re.split(r"[,\s]+", str(form.get("tools") or "")) if t.strip()]
    if tools or not sparse:
        tp["tools"] = tools or None
    if tp:
        ch["tool_policy"] = tp
    ch["mcp_servers"] = [str(x) for x in form.getlist("mcp") if str(x).strip()]
    lim: dict[str, Any] = {}
    for k, kind, label in (("max_budget_usd_per_run", float, "Ngân sách mỗi run"), ("budget_usd_per_day", float, "Ngân sách mỗi ngày"),
                           ("max_runtime_s", int, "Thời gian chạy tối đa"), ("max_concurrent_runs", int, "Số run đồng thời")):
        val = _num(form.get(k), kind, label)
        if val is not None or (not sparse and k != "max_concurrent_runs"):
            lim[k] = val
    if lim:
        ch["limits"] = lim
    return ch


async def agent_create(request: Request) -> Response:
    _, form = await post_guard(request)
    aid = str(form.get("id") or "").strip()
    tpl = str(form.get("template") or "").strip()
    values = {k: form.get(k) for k in form.keys()}
    values["mcp"] = form.getlist("mcp")
    try:
        ch = agent_changes(form, creating=True, from_template=bool(tpl))
        soul = str(form.get("soul") or "").strip() or None
        if form.get("confirm") != "yes":
            tp = ch.get("tool_policy") or {}
            msg = (f"Tạo agent {aid or '?'}" + (f" từ mẫu {tpl}" if tpl else "") + "?"
                   + f"\nChế độ quyền: {tp.get('permission_mode') or '(theo mẫu)'} · Tool: {', '.join(tp.get('tools') or []) or '(theo mẫu)'}"
                   + f"\nMCP: {', '.join(ch.get('mcp_servers') or []) or '(không)'}")
            return await render(request, "confirm.html", {"message": msg, "fields": carry(form), "action": "/agents/create", "danger": False,
                                                          "back": "/agents/new", "note": None})
        if tpl:
            await rpc(request, "agent.create_from_template", {"id": aid, "template": tpl, "overrides": ch, **({"soul": soul} if soul else {})}, audit=True)
        else:
            await rpc(request, "agent.create", {"config": {"id": aid, **ch}, "soul": soul}, audit=True)
    except (RpcError, ValueError) as e:
        return await render(request, "agent_form.html", await _agent_form_ctx(request, None, values, err_text(e)), status=400)
    return flash_redirect(request, f"/agents/{aid}", "ok", f"Đã tạo agent {aid}" + (f" từ template {tpl}" if tpl else ""))


async def agent_update(request: Request) -> Response:
    _, form = await post_guard(request)
    aid = request.path_params["id"]
    values = {k: form.get(k) for k in form.keys()}
    values["mcp"] = form.getlist("mcp")
    try:
        cur = await rpc(request, "agent.get", {"id": aid})
        ch = agent_changes(form, creating=False, from_template=False)
        tp_old = cur.get("tool_policy") or {}
        tp_new = ch.get("tool_policy") or {}
        risky = []
        if (tp_new.get("permission_mode") or None) != (tp_old.get("permission_mode") or None):
            risky.append(f"chế độ quyền: {tp_old.get('permission_mode') or 'mặc định'} → {tp_new.get('permission_mode') or 'mặc định'}")
        if (tp_new.get("tools") or None) != (tp_old.get("tools") or None):
            risky.append(f"tool: {', '.join(tp_old.get('tools') or []) or 'mặc định'} → {', '.join(tp_new.get('tools') or []) or 'mặc định'}")
        if sorted(ch.get("mcp_servers") or []) != sorted(cur.get("mcp_servers") or []):
            risky.append(f"MCP: {', '.join(cur.get('mcp_servers') or []) or '(không)'} → {', '.join(ch.get('mcp_servers') or []) or '(không)'}")
        if risky and form.get("confirm") != "yes":
            return await render(request, "confirm.html", {"message": f"Đổi quyền của agent {aid}?\n" + "\n".join(risky), "fields": carry(form),
                                                          "action": f"/agents/{aid}/save", "danger": True, "back": f"/agents/{aid}/edit", "note": None})
        soul = str(form.get("soul") or "")
        params: dict[str, Any] = {"id": aid, "changes": ch}
        if soul.strip() and soul.replace("\r\n", "\n").strip() != (cur.get("soul") or "").strip():
            params["soul"] = soul.replace("\r\n", "\n")
        res = await rpc(request, "agent.update", params, audit=True)
    except (RpcError, ValueError) as e:
        try:
            cur = await rpc(request, "agent.get", {"id": aid})
        except RpcError:
            cur = {"id": aid}
        return await render(request, "agent_form.html", await _agent_form_ctx(request, cur, values, err_text(e)), status=400)
    return flash_redirect(request, f"/agents/{aid}", "ok", f"Đã lưu agent {aid} (revision {res.get('config_revision')})")


async def agent_page(request: Request) -> Response:
    aid = request.path_params["id"]
    agent = await rpc(request, "agent.get", {"id": aid})
    runs = await rpc(request, "run.list", {"agent": aid, "limit": 25})
    revs = await rpc(request, "agent.revisions", {"id": aid})
    pend = with_wait(await rpc(request, "approval.list", {"agent_id": aid}))
    try:
        sandbox = await rpc(request, "guard.sandbox.get", {"id": aid}) or {}
    except RpcError:
        sandbox = {}
    try:
        pol = (await rpc(request, "learn.policy_get", {}) or {}).get("agents") or {}
        learn_on = (pol.get(aid) or {}).get("enabled") if isinstance(pol.get(aid), dict) else None
    except RpcError:
        learn_on = None
    snap = await rpc(request, "tui.snapshot")
    live = next((r for r in snap.get("agents") or [] if r["id"] == aid), None)
    return await render(request, "agent.html", {"nav": "agents", "a": agent, "runs": runs, "revs": list(reversed(revs or []))[:15], "pend": pend,
                                                "learn_on": learn_on, "live": live, "sandbox": sandbox, "titles": await task_titles(request, runs)})


async def runs_page(request: Request) -> Response:
    q = request.query_params
    params: dict[str, Any] = {"limit": int(q.get("limit") or 100)}
    if q.get("agent"):
        params["agent"] = q["agent"]
    if q.get("status"):
        params["status"] = q["status"]
    runs = await rpc(request, "run.list", params)
    agents = [a["id"] for a in await rpc(request, "agent.list", {})]
    return await render(request, "runs.html", {"nav": "runs", "runs": runs, "agents": agents, "q": dict(q), "statuses": P.RUN_STATUS,
                                               "titles": await task_titles(request, runs)})


async def task_titles(request: Request, runs: list[dict[str, Any]]) -> dict[str, str]:
    """task_id → tiêu đề (để danh sách run hiện tiêu đề người đọc được thay vì prompt thô của hub)."""
    if not any(r.get("task_id") for r in runs or []):
        return {}
    try:
        return {t["id"]: t.get("title") or "" for t in await rpc(request, "task.list", {"limit": 1000}) or []}
    except RpcError:
        return {}


def replay_items(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Phát lại từ DB: gộp tool.start + tool.complete cùng id thành một mục (như TUI)."""
    out: list[dict[str, Any]] = []
    idx: dict[str, int] = {}
    for ev in sorted(events, key=lambda e: (e.get("ts") or "", int(e.get("seq") or 0))):
        it = P.transcript_item(ev)
        if it is None:
            continue
        dom = it.get("dom")
        if dom and dom in idx:
            prev = out[idx[dom]]
            it["diff"] = prev.get("diff") or it.get("diff")
            it["summary"] = it.get("summary") or prev.get("summary")
            out[idx[dom]] = it
            continue
        if dom:
            idx[dom] = len(out)
        out.append(it)
    return out


async def _run_ctx(request: Request, rid: str) -> dict[str, Any]:
    run = await rpc(request, "run.get", {"id": rid})
    pend = with_wait(await rpc(request, "approval.list", {"run_id": run["id"]}))
    if run.get("task_id"):
        try:
            run["task_title"] = (await rpc(request, "task.get", {"id": run["task_id"]}) or {}).get("title")
        except RpcError:
            pass
    tokens = P.tokens_of(run.get("usage"))
    live = None
    if P.is_live_run(run):
        snap = await rpc(request, "tui.snapshot")
        live = next((r for r in snap.get("agents") or [] if r["id"] == run["agent_id"]), None)
        if live and live.get("current_run_id") == run["id"]:
            tokens = live.get("run_tokens") or tokens
    return {"run": run, "pend": pend, "tokens": tokens, "live": live}


async def run_page(request: Request) -> Response:
    rid = request.path_params["id"]
    run = await rpc(request, "run.get", {"id": rid, "events": True})
    events = run.pop("events", []) or []
    status = await rpc(request, "hub.status")
    epoch = status["epoch"]
    since = max([int(e.get("seq") or 0) for e in events if e.get("epoch") == epoch] or [0])
    ctx = await _run_ctx(request, run["id"])
    children = [r for r in (await rpc(request, "run.list", {"agent": run["agent_id"], "limit": 50}) or []) if r.get("parent_run_id") == run["id"]]
    qs = await questions(request, "all", run_id=run["id"])
    return await render(request, "run.html", {**ctx, "nav": "runs", "items": replay_items(events), "since": since, "epoch": epoch, "children": children,
                                              "run_questions": [q for q in qs if q["status"] in ("open", "with_manager")]})


async def partial_run(request: Request) -> Response:
    ctx = await _run_ctx(request, request.path_params["id"])
    sess = session_of(request)
    return ws(request).templates.TemplateResponse(request, "_run_bar.html", {**ctx, "csrf": sess.csrf if sess else ""})


async def inbox_page(request: Request) -> Response:
    """"Cần bạn": yêu cầu duyệt + câu hỏi chờ bạn + câu hỏi đang ở điều phối viên + task bị chặn không có câu hỏi."""
    pend = sorted(with_wait(await rpc(request, "approval.list", {"limit": 500})), key=lambda a: a.get("opened_at") or "")
    qs = await questions(request, "pending")
    try:
        blocked = await rpc(request, "task.list", {"status": "blocked", "limit": 100}) or []
    except RpcError:
        blocked = []
    asked = {q.get("task_id") for q in qs}
    blocked = [t for t in blocked if t.get("status") == "blocked" and t["id"] not in asked]
    recent = [q for q in await questions(request, "answered") if q.get("answered_at")][:8]
    return await render(request, "inbox.html", {"nav": "inbox", "pend": pend, "q_open": [q for q in qs if q["status"] == "open"],
                                                 "q_mgr": [q for q in qs if q["status"] == "with_manager"], "blocked": blocked, "recent": recent})


async def approvals_page(request: Request) -> Response:
    pend = with_wait(await rpc(request, "approval.list", {"limit": 500}))
    hist = [a for a in await rpc(request, "approval.list", {"status": "all", "limit": 60}) if a.get("status") not in ("open", "waiting_human")]
    return await render(request, "approvals.html", {"nav": "approvals", "pend": sorted(pend, key=lambda a: a.get("opened_at") or ""), "hist": hist})


async def tasks_page(request: Request) -> Response:
    q = request.query_params
    params: dict[str, Any] = {"limit": 500}
    if q.get("team"):
        params["team"] = q["team"]
    tasks = await rpc(request, "task.list", params)
    asking = {q.get("task_id"): q for q in await questions(request, "pending") if q.get("task_id")}
    for t in tasks:
        t["question"] = asking.get(t["id"])
    cols = {c: [t for t in tasks if t.get("status") == c] for c in P.TASK_COLUMNS}
    if not q.get("all"):
        cols["done"] = cols["done"][:15]
    teams = await rpc(request, "team.list", {})
    agents = [a["id"] for a in await rpc(request, "agent.list", {})]
    open_tasks = [t for t in tasks if t.get("status") not in ("done", "archived")]
    return await render(request, "tasks.html", {"nav": "tasks", "cols": cols, "teams": teams, "agents": agents, "open_tasks": open_tasks, "q": dict(q)})


async def task_page(request: Request) -> Response:
    t = await rpc(request, "task.get", {"id": request.path_params["id"]})
    qs = await questions(request, "all", task_id=t["id"])
    return await render(request, "task.html", {"nav": "tasks", "t": t, "task_questions": [q for q in qs if q["status"] in ("open", "with_manager")]})


async def teams_page(request: Request) -> Response:
    teams = await rpc(request, "team.list", {"include_archived": True})
    agents = [a["id"] for a in await rpc(request, "agent.list", {})]
    return await render(request, "teams.html", {"nav": "teams", "teams": teams, "agents": agents})


async def learnings_page(request: Request) -> Response:
    q = request.query_params
    feed_types = ("learning.applied", "learning.rolled_back", "learning.eval_failed", "learning.deferred", "guard.blocked", "learning.canary", "learning.promoted")
    feed = [x for x in await rpc(request, "learn.feed", {"days": int(q.get("days") or 7), "limit": 300, **({"agent": q["agent"]} if q.get("agent") else {})})
            if x.get("type") in feed_types]
    ledger = await rpc(request, "learn.ledger", {"limit": 60, **({"agent": q["agent"]} if q.get("agent") else {})})
    golden = await rpc(request, "golden.list", {"all": True})
    digest = await rpc(request, "digest.show", {})
    agents = await rpc(request, "agent.list", {})
    policy = await rpc(request, "learn.policy_get", {})
    agent_pol = (policy or {}).get("agents") or {}
    learn = [{"id": a["id"], "enabled": (agent_pol.get(a["id"]) or {}).get("enabled", True) if isinstance(agent_pol.get(a["id"]), dict) else True} for a in agents]
    return await render(request, "learnings.html", {"nav": "learnings", "feed": feed, "ledger": ledger, "golden": (golden or {}).get("tasks") or [],
                                                    "digest": digest or {}, "learn": learn, "policy": policy or {}, "q": dict(q), "agents": [a["id"] for a in agents]})


async def ledger_entry(request: Request) -> Response:
    e = await rpc(request, "learn.show", {"id": request.path_params["id"]})
    return await render(request, "ledger.html", {"nav": "learnings", "e": e})


async def mcp_page(request: Request) -> Response:
    servers = await rpc(request, "mcp.list", {"deleted": True})
    agents = await rpc(request, "agent.list", {})
    used: dict[str, list[str]] = {}
    for a in agents:
        for s in a.get("mcp_servers") or []:
            used.setdefault(s, []).append(a["id"])
    return await render(request, "mcp.html", {"nav": "mcp", "servers": servers, "used": used})


async def mcp_show(request: Request) -> Response:
    s = await rpc(request, "mcp.show", {"name": request.path_params["name"]})
    return await render(request, "mcp_show.html", {"nav": "mcp", "s": s})


async def costs_page(request: Request) -> Response:
    q = request.query_params
    days = int(q.get("days") or 7)
    from datetime import datetime, timedelta, timezone

    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    by_agent = await rpc(request, "report.cost", {"by": "agent", "since": since})
    by_day = await rpc(request, "report.cost", {"by": "day", "since": since})
    by_team = await rpc(request, "report.cost", {"by": "team", "since": since})
    rep = await rpc(request, "report.get", {"since": since})
    snap = await rpc(request, "tui.snapshot")
    return await render(request, "costs.html", {"nav": "costs", "by_agent": by_agent, "by_day": by_day, "by_team": by_team, "rep": rep, "days": days,
                                                "hub": snap.get("hub") or {}})


async def audit_page(request: Request) -> Response:
    q = request.query_params
    params: dict[str, Any] = {"limit": int(q.get("limit") or 200)}
    for k in ("channel", "agent_id", "kind"):
        if q.get(k):
            params[k] = q[k]
    rows = await rpc(request, "audit.list", params)
    return await render(request, "audit.html", {"nav": "audit", "rows": rows, "q": dict(q)})


async def settings_page(request: Request) -> Response:
    es = await rpc(request, "estop.get")
    vals = []
    for key, label, hint in SAFE_CONFIG:
        try:
            v = await rpc(request, "config.get", {"key": key})
        except RpcError:
            v = None
        vals.append({"key": key, "label": label, "hint": hint, "value": "" if v is None else (json.dumps(v) if isinstance(v, bool) else v)})
    secrets_ = await rpc(request, "secret.list")
    names = {s.get("name") for s in secrets_ or []}
    tg_chats = []
    try:
        tg_chats = await rpc(request, "config.get", {"key": "notify.telegram.allowed_chat_ids"}) or []
    except RpcError:
        pass
    integ = await rpc(request, "guard.integrity.status")
    status = await rpc(request, "hub.status")
    st = ws(request)
    return await render(request, "settings.html", {"nav": "settings", "estop": es, "cfg": vals, "secrets": secrets_, "slack": "SLACK_WEBHOOK_URL" in names or "SLACK_BOT_TOKEN" in names,
                                                   "telegram": "TELEGRAM_BOT_TOKEN" in names, "tg_chats": tg_chats, "integ": integ, "status": status,
                                                   "web": {"host": st.host, "port": st.port, "sessions": len(st.auth.sessions)}})


# ============================================================================ thao tác (POST /do/<tên>)
def _req(form: FormData, key: str, label: str | None = None) -> str:
    v = str(form.get(key) or "").strip()
    if not v:
        raise ValueError(f"thiếu {label or key}")
    return v


def _opt_float(form: FormData, key: str, label: str) -> float | None:
    return _num(form.get(key), float, label)


@dataclass
class Action:
    method: str
    build: Callable[[FormData], dict[str, Any]]
    ok: Callable[[dict[str, Any], FormData], str] | str
    confirm: Callable[[FormData], str] | str | None = None
    danger: bool = False
    audit: bool = True
    goto: Callable[[Any, FormData], str | None] | None = None
    note: str | None = None  # nhãn ô ghi chú trong hộp xác nhận (vd lý do từ chối)


def _parse_cfg_value(raw: str) -> Any:
    from ..config import parse_value

    raw = (raw or "").strip()
    return None if raw == "" else parse_value(raw)


def _run_goto(res: Any, form: FormData) -> str | None:
    rid = (res or {}).get("id") or (res or {}).get("run_id")
    return f"/runs/{rid}" if rid else None


ACTIONS: dict[str, Action] = {
    # ---- duyệt thao tác phá huỷ (FR-UI-04) — approval.* tự ghi audit theo `channel`
    "approve": Action("approval.approve", lambda f: {"id": _req(f, "id"), "channel": CHANNEL, "by": "user", "note": f.get("note") or None},
                      "Đã cho phép thao tác", confirm=lambda f: f"Cho phép agent thực hiện thao tác phá huỷ này MỘT lần?\n{f.get('preview') or ''}", audit=False),
    "approve_fp": Action("approval.approve", lambda f: {"id": _req(f, "id"), "channel": CHANNEL, "by": "user", "false_positive": True, "note": "báo nhầm (web)"},
                         "Đã cho phép và đánh dấu báo nhầm", confirm="Cho phép và đánh dấu đây là BÁO NHẦM của bộ phân loại?", audit=False),
    # "luôn cho phép" (LLM-2): luật hẹp do daemon dựng (`always_prepare`); LUÔN qua trang bước 2 nói rõ luật (dù không nặng)
    "approve_always": Action("approval.approve", lambda f: {"id": _req(f, "id"), "channel": CHANNEL, "by": "user", "always": True,
                                                            "note": "luôn cho phép (web)"},
                             lambda r, f: "Đã cho phép và tạo luật luôn cho phép " + str(((r or {}).get("always_rule") or {}).get("id") or ""),
                             confirm="Cho phép thao tác này VÀ tạo luật luôn cho phép (không hỏi lại thao tác y hệt)?", audit=False),
    "deny": Action("approval.deny", lambda f: {"id": _req(f, "id"), "channel": CHANNEL, "by": "user", "note": f.get("note") or None},
                   "Đã từ chối thao tác", confirm=lambda f: f"Từ chối thao tác này?\n{f.get('preview') or ''}", danger=True, audit=False,
                   note="Ghi chú cho agent (tuỳ chọn)"),
    # ---- câu hỏi của agent (UX) — question.* HUMAN_ONLY ở daemon, tự audit theo `_audit.channel=web`
    "question_answer": Action("question.answer", lambda f: _answer_params(f), "Đã gửi câu trả lời — agent sẽ làm tiếp"),
    "question_take": Action("question.escalate", lambda f: {"id": _req(f, "id"), "note": "người lấy về tự trả lời (web)", "channel": CHANNEL},
                            "Câu hỏi đã chuyển về bạn — trả lời ngay bên dưới"),
    "question_cancel": Action("question.cancel", lambda f: {"id": _req(f, "id"), "reason": "người huỷ từ web", "channel": CHANNEL}, "Đã huỷ câu hỏi",
                              confirm="Huỷ câu hỏi này? Agent sẽ không nhận được câu trả lời.", danger=True),
    # ---- run
    "run_start": Action("run.start", lambda f: {"agent": _req(f, "agent", "agent"), "prompt": _req(f, "prompt", "nội dung công việc"),
                                                **({"cwd": str(f.get("cwd")).strip()} if str(f.get("cwd") or "").strip() else {}),
                                                **({"budget_usd": b} if (b := _opt_float(f, "budget_usd", "Ngân sách")) is not None else {})},
                        lambda r, f: f"Đã giao việc cho {f.get('agent')} — run {r.get('id')}", goto=_run_goto),
    "run_continue": Action("run.continue", lambda f: {"id": _req(f, "id"), "prompt": _req(f, "prompt", "tin nhắn")},
                           lambda r, f: f"Đã gửi tiếp — run mới {r.get('id')}", goto=_run_goto),
    "run_resume": Action("run.resume", lambda f: {"id": _req(f, "id")}, lambda r, f: f"Đã tiếp tục — run mới {r.get('id')}", goto=_run_goto),
    "steer": Action("run.steer", lambda f: {"id": _req(f, "id"), "text": _req(f, "text", "nội dung chỉ đạo"), "channel": CHANNEL, "by": "user"},
                    lambda r, f: f"Đã gửi chỉ đạo (lần {r.get('steer_count')})"),
    "stop": Action("run.stop", lambda f: {"id": _req(f, "id")}, "Đã yêu cầu dừng run", confirm=lambda f: f"Dừng run {f.get('id')}? Agent sẽ ngừng việc đang làm.",
                   danger=True),
    "feedback": Action("feedback.record", lambda f: {"run_id": _req(f, "run_id"), "kind": _req(f, "kind"), "note": f.get("note") or None, "source": CHANNEL},
                       "Đã ghi nhận phản hồi — agent sẽ học từ đây"),
    # ---- agent
    "agent_pause": Action("agent.pause", lambda f: {"id": _req(f, "id")}, "Đã tạm dừng agent (không nhận việc mới)",
                          confirm=lambda f: f"Tạm dừng agent {f.get('id')}?"),
    "agent_resume": Action("agent.resume", lambda f: {"id": _req(f, "id")}, "Agent đã hoạt động lại",
                           confirm=lambda f: f"Cho agent {f.get('id')} hoạt động lại (nhận việc mới)?"),
    "agent_delete": Action("agent.delete", lambda f: {"id": _req(f, "id")}, lambda r, f: f"Đã chuyển agent {f.get('id')} vào thùng rác (khôi phục được)",
                           confirm=lambda f: f"Xoá agent {f.get('id')}? (xoá mềm — vào thùng rác, khôi phục được trong thời hạn giữ)", danger=True,
                           goto=lambda r, f: "/agents"),
    "agent_restore": Action("trash.restore", lambda f: {"id": _req(f, "id"), "kind": "agent"}, lambda r, f: f"Đã khôi phục agent {f.get('id')}",
                            confirm=lambda f: f"Khôi phục agent {f.get('id')} từ thùng rác (kèm cấu hình, quyền, MCP cũ)?",
                            goto=lambda r, f: f"/agents/{f.get('id')}"),
    "agent_revert": Action("agent.revert", lambda f: {"id": _req(f, "id"), "to": int(_req(f, "to", "revision"))},
                           lambda r, f: f"Đã đưa agent về cấu hình revision {f.get('to')}", confirm=lambda f: f"Đưa agent {f.get('id')} về cấu hình revision {f.get('to')}?"),
    # SEC §8: ngoại lệ sandbox localhost theo agent — HUMAN_ONLY, service tự ghi audit `guard.sandbox_override` theo `channel`
    "sandbox_localhost": Action("guard.sandbox.set", lambda f: {"id": _req(f, "id"), "allow_localhost": f.get("on") == "1", "by": "user", "channel": CHANNEL},
                                lambda r, f: f"Đã {'BẬT' if f.get('on') == '1' else 'tắt'} ngoại lệ localhost cho {f.get('id')} (áp dụng từ run kế tiếp)",
                                confirm=lambda f: (f"BẬT ngoại lệ sandbox cho agent {f.get('id')}?\nAgent sẽ được MỞ CỔNG và GỌI MỌI DỊCH VỤ trên máy này "
                                                   "(127.0.0.1/::1): DB/dev server cục bộ, cổng debug Chrome nếu đang mở… Chỉ người tắt được.")
                                if f.get("on") == "1" else f"Tắt ngoại lệ localhost cho agent {f.get('id')}?", danger=True, audit=False),
    "learn_toggle": Action("learn.policy_set", lambda f: {"key": f"agents.{_req(f, 'agent')}.enabled", "value": f.get("on") == "1", "parse": False},
                           lambda r, f: f"Đã {'bật' if f.get('on') == '1' else 'tắt'} tự học cho {f.get('agent')}",
                           confirm=lambda f: f"{'Bật' if f.get('on') == '1' else 'Tắt'} tự học cho agent {f.get('agent')}?"),
    # ---- task / team
    "task_create": Action("task.create", lambda f: {k: v for k, v in {"title": str(f.get("title") or "").strip(), "body": str(f.get("body") or ""),
                                                                    "assignee": str(f.get("assignee") or "").strip() or None, "team": str(f.get("team") or "").strip() or None,
                                                                    "parents": [str(x) for x in f.getlist("parents") if str(x).strip()],
                                                                    "priority": int(f.get("priority") or 0)}.items() if v not in (None, "", [])},
                          lambda r, f: f"Đã tạo task {r.get('id')}", goto=lambda r, f: f"/tasks/{r.get('id')}" if (r or {}).get("id") else None),
    "task_comment": Action("task.comment", lambda f: {"id": _req(f, "id"), "text": _req(f, "text", "bình luận"), "author": "human", "steer": f.get("steer") == "1"},
                           "Đã thêm bình luận"),
    "task_block": Action("task.block", lambda f: {"id": _req(f, "id"), "reason": f.get("reason") or "người chặn từ web"}, "Đã chặn task"),
    "task_unblock": Action("task.unblock", lambda f: {"id": _req(f, "id")}, "Đã gỡ chặn task"),
    "task_retry": Action("task.retry", lambda f: {"id": _req(f, "id")}, "Đã cho chạy lại task"),
    "task_archive": Action("task.archive", lambda f: {"id": _req(f, "id")}, "Đã lưu trữ task", confirm=lambda f: f"Lưu trữ task {f.get('id')}?", danger=True,
                           goto=lambda r, f: "/tasks"),
    "team_create": Action("team.create", lambda f: {"config": {"id": _req(f, "id", "id team"), "name": str(f.get("name") or "").strip() or None,
                                                               "charter": str(f.get("charter") or "")}}, lambda r, f: f"Đã tạo team {f.get('id')}"),
    "team_member": Action("team.member_set", lambda f: {"team": _req(f, "team"), "agent": _req(f, "agent"), "role": f.get("role") or "worker"},
                          lambda r, f: f"Đã thêm {f.get('agent')} vào team {f.get('team')}",
                          confirm=lambda f: f"Thêm {f.get('agent')} vào team {f.get('team')} với vai trò {f.get('role') or 'worker'}?"),
    "team_member_rm": Action("team.member_rm", lambda f: {"team": _req(f, "team"), "agent": _req(f, "agent")}, "Đã gỡ thành viên",
                             confirm=lambda f: f"Gỡ {f.get('agent')} khỏi team {f.get('team')}?", danger=True),
    "team_archive": Action("team.archive", lambda f: {"id": _req(f, "id")}, "Đã lưu trữ team", confirm=lambda f: f"Lưu trữ team {f.get('id')}?", danger=True),
    "team_restore": Action("team.restore", lambda f: {"id": _req(f, "id")}, "Đã khôi phục team"),
    "team_estop": Action("team.estop", lambda f: {"id": _req(f, "id"), "on": f.get("on") == "1", "stop_running": f.get("stop_running") == "1",
                                                  "reason": f.get("reason") or "web"},
                         lambda r, f: f"ESTOP team {f.get('id')}: {'BẬT' if f.get('on') == '1' else 'tắt'}",
                         confirm=lambda f: f"{'BẬT' if f.get('on') == '1' else 'Tắt'} dừng khẩn cấp cho team {f.get('id')}?", danger=True),
    # ---- học
    "learn_rollback": Action("learn.rollback", lambda f: {"id": _req(f, "id"), "reason": f.get("note") or "người rollback từ web", "chain": f.get("chain") == "1"},
                             lambda r, f: f"Đã rollback {f.get('id')}", confirm=lambda f: f"Rollback thay đổi học {f.get('id')}? Nội dung sẽ về bản trước.", danger=True,
                             note="Lý do (tuỳ chọn)"),
    "pin": Action("knowledge.pin", lambda f: {"agent": _req(f, "agent"), "target": _req(f, "target"), "ref": _req(f, "ref"), "on": f.get("on") == "1"},
                  lambda r, f: f"Đã {'ghim' if f.get('on') == '1' else 'bỏ ghim'} {f.get('target')}:{f.get('ref')}"),
    "golden_rm": Action("golden.rm", lambda f: {"id": _req(f, "id")}, "Đã xoá golden task (xoá mềm)", confirm=lambda f: f"Xoá golden task {f.get('id')}?", danger=True),
    "golden_restore": Action("golden.restore", lambda f: {"id": _req(f, "id")}, "Đã khôi phục golden task",
                             confirm=lambda f: f"Khôi phục golden task {f.get('id')}?"),
    "golden_from_run": Action("golden.from_run", lambda f: {"run_id": _req(f, "run_id")}, lambda r, f: f"Đã tạo golden task {(r or {}).get('id')} từ run",
                              confirm="Lấy run này làm golden task (bài kiểm tra chuẩn cho agent)?"),
    "digest_run": Action("digest.run", lambda f: {"no_send": True}, "Đã tạo bản tin học"),
    # ---- MCP
    "mcp_add": Action("mcp.add", lambda f: {"config": _mcp_config(f), "probe": True}, lambda r, f: _mcp_ok(r), goto=lambda r, f: "/mcp", danger=True,
                      confirm=lambda f: _mcp_confirm(f)),
    "mcp_probe": Action("mcp.probe", lambda f: {"name": _req(f, "name")}, lambda r, f: _probe_msg(r)),
    "mcp_remove": Action("mcp.remove", lambda f: {"name": _req(f, "name"), "force": f.get("force") == "1"}, lambda r, f: f"Đã xoá MCP {f.get('name')} (khôi phục được)",
                         confirm=lambda f: f"Xoá MCP server {f.get('name')}? Agent đang dùng sẽ mất các tool này.", danger=True, goto=lambda r, f: "/mcp"),
    "mcp_restore": Action("mcp.restore", lambda f: {"name": _req(f, "name")}, lambda r, f: f"Đã khôi phục MCP {f.get('name')}",
                          confirm=lambda f: f"Khôi phục MCP server {f.get('name')}? Server MCP chạy NGOÀI sandbox của agent."),
    "mcp_import": Action("mcp.import", lambda f: {"path": str(f.get("path") or "~/.claude.json").strip() or "~/.claude.json", "probe": True,
                                                  "overwrite": f.get("overwrite") == "1", **({"select": sel} if (sel := [str(x) for x in f.getlist("select")]) else {})},
                         lambda r, f: "Đã nhập: " + (", ".join(f"{i.get('name')} ({i.get('status')})" for i in (r or {}).get("items") or []) or "không có gì"),
                         confirm="Nhập các MCP server đã chọn từ cấu hình Claude Code vào registry của hub?"),
    # ---- an toàn & cài đặt
    "estop": Action("estop.set", lambda f: {"on": f.get("on") == "1", "stop_running": f.get("stop_running") == "1", "reason": f.get("note") or "web"},
                    lambda r, f: "ESTOP đã BẬT — không run mới nào được chạy" if f.get("on") == "1" else "ESTOP đã tắt",
                    confirm=lambda f: "BẬT dừng khẩn cấp (ESTOP)? Không run mới nào được chạy" + (" và mọi run đang chạy sẽ bị dừng." if f.get("stop_running") == "1" else ".")
                    if f.get("on") == "1" else "Tắt ESTOP — cho phép chạy lại?", danger=True, note="Lý do (tuỳ chọn)"),
    "config_set": Action("config.set", lambda f: {"key": _safe_key(_req(f, "key")), "value": _parse_cfg_value(str(f.get("value") or ""))},
                         lambda r, f: f"Đã đặt {f.get('key')} = {json.dumps((r or {}).get(f.get('key')), ensure_ascii=False)}",
                         confirm=lambda f: f"Đổi cấu hình {f.get('key')} thành '{f.get('value')}'?"),
    "secret_set": Action("secret.set", lambda f: {"name": _req(f, "name", "tên secret"), "value": _req(f, "value", "giá trị")},
                         lambda r, f: f"Đã lưu secret {r.get('name')} (giá trị không bao giờ hiển thị lại)",
                         confirm=lambda f: f"Lưu secret {f.get('name')}? Nếu đã có secret cùng tên, giá trị cũ bị GHI ĐÈ.", danger=True),
    "secret_rm": Action("secret.rm", lambda f: {"name": _req(f, "name")}, lambda r, f: f"Đã xoá secret {f.get('name')}", confirm=lambda f: f"Xoá secret {f.get('name')}?",
                        danger=True),
    "notify_test": Action("notify.test", lambda f: {"text": "cfharness: thử thông báo từ giao diện web"}, lambda r, f: f"Đã gửi thử: {json.dumps(r, ensure_ascii=False)[:200]}"),
    "auth_reset": Action("auth.reset", lambda f: {}, "Đã xoá cờ 'claude chưa đăng nhập' — run sẽ chạy lại"),
}


def _answer_params(f: FormData) -> dict[str, Any]:
    keys = [str(x) for x in f.getlist("keys") if str(x).strip()]
    text = str(f.get("text") or "").strip()
    if not keys and not text:
        raise ValueError("chọn một phương án hoặc nhập câu trả lời")
    return {"id": _req(f, "id"), "keys": keys, "text": text or None, "channel": CHANNEL}


def _safe_key(k: str) -> str:
    if k not in {x[0] for x in SAFE_CONFIG}:
        raise ValueError(f"khoá '{k}' không sửa được từ web — dùng `cfharness config set` trong terminal")
    return k


def _mcp_config(f: FormData) -> dict[str, Any]:
    name = _req(f, "name", "tên server")
    transport = str(f.get("transport") or "stdio")
    cfg: dict[str, Any] = {"name": name, "transport": transport, "trust": "trusted" if f.get("trust") == "trusted" else "untrusted",
                           "description": str(f.get("description") or "")}
    if transport == "stdio":
        import shlex

        parts = shlex.split(_req(f, "command", "lệnh chạy"))
        cfg["command"], cfg["args"] = parts[0], parts[1:]
    else:
        cfg["url"] = _req(f, "url", "URL")
    env: dict[str, str] = {}
    for line in str(f.get("env") or "").splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            if k.strip():
                env[k.strip()] = v.strip()
    if env:
        cfg["env"] = env
    return cfg


def _mcp_confirm(f: FormData) -> str:
    try:
        cfg = _mcp_config(f)
    except ValueError as e:
        return f"Thêm MCP server? ({e})"
    what = (cfg.get("command", "") + " " + " ".join(cfg.get("args") or [])).strip() if cfg["transport"] == "stdio" else cfg.get("url", "")
    trust = "TIN CẬY (tool ghi chạy không cần duyệt)" if cfg["trust"] == "trusted" else "cần duyệt"
    return (f"Thêm MCP server {cfg['name']} ({trust})?\nHub sẽ CHẠY NGAY để kiểm tra, ngoài sandbox, dưới quyền của bạn:\n{what}"
            + (f"\nBiến môi trường: {', '.join(cfg['env'])}" if cfg.get("env") else ""))


def carry(form: FormData) -> list[tuple[str, str]]:
    """Trường form mang sang trang xác nhận — KHÔNG BAO GIỜ gồm csrf/khoá phiên (khoá không được nằm trong HTML)."""
    return [(k, v) for k, v in form.multi_items() if k not in ("csrf", "confirm", "confirm2", KEY_FIELD) and isinstance(v, str)]


HEAVY_EXTRA = ("file",)


def heavy_approval(ap: dict[str, Any], classes: list[str] | tuple[str, ...]) -> bool:
    from ..notify.telegram import is_heavy

    return is_heavy(ap, tuple(classes or ()) + HEAVY_EXTRA)


def _probe_msg(r: Any) -> str:
    r = r or {}
    if r.get("ok"):
        return f"Kết nối được — {len(r.get('tools') or {})} tool ({r.get('elapsed_ms')} ms)"
    return f"Không kết nối được: {r.get('error') or '?'}"


def _mcp_ok(r: Any) -> str:
    pr = (r or {}).get("probe")
    return f"Đã thêm MCP {(r or {}).get('name')}" + (f" — {_probe_msg(pr)}" if pr else "")


async def do_action(request: Request) -> Response:
    _, form = await post_guard(request)
    name = request.path_params["name"]
    act = ACTIONS.get(name)
    if act is None:
        raise HTTPException(404, "Không có thao tác này")
    back = safe_next(form.get("next"), "/")
    try:
        params = act.build(form)
    except ValueError as e:
        return flash_redirect(request, back, "error", f"Chưa đủ thông tin: {e}")
    if act.confirm and form.get("confirm") != "yes":
        msg = act.confirm(form) if callable(act.confirm) else act.confirm
        fields = [(k, v) for k, v in carry(form) if not (name == "secret_set" and k == "value")]  # giá trị secret không vào HTML
        return await render(request, "confirm.html", {"message": msg + ("\n(Nhập lại giá trị ở trang Cài đặt và tích ô xác nhận.)" if name == "secret_set" else ""),
                                                      "fields": fields, "action": f"/do/{name}", "danger": act.danger, "back": back,
                                                      "note": act.note})
    if name == "approve_always" and form.get("confirm2") != "yes":
        # bước 2 BẮT BUỘC: trang nói rõ luật sẽ tạo (agent + tool + dấu vân tay / agent + host) — `always_offer` do daemon tính
        try:
            ap = await rpc(request, "approval.get", {"id": params["id"]})
        except RpcError as e:
            return flash_redirect(request, back, "error", err_text(e))
        if not ap.get("always_offer"):
            return flash_redirect(request, back, "error", f"Yêu cầu {ap.get('id')} không đủ điều kiện 'luôn cho phép' — chỉ cho phép một lần")
        return await render(request, "approve2.html", {"a": ap, "view": P.action_view(ap), "fields": carry(form), "action": f"/do/{name}", "back": back,
                                                       "always": ap["always_offer"]})
    if name in ("approve", "approve_fp") and form.get("confirm2") != "yes":
        # thao tác phá huỷ NẶNG (tài nguyên bảo vệ / lớp trong notify.telegram.confirm_classes + file) → bước 2, như Telegram (WT-5/6)
        try:
            ap = await rpc(request, "approval.get", {"id": params["id"]})
            classes = await rpc(request, "config.get", {"key": "notify.telegram.confirm_classes"}) or []
        except RpcError as e:
            return flash_redirect(request, back, "error", err_text(e))
        if heavy_approval(ap, classes):
            return await render(request, "approve2.html", {"a": ap, "view": P.action_view(ap), "fields": carry(form), "action": f"/do/{name}", "back": back})
    try:
        res = await rpc(request, act.method, params, audit=act.audit)
    except RpcError as e:
        return flash_redirect(request, back, "error", err_text(e))
    msg = act.ok(res, form) if callable(act.ok) else act.ok
    dest = (act.goto(res, form) if act.goto else None) or back
    if request.headers.get("hx-request"):
        sess = session_of(request)
        if sess:
            sess.flash("ok", msg)
        return Response(status_code=204, headers={"HX-Redirect": dest})
    return flash_redirect(request, dest, "ok", msg)


# ============================================================================ SSE
def _sse(obj: dict[str, Any]) -> str:
    data = json.dumps(obj, ensure_ascii=False)
    return f"data: {data}\n\n"


async def _stream(request: Request, params: dict[str, Any], convert: Callable[[dict[str, Any]], list[dict[str, Any]]]) -> AsyncIterator[str]:
    st = ws(request)
    q: asyncio.Queue = asyncio.Queue(maxsize=20_000)
    sub = subscribe(st.client, params)

    async def pump() -> None:
        try:
            async for msg in sub:
                await q.put(msg)
                if "end" in msg:
                    break
        except Exception as e:  # daemon tắt, run không tồn tại…
            await q.put({"end": {"reason": err_text(e)}})
        finally:
            await q.put(None)

    task = asyncio.create_task(pump())
    try:
        yield "retry: 3000\n\n"
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), HEARTBEAT_S)
            except asyncio.TimeoutError:
                yield ": ping\n\n"
                continue
            if msg is None:
                yield _sse({"t": "end", "reason": "closed"})
                break
            if "subscribed" in msg:
                yield _sse({"t": "hello", "truncated": bool((msg["subscribed"] or {}).get("truncated"))})
                continue
            if "end" in msg:
                yield _sse({"t": "end", "reason": (msg["end"] or {}).get("reason") or "closed"})
                break
            for out in convert(msg):
                yield _sse(out)
    finally:
        sub.close()
        task.cancel()


def _run_converter(templates: Jinja2Templates) -> Callable[[dict[str, Any]], list[dict[str, Any]]]:
    mod = templates.env.get_template("_items.html").module

    def conv(ev: dict[str, Any]) -> list[dict[str, Any]]:
        t = ev.get("type")
        p = ev.get("payload") or {}
        if t in ("message.delta", "reasoning.delta"):
            if ev.get("subagent_id"):
                return []
            return [{"t": "delta", "k": "text" if t == "message.delta" else "reasoning", "text": p.get("text") or "", "seq": ev.get("seq")}]
        out: list[dict[str, Any]] = []
        s = P.stats_of(ev)
        if s:
            out.append({"t": "stats", **s})
        it = P.transcript_item(ev)
        if it is not None:
            out.append({"t": "item", "kind": it["kind"], "dom": it.get("dom"), "seq": ev.get("seq"), "html": str(mod.item(it))})
        if t in ("approval.open", "approval.waiting_human", "approval.resolved", "user.message", "steer.ack") or (t or "").startswith(("run.", "question.")):
            out.append({"t": "refresh"})
        return out

    return conv


async def sse_run(request: Request) -> Response:
    rid = request.path_params["id"]
    q = request.query_params
    params = {"run_id": rid, "since_seq": int(q.get("since") or 0), "epoch": q.get("epoch") or None, "level": "detail", "until_run_end": True}
    return StreamingResponse(_stream(request, params, _run_converter(ws(request).templates)), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


async def sse_firehose(request: Request) -> Response:
    mod = ws(request).templates.env.get_template("_items.html").module

    def conv(ev: dict[str, Any]) -> list[dict[str, Any]]:
        it = P.activity_item(ev)
        return [{"t": "line", "html": str(mod.line(it))}] if it else []

    st = await rpc(request, "hub.status")
    params = {"level": "summary", "since_seq": max(0, int(st.get("last_seq") or 0) - 300), "epoch": st.get("epoch")}
    return StreamingResponse(_stream(request, params, conv), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


# ============================================================================ lỗi
async def http_error(request: Request, exc: Exception) -> Response:
    code = getattr(exc, "status_code", 500)
    detail = getattr(exc, "detail", "Lỗi")
    if request.url.path.startswith(("/sse/", "/partials/")) or request.method != "GET":
        return PlainTextResponse(str(detail), status_code=code)
    st = ws(request)
    return st.templates.TemplateResponse(request, "error.html", {"code": code, "message": detail, "htmx": HTMX, "badge": {}, "csrf": "", "flashes": [], "nav": ""},
                                         status_code=code)


async def rpc_error(request: Request, exc: Exception) -> Response:
    msg = err_text(exc)
    code = 404 if isinstance(exc, RpcError) and exc.code == -32004 else 503 if isinstance(exc, (DaemonUnavailable, OSError)) else 400
    if request.url.path.startswith(("/sse/", "/partials/")):
        return PlainTextResponse(msg, status_code=code)
    st = ws(request)
    sess = session_of(request)
    return st.templates.TemplateResponse(request, "error.html", {"code": code, "message": msg, "htmx": HTMX, "badge": {"down": code == 503},
                                                                 "csrf": sess.csrf if sess else "", "flashes": [], "nav": ""}, status_code=code)


async def favicon(request: Request) -> Response:
    return RedirectResponse("/static/favicon.svg", status_code=301)


# ============================================================================ dựng app
def _manager_routes() -> list[Any]:
    from .manager import routes as _r

    return _r()


def make_templates() -> Jinja2Templates:
    t = Jinja2Templates(directory=str(HERE / "templates"))
    env = t.env
    env.autoescape = True
    env.globals.update({"agent_status": P.agent_status, "run_status": P.run_status, "task_status": lambda s: P.TASK_STATUS.get(s or "", (s or "—", "muted")),
                        "is_live_run": P.is_live_run, "action_view": P.action_view, "wait_reason": P.wait_reason, "tokens_of": P.tokens_of, "TASK_COLUMNS": P.TASK_COLUMNS,
                        "question_view": P.question_view, "run_title": P.run_title})
    env.filters.update({"cost": P.fmt_cost, "tok": P.fmt_tokens, "dur": P.fmt_dur, "ts": P.fmt_ts, "ago": P.ago, "first": P.first_line,
                        "tool": P.short_tool, "md": lambda s: Markup(P.md(s)), "usd": lambda v: "—" if v is None else f"${float(v):.4f}",
                        "j": lambda v: json.dumps(v, ensure_ascii=False, indent=2, default=str), "iso": P.iso_ts, "avatar": P.avatar_tone})
    return t


def create_app(paths: HubPaths, *, auth: WebAuth | None = None, host: str = "127.0.0.1", port: int = 8765, name: str = WEB_HOST) -> Starlette:
    state = WebState(paths=paths, auth=auth or WebAuth(), host=host, port=port, client=AsyncHubClient(paths.socket, channel=CHANNEL, timeout=60.0),
                     templates=make_templates(), name=name)
    routes = [
        Route("/", overview),
        Route("/login", login, methods=["GET", "POST"]),
        Route("/logout", logout, methods=["POST"]),
        Route("/favicon.ico", favicon),
        Route("/partials/overview", partial_overview),
        Route("/agents", agents_page),
        Route("/agents/new", agent_new),
        Route("/agents/create", agent_create, methods=["POST"]),
        Route("/agents/{id}", agent_page),
        Route("/agents/{id}/edit", agent_edit),
        Route("/agents/{id}/save", agent_update, methods=["POST"]),
        Route("/runs", runs_page),
        Route("/runs/{id}", run_page),
        Route("/partials/run/{id}", partial_run),
        Route("/inbox", inbox_page),
        Route("/approvals", approvals_page),
        Route("/tasks", tasks_page),
        Route("/tasks/{id}", task_page),
        Route("/teams", teams_page),
        Route("/learnings", learnings_page),
        Route("/learnings/{id}", ledger_entry),
        Route("/mcp", mcp_page),
        Route("/mcp/{name}", mcp_show),
        Route("/costs", costs_page),
        Route("/audit", audit_page),
        Route("/settings", settings_page),
        Route("/do/{name}", do_action, methods=["POST"]),
        Route("/sse/run/{id}", sse_run),
        Route("/sse/firehose", sse_firehose),
        *_manager_routes(),
        Mount("/static", app=StaticFiles(directory=str(HERE / "static")), name="static"),
    ]
    app = Starlette(routes=routes, exception_handlers={HTTPException: http_error, RpcError: rpc_error, DaemonUnavailable: rpc_error})
    app.state.web = state
    app.add_middleware(GuardMiddleware, state=state)
    return app


# M7 MGR-UI: thao tác ghi của trang Điều phối (manager_set / manager_unset) — module manager dùng lại helper của app
from . import manager as _mgr  # noqa: E402

_mgr.register_actions(ACTIONS, Action, _req)

__all__ = ["create_app", "ACTIONS", "agent_changes", "replay_items", "WebState", "urlencode"]
