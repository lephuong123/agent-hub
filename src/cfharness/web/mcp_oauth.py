"""Web: "Kết nối" MCP server OAuth một chạm (docs/dev-notes/mcp-oauth.md).

- `POST /do/mcp_connect` (CSRF + khoá phiên WT-1 qua `post_guard`): `mcp.oauth_start` với redirect loopback
  `http://127.0.0.1:<port>/mcp/oauth/callback` (`redirect_uri`) → trang chuyển tiếp tới `authorize_url`. Đây là chỗ DUY NHẤT web đưa trình duyệt ra
  URL ngoài; URL phải là https (http chỉ loopback). Không dùng 303 thẳng: CSP `form-action 'self'` của trang chặn chuyển hướng
  khác nguồn sau khi gửi form (Chrome/Firefox) — trang trung gian dùng `<meta refresh>` + nút bấm (không cần script inline).
- `GET /mcp/oauth/callback`: CÔNG KHAI với GuardMiddleware (cookie phiên SameSite=Strict KHÔNG được gửi trong điều hướng
  cấp cao từ trang của nhà cung cấp). An toàn vì `state` (256 bit, một lần, ≤ 10 phút, daemon giữ) là lớp chống CSRF/giả
  callback; trang trả về không lộ gì ngoài tên server + kết quả. Có phiên (trình duyệt gửi cookie) → flash + 303; không có →
  trang kết quả + `<meta refresh>` về `/mcp/<tên>?oauth=<kết quả>` (điều hướng cùng nguồn → cookie có lại).
- `mcp_disconnect` = thao tác trong `ACTIONS` (hộp xác nhận) → `mcp.oauth_disconnect`.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from ..rpc import DaemonUnavailable, RpcError
from ..mcp_client.oauth import is_loopback_host

CALLBACK_PATH = "/mcp/oauth/callback"
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")


def _app() -> Any:
    from . import app as A  # import trễ: app.py import module này

    return A


def redirect_uri(port: int) -> str:
    """Redirect URI đăng ký với nhà cung cấp: loopback IP (RFC 8252 §7.3). Nhiều máy chủ cấp quyền (vd Notion) chỉ nhận https
    hoặc loopback — `cfharness.localhost` bị từ chối. Trình duyệt về `127.0.0.1:<port>/mcp/oauth/callback` → GuardMiddleware
    303 sang host web riêng (giữ nguyên query; không đặt cookie trên 127.0.0.1) → `callback`; daemon đổi code bằng ĐÚNG URI này."""
    return f"http://127.0.0.1:{port}{CALLBACK_PATH}"


def _external_ok(url: str) -> bool:
    s = urlsplit(url or "")
    return bool(s.netloc) and (s.scheme == "https" or (s.scheme == "http" and is_loopback_host(s.hostname)))


async def connect(request: Request) -> Response:
    A = _app()
    _, form = await A.post_guard(request)
    name = str(form.get("name") or "")
    if not _NAME_RE.match(name):
        return A.flash_redirect(request, "/mcp", "error", "Tên MCP server không hợp lệ")
    back = f"/mcp/{name}"
    st = A.ws(request)
    try:
        res = await A.rpc(request, "mcp.oauth_start", {"name": name, "redirect_uri": redirect_uri(st.port)}, audit=True)
    except (RpcError, DaemonUnavailable) as e:
        return A.flash_redirect(request, back, "error", f"Không bắt đầu được đăng nhập: {A.err_text(e)}")
    url = str((res or {}).get("authorize_url") or "")
    if not _external_ok(url):
        return A.flash_redirect(request, back, "error", "URL đăng nhập không an toàn (không phải https) — từ chối")
    if request.headers.get("hx-request"):
        return Response(status_code=204, headers={"HX-Redirect": url})
    return await A.render(request, "mcp_oauth_go.html", {"nav": "mcp", "name": name, "url": url, "host": urlsplit(url).netloc,
                                                         "client": res.get("client"), "back": back})


async def callback(request: Request) -> Response:
    A = _app()
    q = request.query_params
    state = q.get("state") or ""
    params = {"state": state, "code": q.get("code"), "error": q.get("error"), "error_description": q.get("error_description"), "iss": q.get("iss")}
    name, ok, msg = None, False, ""
    if not state:
        msg = "Thiếu state — mở lại trang MCP và bấm Kết nối."
    else:
        try:
            res = await A.rpc(request, "mcp.oauth_callback", {k: v for k, v in params.items() if v is not None}, audit=True)
            name, ok = res.get("name"), bool(res.get("ok"))
            msg = f"Đã kết nối {name}." if ok else f"Kết nối {name} thất bại: {res.get('error')}"
        except (RpcError, DaemonUnavailable) as e:
            msg = f"Không hoàn tất được đăng nhập: {A.err_text(e)}"
    name = name if isinstance(name, str) and _NAME_RE.match(name) else None
    dest = f"/mcp/{name}" if name else "/mcp"
    if A.session_of(request) is not None:
        return A.flash_redirect(request, dest, "ok" if ok else "error", msg)
    nxt = dest + f"?oauth={'connected' if ok else 'error'}"
    return A.ws(request).templates.TemplateResponse(
        request, "mcp_oauth_done.html", {"ok": ok, "message": msg, "next": nxt, "htmx": A.HTMX, "badge": {}, "csrf": "", "flashes": [], "nav": ""},
        status_code=200 if ok else 400)


def routes() -> list[Route]:
    """Đặt TRƯỚC `/do/{name}` trong `create_app`."""
    return [Route("/do/mcp_connect", connect, methods=["POST"]), Route(CALLBACK_PATH, callback)]


def register_actions(actions: dict[str, Any], Action: Any, req: Any) -> None:
    actions["mcp_disconnect"] = Action(
        "mcp.oauth_disconnect", lambda f: {"name": req(f, "name")},
        lambda r, f: f"Đã ngắt kết nối {f.get('name')}" + (" (đã thu hồi token)" if (r or {}).get("revoked") else ""),
        confirm=lambda f: f"Ngắt kết nối OAuth của {f.get('name')}? Hub thu hồi token (nếu nhà cung cấp hỗ trợ) và xoá khỏi secret store; "
                          "agent dùng server này sẽ không chạy được tới khi kết nối lại.",
        danger=True, goto=lambda r, f: f"/mcp/{f.get('name')}")

