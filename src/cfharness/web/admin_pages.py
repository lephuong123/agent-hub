"""Trang "Tổng quan máy" (`/admin/overview`) — mọi hub trên máy nhiều người dùng (admin_overview.py, docs/dev-notes/admin-overview.md).

CHỈ ĐỌC, client thuần: RPC `admin.overview` (∈ HUMAN_ONLY) — daemon đọc thư mục trạng thái chung; web không đụng file/DB. Nội dung chỉ là
số/enum đã lọc qua `admin_overview.SPEC` ở daemon; template vẫn autoescape, không script/style inline.
Mục menu chỉ hiện khi thư mục chung tồn tại (`nav_available`, cache 60 s trên WebState để không gọi RPC mỗi trang).
"""

from __future__ import annotations

import time
from typing import Any

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from ..rpc import DaemonUnavailable, RpcError
from .app import render, rpc, ws

NAV_TTL_S = 60.0
STATE_VI = {"ok": ("Đang chạy", "ok"), "stale": ("Quá hạn", "warn"), "stopped": ("Đã dừng", "muted"), "malformed": ("File hỏng", "danger"),
            "owner_mismatch": ("Sai chủ file", "danger")}


async def nav_available(request: Request) -> bool:
    st = ws(request)
    cache = getattr(st, "_admin_nav", None)
    now = time.monotonic()
    if cache and now - cache[0] < NAV_TTL_S:
        return bool(cache[1])
    try:
        res = await rpc(request, "admin.overview")
        ok = bool((res or {}).get("exists"))
    except (RpcError, DaemonUnavailable, OSError):
        ok = False
    st._admin_nav = (now, ok)  # type: ignore[attr-defined]
    return ok


async def overview_page(request: Request) -> Response:
    res = await rpc(request, "admin.overview")
    ws(request)._admin_nav = (time.monotonic(), bool(res.get("exists")))  # type: ignore[attr-defined]
    return await render(request, "admin_overview.html", {"nav": "admin", "ov": res, "states": STATE_VI})


def routes() -> list[Any]:
    return [Route("/admin/overview", overview_page)]


__all__ = ["nav_available", "routes"]
