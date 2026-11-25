"""Trang MCP (Cài đặt › MCP): danh sách THÔ, CHỈ ĐỌC các MCP server do `claude` quản lý theo từng tài khoản.

MCP bên thứ ba do chính `claude` giữ (`claude mcp add/login`, connector claude.ai); hub không còn lớp chính sách nào (gán theo agent,
tin cậy, include/exclude/duyệt tool, annotations): mọi agent dùng mọi server của tài khoản Claude đang chạy run.

- `GET /mcp`: RPC `mcp.list` `{}` → `{"accounts": [{"id", "label", "config_path", "servers": [{"name", "transport", "target"}], "error"}]}`
  (đọc `.claude.json`, không có trạng thái kết nối). Nhóm theo tài khoản; lỗi theo tài khoản; trạng thái trống.
- URL cũ (`/mcp/{name}`, `/mcp/{name}/tool`, `/mcp/agent/{id}`) → chuyển về `/mcp` (bookmark cũ không 500).
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from ..rpc import DaemonUnavailable, RpcError
from .app import err_text, render, rpc


def accounts_view(res: Any) -> list[dict[str, Any]]:
    """Kết quả `mcp.list` → danh sách tài khoản đã chuẩn hoá cho template (bỏ phần tử sai kiểu, không bao giờ ném)."""
    out: list[dict[str, Any]] = []
    raw = res.get("accounts") if isinstance(res, dict) else None
    for a in raw if isinstance(raw, list) else []:
        if not isinstance(a, dict):
            continue
        servers = [{"name": str(s.get("name") or ""), "transport": str(s.get("transport") or ""), "target": str(s.get("target") or "")}
                   for s in a.get("servers") or [] if isinstance(s, dict)]
        out.append({"id": a.get("id"), "label": str(a.get("label") or a.get("id") or "Mặc định của máy"),
                    "config_path": str(a.get("config_path") or ""), "servers": servers, "error": a.get("error") or None})
    return out


async def mcp_page(request: Request) -> Response:
    error = None
    try:
        accounts = accounts_view(await rpc(request, "mcp.list", {}))
    except (RpcError, DaemonUnavailable, OSError) as e:
        accounts, error = [], err_text(e)
    total = sum(len(a["servers"]) for a in accounts)
    return await render(request, "mcp.html", {"nav": "mcp", "accounts": accounts, "total": total, "error": error})


async def legacy_redirect(request: Request) -> Response:
    """Trang chính sách MCP cũ đã gỡ → về danh sách."""
    return RedirectResponse("/mcp", status_code=303)


def routes() -> list[Any]:
    from starlette.routing import Route

    return [
        Route("/mcp", mcp_page),
        Route("/mcp/{rest:path}", legacy_redirect, methods=["GET", "HEAD", "POST"]),
    ]
