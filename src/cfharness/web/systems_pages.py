"""Trang "Hệ thống ngoài" `/systems` (đề xuất #9, docs/dev-notes/systems.md) — app trong ~/Workspace và job launchd / Task Scheduler.

- `/systems`: danh sách hệ thống đã đăng ký (`system.list`) + trạng thái từng job; `?scan=1` = quét thử (`system.scan {}` — KHÔNG đăng ký,
  chỉ liệt kê hệ thống mới / đã mất) rồi người bấm "Đăng ký" (`system.scan{apply}`); form thêm theo đường dẫn (`system.add`).
- `/systems/{id}`: chi tiết + đuôi log từng job (`system.get{log_bytes}` — daemon đã che secret; template autoescape).
- Thao tác (`POST /do/<tên>`, CSRF + khoá phiên, `_audit.channel=web`): tạm dừng / bật lại / khởi động lại job (`system.job`), khoá / mở khoá
  (`system.lock`), gỡ hệ thống (`system.rm`), đăng ký (`system.scan{apply}` / `system.add`). Mọi method ghi ∈ `auth.HUMAN_ONLY` (service còn
  `require_human` cho đăng ký/khoá) → web chỉ là người; điều phối viên/agent không đi qua đây.

Module chỉ gọi RPC; không đọc plist, không gọi `launchctl` (chỉ daemon gọi).
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import Response

from ..rpc import METHOD_NOT_FOUND, DaemonUnavailable, RpcError
from .app import err_text, render, rpc

LOG_BYTES = 4000
JOB_STATUS = {"running": ("đang chạy", "ok", "play"), "idle": ("chờ lịch", "muted", "clock"), "paused": ("TẠM DỪNG", "warn", "pause"),
              "not_loaded": ("chưa nạp", "muted", "circle"), "error": ("lỗi lần trước", "danger", "alert")}
JOB_KIND = {"service": "dịch vụ (luôn chạy)", "scheduled": "theo lịch", "on_demand": "khi được gọi"}
JOB_ACTIONS = {"pause": "tạm dừng", "resume": "bật lại", "restart": "khởi động lại"}


def job_confirm(action: str, label: str) -> str:
    """Câu xác nhận (chữ hub; `label` là label job từ plist — template/confirm.html autoescape)."""
    if action == "pause":
        return (f"Tạm dừng job {label}?\nJob bị gỡ khỏi launchd và tắt tự nạp — không chạy theo lịch, không tự bật lại khi khởi động máy, "
                "cho tới khi bạn bật lại.")
    if action == "resume":
        return f"Bật lại job {label}?\nJob được nạp lại và chạy theo cấu hình của nó (lịch / luôn chạy)."
    return f"Khởi động lại job {label}?\nTiến trình đang chạy (nếu có) bị dừng và chạy lại ngay."


def summary(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Đếm job theo trạng thái (thuần) — cho dòng tóm tắt đầu trang."""
    out = {"systems": len(rows), "jobs": 0, "running": 0, "paused": 0, "error": 0, "locked": 0}
    for s in rows:
        for j in s.get("jobs") or []:
            out["jobs"] += 1
            if j.get("status") in ("running", "paused", "error"):
                out[j["status"]] += 1
            if j.get("locked"):
                out["locked"] += 1
    return out


def _unavailable(e: Exception) -> str | None:
    if isinstance(e, DaemonUnavailable) or (isinstance(e, RpcError) and e.code == METHOD_NOT_FOUND):
        return "Daemon đang chạy bản cũ chưa có quản lý hệ thống — khởi động lại daemon (`cfharness daemon restart`)."
    return None


async def systems_page(request: Request) -> Response:
    unavailable = error = None
    rows: list[dict[str, Any]] = []
    try:
        rows = await rpc(request, "system.list") or []
    except (RpcError, DaemonUnavailable) as e:
        unavailable = _unavailable(e)
        if unavailable is None:
            error = err_text(e)
    scan = None
    if request.query_params.get("scan") and unavailable is None:
        try:  # quét thử: không đăng ký gì (daemon vẫn ghi audit `system.scan` kênh web)
            scan = await rpc(request, "system.scan", {}, audit=True)
        except RpcError as e:
            error = err_text(e)
    if scan:
        new = set(scan.get("new") or [])
        scan["new_rows"] = [s for s in scan.get("found") or [] if s.get("id") in new]
    return await render(request, "systems.html", {"nav": "systems", "rows": rows, "sum": summary(rows), "scan": scan, "error": error,
                                                  "unavailable": unavailable, "job_status": JOB_STATUS, "job_kind": JOB_KIND,
                                                  "job_confirm": job_confirm})


async def system_page(request: Request) -> Response:
    sid = request.path_params["id"]
    s = await rpc(request, "system.get", {"id": sid, "log_bytes": LOG_BYTES})  # không có → trang lỗi 404 chung
    return await render(request, "system.html", {"nav": "systems", "s": s, "job_status": JOB_STATUS, "job_kind": JOB_KIND,
                                                 "job_confirm": job_confirm})


# ============================================================================ thao tác (POST /do/<tên>)
def _job_action(f: Any) -> str:
    a = str(f.get("action") or "")
    if a not in JOB_ACTIONS:
        raise ValueError("thao tác job phải là pause|resume|restart")
    return a


def register_actions(actions: dict[str, Any], Action: Any, _req: Any) -> None:
    """Thao tác ghi của trang Hệ thống ngoài. Method ∈ `MUTATING_SYSTEMS` → daemon tự audit (`wrap_with_audit`) theo `_audit.channel=web` (`audit=True`)."""
    actions["system_job"] = Action(
        "system.job",
        lambda f: {"label": _req(f, "label", "job"), "action": _job_action(f), "reason": f"người {JOB_ACTIONS[_job_action(f)]} từ web"},
        lambda r, f: f"Đã {JOB_ACTIONS.get(str(f.get('action')), '?')} job {f.get('label')} — trạng thái: {(r or {}).get('status_vi') or '?'}",
        confirm=lambda f: job_confirm(_job_action(f), str(f.get("label") or "")),
        audit=True)
    actions["system_lock"] = Action(
        "system.lock", lambda f: {"label": _req(f, "label", "job"), "on": str(f.get("on") or "1") == "1"},
        lambda r, f: (f"Đã khoá job {f.get('label')} — điều phối viên không thao tác được nữa" if str(f.get("on") or "1") == "1"
                      else f"Đã mở khoá job {f.get('label')} — điều phối viên lại được tạm dừng / bật lại / khởi động lại"),
        confirm=lambda f: (f"Khoá job {f.get('label')}?\nĐiều phối viên không tạm dừng / bật lại / khởi động lại được job này; bạn vẫn làm được."
                           if str(f.get("on") or "1") == "1" else
                           f"Mở khoá job {f.get('label')}?\nĐiều phối viên lại được tạm dừng / bật lại / khởi động lại job này "
                           f"(trong trần manager.max_system_ops_per_hour)."),
        audit=True)
    actions["system_rm"] = Action(
        "system.rm", lambda f: {"id": _req(f, "id", "hệ thống")}, lambda r, f: f"Đã gỡ hệ thống {f.get('id')} khỏi danh sách quản lý",
        confirm=lambda f: (f"Gỡ hệ thống {f.get('id')} khỏi danh sách quản lý?\nKhông đụng tới thư mục hay job của nó (job đang tạm dừng vẫn "
                           "tạm dừng) — chỉ hub thôi quản lý, điều phối viên không thao tác được nữa."),
        danger=True, audit=True, goto=lambda r, f: "/systems")
    actions["system_scan_apply"] = Action(
        "system.scan", lambda f: {"apply": True},
        lambda r, f: f"Đã đăng ký {len((r or {}).get('new') or [])} hệ thống mới — tổng {(r or {}).get('registered', '?')}",
        confirm=lambda f: ("Đăng ký MỌI hệ thống mới vừa quét thấy?\nĐiều phối viên sẽ được tạm dừng / bật lại / khởi động lại job của chúng "
                           "(trừ job bạn khoá). Gỡ hoặc khoá từng cái ở trang này."),
        audit=True, goto=lambda r, f: "/systems")
    actions["system_add"] = Action(
        "system.add", lambda f: {"path": _req(f, "path", "đường dẫn thư mục"), **({"id": str(f.get("id")).strip()} if str(f.get("id") or "").strip() else {})},
        lambda r, f: f"Đã đăng ký hệ thống {(r or {}).get('id')} ({len((r or {}).get('jobs') or [])} job)",
        confirm=lambda f: (f"Đăng ký thư mục {f.get('path')} làm hệ thống?\nĐiều phối viên sẽ được tạm dừng / bật lại / khởi động lại job "
                           "launchd trỏ vào thư mục này (trừ job bạn khoá)."),
        audit=True, goto=lambda r, f: f"/systems/{(r or {}).get('id')}" if (r or {}).get("id") else "/systems")


def routes() -> list[Any]:
    from starlette.routing import Route

    return [Route("/systems", systems_page), Route("/systems/{id}", system_page)]


__all__ = ["register_actions", "routes", "job_confirm", "summary", "JOB_STATUS"]
