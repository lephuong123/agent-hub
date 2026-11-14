"""Nút "Cập nhật hệ thống" (updater.py) — thẻ ở đầu trang Cài đặt + trang theo dõi `/update`.

- `/partials/update` (htmx `load`; tự poll 3 s khi đang cập nhật): RPC đọc `update.check` (daemon cache 10 phút) + `update.status`. Daemon
  đang dừng để cài đè → thẻ "đang khởi động lại…" và tiếp tục poll (không lỗi).
- `/update`: trang chỉ có thẻ trên — đích sau khi bấm nút, vì trang Cài đặt cần daemon mà daemon sắp dừng.
- Thao tác `update_start` / `update_start_force` qua `ACTIONS` (CSRF + khoá phiên WT-1, `_audit.channel=web`, luôn có hộp xác nhận);
  `update.start` ∈ HUMAN_ONLY và tự ghi audit.
Web chỉ là client RPC — không đọc file trạng thái trực tiếp. Bản web đang chạy khác bản daemon → nhắc mở lại `cfharness web`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from .. import __version__
from ..rpc import DaemonUnavailable, RpcError
from .app import render, rpc, session_of, ws

PHASES = {"starting": "Chuẩn bị", "waiting_daemon": "Chờ daemon dừng", "installing": "Đang tải và cài bản mới",
          "restarting": "Khởi động lại daemon", "health": "Chờ daemon trả lời", "rolling_back": "Quay về bản trước",
          "rollback_restarting": "Khởi động lại bản trước", "verifying": "Kiểm tra phiên bản", "finished": "Xong"}


async def _ctx(request: Request, refresh: bool = False) -> dict[str, Any]:
    try:
        chk = await rpc(request, "update.check", {"refresh": refresh})
        stat = await rpc(request, "update.status", {"lines": 30})
        hs = await rpc(request, "hub.status") or {}
    except (DaemonUnavailable, OSError):
        return {"down": True, "web_version": __version__}
    except RpcError as e:
        return {"error": e.message, "web_version": __version__}
    st = dict(stat.get("state") or {})
    for k in ("started_at", "finished_at", "updated_at"):  # helper ghi epoch giây → ISO cho bộ lọc `ago`/`iso` của template
        if isinstance(st.get(k), (int, float)):
            st[k] = datetime.fromtimestamp(st[k], timezone.utc).isoformat()
    return {"chk": chk, "st": st, "busy": bool(stat.get("in_progress")), "log_tail": stat.get("log_tail") or "",
            "phase": PHASES.get(str(st.get("phase") or ""), st.get("phase")), "web_version": __version__,
            "web_stale": chk.get("current") != __version__, "runs": int(hs.get("running") or 0) + int(hs.get("queued") or 0)}


async def partial_update(request: Request) -> Response:
    ctx = await _ctx(request, refresh=request.query_params.get("refresh") == "1")
    sess = session_of(request)
    return ws(request).templates.TemplateResponse(request, "_update.html", {**ctx, "csrf": sess.csrf if sess else "",
                                                                            "full": request.query_params.get("full") == "1"})


async def update_page(request: Request) -> Response:
    return await render(request, "update.html", {"nav": "settings"})


def _start_confirm(f: Any) -> str:
    base = (f"Cập nhật cfharness lên {f.get('tag') or 'bản mới nhất'}?\nDaemon sẽ dừng khoảng 1 phút để cài bản mới rồi tự chạy lại."
            "\nTrước khi cài, hub tạo một bản sao lưu; daemon không lên được thì tự quay về bản đang chạy.")
    if f.get("force") == "1":
        base += "\nRun đang chạy sẽ bị NGẮT (tự tiếp tục sau khi daemon chạy lại)."
    return base


def register_actions(actions: dict[str, Any], Action: Any, _req: Any) -> None:
    def ok(r: Any, f: Any) -> str:
        return f"Đang cập nhật lên {(r or {}).get('tag')} — daemon dừng khoảng 1 phút rồi tự chạy lại; trang này tự cập nhật trạng thái"

    actions["update_start"] = Action("update.start", lambda f: {"force": f.get("force") == "1", "tag": str(f.get("tag") or "").strip() or None}, ok, confirm=_start_confirm,
                                     danger=True, goto=lambda r, f: "/update")


def routes() -> list[Any]:
    return [Route("/update", update_page), Route("/partials/update", partial_update)]


__all__ = ["register_actions", "routes"]
