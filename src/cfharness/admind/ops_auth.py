"""Thao tác đăng nhập + tài khoản quản trị (FR-SA-AUTH-03..08, 11)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from . import rbac
from .alerts import raise_alert
from .auth import AuthError
from .db import dumps
from .ops import OpError, op
from .ptypes import Email, Name, ObjId, Params, Password, TotpCode, clean_text

RoleLit = Literal["super_admin", "operator", "auditor", "team_lead"]


def _auth_call(fn: Any, *a: Any, **kw: Any) -> Any:
    try:
        return fn(*a, **kw)
    except AuthError as e:
        raise OpError(e.code, e.message, e.data) from None


class Scope(Params):
    groups: list[str] = Field(default_factory=list, max_length=50)
    zones: list[str] = Field(default_factory=list, max_length=50)
    teams: list[str] = Field(default_factory=list, max_length=50)


def enroll_url(ctx, code: str) -> str:
    from .ops_system import setting

    return f"https://{setting(ctx.db, 'sac.hostname')}/enroll/{code}"


# ============================================================================ đăng nhập
class LoginP(Params):
    email: Email
    password: str = Field(min_length=1, max_length=256)
    totp: TotpCode


@op("auth.login", group="self", risk="normal", params=LoginP, public=True, target=lambda p: p.email)
def login(ctx, p: LoginP) -> dict[str, Any]:
    """Đăng nhập: mật khẩu + TOTP → ticket phiên."""
    tok, acct = _auth_call(ctx.state.auth.login, p.email, p.password, p.totp, ip=ctx.ip, ua=ctx.ua)
    ctx.account = acct
    seen = ctx.db.get_setting(f"seen_ips.{acct['id']}", []) or []
    if ctx.ip and ctx.ip not in seen:
        if seen:
            raise_alert(ctx.state, "admin", acct["id"], "superadmin_login_new_ip", "warn")
        ctx.db.set_setting(f"seen_ips.{acct['id']}", (seen + [ctx.ip])[-20:])
    return {"ticket": tok, "account": acct, "nav": sorted(rbac.visible_groups(acct["role"]))}


@op("auth.logout", group="self", risk="normal")
def logout(ctx, p) -> dict[str, Any]:
    """Đăng xuất phiên hiện tại."""
    if ctx.ticket:
        ctx.state.auth.revoke_sid(ctx.ticket.sid)
    return {"ok": True}


class StepUpP(Params):
    totp: TotpCode


@op("auth.step_up", group="self", risk="normal", params=StepUpP)
def step_up(ctx, p: StepUpP) -> dict[str, Any]:
    """Nhập lại TOTP cho thao tác mức Cao (5 phút)."""
    if ctx.ticket is None:
        raise OpError("unauthenticated", "chưa đăng nhập")
    raw = ctx.db.one("SELECT * FROM admin_accounts WHERE id=?", (ctx.account["id"],)) or {}
    if not ctx.state.auth._check_totp(raw, p.totp):  # noqa: SLF001
        ctx.state.auth.limiter.fail(ctx.ip or "?")
        raise OpError("unauthenticated", "mã TOTP không đúng")
    ctx.ticket.strong_at = ctx.state.clock()
    return {"ok": True}


@op("auth.me", group="self", risk="read")
def me(ctx, p) -> dict[str, Any]:
    """Tài khoản + menu được thấy + phiên còn mạnh (step-up) không."""
    return {"account": ctx.account, "nav": sorted(rbac.visible_groups(ctx.account["role"])),
            "strong": bool(ctx.ticket and ctx.state.auth.strong(ctx.ticket)), "sid": ctx.ticket.sid if ctx.ticket else None}


class SessionsP(Params):
    all: bool = False


@op("auth.sessions", group="self", risk="read", params=SessionsP)
def sessions(ctx, p: SessionsP) -> list[dict[str, Any]]:
    """Phiên SAC đang hoạt động — của mình, hoặc mọi người (Super Admin)."""
    everyone = p.all and ctx.account["role"] == rbac.SUPER
    rows = ctx.state.auth.sessions(None if everyone else ctx.account["id"])
    emails = {a["id"]: a["email"] for a in ctx.db.all("SELECT id, email FROM admin_accounts")}
    for r in rows:
        r["email"] = emails.get(r["admin_id"], "")
        r["current"] = bool(ctx.ticket and r["sid"] == ctx.ticket.sid)
    return rows


class SidP(Params):
    sid: str = Field(pattern=r"^[0-9a-f]{12}$")


@op("auth.session_revoke", group="self", risk="normal", params=SidP)
def session_revoke(ctx, p: SidP) -> dict[str, Any]:
    """Thu hồi một phiên (của mình; Super Admin thu hồi được phiên người khác)."""
    own = None if ctx.account["role"] == rbac.SUPER else ctx.account["id"]
    if not ctx.state.auth.revoke_sid(p.sid, admin_id=own):
        raise OpError("not_found", "không có phiên")
    return {"ok": True}


# ============================================================================ kích hoạt tài khoản (public, bằng mã một lần)
class EnrollP(Params):
    code: str = Field(min_length=20, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


@op("auth.enroll_begin", group="self", risk="read", params=EnrollP, public=True)
def enroll_begin(ctx, p: EnrollP) -> dict[str, Any]:
    """Mở link kích hoạt: trả QR TOTP để quét."""
    return _auth_call(ctx.state.auth.enroll_begin, p.code)


class EnrollFinishP(EnrollP):
    password: Password
    totp: TotpCode


@op("auth.enroll_finish", group="self", risk="normal", params=EnrollFinishP, public=True)
def enroll_finish(ctx, p: EnrollFinishP) -> dict[str, Any]:
    """Đặt mật khẩu + xác nhận TOTP → tài khoản hoạt động."""
    acct = _auth_call(ctx.state.auth.enroll_finish, p.code, p.password, p.totp)
    ctx.account = acct
    ctx.audit_target = acct.get("email")
    return {"account": acct}


# ============================================================================ khởi tạo (root trên máy)
class BootstrapP(Params):
    email: Email
    name: Name


@op("sys.bootstrap", group="system", risk="normal", params=BootstrapP, root_only=True, target=lambda p: p.email)
def bootstrap(ctx, p: BootstrapP) -> dict[str, Any]:
    """`cfharness superadmin init`: tạo Super Admin (đầu tiên hoặc khôi phục khi không còn ai) — trả mã kích hoạt một lần."""
    acct, code = _auth_call(ctx.state.auth.create, email=p.email, name=clean_text(p.name), role=rbac.SUPER, scope={}, created_by="root")
    return {"account": acct, "enroll_code": code, "enroll_url": enroll_url(ctx, code),
            "super_admins": ctx.state.auth.count_super(active_only=False)}


class RecoverP(Params):
    email: Email


@op("sys.recover", group="system", risk="normal", params=RecoverP, root_only=True, target=lambda p: p.email)
def recover(ctx, p: RecoverP) -> dict[str, Any]:
    """Break-glass tối thiểu (AUTH-09): root trên máy cấp lại mã kích hoạt cho một Super Admin (mất TOTP)."""
    a = ctx.state.auth.account_by_email(p.email)
    if not a or a["role"] != rbac.SUPER:
        raise OpError("not_found", "không có Super Admin với email này")
    code = ctx.state.auth.reissue_enroll(a["id"])
    raise_alert(ctx.state, "admin", a["id"], "superadmin_login_new_ip", "crit")
    return {"enroll_code": code, "enroll_url": enroll_url(ctx, code)}


# ============================================================================ quản lý tài khoản quản trị (AUTH-08)
@op("admin.list", group="admin_accounts", risk="read")
def admin_list(ctx, p) -> list[dict[str, Any]]:
    """Danh sách tài khoản quản trị."""
    return [ctx.state.auth.account(r["id"]) for r in ctx.db.all("SELECT id FROM admin_accounts ORDER BY created_at")]


class AdminCreateP(Params):
    email: Email
    name: Name
    role: RoleLit
    scope: Scope = Field(default_factory=Scope)
    user_id: ObjId | None = None   # Team Lead: gắn với user hệ thống


@op("admin.create", group="admin_accounts", risk="high", params=AdminCreateP, target=lambda p: p.email)
def admin_create(ctx, p: AdminCreateP) -> dict[str, Any]:
    """Mời tài khoản quản trị mới (Super Admin mới: mức Tới hạn qua `admin.set_role`). Trả mã kích hoạt MỘT lần."""
    if p.role == rbac.SUPER:
        raise OpError("invalid", "tạo Super Admin: tạo vai trò khác rồi nâng bằng 'đổi vai trò' (cần người thứ hai duyệt)")
    if p.role == rbac.TEAM_LEAD and not p.scope.teams:
        raise OpError("invalid", "Team Lead phải gắn ít nhất một team")
    acct, code = _auth_call(ctx.state.auth.create, email=p.email, name=clean_text(p.name), role=p.role, scope=p.scope.model_dump(),
                            created_by=ctx.actor_id, user_id=p.user_id)
    return {"account": acct, "enroll_code": code, "enroll_url": enroll_url(ctx, code)}


class AdminUpdateP(Params):
    id: ObjId
    name: Name | None = None
    scope: Scope | None = None


@op("admin.update", group="admin_accounts", risk="high", params=AdminUpdateP, target=lambda p: p.id)
def admin_update(ctx, p: AdminUpdateP) -> dict[str, Any]:
    """Sửa tên / phạm vi của tài khoản quản trị."""
    a = ctx.state.auth.account(p.id)
    if not a:
        raise OpError("not_found", "không có tài khoản")
    if p.name is not None:
        ctx.db.exec("UPDATE admin_accounts SET name=? WHERE id=?", (clean_text(p.name), p.id))
    if p.scope is not None:
        ctx.db.exec("UPDATE admin_accounts SET scope=? WHERE id=?", (dumps(p.scope.model_dump()), p.id))
    return ctx.state.auth.account(p.id) or {}


class SetRoleP(Params):
    id: ObjId
    role: RoleLit
    reason: str = Field(default="", max_length=500)


@op("admin.set_role", group="admin_accounts", risk="critical", params=SetRoleP, target=lambda p: p.id)
def admin_set_role(ctx, p: SetRoleP) -> dict[str, Any]:
    """Đổi vai trò (thêm/hạ Super Admin — mức Tới hạn, four-eyes)."""
    a = ctx.state.auth.account(p.id)
    if not a:
        raise OpError("not_found", "không có tài khoản")
    if p.id == ctx.actor_id:
        raise OpError("forbidden", "không tự đổi vai trò của mình")
    if a["role"] == rbac.SUPER and p.role != rbac.SUPER and a["state"] == "active" and ctx.state.auth.count_super() <= 1:
        raise OpError("conflict", "không hạ được Super Admin cuối cùng")
    ctx.db.exec("UPDATE admin_accounts SET role=? WHERE id=?", (p.role, p.id))
    ctx.state.auth.revoke_all(p.id)
    return ctx.state.auth.account(p.id) or {}


class IdP(Params):
    id: ObjId


def _guard_last_super(ctx, a: dict[str, Any]) -> None:
    if a["role"] == rbac.SUPER and a["state"] == "active" and ctx.state.auth.count_super() <= 1:
        raise OpError("conflict", "không thao tác được trên Super Admin cuối cùng")


@op("admin.lock", group="admin_accounts", risk="high", params=IdP, target=lambda p: p.id)
def admin_lock(ctx, p: IdP) -> dict[str, Any]:
    """Khoá tài khoản quản trị (huỷ mọi phiên)."""
    a = ctx.state.auth.account(p.id)
    if not a:
        raise OpError("not_found", "không có tài khoản")
    if p.id == ctx.actor_id:
        raise OpError("forbidden", "không tự khoá mình")
    _guard_last_super(ctx, a)
    ctx.state.auth.set_state(p.id, "locked")
    return {"ok": True}


@op("admin.unlock", group="admin_accounts", risk="high", params=IdP, target=lambda p: p.id)
def admin_unlock(ctx, p: IdP) -> dict[str, Any]:
    """Mở khoá tài khoản quản trị (về hoạt động nếu đã kích hoạt, không thì chờ kích hoạt)."""
    raw = ctx.db.one("SELECT pw_hash, totp_on FROM admin_accounts WHERE id=?", (p.id,))
    if not raw:
        raise OpError("not_found", "không có tài khoản")
    ctx.state.auth.set_state(p.id, "active" if raw["pw_hash"] and raw["totp_on"] else "pending")
    return {"ok": True}


@op("admin.reset_totp", group="admin_accounts", risk="high", params=IdP, target=lambda p: p.id)
def admin_reset_totp(ctx, p: IdP) -> dict[str, Any]:
    """Đặt lại mật khẩu + TOTP: tài khoản về chờ kích hoạt, mã kích hoạt mới (MỘT lần)."""
    a = ctx.state.auth.account(p.id)
    if not a:
        raise OpError("not_found", "không có tài khoản")
    if p.id == ctx.actor_id:
        raise OpError("forbidden", "không tự đặt lại TOTP của mình qua SAC — dùng `cfharness superadmin recover` trên máy")
    code = ctx.state.auth.reissue_enroll(p.id)
    return {"enroll_code": code, "enroll_url": enroll_url(ctx, code)}


class DeleteP(IdP):
    reason: str = Field(default="", max_length=500)


@op("admin.delete", group="admin_accounts", risk="critical", params=DeleteP, target=lambda p: p.id)
def admin_delete(ctx, p: DeleteP) -> dict[str, Any]:
    """Xoá tài khoản quản trị (four-eyes)."""
    a = ctx.state.auth.account(p.id)
    if not a:
        raise OpError("not_found", "không có tài khoản")
    if p.id == ctx.actor_id:
        raise OpError("forbidden", "không tự xoá mình")
    _guard_last_super(ctx, a)
    ctx.state.auth.revoke_all(p.id)
    ctx.db.exec("DELETE FROM admin_accounts WHERE id=?", (p.id,))
    ctx.state.vault.delete(f"totp.{p.id}")
    return {"ok": True}
