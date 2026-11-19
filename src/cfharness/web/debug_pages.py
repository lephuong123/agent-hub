"""Trang web công cụ debug (L6, docs/dev-notes/plan-logging.md B6) — CHỈ ĐỌC, client thuần của daemon.

- `/logs` "Nhật ký hệ thống": bảng log có bộ lọc theo trường (giống `cfharness logs`) qua RPC `logs.query` (HUMAN_ONLY, daemon tự ghi
  `read:logs.query`).
- `/trace/{id}`: dòng thời gian của yêu cầu `r_…` / run / task qua RPC `trace.get` (HUMAN_ONLY). `/audit` link `req_id` → đây; trang run link
  "Dòng thời gian".

Chữ log/audit là DỮ LIỆU: template autoescape, không `|safe`, không script/style inline. Không có thao tác ghi (không `ACTIONS`).
"""

from __future__ import annotations

import re
from typing import Any

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import Response

from ..rpc import RpcError
from .app import render, rpc

FILTER_KEYS = ("since", "until", "level", "logger", "req", "run", "agent", "task", "proc")
LEVELS = ("", "debug", "info", "warning", "error", "critical")
PROCS = ("", "daemon", "web", "mcp", "cli")
LEVEL_TONE = {"debug": "muted", "info": "info", "warning": "warn", "error": "danger", "critical": "danger strong"}
SOURCE_TONE = {"audit": "accent", "log": "muted", "event": "info", "guard": "warn", "approval": "ask", "run": "ok"}
SOURCE_LABEL = {"audit": "audit", "log": "log", "event": "event", "guard": "guard", "approval": "duyệt", "run": "run"}
_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{3,64}$")
_RUN_RE = re.compile(r"^r_[0-9a-f]{6,32}$")
_REQ_RE = re.compile(r"^r_[0-7][0-9A-HJKMNP-TV-Z]{25}$")


def _limit(v: Any, default: int, hi: int) -> int:
    try:
        return max(1, min(int(v or default), hi))
    except (TypeError, ValueError):
        return default


def id_kind(v: Any) -> str | None:
    """Loại id để dựng link nội bộ: req (`/trace/`), run (`/runs/`) — chỉ khi đúng dạng (không bao giờ link chuỗi tự do)."""
    s = str(v or "")
    if _REQ_RE.match(s):
        return "req"
    if _RUN_RE.match(s):
        return "run"
    return None


async def logs_page(request: Request) -> Response:
    q = request.query_params
    params: dict[str, Any] = {k: q[k].strip() for k in FILTER_KEYS if q.get(k, "").strip()}
    if "since" not in params and not any(k in params for k in ("req", "run", "task", "until")):
        params["since"] = "2h"  # mặc định: 2 giờ gần nhất (đọc ít, nhanh)
    params["limit"] = _limit(q.get("limit"), 200, 2000)
    res: dict[str, Any] = {}
    error = None
    try:
        res = await rpc(request, "logs.query", params) or {}
    except RpcError as e:
        if e.code != -32602:
            raise
        error = e.message
    rows = list(reversed(res.get("rows") or []))  # mới nhất trên cùng
    return await render(request, "logs.html", {"nav": "logs", "rows": rows, "res": res, "q": params, "error": error, "levels": LEVELS,
                                               "procs": PROCS, "level_tone": LEVEL_TONE, "id_kind": id_kind})


async def trace_page(request: Request) -> Response:
    ident = request.path_params.get("id") or ""
    if not _ID_RE.match(ident):
        raise HTTPException(404, "id không hợp lệ")
    limit = _limit(request.query_params.get("limit"), 1000, 5000)
    res = await rpc(request, "trace.get", {"id": ident, "limit": limit}) or {}
    return await render(request, "trace.html", {"nav": "logs", "res": res, "ident": ident, "source_tone": SOURCE_TONE, "source_label": SOURCE_LABEL,
                                                "level_tone": LEVEL_TONE, "id_kind": id_kind})


def routes() -> list[Any]:
    from starlette.routing import Route

    return [Route("/logs", logs_page), Route("/trace/{id}", trace_page)]


__all__ = ["routes", "id_kind"]
