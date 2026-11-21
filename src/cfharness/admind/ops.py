"""Sổ đăng ký mã thao tác của admind (plan-superadmin §2.2–2.3).

Mỗi thao tác khai: `group` (ma trận rbac), `risk` (`read|normal|high|critical` — bảng 7.19), model tham số (pydantic, `extra=forbid`,
mọi chuỗi qua regex), `scope` (`none|user|team|machine`) và hàm lấy `target` để ghi audit. KHÔNG có thao tác nào nhận chuỗi lệnh.
Module thao tác (`ops_*.py`) tự đăng ký khi được import (`load_all`).
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from .ptypes import NoParams, Params

if TYPE_CHECKING:
    from .dispatch import Ctx

RISKS = ("read", "normal", "high", "critical")
SCOPES = ("none", "user", "team", "machine")
ERROR_CODES = ("invalid", "unauthenticated", "forbidden", "step_up_required", "approval_required", "not_found", "conflict", "failed",
               "locked", "internal", "unavailable")


class OpError(Exception):
    def __init__(self, code: str, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code if code in ERROR_CODES else "failed"
        self.message = message
        self.data = data


@dataclass
class Op:
    name: str
    group: str
    risk: str
    handler: Callable[["Ctx", Any], Any]
    params: type[Params] = NoParams
    scope: str = "none"
    target: Callable[[Any], str | None] | None = None
    public: bool = False        # không cần phiên (đăng nhập, kích hoạt tài khoản)
    root_only: bool = False     # chỉ từ root (CLI sudo trên máy), không cần phiên (khởi tạo, khôi phục)
    audit_read: bool = False    # thao tác đọc nhạy cảm vẫn ghi audit (xuất dữ liệu, xem log)
    doc: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def need(self) -> str:
        return "read" if self.risk == "read" else "write"


REGISTRY: dict[str, Op] = {}

OP_MODULES = (
    "cfharness.admind.ops_auth",
    "cfharness.admind.ops_system",
    "cfharness.admind.ops_users",
    "cfharness.admind.ops_domains",
    "cfharness.admind.ops_monitor",
    "cfharness.admind.ops_jobs",
    "cfharness.admind.ops_lifecycle",
    "cfharness.admind.ops_teams",
    "cfharness.admind.ops_policy",
    "cfharness.admind.ops_claude",
    "cfharness.admind.ops_reports",
)


def op(name: str, *, group: str, risk: str, params: type[Params] = NoParams, scope: str = "none",
       target: Callable[[Any], str | None] | None = None, public: bool = False, root_only: bool = False, audit_read: bool = False,
       **meta: Any) -> Callable[[Callable[["Ctx", Any], Any]], Callable[["Ctx", Any], Any]]:
    if risk not in RISKS or scope not in SCOPES:
        raise ValueError(f"{name}: risk/scope không hợp lệ")

    def deco(fn: Callable[["Ctx", Any], Any]) -> Callable[["Ctx", Any], Any]:
        if name in REGISTRY and REGISTRY[name].handler is not fn:
            raise ValueError(f"mã thao tác trùng: {name}")
        REGISTRY[name] = Op(name=name, group=group, risk=risk, handler=fn, params=params, scope=scope, target=target, public=public,
                            root_only=root_only, audit_read=audit_read, doc=(fn.__doc__ or "").strip().split("\n")[0], meta=meta)
        return fn

    return deco


def load_all() -> dict[str, Op]:
    for m in OP_MODULES:
        try:
            importlib.import_module(m)
        except ModuleNotFoundError as e:
            if e.name != m:  # module thao tác có mà import con lỗi → báo; module chưa có (đang làm dần) → bỏ qua
                raise
    return REGISTRY
