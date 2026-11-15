"""Thẻ "Không gian số" ở Cài đặt (filehub/) — `/partials/filehub` (htmx `load`) + `/filehub/open` (link "Mở trên Không gian số").

- Đọc: RPC `filehub.status` (trạng thái đồng bộ, thư mục chung, bản xung đột, khoá run). Daemon không trả lời → thẻ báo, không lỗi trang.
- Ghi: `filehub_pause|filehub_resume|filehub_resume_push` qua `ACTIONS` (CSRF + khoá phiên WT-1, audit `web`) — CHỈ khi dùng web trên chính
  máy chạy hub (`is_local_request`); qua link public (cloud) thẻ chỉ để xem (quyết định 1 của plan-cloud-files). Thiết lập/đăng nhập/thêm/gỡ
  thư mục chỉ ở terminal (`cfharness filehub …`).
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


def _local_only(fn: Any) -> Any:
    async def call(request: Request, p: dict[str, Any]) -> Any:
        from .file_pages import is_local_request

        if not is_local_request(request):
            raise RpcError(-32003, "Thao tác thư mục chung chỉ làm được khi dùng web trên chính máy chạy hub (hoặc `cfharness filehub …` trong "
                                   "terminal). Qua link public chỉ xem trạng thái.")
        return await fn(request, p)

    return call


def register_actions(actions: dict[str, Any], Action: Any, _req: Any) -> None:
    async def _pause(request: Request, p: dict[str, Any]) -> Any:
        return await rpc(request, "filehub.pause", {}, audit=True)

    async def _resume(request: Request, p: dict[str, Any]) -> Any:
        return await rpc(request, "filehub.resume", {}, audit=True)

    async def _push(request: Request, p: dict[str, Any]) -> Any:
        return await rpc(request, "filehub.resume", {"root_id": p.get("root_id")}, audit=True)

    actions["filehub_pause"] = Action("filehub.pause", lambda f: {}, "Đã tạm dừng đồng bộ Không gian số", call=_local_only(_pause),
                                      confirm="Tạm dừng đồng bộ với Không gian số?\nTệp người sửa trên web sẽ không về máy (và ngược lại) tới khi chạy lại.",
                                      goto=lambda r, f: "/settings#filehub")
    actions["filehub_resume"] = Action("filehub.resume", lambda f: {}, "Đã chạy lại đồng bộ", call=_local_only(_resume),
                                       goto=lambda r, f: "/settings#filehub")
    actions["filehub_resume_push"] = Action("filehub.resume", lambda f: {"root_id": _req(f, "root_id")}, "Đã cho đẩy lên tiếp", call=_local_only(_push),
                                            confirm="Cho tiến trình đồng bộ đẩy tiếp các thay đổi (gồm các tệp đã xoá) lên Không gian số?",
                                            goto=lambda r, f: "/settings#filehub")


def routes() -> list[Any]:
    return [Route("/partials/filehub", partial_filehub), Route("/filehub/open", filehub_open)]


__all__ = ["register_actions", "routes", "STATE_VIEW"]
