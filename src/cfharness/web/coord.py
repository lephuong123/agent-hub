"""Island trang Điều phối (GĐ2 phương án B — `docs/dev-notes/dieu-phoi-island.md`): `GET /manager?ui=next`.

Trang = khung `base.html` (thanh bên, menu, panel xem file, hộp xác nhận) + MỘT điểm gắn `#coord` do island Preact vẽ
(`static/coord/*.js`, không bước build — template `htm`). Dữ liệu đầu tiên là ảnh chụp v2 (`manager_feed.build_snapshot`) nhúng trong
`<script type="application/json">` (khối dữ liệu, không chạy → hợp CSP); sau đó island chỉ dùng hợp đồng v2 (`/api/manager/*`,
`/sse/manager/v2`) + `POST /manager/send|stop|read` sẵn có.

Thư viện ESM vendored ở `static/vendor/` (phiên bản ghim, xem `static/vendor/README.md`) nạp qua import map. Import map là script NỘI TUYẾN
(trình duyệt không nhận import map ngoài) → CSP của RIÊNG trang này thêm `'sha256-…'` của đúng nội dung đó; mọi trang khác giữ
`script-src 'self'`. Mỗi URL trong import map mang `?v=` (`static_url`) → Cache-Control immutable; module của island import nhau bằng
đường dẫn tương đối, import map ánh xạ lại từng URL `/static/coord/<tệp>.js` sang bản có `?v=` (đổi tệp là đổi URL).
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from .app import CSP, HERE, render, session_of, static_url

# bare specifier → tệp trong static/ (phiên bản nằm trong tên thư mục; đổi phiên bản = thêm thư mục mới, sửa ở đây và README)
VENDOR = {
    "preact": "vendor/preact-11.1.1/preact.mjs",
    "preact/hooks": "vendor/preact-11.1.1/hooks.mjs",
    "@preact/signals-core": "vendor/preact-signals-core-1.14.5/signals-core.mjs",
    "@preact/signals": "vendor/preact-signals-2.11.4/signals.mjs",
    "htm": "vendor/htm-3.1.1/htm.module.js",
    "streaming-markdown": "vendor/streaming-markdown-0.2.15/smd.min.js",
    "dompurify": "vendor/dompurify-3.4.18/purify.es.mjs",
}
ISLAND_DIR = "coord"
ENTRY = "coord/main.js"


def import_map() -> str:
    """JSON import map (chuỗi CỐ ĐỊNH theo mtime tệp — băm CSP tính trên đúng chuỗi này)."""
    imports = {k: static_url(v) for k, v in VENDOR.items()}
    for f in sorted((HERE / "static" / ISLAND_DIR).glob("*.js")):
        rel = f"{ISLAND_DIR}/{f.name}"
        imports[f"/static/{rel}"] = static_url(rel)
    return json.dumps({"imports": imports}, separators=(",", ":"), sort_keys=True)


def csp_with(script: str) -> str:
    """CSP chung + băm sha256 của MỘT script nội tuyến (import map)."""
    h = base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode()
    return CSP.replace("script-src 'self'", f"script-src 'self' 'sha256-{h}'", 1)


async def coord_page(request: Request) -> Response:
    from .manager import _page
    from .manager_feed import build_snapshot, hub_flags

    ctx, snap = await build_snapshot(request)
    if "setup" in snap:  # chưa có điều phối viên / daemon cũ: trang cũ vẽ phần chỉ định (form + xác nhận) — island chỉ cho trang chat
        return await _page(request, ctx)
    sess = session_of(request)
    imap = import_map()
    # luồng đang xem (tiêu đề nhóm "Hội thoại · <luồng>" vẽ sẵn ở thanh bên) — tính ở đây: trong Jinja `first` bị ghi đè bằng P.first_line
    sc = snap.get("scope")
    wsb = snap.get("workstreams") or {}
    wst = next((t for t in [*(wsb.get("active") or []), *(wsb.get("closed") or [])] if t.get("ws") == sc), None) if sc and sc != "_" else None
    boot: dict[str, Any] = {"snap": snap, "csrf": sess.csrf if sess else "", "icons": static_url("icons.svg"), "hub": await hub_flags(request)}
    resp = await render(request, "manager_next.html", {"nav": "manager", "boot": boot, "importmap": imap, "entry": static_url(ENTRY),
                                                       "title": snap.get("title"), "wst": wst})
    resp.headers["content-security-policy"] = csp_with(imap)
    resp.headers["cache-control"] = "no-store"
    return resp


_MIN: dict[str, tuple[float, bytes]] = {}  # tên tệp → (mtime, bản đã thu gọn)


async def coord_js(request: Request) -> Response:
    """`/static/coord/<tệp>.js` — mã island ĐÃ THU GỌN (`jsmin`: bỏ chú thích/thụt lề, ngữ nghĩa giữ nguyên) thay vì tệp nguồn nguyên văn.
    Route này đứng TRƯỚC mount `/static` (StaticFiles). Cache theo mtime; Cache-Control do GuardMiddleware gắn (`?v=` → immutable)."""
    from .jsmin import minify

    name = request.path_params["name"]
    f = HERE / "static" / ISLAND_DIR / name
    if not name.endswith(".js") or "/" in name or "\\" in name or name.startswith(".") or not f.is_file():
        return PlainTextResponse("Không có tệp này", status_code=404)
    mt = f.stat().st_mtime
    hit = _MIN.get(name)
    if hit is None or hit[0] != mt:
        src = f.read_text(encoding="utf-8")
        try:
            body = minify(src).encode("utf-8")
        except ValueError:  # pragma: no cover — cú pháp lạ: phục vụ nguyên văn còn hơn hỏng trang
            body = src.encode("utf-8")
        hit = _MIN[name] = (mt, body)
    return Response(hit[1], media_type="text/javascript; charset=utf-8")


def routes() -> list[Any]:
    from starlette.routing import Route

    return [Route("/static/coord/{name}", coord_js)]
