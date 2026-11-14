"""Trang quản trị máy (admin_overview.py, admin_rpc.py; docs/dev-notes/admin-overview.md, admin-users.md).

Phía ADMIN (menu chỉ hiện khi daemon thuộc nhóm `cfharness-admin` VÀ máy có thư mục chung — RPC vẫn kiểm lại `require_admin`):
- `/admin/overview` (tổng quan mọi hub), `/admin/users` (danh sách user + cảnh báo + cờ ESTOP), `/admin/users/<u>` (chi tiết: hub,
  biểu đồ chi phí/ngày + run/giờ vẽ bằng SVG trong template — tọa độ tính ở đây, không script, không CDN), `/admin/alerts`, `/admin/audit`.
- Ghi DUY NHẤT: bật/gỡ ESTOP admin (`admin_estop_on|off` trong ACTIONS, luôn xác nhận). Chính sách: chỉ HIỆN lệnh sudo.
Phía USER: `/machine` "Quản trị máy" — chỉ đọc: đúng JSON hub này xuất cho admin, chính sách có hiệu lực, ESTOP admin, admin-log.

Client thuần (RPC `admin.*` ∈ HUMAN_ONLY); nội dung chỉ số/enum đã lọc ở daemon; template autoescape.
"""

from __future__ import annotations

import json
import time
from typing import Any

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from ..rpc import DaemonUnavailable, RpcError
from .app import render, rpc, ws

NAV_TTL_S = 60.0
STATE_VI = {"ok": ("Đang chạy", "ok"), "stale": ("Quá hạn", "warn"), "stopped": ("Đã dừng", "muted"), "malformed": ("File hỏng", "danger"),
            "owner_mismatch": ("Sai chủ file", "danger")}
USER_TONE = {"ok": "ok", "stale": "warn", "stopped": "muted", "bad_file": "danger", "policy_mismatch": "warn", "revoked_running": "danger",
             "not_installed": "muted", "installed_no_export": "warn"}


async def nav_badges(request: Request) -> dict[str, Any]:
    """{admin, machine} cho menu — cache 60 s trên WebState (ESTOP admin lấy mỗi trang qua `estop.get` ở `app.render`)."""
    st = ws(request)
    cache = getattr(st, "_admin_nav", None)
    now = time.monotonic()
    if cache and now - cache[0] < NAV_TTL_S:
        return dict(cache[1])
    try:
        res = await rpc(request, "admin.nav") or {}
        out = {"admin": bool(res.get("is_admin") and res.get("managed")), "machine": bool(res.get("managed"))}
    except (RpcError, DaemonUnavailable, OSError):
        out = {"admin": False, "machine": False}
    st._admin_nav = (now, out)  # type: ignore[attr-defined]
    return dict(out)


async def nav_available(request: Request) -> bool:  # tương thích: mục "Tổng quan máy"
    return bool((await nav_badges(request)).get("admin"))


async def _admin_call(request: Request, method: str, params: dict[str, Any] | None = None) -> Any:
    try:
        return await rpc(request, method, params or {})
    except RpcError as e:
        if "cfharness-admin" in str(e):
            raise HTTPException(403, "Chỉ người quản trị máy (nhóm cfharness-admin)") from None
        raise


async def overview_page(request: Request) -> Response:
    res = await _admin_call(request, "admin.overview")
    return await render(request, "admin_overview.html", {"nav": "admin", "ov": res, "states": STATE_VI})


async def users_page(request: Request) -> Response:
    from ..admin_watch import ALERT_VI

    res = await _admin_call(request, "admin.users")
    return await render(request, "admin_users.html", {"nav": "admin_users", "r": res, "tones": USER_TONE, "alert_vi": ALERT_VI,
                                                      "reasons": _reasons()})


def _reasons() -> list[str]:
    from ..admin_control import REASON_CODES

    return list(REASON_CODES)


def bars(series: list[dict[str, Any]], *, width: int = 640, height: int = 160, pad: int = 24) -> dict[str, Any]:
    """Tọa độ cột cho biểu đồ SVG (thuần, chỉ số). Nhãn trục = chuỗi ngày/giờ do hub tạo (không phải dữ liệu agent)."""
    vals = [float(x.get("value") or 0) for x in series]
    top = max(vals) if vals and max(vals) > 0 else 1.0
    n = max(1, len(series))
    slot = (width - pad) / n
    out = []
    for i, x in enumerate(series):
        v = float(x.get("value") or 0)
        h = round((height - pad) * v / top, 1)
        out.append({"x": round(pad + i * slot + slot * 0.15, 1), "y": round(height - pad - h, 1), "w": round(max(1.0, slot * 0.7), 1), "h": h,
                    "label": str(x.get("label") or ""), "value": v, "tick": i % max(1, n // 6) == 0})
    return {"bars": out, "width": width, "height": height, "pad": pad, "top": top}


async def user_page(request: Request) -> Response:
    from ..admin_overview import USER_RE
    from ..admin_watch import ALERT_VI

    u = request.path_params["user"]
    if not USER_RE.match(u):
        raise HTTPException(404, "Không có user này")
    days = 30
    try:
        days = max(1, min(int(request.query_params.get("days") or 30), 90))
    except ValueError:
        pass
    try:
        res = await _admin_call(request, "admin.user", {"user": u, "days": days})
    except RpcError as e:
        raise HTTPException(404, str(e)) from None
    hist = res.get("history") or {}
    charts = {"cost": bars(hist.get("cost_by_day") or []), "running": bars(hist.get("running_by_hour") or [])}
    pol = res.get("policy") or {}
    return await render(request, "admin_user.html", {"nav": "admin_users", "r": res, "states": STATE_VI, "tones": USER_TONE, "alert_vi": ALERT_VI,
                                                     "charts": charts, "days": days, "reasons": _reasons(),
                                                     "policy_json": json.dumps(pol.get("policy") or {}, ensure_ascii=False, indent=2)})


async def alerts_page(request: Request) -> Response:
    from ..admin_watch import ALERT_VI

    res = await _admin_call(request, "admin.alerts")
    return await render(request, "admin_alerts.html", {"nav": "admin_users", "r": res, "alert_vi": ALERT_VI})


async def audit_page(request: Request) -> Response:
    from ..admin_overview import USER_RE

    u = request.query_params.get("user") or None
    if u is not None and not USER_RE.match(u):
        u = None
    res = await _admin_call(request, "admin.audit", {"user": u, "days": 7, "limit": 500})
    return await render(request, "admin_audit.html", {"nav": "admin_users", "r": res, "user": u})


async def machine_page(request: Request) -> Response:
    res = await rpc(request, "admin.self")
    exp = res.get("export") or {}
    return await render(request, "admin_machine.html", {"nav": "machine", "r": res,
                                                        "preview_json": json.dumps(exp.get("preview") or {}, ensure_ascii=False, indent=2, sort_keys=True),
                                                        "policy_json": json.dumps((res.get("policy") or {}).get("policy") or {}, ensure_ascii=False, indent=2)})


def register_actions(actions: dict[str, Any], Action: Any, _req: Any) -> None:
    from ..admin_control import REASON_CODES

    def target(f: Any) -> dict[str, Any]:
        u = str(f.get("user") or "").strip()
        return {"all": True} if u in ("", "*") else {"user": u}

    def who(f: Any) -> str:
        u = str(f.get("user") or "").strip()
        return "MỌI user trên máy" if u in ("", "*") else f"user {u}"

    def on_params(f: Any) -> dict[str, Any]:
        code = str(f.get("reason_code") or "other")
        if code not in REASON_CODES:
            raise ValueError("mã lý do không hợp lệ")
        return {"on": True, "reason_code": code, **target(f)}

    actions["admin_estop_on"] = Action("admin.estop_set", on_params, lambda r, f: f"ESTOP quản trị máy BẬT cho {who(f)} — đã ghi admin-log",
                                       confirm=lambda f: f"BẬT ESTOP quản trị máy cho {who(f)}? Run mới bị chặn, run đang chạy bị dừng; user không tự gỡ được.",
                                       danger=True)
    actions["admin_estop_off"] = Action("admin.estop_set", lambda f: {"on": False, **target(f)}, lambda r, f: f"Đã gỡ ESTOP quản trị máy cho {who(f)} — đã ghi admin-log",
                                        confirm=lambda f: f"GỠ ESTOP quản trị máy cho {who(f)}? (ghi admin-log + cảnh báo hồi phục)", danger=True)


def routes() -> list[Any]:
    return [Route("/admin/overview", overview_page), Route("/admin/users", users_page), Route("/admin/users/{user}", user_page),
            Route("/admin/alerts", alerts_page), Route("/admin/audit", audit_page), Route("/machine", machine_page)]


__all__ = ["bars", "nav_available", "nav_badges", "register_actions", "routes"]
