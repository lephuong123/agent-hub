"""Thẻ "Không gian số" ở Cài đặt (filehub/) — `/partials/filehub` (htmx `load`), trang đăng nhập `/filehub/login`, callback relay OAuth
`/filehub/oauth/cb` và `/filehub/open` (link "Mở trên Không gian số"). docs/dev-notes/filehub.md, plan-filehub-workspace-root.md (W7).

- Đọc: RPC `filehub.status`. Daemon không trả lời → thẻ báo, không lỗi trang.
- Ghi (W7 — làm được cả khi mở qua link public; vẫn cần đăng nhập web + TOTP + CSRF + khoá phiên WT-1, audit `web`): mọi thao tác là
  `ACTIONS` gọi RPC `filehub.*` (không logic trong template → app desktop gọi lại cùng RPC). Thiết lập / đăng nhập / chuyển chế độ / gỡ
  = `Action.step2` (bước 2 bắt buộc). Qua link public: thiết lập không đổi được server/binary đã đặt (`remote` — service kiểm).
- Đăng nhập: web trên chính máy hub (`is_local_request`) → listener loopback của binary như CLI; qua link public → RELAY OAuth: daemon chạy
  binary với `--redirect-uri <web.public_origin>/filehub/oauth/cb` → trang `/filehub/login` đưa link trang đồng ý (tab mới) → Không gian số
  chuyển trình duyệt về `GET /filehub/oauth/cb?code&state` — route CÔNG KHAI với GuardMiddleware (cookie phiên SameSite=Strict không đi
  theo điều hướng chéo site). An toàn vì `state` (do binary sinh, daemon giữ ≤ 10 phút, MỘT lần, so `compare_digest`) + PKCE verifier không
  rời binary; route giới hạn tốc độ theo IP, KHÔNG phản chiếu tham số (trang kết quả chỉ chữ cố định).
- `/filehub/open?run=…&path=…` → RPC `filehub.link` → chuyển hướng tới `<server>/files/<folder_id>?file=<file_id>`; URL dựng ở daemon.
"""

from __future__ import annotations

from typing import Any

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route

from ..rpc import DaemonUnavailable, RpcError
from .app import rpc, session_of, ws

CALLBACK_PATH = "/filehub/oauth/cb"
STATE_VIEW = {"idle": ("đã đồng bộ", "ok"), "syncing": ("đang đồng bộ", "info"), "conflict": ("có bản xung đột", "warn"), "offline": ("mất kết nối", "warn"),
              "auth_expired": ("hết phiên đăng nhập", "danger"), "error": ("lỗi", "danger"), "paused": ("tạm dừng", "warn"),
              "not_configured": ("chưa thiết lập", ""), "version_mismatch": ("binary sync sai phiên bản", "danger"), "starting": ("đang khởi động", "info"),
              "revoked": ("mất quyền truy cập", "danger")}
# chữ lỗi đăng nhập do DAEMON dựng (`filehub.service.login_error_text` → `login.message`); web không phản chiếu chữ lấy từ query


def _local(request: Request) -> bool:
    from .file_pages import is_local_request

    return is_local_request(request)


async def partial_filehub(request: Request) -> Response:
    ctx: dict[str, Any]
    try:
        ctx = {"data": await rpc(request, "filehub.status")}
    except (DaemonUnavailable, OSError):
        ctx = {"down": True}
    except RpcError as e:
        ctx = {"error": e.message}
    sess = session_of(request)
    return ws(request).templates.TemplateResponse(request, "_filehub.html", {**ctx, "csrf": sess.csrf if sess else "", "states": STATE_VIEW,
                                                                             "local": _local(request)})


async def _login_ctx(request: Request) -> dict[str, Any]:
    try:
        st = await rpc(request, "filehub.status")
    except (DaemonUnavailable, OSError, RpcError):
        return {"down": True}
    lg = (st or {}).get("login") or {}
    url = str(lg.get("url") or "")
    return {"lg": lg, "url": url if url.startswith(("https://", "http://")) else "",
            "folders": (st or {}).get("folders") or [], "local": _local(request)}


async def login_page(request: Request) -> Response:
    """Lượt đăng nhập đang chờ: link trang đồng ý (tab mới) + trạng thái tự làm mới (htmx) tới khi xong/lỗi."""
    from .app import render

    return await render(request, "filehub_login.html", {"nav": "settings", **(await _login_ctx(request))})


async def partial_login(request: Request) -> Response:
    sess = session_of(request)
    return ws(request).templates.TemplateResponse(request, "_filehub_login.html", {**(await _login_ctx(request)), "csrf": sess.csrf if sess else ""})


async def oauth_callback(request: Request) -> Response:
    """Relay OAuth: Không gian số chuyển về đây sau trang đồng ý. Công khai (không cần phiên); giới hạn lần SAI theo IP + toàn cục;
    kết quả chỉ chữ cố định (không phản chiếu code/state/error)."""
    from .app import _ip

    st = ws(request)
    ip = _ip(request)
    lim = st.fh_limiter
    q = request.query_params
    ok, msg, status = False, "Liên kết đăng nhập không hợp lệ hoặc đã hết hạn — bắt đầu lại ở Cài đặt › Không gian số.", 400
    if lim.locked(ip):
        msg, status = "Thử sai quá nhiều lần — đợi ít phút rồi bắt đầu lại.", 429
    else:
        params = {k: str(q.get(k))[:4096] for k in ("code", "state", "error") if q.get(k) is not None}
        res: dict[str, Any] | None = None
        if params.get("state"):
            try:
                res = await rpc(request, "filehub.login_callback", params, audit=True)
            except RpcError:
                res = None
            except (DaemonUnavailable, OSError):
                msg, status, res = "Hub không trả lời — thử lại sau.", 503, {}
        if res is None:
            lim.fail(ip)
        elif res.get("ok"):
            ok, msg, status = True, "Đã đăng nhập Không gian số — quay lại Cài đặt của hub.", 200
        elif res:
            msg = str(res.get("message") or "Đăng nhập chưa xong — mở lại Cài đặt › Không gian số để xem lỗi và thử lại.")
    nxt = "/settings#filehub"
    if session_of(request) is not None:  # hiếm: trình duyệt có gửi cookie (cùng site)
        from .app import flash_redirect

        return flash_redirect(request, nxt, "ok" if ok else "error", msg)
    from .app import HTMX

    return st.templates.TemplateResponse(request, "filehub_oauth_done.html", {"ok": ok, "message": msg, "next": nxt, "htmx": HTMX, "badge": {}, "csrf": "",
                                                                              "flashes": [], "nav": ""}, status_code=status)


async def filehub_open(request: Request) -> Response:
    q = request.query_params
    path = str(q.get("path") or "")
    run = str(q.get("run") or "").strip()
    if not path or len(path) > 4096 or len(run) > 200:
        raise HTTPException(400, "Thiếu/sai tham số path")
    try:
        res = await rpc(request, "filehub.link", {"path": path, **({"run_id": run} if run else {})})
    except RpcError as e:
        raise HTTPException(404, e.message) from None
    except (DaemonUnavailable, OSError):
        raise HTTPException(503, "Daemon không trả lời") from None
    url = str((res or {}).get("url") or "")
    if not url.startswith(("https://", "http://")):
        raise HTTPException(404, "Chưa có link cho tệp này")
    return RedirectResponse(url, status_code=303)


def register_actions(actions: dict[str, Any], Action: Any, _req: Any) -> None:
    def _login_params(request: Request, p: dict[str, Any]) -> dict[str, Any]:
        # web luôn tự đưa link (không để binary mở trình duyệt trên máy hub); qua link public → relay OAuth
        local = _local(request)
        return {**p, "no_browser": True, "relay": not local}

    async def _pause(request: Request, p: dict[str, Any]) -> Any:
        return await rpc(request, "filehub.pause", {}, audit=True)

    async def _resume(request: Request, p: dict[str, Any]) -> Any:
        return await rpc(request, "filehub.resume", {}, audit=True)

    async def _push(request: Request, p: dict[str, Any]) -> Any:
        return await rpc(request, "filehub.resume", {"root_id": p.get("root_id")}, audit=True)

    async def _setup(request: Request, p: dict[str, Any]) -> Any:
        return await rpc(request, "filehub.setup", {**_login_params(request, p), "remote": not _local(request)}, audit=True)

    async def _login(request: Request, p: dict[str, Any]) -> Any:
        return await rpc(request, "filehub.login", _login_params(request, p), audit=True)

    async def _switch(request: Request, p: dict[str, Any]) -> Any:
        return await rpc(request, "filehub.switch_to_workspace", _login_params(request, p), audit=True)

    def _setup_build(f: Any) -> dict[str, Any]:
        scope = str(f.get("scope") or "workspace")
        if scope not in ("workspace", "folder"):
            raise ValueError("scope phải là workspace|folder")
        out = {"server": _req(f, "server"), "binary": _req(f, "binary"), "scope": scope}
        for k in ("name", "local_parent"):
            v = str(f.get(k) or "").strip()
            if v:
                out[k] = v
        return out

    def _scope_text(f: Any) -> str:
        return ("TOÀN BỘ workspace (chọn workspace ở trang đồng ý; tài khoản phải là Workspace Admin)" if str(f.get("scope") or "workspace") == "workspace"
                else "MỘT thư mục (chọn/tạo ở trang đồng ý)")

    goto_login = lambda r, f: "/filehub/login"  # noqa: E731
    actions["filehub_pause"] = Action("filehub.pause", lambda f: {}, "Đã tạm dừng đồng bộ Không gian số", call=_pause,
                                      confirm="Tạm dừng đồng bộ với Không gian số?\nTệp người sửa trên web sẽ không về máy (và ngược lại) tới khi chạy lại.",
                                      goto=lambda r, f: "/settings#filehub")
    actions["filehub_resume"] = Action("filehub.resume", lambda f: {}, "Đã chạy lại đồng bộ", call=_resume, goto=lambda r, f: "/settings#filehub")
    actions["filehub_resume_push"] = Action("filehub.resume", lambda f: {"root_id": _req(f, "root_id")}, "Đã cho đẩy lên tiếp", call=_push,
                                            confirm="Cho tiến trình đồng bộ đẩy tiếp các thay đổi (gồm các tệp đã xoá) lên Không gian số?",
                                            goto=lambda r, f: "/settings#filehub")
    actions["filehub_setup"] = Action(
        "filehub.setup", _setup_build, "Đã bắt đầu đăng nhập Không gian số", call=_setup, goto=goto_login,
        confirm=lambda f: f"Thiết lập đồng bộ với Không gian số {f.get('server')}?\nChế độ: {_scope_text(f)}.",
        step2=lambda f: ("Hub sẽ chạy tiến trình đồng bộ 2 chiều dưới tài khoản Không gian số bạn đồng ý (tệp agent ghi mang tên tài khoản đó). "
                         + ("Chế độ workspace: agent được giao việc ghi được MỌI thư mục của workspace, kể cả thư mục người khác tạo sau — "
                            "lưới an toàn là phiên bản + thùng rác Không gian số và báo sau mỗi run." if str(f.get("scope") or "workspace") == "workspace"
                            else "Chỉ thư mục được chọn ở trang đồng ý được đồng bộ.")),
        danger=True)
    actions["filehub_login"] = Action(
        "filehub.login", lambda f: {}, "Đã bắt đầu đăng nhập lại", call=_login, goto=goto_login,
        confirm="Đăng nhập lại Không gian số (thiết bị cfharness)?",
        step2="Đăng nhập cấp token MỚI cho tiến trình đồng bộ của hub. Đăng nhập bằng tài khoản có quyền với thư mục chung "
              "(chế độ workspace: tài khoản admin của workspace).", danger=True)
    actions["filehub_switch"] = Action(
        "filehub.switch_to_workspace", lambda f: {}, "Đã bắt đầu chuyển sang toàn bộ workspace", call=_switch, goto=goto_login,
        confirm="Chuyển thư mục chung sang TOÀN BỘ workspace?",
        step2="Sau khi bạn đồng ý ở Không gian số: thư mục chung cũ được gỡ khỏi hub (tệp trên máy và trên web GIỮ NGUYÊN), hub đồng bộ cả "
              "workspace về một bản sao MỚI. Agent được giao việc ghi được mọi thư mục của workspace. Kiểm bản mới rồi tự xoá bản cũ; thu hồi "
              "thiết bị cũ ở Không gian số › Hồ sơ › Thiết bị.", danger=True)
    actions["filehub_remove"] = Action(
        "filehub.remove", lambda f: {"name": _req(f, "name")}, lambda r, f: f"Đã gỡ «{f.get('name')}» khỏi hub (tệp giữ nguyên)",
        confirm=lambda f: f"Gỡ thư mục chung «{f.get('name')}» khỏi hub?",
        step2=lambda f: (f"Hub dừng đồng bộ «{f.get('name')}», agent không còn giao việc vào đó được. Tệp trên máy và trên Không gian số GIỮ "
                         "NGUYÊN (không xoá gì)."), danger=True, goto=lambda r, f: "/settings#filehub")


def routes() -> list[Any]:
    return [Route("/partials/filehub", partial_filehub), Route("/partials/filehub/login", partial_login), Route("/filehub/login", login_page),
            Route(CALLBACK_PATH, oauth_callback), Route("/filehub/open", filehub_open)]


__all__ = ["register_actions", "routes", "STATE_VIEW", "CALLBACK_PATH"]
