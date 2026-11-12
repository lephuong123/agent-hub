"""Xem / mở file agent đã thao tác — chip file trong diễn biến run và khối "Thay đổi file" (docs/dev-notes/file-open.md).

Tiến trình web KHÔNG đọc file: mọi nội dung đi qua RPC `file.read` của daemon (HUMAN_ONLY, tự audit), mở app qua `file.open`.
- `GET /files/view?run=<id>|agent=<id>&path=<p>` — trình xem chỉ đọc (cả máy cục bộ lẫn web public): văn bản/mã escape + số dòng
  (≤ `fileview.VIEW_MAX`, quá thì báo "đã cắt"), ảnh hiện inline, PDF mở inline/tải về, nhị phân khác chỉ có nút tải về.
- `GET /files/raw?…[&dl=1]` — byte gốc (≤ `fileview.RAW_MAX`). Inline CHỈ cho ảnh raster + PDF; SVG inline kèm CSP `sandbox` (không
  chạy script); mọi loại khác `Content-Disposition: attachment` + `application/octet-stream`. Luôn `X-Content-Type-Options: nosniff`.
- `POST /files/open` (CSRF + khoá phiên WT-1 qua `post_guard`) — CHỈ khi request là CỤC BỘ (`is_local_request`): mở bằng app mặc định
  của OS hoặc mở thư mục chứa file (`reveal=1`). Web public → 403 (mở trên máy chủ thì không ai thấy).

"Cục bộ" = kết nối tới từ loopback VÀ Host là host cục bộ của web (không phải `web.public_origin`) VÀ không có header do proxy/tunnel
thêm (`X-Forwarded-For`, `Forwarded`, `CF-Connecting-IP`, …). Header client tự gửi chỉ có thể làm request bị coi là KHÔNG cục bộ
(hạ quyền), không bao giờ ngược lại.
"""

from __future__ import annotations

import base64
from typing import Any
from urllib.parse import urlencode

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from ..rpc import METHOD_NOT_FOUND, DaemonUnavailable, RpcError
from .security import allowed_hosts, is_loopback

PROXY_HEADERS = ("x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "forwarded", "x-real-ip", "cf-connecting-ip", "cf-ray",
                 "true-client-ip", "x-client-ip", "fly-client-ip", "tailscale-user-login")
INLINE_RASTER = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp", "image/x-icon", "image/avif"}
RAW_CSP = "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; sandbox"
LOCAL_TIP = "Mở bằng ứng dụng mặc định trên máy này (chỉ khi dùng web trên chính máy chạy hub)"
CLOUD_TIP = "Xem trong trình duyệt (chỉ đọc)"


def is_local_request(request: Request) -> bool:
    """Web đang được dùng trên CHÍNH máy chạy hub (không qua tunnel)?"""
    from .app import ws

    st = ws(request)
    host = (request.headers.get("host") or "").lower()
    if st.is_public(host) or host not in allowed_hosts(st.name, st.port):
        return False
    peer = request.client.host if request.client else ""
    if not is_loopback(peer):
        return False
    return not any(h in request.headers for h in PROXY_HEADERS)


def file_params(q: Any) -> dict[str, Any]:
    """Tham số RPC từ query/form: `run` hoặc `agent` + `path`. Gốc KHÔNG bao giờ lấy từ client (daemon tự suy ra)."""
    path = str(q.get("path") or "")
    run, agent = str(q.get("run") or "").strip(), str(q.get("agent") or "").strip()
    if not path or not (run or agent):
        raise HTTPException(400, "Thiếu tham số: cần run (hoặc agent) và path")
    if len(path) > 4096 or len(run) > 200 or len(agent) > 200:
        raise HTTPException(400, "Tham số quá dài")
    p: dict[str, Any] = {"path": path}
    if run:
        p["run_id"] = run
    else:
        p["agent_id"] = agent
    return p


def qs(p: dict[str, Any], **extra: Any) -> str:
    q = {"run": p.get("run_id"), "agent": p.get("agent_id"), "path": p["path"], **extra}
    return urlencode({k: v for k, v in q.items() if v})


def _no_daemon_method(e: Exception) -> bool:
    return isinstance(e, RpcError) and e.code == METHOD_NOT_FOUND


async def file_view(request: Request) -> Response:
    from . import app as A
    from ..fileview import VIEW_MAX

    p = file_params(request.query_params)
    info = await A.rpc(request, "file.read", {**p, "max_bytes": VIEW_MAX}, audit=True)
    data = base64.b64decode(info.pop("data_b64", "") or "")
    try:
        from datetime import datetime

        info["mtime_s"] = datetime.fromtimestamp(float(info.get("mtime") or 0)).strftime("%d/%m/%Y %H:%M") if info.get("mtime") else ""
    except (TypeError, ValueError, OSError):
        info["mtime_s"] = ""
    lines: list[str] = []
    if info.get("kind") == "text":
        text = data.decode("utf-8", errors="replace")
        lines = text.split("\n")
        if lines and lines[-1] == "" and len(lines) > 1:
            lines.pop()
    return await A.render(request, "file_view.html", {
        "nav": "runs" if p.get("run_id") else "agents", "f": info, "lines": lines, "q": qs(p), "p": p, "local": is_local_request(request),
        "view_max": VIEW_MAX})


async def file_raw(request: Request) -> Response:
    from . import app as A
    from ..fileview import RAW_MAX

    p = file_params(request.query_params)
    info = await A.rpc(request, "file.read", {**p, "max_bytes": RAW_MAX, "whole": True}, audit=True)
    data = base64.b64decode(info.get("data_b64") or "")
    mime, kind = str(info.get("mime") or ""), info.get("kind")
    name = str(info.get("name") or "file")
    dl = request.query_params.get("dl") == "1"
    headers = {"x-content-type-options": "nosniff", "cache-control": "no-store", "cross-origin-resource-policy": "same-origin"}
    ascii_name = "".join(ch if 32 < ord(ch) < 127 and ch not in '"\\;' else "_" for ch in name) or "file"
    from urllib.parse import quote

    disp_name = f'filename="{ascii_name}"; filename*=UTF-8\'\'{quote(name, safe="")}'
    if not dl and kind == "image" and mime in INLINE_RASTER:
        ctype, disp = mime, "inline"
    elif not dl and mime == "image/svg+xml":
        ctype, disp = mime, "inline"
        headers["content-security-policy"] = RAW_CSP  # SVG có thể chứa <script> — sandbox: không chạy script, không cùng origin
    elif not dl and kind == "pdf":
        ctype, disp = "application/pdf", "inline"
    else:
        ctype, disp = "application/octet-stream", "attachment"
    if disp == "attachment" and "content-security-policy" not in headers:
        headers["content-security-policy"] = RAW_CSP
    headers["content-disposition"] = f"{disp}; {disp_name}"
    return Response(data, media_type=ctype, headers=headers)


async def file_open(request: Request) -> Response:
    from . import app as A

    _, form = await A.post_guard(request)
    want_json = "application/json" in (request.headers.get("accept") or "")
    back = A.safe_next(form.get("next"), "/")

    def out(ok: bool, msg: str, code: int = 200) -> Response:
        if want_json:
            return JSONResponse({"ok": ok, "message": msg}, status_code=code)
        if code >= 400 and code != 400:
            raise HTTPException(code, msg)
        return A.flash_redirect(request, back, "ok" if ok else "error", msg)

    if not is_local_request(request):
        return out(False, "Chỉ mở được bằng ứng dụng khi dùng web trên CHÍNH máy chạy hub. Qua link public hãy dùng \"Xem\" hoặc tải về.", 403)
    p = file_params(form)
    reveal = form.get("reveal") in ("1", "true", "yes")
    try:
        res = await A.rpc(request, "file.open", {**p, "reveal": reveal}, audit=True) or {}
    except (RpcError, DaemonUnavailable) as e:
        if _no_daemon_method(e):
            return out(False, A.err_text(e), 503)
        return out(False, A.err_text(e), 400)
    name = str(res.get("path") or p["path"]).replace("\\", "/").rsplit("/", 1)[-1]
    return out(True, ("Đã mở thư mục chứa " if reveal else "Đã mở ") + name + " trên máy này")


def routes() -> list[Any]:
    return [
        Route("/files/view", file_view),
        Route("/files/raw", file_raw),
        Route("/files/open", file_open, methods=["POST"]),
    ]


__all__ = ["routes", "is_local_request", "file_params", "PROXY_HEADERS", "LOCAL_TIP", "CLOUD_TIP"]
