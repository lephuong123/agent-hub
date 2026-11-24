"""Trang của SAC: bảng đường dẫn → hàm dựng trang (mỗi trang gọi vài thao tác admind), menu theo vai trò, bộ lọc Jinja.

Thêm trang: `@page(r"^/users/(?P<id>[^/]+)$", group="user_view")`. Vai trò không có `group` → 404 (VIS-08), admind vẫn kiểm lại.
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from jinja2.exceptions import UndefinedError
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from ..admind import rbac

if TYPE_CHECKING:
    from .app import SacApp, Sess

NOT_FOUND = PlainTextResponse("Not Found", status_code=404)

# (nhãn, đường dẫn, nhóm quyền cần có) — thứ tự = thứ tự menu
NAV: list[tuple[str, str, str]] = [
    ("Tổng quan", "/", "user_view"),
    ("Người dùng", "/users", "user_view"),
    ("Tên miền", "/domains", "domain"),
    ("Team", "/teams", "team_board"),
    ("Báo cáo", "/reports", "report"),
    ("Chi phí", "/costs", "cost"),
    ("Chính sách", "/policy", "policy"),
    ("Tài khoản Claude", "/claude", "claude"),
    ("Cảnh báo", "/alerts", "monitor"),
    ("Máy chủ", "/system", "monitor"),
    ("Cập nhật hub", "/updates", "hub"),
    ("Job", "/jobs", "job"),
    ("Chờ duyệt", "/approvals", "approval"),
    ("Nhật ký", "/audit", "audit"),
    ("Quản trị viên", "/admins", "admin_accounts"),
    ("Cấu hình", "/settings", "system"),
]

OP_LABEL = {
    "admin.create": "Mời quản trị viên", "admin.update": "Sửa quản trị viên", "admin.lock": "Khoá", "admin.unlock": "Mở khoá",
    "admin.reset_totp": "Đặt lại TOTP", "admin.delete": "Xoá quản trị viên", "admin.set_role": "Đổi vai trò",
    "auth.session_revoke": "Thu hồi phiên", "approval.decide": "Duyệt", "approval.cancel": "Huỷ yêu cầu", "job.retry": "Thử lại",
    "job.cancel": "Huỷ job", "sys.config_set": "Lưu cấu hình", "sys.secret_set": "Lưu secret", "sys.cf_token_rotate": "Xoay token",
    "estop.on": "Bật ESTOP", "estop.on_timed": "Bật ESTOP có hẹn gỡ", "hub.rollout": "Cập nhật theo đợt", "user.import_csv": "Nhập CSV", "estop.off": "Gỡ ESTOP", "alert.ack": "Xác nhận cảnh báo", "alert.mute": "Tắt tiếng",
}
REVEAL_KEYS = {"enroll_url": "Link kích hoạt tài khoản quản trị (gửi cho người được mời — chỉ hiện MỘT lần)",
               "totp_url": "Link đăng ký TOTP của user (chỉ hiện MỘT lần, hạn 24 giờ)"}


def nav_for(role: str) -> list[tuple[str, str]]:
    return [(label, href) for label, href, g in NAV if rbac.allowed(role, g, "read")]


def after_op(s: "Sess", name: str, result: Any) -> None:
    if isinstance(result, dict):
        for k, label in REVEAL_KEYS.items():
            if result.get(k):
                s.reveal.append({"label": label, "value": result[k]})


# ============================================================================ bộ lọc
def ts(v: Any, fmt: str = "%d/%m %H:%M") -> str:
    try:
        return datetime.fromtimestamp(float(v)).strftime(fmt) if v else "—"
    except (TypeError, ValueError, OSError):
        return "—"


def ago(v: Any) -> str:
    try:
        d = time.time() - float(v)
    except (TypeError, ValueError, UndefinedError):  # khoá thiếu trong số liệu hub (hub mới chưa xuất trạng thái) → Undefined
        return "—"
    if d < 90:
        return "vừa xong"
    if d < 3600:
        return f"{int(d // 60)} phút trước"
    if d < 86400:
        return f"{int(d // 3600)} giờ trước"
    return f"{int(d // 86400)} ngày trước"


def money(v: Any) -> str:
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError, UndefinedError):  # vd `p.cost.usd.today` khi hub chưa xuất số liệu
        return "—"


STATE_VI = {
    "pending": ("chờ kích hoạt", "muted"), "active": ("hoạt động", "ok"), "locked": ("đã khoá", "bad"), "queued": ("chờ", "muted"),
    "running": ("đang chạy", "info"), "succeeded": ("xong", "ok"), "failed": ("lỗi", "bad"), "cancelled": ("đã huỷ", "muted"),
    "awaiting_approval": ("chờ duyệt", "warn"), "approved": ("đã duyệt", "info"), "rejected": ("từ chối", "muted"),
    "expired": ("hết hạn", "muted"), "done": ("đã chạy", "ok"), "skipped": ("bỏ qua (đã có)", "muted"), "undone": ("đã dọn", "muted"),
    "creating": ("đang tạo", "info"), "create_failed": ("lỗi tạo", "bad"), "suspended": ("tạm khoá", "warn"),
    "revoked": ("đã thu hồi", "muted"), "deleted": ("đã xoá", "muted"), "ok": ("ổn", "ok"), "stale": ("quá hạn", "warn"),
    "stopped": ("đã dừng", "warn"), "malformed": ("hỏng", "bad"), "owner_mismatch": ("sai chủ", "bad"), "unknown": ("chưa rõ", "muted"),
    "missing": ("chưa có số liệu", "muted"), "on": ("đang mở", "bad"), "ack": ("đã xác nhận", "warn"), "resolved": ("đã xử lý", "ok"),
    "healthy": ("ổn", "ok"), "unhealthy": ("lỗi", "bad"), "revoking": ("đang thu hồi", "warn"),
}


def state_pill(v: Any) -> tuple[str, str]:
    return STATE_VI.get(str(v or ""), (str(v or "—"), "muted"))


GLOBALS: dict[str, Any] = {"role_vi": rbac.ROLE_VI, "state_pill": state_pill, "ROLES": rbac.ROLES}
FILTERS: dict[str, Any] = {"ts": ts, "ago": ago, "money": money}


# ============================================================================ trang
PageFn = Callable[["SacApp", Request, "Sess", dict[str, str]], Awaitable[Response]]
PAGES: list[tuple[re.Pattern[str], str, PageFn]] = []
APIS: dict[str, tuple[str, Callable[..., Awaitable[Any]]]] = {}


def page(pattern: str, *, group: str) -> Callable[[PageFn], PageFn]:
    def deco(fn: PageFn) -> PageFn:
        PAGES.append((re.compile(pattern), group, fn))
        return fn
    return deco


def api_view(name: str, *, group: str) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    def deco(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        APIS[name] = (group, fn)
        return fn
    return deco


async def dispatch(app: "SacApp", request: Request, s: "Sess") -> Response:
    path = request.url.path.rstrip("/") or "/"
    for rx, group, fn in PAGES:
        m = rx.match(path)
        if m:
            if not rbac.allowed(s.account["role"], group, "read"):
                return NOT_FOUND
            return await fn(app, request, s, m.groupdict())
    return NOT_FOUND


async def api(app: "SacApp", request: Request, s: "Sess") -> Any:
    name = request.path_params["name"]
    if name not in APIS:
        return None
    group, fn = APIS[name]
    if not rbac.allowed(s.account["role"], group, "read"):
        return None
    return await fn(app, request, s)


def can(s: "Sess", group: str, need: str = "write") -> bool:
    return rbac.allowed(s.account["role"], group, need)


GLOBALS["can"] = can


def has_op(name: str) -> bool:
    """Thao tác đã có trong bản admind này chưa (nút của tính năng chưa làm thì ẩn)."""
    from ..admind.ops import load_all

    return name in load_all()


GLOBALS["has_op"] = has_op


# trang cụ thể nằm ở các module pages_*.py (import để đăng ký)
from . import pages_core, pages_extra, pages_ops, pages_policy, pages_team  # noqa: E402,F401
