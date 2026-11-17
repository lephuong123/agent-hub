"""Thẻ "Không gian số" ở Cài đặt (filehub/) — `/partials/filehub` (htmx `load`), trang đăng nhập `/filehub/login`, callback relay OAuth
`/filehub/oauth/cb` và `/filehub/open` (link "Mở trên Không gian số"). docs/dev-notes/filehub.md, plan-filehub-workspace-root.md (W7).

- Đọc: RPC `filehub.status` (trạng thái đồng bộ, thư mục chung, bản xung đột, khoá run, lượt đăng nhập). Daemon không trả lời → thẻ báo,
  không lỗi trang. Thẻ tự làm mới mỗi 3 s khi có lượt đăng nhập đang chờ đồng ý; thư mục đã cấp lúc đăng nhập mà chưa nối → form "Thêm".
- Ghi (W7 + #121 — làm được cả khi mở qua link public; vẫn cần đăng nhập web + TOTP + CSRF + Origin + khoá phiên WT-1, audit `web`, RPC
  `filehub.*` ∈ HUMAN_ONLY): mọi thao tác là `ACTIONS` gọi RPC `filehub.*` (không logic trong template → app desktop gọi lại cùng RPC):
  `filehub_setup` (máy chủ + chế độ workspace|folder + tên → đăng nhập, xong tự nối thư mục đầu), `filehub_login` (đăng nhập lại / cấp thêm
  thư mục), `filehub_switch`, `filehub_add` (thư mục đã cấp), `filehub_remove`, `filehub_rename`, `filehub_pause|filehub_resume|filehub_resume_push`.
  Thiết lập / đăng nhập / chuyển chế độ / gỡ = `Action.step2` (bước 2 bắt buộc).
- Ranh giới: web KHÔNG BAO GIỜ nhận đường dẫn binary sync (daemon dùng cấu hình hoặc tự dò ở chỗ cài cố định — `filehub.detect_binary`,
  không dò PATH); đường dẫn bản sao cục bộ (`local_parent`/`local_path`) chỉ đặt được khi dùng web trên chính máy chạy hub
  (`is_local_request`), qua link public dùng `<local_parent>/<tên>`; qua link public thiết lập không đổi được server/binary đã đặt (`remote`
  — service kiểm). Link đồng ý luôn `no_browser` (không mở trình duyệt trên máy hub).
- Đăng nhập: web trên chính máy hub → listener loopback của binary như CLI; qua link public → RELAY OAuth: daemon chạy
  binary với `--redirect-uri <web.public_origin>/filehub/oauth/cb` → trang `/filehub/login` (và thẻ) đưa link trang đồng ý (tab mới) → Không
  gian số chuyển trình duyệt về `GET /filehub/oauth/cb?code&state` — route CÔNG KHAI với GuardMiddleware (cookie phiên SameSite=Strict không
  đi theo điều hướng chéo site). An toàn vì `state` (do binary sinh, daemon giữ ≤ 10 phút, MỘT lần, so `compare_digest`) + PKCE verifier không
  rời binary; route giới hạn tốc độ theo IP, KHÔNG phản chiếu tham số (trang kết quả chỉ chữ cố định).
- `/filehub/open?run=…&path=…` → RPC `filehub.link` → chuyển hướng tới `<server>/files/<folder_id>?file=<file_id>`; URL dựng ở daemon từ
  cấu hình `filehub.server` (người đặt) + id trong index của binary sync — không lấy từ client.
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
    data = ctx.get("data")
    if isinstance(data, dict):
        have = {f.get("root_id") for f in data.get("folders") or []}
        lg = data.get("login") or {}
        ctx["available"] = [r for r in lg.get("roots") or [] if isinstance(r, dict) and r.get("id") and r.get("id") not in have]
        url = str(lg.get("url") or "")
        ctx["login_url"] = url if lg.get("state") in ("starting", "waiting") and url.startswith(("https://", "http://")) else ""
        ctx["polling"] = lg.get("state") in ("starting", "waiting")
    sess = session_of(request)
    return ws(request).templates.TemplateResponse(request, "_filehub.html", {**ctx, "csrf": sess.csrf if sess else "", "states": STATE_VIEW,
                                                                             "local": _local(request)})


async def partial_lane(request: Request) -> Response:
    """Thẻ "Cổng cfharness" (chế độ lane): RPC `filehub.lane_status` (không bao giờ có khoá) + tên thư mục đang ở chế độ sync (để chuyển)."""
    ctx: dict[str, Any]
    try:
        ctx = {"data": await rpc(request, "filehub.lane_status")}
        st = await rpc(request, "filehub.status")
        ctx["sync_folders"] = [str(f.get("name")) for f in (st or {}).get("folders") or [] if f.get("name")]
    except (DaemonUnavailable, OSError):
        ctx = {"down": True}
    except RpcError as e:
        ctx = {"error": e.message}
    sess = session_of(request)
    return ws(request).templates.TemplateResponse(request, "_filehub_lane.html", {**ctx, "csrf": sess.csrf if sess else ""})


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


NEXT = "/settings#filehub"
LOCAL_ONLY_MSG = ("Đường dẫn bản sao trên máy chỉ đặt được khi dùng web trên chính máy chạy hub (hoặc `cfharness filehub …` trong terminal). "
                  "Qua link public, thư mục được đặt ở <thư mục cha mặc định>/<tên>.")


def _server(f: Any) -> str:
    v = str(f.get("server") or "").strip().rstrip("/")
    if not v.startswith("https://") or len(v) > 300 or any(c.isspace() for c in v):
        raise ValueError("địa chỉ Không gian số phải là URL https://… (vd https://trungtamai.cchchn.com)")
    return v


def _name(f: Any, key: str = "name", default: str | None = None) -> str:
    from ..filehub.service import valid_name

    v = str(f.get(key) or "").strip() or (default or "")
    if not valid_name(v):
        raise ValueError("tên thư mục: chữ, số, khoảng trắng, - _ . (≤ 80 ký tự)")
    return v


def _local_path(f: Any, key: str) -> dict[str, Any]:
    v = str(f.get(key) or "").strip()
    return {key: v} if v else {}


def _guard_local(request: Request, p: dict[str, Any], *keys: str) -> None:
    if any(p.get(k) for k in keys) and not _local(request):
        raise RpcError(-32003, LOCAL_ONLY_MSG)


def register_actions(actions: dict[str, Any], Action: Any, _req: Any) -> None:
    def _login_params(request: Request, p: dict[str, Any]) -> dict[str, Any]:
        # web luôn tự đưa link (không để binary mở trình duyệt trên máy hub); qua link public → relay OAuth
        return {**p, "no_browser": True, "relay": not _local(request)}

    def simple(method: str) -> Any:
        async def call(request: Request, p: dict[str, Any]) -> Any:
            return await rpc(request, method, p, audit=True)

        return call

    async def _setup(request: Request, p: dict[str, Any]) -> Any:
        _guard_local(request, p, "local_parent", "local_path")
        return await rpc(request, "filehub.setup", {**_login_params(request, p), "remote": not _local(request)}, audit=True)

    async def _login(request: Request, p: dict[str, Any]) -> Any:
        return await rpc(request, "filehub.login", _login_params(request, {}), audit=True)

    async def _switch(request: Request, p: dict[str, Any]) -> Any:
        return await rpc(request, "filehub.switch_to_workspace", _login_params(request, {}), audit=True)

    async def _add(request: Request, p: dict[str, Any]) -> Any:
        _guard_local(request, p, "local_path")
        return await rpc(request, "filehub.add", p, audit=True)

    def _setup_build(f: Any) -> dict[str, Any]:
        # KHÔNG có `binary`: web không bao giờ nhận đường dẫn binary sync (daemon tự dò — filehub.detect_binary)
        scope = str(f.get("scope") or "workspace")
        if scope not in ("workspace", "folder"):
            raise ValueError("scope phải là workspace|folder")
        out: dict[str, Any] = {"server": _server(f), "scope": scope, **_local_path(f, "local_parent")}
        if str(f.get("name") or "").strip() or scope == "folder":
            out["name"] = _name(f, default="cfharness")
        return out

    def _scope_text(f: Any) -> str:
        return ("TOÀN BỘ workspace (chọn workspace ở trang đồng ý; tài khoản phải là Workspace Admin)" if str(f.get("scope") or "workspace") == "workspace"
                else f"MỘT thư mục «{str(f.get('name') or 'cfharness').strip()}» (chọn/tạo ở trang đồng ý, nơi bạn là Chủ sở hữu)")

    goto_login = lambda r, f: "/filehub/login"  # noqa: E731
    actions["filehub_pause"] = Action("filehub.pause", lambda f: {}, "Đã tạm dừng đồng bộ Không gian số", call=simple("filehub.pause"),
                                      confirm="Tạm dừng đồng bộ với Không gian số?\nTệp người sửa trên web sẽ không về máy (và ngược lại) tới khi chạy lại.",
                                      goto=lambda r, f: NEXT)
    actions["filehub_resume"] = Action("filehub.resume", lambda f: {}, "Đã chạy lại đồng bộ", call=simple("filehub.resume"), goto=lambda r, f: NEXT)
    actions["filehub_resume_push"] = Action("filehub.resume", lambda f: {"root_id": _req(f, "root_id")}, "Đã cho đẩy lên tiếp",
                                            call=simple("filehub.resume"),
                                            confirm="Cho tiến trình đồng bộ đẩy tiếp các thay đổi (gồm các tệp đã xoá) lên Không gian số?",
                                            goto=lambda r, f: NEXT)
    actions["filehub_setup"] = Action(
        "filehub.setup", _setup_build,
        "Đã tạo link đồng ý — mở trang đồng ý, đăng nhập rồi bấm Cho phép; thư mục chung được nối tự động khi xong.", call=_setup, goto=goto_login,
        confirm=lambda f: (f"Nối hub với Không gian số {str(f.get('server') or '').strip()}?\nChế độ: {_scope_text(f)}.\nBước tiếp theo: mở link "
                           "đồng ý, đăng nhập tài khoản Không gian số rồi bấm Cho phép."),
        step2=lambda f: ("Hub sẽ chạy tiến trình đồng bộ 2 chiều dưới tài khoản Không gian số bạn đồng ý (tệp agent ghi mang tên tài khoản đó). "
                         + ("Chế độ workspace: agent được giao việc ghi được MỌI thư mục của workspace, kể cả thư mục người khác tạo sau — "
                            "lưới an toàn là phiên bản + thùng rác Không gian số và báo sau mỗi run." if str(f.get("scope") or "workspace") == "workspace"
                            else "Chỉ thư mục được chọn ở trang đồng ý được đồng bộ.")
                         + " Xoá của agent không cần duyệt (khôi phục ở thùng rác Không gian số)."),
        danger=True)
    actions["filehub_login"] = Action(
        "filehub.login", lambda f: {}, "Đã tạo link đồng ý thiết bị mới — mở trang đồng ý để đăng nhập lại", call=_login, goto=goto_login,
        confirm="Đăng nhập lại Không gian số cho máy chạy hub? (tạo link đồng ý thiết bị mới — chế độ một thư mục: cấp thêm thư mục được)",
        step2="Đăng nhập cấp token MỚI cho tiến trình đồng bộ của hub. Đăng nhập bằng tài khoản có quyền với thư mục chung "
              "(chế độ workspace: tài khoản admin của workspace).", danger=True)
    actions["filehub_switch"] = Action(
        "filehub.switch_to_workspace", lambda f: {}, "Đã bắt đầu chuyển sang toàn bộ workspace", call=_switch, goto=goto_login,
        confirm="Chuyển thư mục chung sang TOÀN BỘ workspace?",
        step2="Sau khi bạn đồng ý ở Không gian số: thư mục chung cũ được gỡ khỏi hub (tệp trên máy và trên web GIỮ NGUYÊN), hub đồng bộ cả "
              "workspace về một bản sao MỚI. Agent được giao việc ghi được mọi thư mục của workspace. Kiểm bản mới rồi tự xoá bản cũ; thu hồi "
              "thiết bị cũ ở Không gian số › Hồ sơ › Thiết bị.", danger=True)
    actions["filehub_add"] = Action(
        "filehub.add", lambda f: {"root_id": _req(f, "root_id"), "name": _name(f), **_local_path(f, "local_path")},
        lambda r, f: f"Đã thêm thư mục chung «{(r or {}).get('name')}» → {(r or {}).get('local_path')} (giao việc: workspace {(r or {}).get('workspace')})",
        call=_add, goto=lambda r, f: NEXT,
        confirm=lambda f: f"Thêm thư mục «{str(f.get('name') or '').strip()}» làm thư mục chung (đồng bộ 2 chiều về máy chạy hub)?")
    actions["filehub_remove"] = Action(
        "filehub.remove", lambda f: {"name": _req(f, "name")}, lambda r, f: f"Đã gỡ «{f.get('name')}» khỏi hub (tệp giữ nguyên)",
        call=simple("filehub.remove"), confirm=lambda f: f"Gỡ thư mục chung «{f.get('name')}» khỏi hub?",
        step2=lambda f: (f"Hub dừng đồng bộ «{f.get('name')}», agent không còn giao việc vào workspace filehub:{f.get('name')} được. Tệp trên máy "
                         "và trên Không gian số GIỮ NGUYÊN (không xoá gì)."), danger=True, goto=lambda r, f: NEXT)
    # ---- cổng cfharness (chế độ lane): khoá CHỈ nhập ở bước 2 (ô password trống, không qua trang xác nhận/carry, không vào HTML/log/audit)
    def _lane_setup_build(f: Any) -> dict[str, Any]:
        server = str(f.get("server") or "").strip().lower()
        url = str(f.get("url") or "").strip()
        from ..filehub.lane.client import SERVER_RE, LaneError, check_url

        if not SERVER_RE.match(server):
            raise ValueError("tên cổng: chữ thường/số/_ (vd qltm)")
        try:
            url = check_url(url)
        except LaneError as e:
            raise ValueError(e.message) from None
        p: dict[str, Any] = {"server": server, "url": url}
        if f.get("confirm2") == "yes":
            key = str(f.get("key") or "").strip()
            if not key:
                raise ValueError("nhập khoá cổng cfharness (tt_lane_…)")
            p["key"] = key
        return p

    actions["filehub_lane_setup"] = Action(
        "filehub.lane_setup", _lane_setup_build, lambda r, f: f"Đã lưu khoá cổng «{(r or {}).get('server')}» ({(r or {}).get('key_prefix')}…) — tài khoản {(r or {}).get('user')}",
        confirm=lambda f: f"Nối hub với cổng cfharness {str(f.get('url') or '').strip()} (tên «{str(f.get('server') or '').strip()}»)?",
        step2=("Dán khoá cổng tạo ở Không gian số › Hồ sơ › Cổng cfharness. Khoá cho hub làm việc với thư mục được cấp DƯỚI TÊN TÀI KHOẢN CỦA BẠN "
               "(agent không bao giờ thấy khoá; chỉ daemon dùng, chỉ gửi tới 127.0.0.1). Hub lưu khoá trong kho secret (0600)."),
        reenter=lambda f: [("key", "Khoá cổng cfharness (tt_lane_…)", "password")], secret_fields=("key",), danger=True,
        goto=lambda r, f: NEXT)
    actions["filehub_lane_disable"] = Action(
        "filehub.lane_disable", lambda f: {"server": _req(f, "server")}, lambda r, f: f"Đã xoá khoá cổng «{f.get('server')}»",
        confirm=lambda f: f"Xoá khoá + cấu hình cổng «{f.get('server')}»? Run trên thư mục chế độ lane sẽ không chạy được tới khi nhập khoá mới.",
        danger=True, goto=lambda r, f: NEXT)
    actions["filehub_switch_mode"] = Action(
        "filehub.switch", lambda f: {"name": _req(f, "name"), "to": "lane" if f.get("to") == "lane" else "sync"},
        lambda r, f: f"Đã chuyển «{(r or {}).get('name')}» sang chế độ {(r or {}).get('mode')}" + (f" — {(r or {}).get('note')}" if (r or {}).get("note") else ""),
        confirm=lambda f: (f"Chuyển thư mục «{f.get('name')}» sang CỔNG CFHARNESS (không đồng bộ về máy nữa)?" if f.get("to") == "lane"
                           else f"Đưa thư mục «{f.get('name')}» về ĐỒNG BỘ 2 CHIỀU cũ?"),
        step2=lambda f: ("Đồng bộ 2 chiều của thư mục này dừng; agent làm việc qua cây dựng cho từng run (mỗi lần lưu lên Không gian số ngay). "
                         "Bản sao cũ trên máy GIỮ NGUYÊN — hub không xoá; kiểm rồi tự xoá. Từ chối nếu đồng bộ còn chưa yên."
                         if f.get("to") == "lane" else "Hub đồng bộ lại thư mục về bản sao cũ trên máy (có thể cũ hơn Không gian số — đồng bộ đối chiếu lại)."),
        danger=True, goto=lambda r, f: NEXT)
    actions["filehub_lane_orphan"] = Action(
        "filehub.lane_orphan", lambda f: {"tree": _req(f, "tree"), "action": "drop" if f.get("action") == "drop" else "push"},
        lambda r, f: ("Đã bỏ thay đổi chưa đẩy" if (r or {}).get("dropped") else ("Đã đẩy hết lên Không gian số" if (r or {}).get("pushed")
                      else f"Còn {len((r or {}).get('left') or [])} thay đổi chưa đẩy được ({(r or {}).get('code')})")),
        confirm=lambda f: ("BỎ hẳn các thay đổi chưa đẩy lên của run này (xoá bản trên máy)?" if f.get("action") == "drop"
                           else "Đẩy lại các thay đổi còn kẹt lên Không gian số? (tệp chứa secret vẫn bị chặn)"),
        danger=True, goto=lambda r, f: NEXT)
    actions["filehub_lane_deletes"] = Action(
        "filehub.resume", lambda f: {"root_id": _req(f, "root_id"), **({"discard": True} if f.get("discard") else {})},
        lambda r, f: (f"Đã KHÔNG xoá {(r or {}).get('discarded', 0)} mục — tệp được trả lại cây làm việc của agent" if f.get("discard")
                      else f"Đã cho xoá {(r or {}).get('released', 0)} mục (vào thùng rác Không gian số)"),
        call=simple("filehub.resume"),
        confirm=lambda f: ("KHÔNG cho agent xoá các mục đang giữ? Tệp ở lại Không gian số và được trả lại cây làm việc của agent." if f.get("discard")
                           else "Cho agent xoá các mục đang giữ trên Không gian số? (vào thùng rác, khôi phục được)"),
        goto=lambda r, f: NEXT)
    actions["filehub_rename"] = Action(
        "filehub.rename", lambda f: {"name": _req(f, "name"), "new_name": _name(f, "new_name")},
        lambda r, f: f"Đã đổi tên «{(r or {}).get('previous')}» → «{(r or {}).get('name')}» (giao việc: workspace {(r or {}).get('workspace')})",
        call=simple("filehub.rename"), goto=lambda r, f: NEXT,
        confirm=lambda f: (f"Đổi tên thư mục chung «{f.get('name')}» thành «{str(f.get('new_name') or '').strip()}» trong cfharness?\n"
                           "Chỉ đổi tên dùng để giao việc (filehub:<tên>) — đường dẫn trên máy và tên trên Không gian số giữ nguyên; tên cũ vẫn khớp."))


def routes() -> list[Any]:
    return [Route("/partials/filehub", partial_filehub), Route("/partials/filehub/lane", partial_lane), Route("/partials/filehub/login", partial_login), Route("/filehub/login", login_page),
            Route(CALLBACK_PATH, oauth_callback), Route("/filehub/open", filehub_open)]


__all__ = ["register_actions", "routes", "STATE_VIEW", "CALLBACK_PATH"]
