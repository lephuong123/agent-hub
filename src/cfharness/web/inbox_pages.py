"""Trang "Việc được giao" (Team user của Super Admin Console — team_inbox.py): xem việc trưởng team giao, Nhận (chọn agent) / Từ chối / Nộp.

Client thuần qua RPC `inbox.*` (∈ HUMAN_ONLY). Menu chỉ hiện khi máy có thư mục team (`/Users/Shared/cfharness/teams`). Nội dung việc là
chữ do trưởng team viết — chỉ hiển thị (autoescape), chỉ tới agent khi NGƯỜI bấm Nhận.
"""

from __future__ import annotations

import time
from typing import Any

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from ..rpc import DaemonUnavailable, RpcError
from .app import render, rpc, ws

TTL_S = 30.0
STATE_VI = {"new": ("Chờ nhận", "warn"), "doing": ("Đang làm", "info"), "submitted": ("Chờ duyệt", "info"), "returned": ("Bị trả lại", "danger"),
            "rejected": ("Đã từ chối", "muted"), "done": ("Xong", "ok")}


async def badge(request: Request) -> dict[str, Any]:
    st = ws(request)
    cache = getattr(st, "_inbox_badge", None)
    now = time.monotonic()
    if cache and now - cache[0] < TTL_S:
        return dict(cache[1])
    from .. import admin_control as AC

    out: dict[str, Any] = {"team_inbox": None}
    if (AC.shared_root() / "teams").is_dir():
        try:
            items = (await rpc(request, "inbox.list") or {}).get("items") or []
            out["team_inbox"] = sum(1 for i in items if i.get("state") in ("new", "returned"))
        except (RpcError, DaemonUnavailable, OSError):
            out["team_inbox"] = None
    st._inbox_badge = (now, out)  # type: ignore[attr-defined]
    return dict(out)


async def inbox_page(request: Request) -> Response:
    res = await rpc(request, "inbox.list") or {}
    agents = await rpc(request, "agent.list", {}) or []
    if isinstance(agents, dict):
        agents = agents.get("agents") or []
    return await render(request, "team_inbox.html", {"nav": "team_inbox", "items": res.get("items") or [], "states": STATE_VI,
                                                "agents": [a for a in agents if not a.get("archived_at")]})


def register_actions(actions: dict[str, Any], Action: Any, _req: Any) -> None:
    actions["inbox_accept"] = Action("inbox.accept", lambda f: {"id": _req(f, "id"), **({"agent": f.get("agent")} if f.get("agent") else {})},
                                     lambda r, f: f"Đã nhận việc — tạo task {r.get('task_id')}",
                                     confirm=lambda f: "Nhận việc này? Hub sẽ tạo task cho agent đã chọn (nội dung việc gửi tới agent).")
    actions["inbox_reject"] = Action("inbox.reject", lambda f: {"id": _req(f, "id"), "reason": f.get("note") or f.get("reason") or ""},
                                     "Đã từ chối — trưởng team được báo", confirm="Từ chối việc này?", note="Lý do từ chối")
    actions["inbox_submit"] = Action("inbox.submit", lambda f: {"id": _req(f, "id"), "result_path": f.get("result_path") or "",
                                                                "note": f.get("note") or ""}, "Đã nộp — chờ trưởng team duyệt")


def routes() -> list[Any]:
    return [Route("/team-inbox", inbox_page)]
