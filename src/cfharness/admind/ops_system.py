"""Thao tác hệ thống: ping, nhật ký quản trị (AUD), cấu hình SAC (SYS), duyệt four-eyes."""

from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import Field

from .. import __version__
from . import rbac
from .ops import OpError, op
from .ptypes import ObjId, Params


@op("sys.ping", group="self", risk="read", public=True)
def ping(ctx, p) -> dict[str, Any]:
    """Kiểm admind còn sống (không lộ gì ngoài phiên bản)."""
    return {"pong": True, "version": __version__, "host_id": ctx.db.host_id}


@op("sys.sac_config", group="self", risk="read", public=True)
def sac_config(ctx, p) -> dict[str, Any]:
    """Cấu hình công khai cho web SAC lúc khởi động (tên miền, Access AUD/team — không phải secret)."""
    return {"hostname": setting(ctx.db, "sac.hostname"), "access_aud": setting(ctx.db, "sac.access_aud"),
            "access_team": setting(ctx.db, "sac.access_team")}


@op("sys.hosts", group="user_view", risk="read")
def hosts(ctx, p) -> list[dict[str, Any]]:
    """Danh sách máy chủ (Q10 — bản này chỉ máy cục bộ điều khiển được)."""
    return ctx.db.all("SELECT id, name, is_local, created_at FROM hosts ORDER BY is_local DESC, name")


# ============================================================================ nhật ký quản trị
class AuditSearchP(Params):
    actor_id: str | None = Field(default=None, max_length=40)
    target: str | None = Field(default=None, max_length=200)
    op_prefix: str | None = Field(default=None, max_length=64, pattern=r"^[a-z_.]*$")
    since: float | None = None
    until: float | None = None
    limit: int = Field(default=200, ge=1, le=2000)
    offset: int = Field(default=0, ge=0)


@op("audit.search", group="audit", risk="read", params=AuditSearchP)
def audit_search(ctx, p: AuditSearchP) -> list[dict[str, Any]]:
    """Tìm nhật ký quản trị. Operator chỉ thấy dòng của chính mình (ma trận mục 8)."""
    actor = p.actor_id
    if ctx.account["role"] == rbac.OPERATOR:
        actor = ctx.account["id"]
    return ctx.state.audit.search(actor_id=actor, target=p.target, op_prefix=p.op_prefix, since=p.since, until=p.until, limit=p.limit,
                                  offset=p.offset)


@op("audit.verify", group="audit", risk="read")
def audit_verify(ctx, p) -> dict[str, Any]:
    """Kiểm chuỗi băm nhật ký quản trị (AUD-02)."""
    r = ctx.state.audit.verify()
    if not r["ok"]:
        from .alerts import raise_alert

        raise_alert(ctx.state, "system", "audit", "tamper", "crit")
    return r


# ============================================================================ cấu hình SAC (SYS)
SETTINGS: dict[str, tuple[type, Any]] = {
    # khoá: (kiểu, mặc định)
    "defaults.version": (str, "v0.2.28"),
    "defaults.port_min": (int, 8711),
    "defaults.port_max": (int, 8799),
    "defaults.workspace_root": (str, "Workspace"),
    "defaults.create_manager": (bool, True),
    "defaults.kgs_url": (str, ""),
    "cf.account_id": (str, ""),
    "cf.tunnel_id": (str, ""),
    "cf.tunnel_name": (str, "cfharness"),
    "cf.ingress_path": (str, "/etc/cfharness/tunnel.yml"),
    "sac.hostname": (str, "superadmin.trungtamdhs.com"),
    "sac.service": (str, "http://127.0.0.1:8700"),
    "sac.access_aud": (str, ""),
    "sac.access_team": (str, ""),
    "visibility.activity": (bool, False),
    "visibility.activity_ref": (str, ""),
    "visibility.transparency": (bool, False),
    "report.close_time": (str, "23:59"),
    "report.send_time": (str, "08:00"),
    "report.keep_days": (int, 90),
    "history.keep_days": (int, 90),
    "notify.telegram_chat": (str, ""),
    "notify.slack_channel": (str, ""),
    "notify.email_to": (str, ""),
    "smtp.host": (str, ""),
    "smtp.port": (int, 587),
    "smtp.user": (str, ""),
    "smtp.from": (str, ""),
    "domain.reserved": (list, ["superadmin", "www", "mail", "api", "admin", "smtp", "ftp", "ns1", "ns2", "status", "cdn", "static"]),
    "domain.redirect_days": (int, 7),
}
LIMITS = {"defaults.port_min": (1024, 65000), "defaults.port_max": (1024, 65535), "report.keep_days": (7, 3650),
          "history.keep_days": (7, 3650), "smtp.port": (1, 65535), "domain.redirect_days": (0, 90)}
TIME_KEYS = ("report.close_time", "report.send_time")


def setting(db: Any, key: str) -> Any:
    t, d = SETTINGS[key]
    v = db.get_setting(key, d)
    return v if isinstance(v, t) else d


@op("sys.config_get", group="system", risk="read")
def config_get(ctx, p) -> dict[str, Any]:
    """Cấu hình SAC (không có secret)."""
    out = {k: setting(ctx.db, k) for k in SETTINGS}
    out["secrets"] = {n: ctx.state.vault.has(n) for n in ("cf.token", "notify.telegram", "notify.slack", "smtp.password")}
    return out


class ConfigSetP(Params):
    key: str = Field(max_length=64)
    value: Any = None
    ref: str = Field(default="", max_length=200)  # số văn bản quy định khi bật mức Hoạt động (NFR-SA-10)


@op("sys.config_set", group="system", risk="high", params=ConfigSetP, target=lambda p: p.key)
def config_set(ctx, p: ConfigSetP) -> dict[str, Any]:
    """Đặt một khoá cấu hình SAC (kiểm kiểu, giới hạn)."""
    if p.key not in SETTINGS:
        raise OpError("invalid", "khoá cấu hình không có")
    t, _d = SETTINGS[p.key]
    v = p.value
    if t is int and (not isinstance(v, int) or isinstance(v, bool)):
        raise OpError("invalid", "giá trị phải là số nguyên")
    if t is bool and not isinstance(v, bool):
        raise OpError("invalid", "giá trị phải là true/false")
    if t is str and not isinstance(v, str):
        raise OpError("invalid", "giá trị phải là chuỗi")
    if t is list and not (isinstance(v, list) and all(isinstance(x, str) and len(x) <= 63 for x in v) and len(v) <= 200):
        raise OpError("invalid", "giá trị phải là danh sách chuỗi")
    if isinstance(v, str) and len(v) > 500:
        raise OpError("invalid", "chuỗi quá dài")
    if p.key in LIMITS:
        lo, hi = LIMITS[p.key]
        if not lo <= v <= hi:
            raise OpError("invalid", f"ngoài khoảng {lo}..{hi}")
    if p.key in TIME_KEYS:
        try:
            time.strptime(v, "%H:%M")
        except ValueError:
            raise OpError("invalid", "giờ dạng HH:MM") from None
    if p.key == "visibility.activity" and v is True and not p.ref.strip():
        raise OpError("invalid", "bật mức xem Hoạt động phải nhập số văn bản quy định nội bộ (NFR-SA-10)")
    ctx.db.set_setting(p.key, v, ctx.actor_id)
    if p.key == "visibility.activity":
        ctx.db.set_setting("visibility.activity_ref", p.ref.strip() if v else "", ctx.actor_id)
    if p.key.startswith("visibility."):
        from .shared import write_visibility

        write_visibility(ctx.state)
    return {"key": p.key, "value": v}


class SecretSetP(Params):
    name: Literal["cf.token", "notify.telegram", "notify.slack", "smtp.password"]
    value: str = Field(min_length=1, max_length=4096)


@op("sys.secret_set", group="system", risk="high", params=SecretSetP, target=lambda p: p.name)
def secret_set(ctx, p: SecretSetP) -> dict[str, Any]:
    """Nạp/đổi secret hệ thống (không bao giờ hiện lại). Đổi token Cloudflare đang có = `sys.cf_token_rotate` (Tới hạn)."""
    if p.name == "cf.token" and ctx.state.vault.has("cf.token"):
        raise OpError("conflict", "đã có token Cloudflare — dùng 'xoay token' (cần người thứ hai duyệt)")
    ctx.state.vault.set(p.name, p.value.strip())
    return {"ok": True}


class CfRotateP(Params):
    value: str = Field(min_length=20, max_length=4096)
    reason: str = Field(default="", max_length=500)


@op("sys.cf_token_rotate", group="system", risk="critical", params=CfRotateP, target=lambda p: "cf.token")
def cf_token_rotate(ctx, p: CfRotateP) -> dict[str, Any]:
    """Xoay token API Cloudflare (four-eyes)."""
    ctx.state.vault.set("cf.token", p.value.strip())
    return {"ok": True}


# ============================================================================ four-eyes
class ApprovalListP(Params):
    state: Literal["pending", "approved", "rejected", "expired", "cancelled", "done", "failed"] | None = None


@op("approval.list", group="approval", risk="read", params=ApprovalListP)
def approval_list(ctx, p: ApprovalListP) -> list[dict[str, Any]]:
    """Yêu cầu four-eyes."""
    return ctx.state.approvals.list(state=p.state)


class DecideP(Params):
    id: ObjId
    approve: bool


@op("approval.decide", group="approval", risk="high", params=DecideP, target=lambda p: p.id)
def approval_decide(ctx, p: DecideP) -> dict[str, Any]:
    """Duyệt / từ chối yêu cầu của Super Admin khác."""
    return ctx.state.approvals.decide(ctx, p.id, p.approve)


class CancelP(Params):
    id: ObjId


@op("approval.cancel", group="self", risk="normal", params=CancelP, target=lambda p: p.id)
def approval_cancel(ctx, p: CancelP) -> dict[str, Any]:
    """Huỷ yêu cầu (người yêu cầu / người duyệt, trước khi chạy)."""
    return ctx.state.approvals.cancel(ctx, p.id)
