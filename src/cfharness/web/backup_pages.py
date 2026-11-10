"""Thẻ "Sao lưu" ở trang Cài đặt (backup.py) — `/partials/backup` (htmx `load`).

- Đọc: RPC `backup.list` (danh sách, lịch tự động, dung lượng). Daemon không trả lời → thẻ báo, không lỗi trang.
- Ghi: thao tác `backup_create` qua `ACTIONS` (CSRF + khoá phiên WT-1, `_audit.channel=web`, có hộp xác nhận); `backup.create` ∈ HUMAN_ONLY.
- Khôi phục KHÔNG có trên web (daemon phải dừng, xác nhận 2 bước ở terminal): mỗi dòng chỉ hiện lệnh CLI `cfharness backup restore <id> --stop`.
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from ..rpc import DaemonUnavailable, RpcError
from .app import rpc, session_of, ws

KIND_LABEL = {"manual": "thủ công", "auto": "tự động", "pre-update": "trước cập nhật", "pre-restore": "trước khôi phục"}


def fmt_size(n: Any) -> str:
    try:
        f = float(n)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if f < 1024 or unit == "GB":
            return f"{f:.0f} {unit}" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1024
    return "?"  # pragma: no cover


async def partial_backup(request: Request) -> Response:
    ctx: dict[str, Any]
    try:
        ctx = {"data": await rpc(request, "backup.list")}
    except (DaemonUnavailable, OSError):
        ctx = {"down": True}
    except RpcError as e:
        ctx = {"error": e.message}
    sess = session_of(request)
    return ws(request).templates.TemplateResponse(request, "_backup.html", {**ctx, "csrf": sess.csrf if sess else "", "kinds": KIND_LABEL,
                                                                            "size": fmt_size})


def register_actions(actions: dict[str, Any], Action: Any, _req: Any) -> None:
    def ok(r: Any, f: Any) -> str:
        r = r or {}
        return f"Đã tạo bản sao lưu {r.get('id')} ({fmt_size(r.get('size'))})"

    actions["backup_create"] = Action(
        "backup.create", lambda f: {"label": str(f.get("label") or "").strip() or None}, ok,
        confirm="Tạo bản sao lưu HUB_HOME ngay?\nhub.db + cấu hình + chính sách + tri thức agent (không gồm secret, đăng nhập web, runs, checkpoints).\n"
                "Hub vẫn chạy bình thường trong lúc sao lưu.",
        goto=lambda r, f: "/settings#backup")


def routes() -> list[Any]:
    return [Route("/partials/backup", partial_backup)]


__all__ = ["register_actions", "routes", "fmt_size"]
