"""Thẻ "Không gian số" ở Cài đặt (filehub/) — `/partials/filehub` (htmx `load`) + `/filehub/open` (link "Mở trên Không gian số").

- Đọc: RPC `filehub.status` (trạng thái đồng bộ, thư mục chung, bản xung đột, khoá run). Daemon không trả lời → thẻ báo, không lỗi trang.
- Ghi qua `ACTIONS` (CSRF + Origin + khoá phiên WT-1, audit `web`, RPC `filehub.*` ∈ HUMAN_ONLY):
  `filehub_setup` (máy chủ + tên thư mục → đăng nhập thiết bị), `filehub_login` (đăng nhập lại), `filehub_add` (thêm thư mục đã cấp lúc đăng
  nhập), `filehub_remove`, `filehub_pause|filehub_resume|filehub_resume_push`. Dùng được cả qua link public (cloud): người dùng hub trên máy
  dùng chung tự đăng nhập Không gian số + nối thư mục làm việc, không cần admin ngồi ở máy (docs/dev-notes/filehub.md § Web).
  Giới hạn: web KHÔNG BAO GIỜ nhận đường dẫn binary sync (daemon dùng cấu hình hoặc tự dò ở chỗ cài cố định — `filehub.detect_binary`);
  đường dẫn bản sao cục bộ chỉ đặt được khi dùng web trên chính máy chạy hub (`is_local_request`), qua cloud dùng `<local_parent>/<tên>`;
  link đồng ý thiết bị luôn `no_browser` (không mở trình duyệt trên máy chủ) — thẻ hiện link + poll tới khi người bấm đồng ý.
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

STATE_VIEW = {"idle": ("đã đồng bộ", "ok"), "syncing": ("đang đồng bộ", "info"), "conflict": ("có bản xung đột", "warn"), "offline": ("mất kết nối", "warn"),
              "auth_expired": ("hết phiên đăng nhập", "danger"), "error": ("lỗi", "danger"), "paused": ("tạm dừng", "warn"),
              "not_configured": ("chưa thiết lập", ""), "version_mismatch": ("binary sync sai phiên bản", "danger"), "starting": ("đang khởi động", "info")}


async def partial_filehub(request: Request) -> Response:
    from .file_pages import is_local_request

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
                                                                             "local": is_local_request(request)})


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
    from .file_pages import is_local_request

    if any(p.get(k) for k in keys) and not is_local_request(request):
        raise RpcError(-32003, LOCAL_ONLY_MSG)


def register_actions(actions: dict[str, Any], Action: Any, _req: Any) -> None:
    def simple(method: str) -> Any:
        async def call(request: Request, p: dict[str, Any]) -> Any:
            return await rpc(request, method, p, audit=True)

        return call

    async def _setup(request: Request, p: dict[str, Any]) -> Any:
        _guard_local(request, p, "local_parent", "local_path")
        return await rpc(request, "filehub.setup", {**p, "no_browser": True}, audit=True)

    async def _login(request: Request, p: dict[str, Any]) -> Any:
        return await rpc(request, "filehub.login", {"no_browser": True}, audit=True)

    async def _add(request: Request, p: dict[str, Any]) -> Any:
        _guard_local(request, p, "local_path")
        return await rpc(request, "filehub.add", p, audit=True)

    def _login_ok(r: Any, f: Any) -> str:
        return ("Đã tạo link đồng ý thiết bị — bấm «Mở trang đồng ý» ở thẻ Không gian số, đăng nhập, chọn/tạo thư mục rồi bấm Cho phép. "
                "Thẻ tự cập nhật khi xong.")

    actions["filehub_setup"] = Action(
        "filehub.setup", lambda f: {"server": _server(f), "name": _name(f, default="cfharness"), **_local_path(f, "local_parent")}, _login_ok,
        call=_setup, goto=lambda r, f: NEXT,
        confirm=lambda f: (f"Nối hub với Không gian số {str(f.get('server') or '').strip()}?\nBước tiếp theo: mở link đồng ý, đăng nhập tài khoản "
                           f"Không gian số, chọn thư mục (nơi bạn là Chủ sở hữu) cho «{str(f.get('name') or 'cfharness').strip()}». Thư mục đồng bộ 2 "
                           "chiều với bản sao trên máy chạy hub; agent làm việc trong workspace filehub:<tên>, xoá của agent không cần duyệt (khôi "
                           "phục ở thùng rác Không gian số)."))
    actions["filehub_login"] = Action("filehub.login", lambda f: {}, _login_ok, call=_login, goto=lambda r, f: NEXT,
                                      confirm="Đăng nhập lại Không gian số cho máy chạy hub? (tạo link đồng ý thiết bị mới)")
    actions["filehub_add"] = Action(
        "filehub.add", lambda f: {"root_id": _req(f, "root_id"), "name": _name(f), **_local_path(f, "local_path")},
        lambda r, f: f"Đã thêm thư mục chung «{(r or {}).get('name')}» → {(r or {}).get('local_path')} (giao việc: workspace {(r or {}).get('workspace')})",
        call=_add, goto=lambda r, f: NEXT,
        confirm=lambda f: f"Thêm thư mục «{str(f.get('name') or '').strip()}» làm thư mục chung (đồng bộ 2 chiều về máy chạy hub)?")
    actions["filehub_remove"] = Action(
        "filehub.remove", lambda f: {"name": _req(f, "name")}, lambda r, f: f"Đã gỡ «{(r or {}).get('removed')}» — {(r or {}).get('note')}",
        call=simple("filehub.remove"), goto=lambda r, f: NEXT, danger=True,
        confirm=lambda f: (f"Gỡ thư mục chung «{f.get('name')}» khỏi hub?\nNgừng đồng bộ thư mục này; tệp trên máy và trên Không gian số được giữ "
                           "nguyên. Agent không dùng được workspace filehub:" + str(f.get("name") or "") + " nữa."))
    actions["filehub_pause"] = Action("filehub.pause", lambda f: {}, "Đã tạm dừng đồng bộ Không gian số", call=simple("filehub.pause"),
                                      confirm="Tạm dừng đồng bộ với Không gian số?\nTệp người sửa trên web sẽ không về máy (và ngược lại) tới khi chạy lại.",
                                      goto=lambda r, f: NEXT)
    actions["filehub_resume"] = Action("filehub.resume", lambda f: {}, "Đã chạy lại đồng bộ", call=simple("filehub.resume"), goto=lambda r, f: NEXT)
    actions["filehub_resume_push"] = Action("filehub.resume", lambda f: {"root_id": _req(f, "root_id")}, "Đã cho đẩy lên tiếp",
                                            call=simple("filehub.resume"),
                                            confirm="Cho tiến trình đồng bộ đẩy tiếp các thay đổi (gồm các tệp đã xoá) lên Không gian số?",
                                            goto=lambda r, f: NEXT)


def routes() -> list[Any]:
    return [Route("/partials/filehub", partial_filehub), Route("/filehub/open", filehub_open)]


__all__ = ["register_actions", "routes", "STATE_VIEW"]
