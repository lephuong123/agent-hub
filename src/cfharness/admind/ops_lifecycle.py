"""Thao tác vòng đời user (USR-02..12), điều khiển hub (HUB-01, 02, 04, 06, 07, 10), truy cập của user (ACC-01..03)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from ..redact import redact_str
from . import lifecycle  # noqa: F401 — đăng ký loại job user.*
from .db import new_id
from .jobs import JobCtx, Step, StepError, kind
from .ops import OpError, op
from .ops_domains import check_sub, free_ports
from .ops_users import find_user
from .ptypes import DnsLabel, Email, Name, ObjId, OsUser, Params, Tag, clean_text

RESERVED_USERS = {"root", "admin", "administrator", "daemon", "nobody", "guest", "shared", "superadmin", "_cfh_sac", "cfharness", "www", "mail"}
TRANSITIONS = {
    "user.suspend": ({"active"}, "suspended"), "user.resume": ({"suspended"}, "active"), "user.revoke": ({"active", "suspended"}, "revoked"),
    "user.restore": ({"revoked"}, "active"), "user.delete": ({"revoked"}, "deleted"),
}


def _transition(ctx, u: dict[str, Any], op_name: str) -> None:
    allowed, _to = TRANSITIONS[op_name]
    if u["state"] not in allowed:
        raise OpError("conflict", f"user đang ở trạng thái '{u['state']}' — không làm được thao tác này")
    if ctx.state.jobs.busy(u["os_user"]):
        raise OpError("conflict", "user đang có job chạy")


# ============================================================================ tạo user (USR-02..04)
@op("user.create_defaults", group="user_lifecycle", risk="read")
def create_defaults(ctx, p) -> dict[str, Any]:
    """Giá trị mặc định cho wizard tạo user."""
    from .ops_system import setting

    w, m = free_ports(ctx.db)
    return {"version": setting(ctx.db, "defaults.version"), "web_port": w, "mcp_port": m, "workspace": setting(ctx.db, "defaults.workspace_root"),
            "create_manager": setting(ctx.db, "defaults.create_manager"), "kgs_url": setting(ctx.db, "defaults.kgs_url")}


class CreateP(Params):
    os_user: OsUser
    full_name: Name
    email: Email | None = None
    group_id: ObjId | None = None
    zone_id: ObjId | None = None
    sub: DnsLabel | None = None
    web_port: int | None = Field(default=None, ge=1024, le=65535)
    mcp_port: int | None = Field(default=None, ge=1024, le=65535)
    version: Tag | None = None
    claude_account_id: ObjId | None = None
    claude_token: str | None = Field(default=None, min_length=20, max_length=400)
    create_manager: bool = True
    kgs_url: str | None = Field(default=None, max_length=300, pattern=r"^https://[A-Za-z0-9.-]+(:\d+)?(/\S*)?$")
    teams: list[ObjId] = Field(default_factory=list, max_length=20)
    budget_usd_per_day: float | None = Field(default=None, ge=0, le=10000)


@op("user.create", group="user_lifecycle", risk="high", params=CreateP, target=lambda p: p.os_user)
def user_create(ctx, p: CreateP) -> dict[str, Any]:
    """Tạo user + hub + tên miền (job 11 bước, chạy lại được). Token Claude chỉ đi vào job (RAM), không lưu DB."""
    from .machine import machine
    from .ops_system import setting

    if p.os_user in RESERVED_USERS:
        raise OpError("invalid", "tên user dành riêng")
    if ctx.db.one("SELECT id FROM hub_users WHERE host_id=? AND os_user=? AND state!='deleted'", (ctx.db.host_id, p.os_user)):
        raise OpError("conflict", "user đã có trong SAC")
    if machine(ctx.state).os_user_exists(p.os_user):
        raise OpError("conflict", "user macOS đã có — dùng 'Nhập user có sẵn'")
    token = (p.claude_token or "").strip().replace(" ", "")
    if p.claude_account_id:
        if not ctx.db.one("SELECT id FROM claude_accounts WHERE id=?", (p.claude_account_id,)):
            raise OpError("not_found", "không có tài khoản Claude")
        token = ctx.state.vault.get(f"claude.{p.claude_account_id}") or ""
    if not token.startswith("sk-ant-"):
        raise OpError("invalid", "cần token Claude (chọn từ kho hoặc dán sk-ant-…)")
    if bool(p.zone_id) != bool(p.sub):
        raise OpError("invalid", "chọn cả zone và tên miền con (hoặc bỏ trống cả hai)")
    web, mcp = (p.web_port, p.mcp_port) if p.web_port and p.mcp_port else free_ports(ctx.db)
    if web == mcp:
        raise OpError("invalid", "cổng web và MCP phải khác nhau")
    for port in (web, mcp):
        if ctx.db.one("SELECT id FROM hubs WHERE web_port=? OR mcp_port=?", (port, port)):
            raise OpError("conflict", f"cổng {port} đã dùng")
    fqdn = None
    if p.zone_id:
        z = ctx.db.one("SELECT name FROM zones WHERE id=?", (p.zone_id,))
        if not z:
            raise OpError("not_found", "không có zone")
        check_sub(ctx.db, p.sub, p.zone_id)  # type: ignore[arg-type]
        fqdn = f"{p.sub}.{z['name']}"
    now = ctx.state.clock()
    uid = new_id("u")
    did = new_id("d") if p.zone_id else None
    old = ctx.db.one("SELECT id FROM hub_users WHERE host_id=? AND os_user=?", (ctx.db.host_id, p.os_user))
    with ctx.db.tx() as c:
        if old:
            uid = old["id"]
            c.execute("UPDATE hub_users SET full_name=?, email=?, group_id=?, state='creating', created_at=?, created_by=?, imported=0, revoked_at=NULL "
                      "WHERE id=?", (clean_text(p.full_name), p.email or "", p.group_id, now, ctx.actor_id, uid))
            c.execute("DELETE FROM hubs WHERE user_id=?", (uid,))
        else:
            c.execute("INSERT INTO hub_users(id, host_id, os_user, full_name, email, group_id, state, created_at, created_by) "
                      "VALUES (?,?,?,?,?,?,'creating',?,?)", (uid, ctx.db.host_id, p.os_user, clean_text(p.full_name), p.email or "",
                                                              p.group_id, now, ctx.actor_id))
        c.execute("INSERT INTO hubs(id, host_id, user_id, web_port, mcp_port, version, install_kind) VALUES (?,?,?,?,?,?,'uv_tool')",
                  (f"hub_{uid[-16:]}", ctx.db.host_id, uid, web, mcp, (p.version or setting(ctx.db, "defaults.version")).lstrip("v")))
        if did:
            c.execute("INSERT INTO domains(id, host_id, hub_user_id, zone_id, sub, kind, ingress_service, state, created_at) "
                      "VALUES (?,?,?,?,?,'hub',?,'creating',?)", (did, ctx.db.host_id, uid, p.zone_id, p.sub, f"http://127.0.0.1:{web}", now))
        if p.claude_account_id:
            c.execute("INSERT OR REPLACE INTO user_claude(user_id, account_id, assigned_at) VALUES (?,?,?)", (uid, p.claude_account_id, now))
    params = {"user_id": uid, "os_user": p.os_user, "full_name": p.full_name, "web_port": web, "mcp_port": mcp, "fqdn": fqdn,
              "domain_id": did, "version": p.version or setting(ctx.db, "defaults.version"), "create_manager": p.create_manager,
              "kgs_url": p.kgs_url, "teams": p.teams, "workspace": setting(ctx.db, "defaults.workspace_root"), "_needs_secrets": True}
    if p.budget_usd_per_day is not None:
        try:
            from .ops_policy import save_user_budget

            save_user_budget(ctx, p.os_user, p.budget_usd_per_day)
        except OpError:  # best-effort: ngân sách lỗi không chặn tạo user (sửa sau ở trang Chính sách)
            pass
    jid = ctx.state.jobs.submit("user.create", target=p.os_user, params=params, created_by=ctx.actor_id, secrets={"claude_token": token})
    ctx.job_id = jid
    return {"job_id": jid, "user_id": uid}


class JobIdP(Params):
    job_id: ObjId


@op("user.totp_link", group="user_lifecycle", risk="normal", params=JobIdP, target=lambda p: p.job_id)
def totp_link(ctx, p: JobIdP) -> dict[str, Any]:
    """Lấy link đăng ký TOTP do job tạo (MỘT lần — sau đó mất)."""
    from .lifecycle import take_reveal

    link = take_reveal(ctx.state, p.job_id)
    if not link:
        raise OpError("not_found", "link đã xem hoặc hết hạn — cấp lại TOTP ở hồ sơ user")
    return {"totp_url": link}


# ============================================================================ khoá / mở / thu hồi / khôi phục / xoá
REASON = Literal["maintenance", "budget", "security", "policy", "revoked", "incident", "other"]


class UserRefP(Params):
    user: str = Field(min_length=1, max_length=64)


class SuspendP(UserRefP):
    reason_code: REASON = "other"


def _submit(ctx, kind_name: str, u: dict[str, Any], **extra: Any) -> dict[str, Any]:
    jid = ctx.state.jobs.submit(kind_name, target=u["os_user"], params={"user_id": u["id"], "os_user": u["os_user"], **extra},
                                created_by=ctx.actor_id, approved_by=ctx.approved_by)
    ctx.job_id = jid
    return {"job_id": jid}


@op("user.suspend", group="user_suspend", risk="high", params=SuspendP, scope="user", target=lambda p: p.user)
def user_suspend(ctx, p: SuspendP) -> dict[str, Any]:
    """Tạm khoá: ESTOP + dừng run + huỷ phiên web + dừng web. Dữ liệu giữ nguyên."""
    u = find_user(ctx, p.user)
    _transition(ctx, u, "user.suspend")
    return _submit(ctx, "user.suspend", u, reason_code=p.reason_code)


@op("user.resume", group="user_suspend", risk="high", params=UserRefP, scope="user", target=lambda p: p.user)
def user_resume(ctx, p: UserRefP) -> dict[str, Any]:
    """Mở khoá (đảo tạm khoá)."""
    u = find_user(ctx, p.user)
    _transition(ctx, u, "user.resume")
    return _submit(ctx, "user.resume", u)


@op("user.revoke", group="user_lifecycle", risk="high", params=UserRefP, scope="user", target=lambda p: p.user)
def user_revoke(ctx, p: UserRefP) -> dict[str, Any]:
    """Thu hồi: ESTOP revoked → bootout → gỡ nhóm → thu hồi tên miền → vô hiệu TOTP/thiết bị. Không xoá HUB_HOME."""
    u = find_user(ctx, p.user)
    _transition(ctx, u, "user.revoke")
    return _submit(ctx, "user.revoke", u)


class RestoreP(UserRefP):
    zone_id: ObjId | None = None
    sub: DnsLabel | None = None


@op("user.restore", group="user_lifecycle", risk="high", params=RestoreP, scope="user", target=lambda p: p.user)
def user_restore(ctx, p: RestoreP) -> dict[str, Any]:
    """Khôi phục user đã thu hồi: nhóm, LaunchDaemon, (tên miền mới nếu chọn), TOTP mới."""
    u = find_user(ctx, p.user)
    _transition(ctx, u, "user.restore")
    did = None
    if p.zone_id and p.sub:
        check_sub(ctx.db, p.sub, p.zone_id)
        did = new_id("d")
        h = ctx.db.one("SELECT web_port FROM hubs WHERE user_id=?", (u["id"],)) or {}
        ctx.db.exec("INSERT INTO domains(id, host_id, hub_user_id, zone_id, sub, kind, ingress_service, state, created_at) VALUES (?,?,?,?,?,'hub',?,"
                    "'creating',?)", (did, ctx.db.host_id, u["id"], p.zone_id, p.sub, f"http://127.0.0.1:{h.get('web_port')}", ctx.state.clock()))
    return _submit(ctx, "user.restore", u, domain_id=did)


class DeleteP(UserRefP):
    confirm: str = Field(max_length=64)
    reason: str = Field(min_length=3, max_length=500)


def _delete_pre(ctx, p: DeleteP) -> dict[str, Any]:
    u = find_user(ctx, p.user)
    if p.confirm != u["os_user"]:
        raise OpError("invalid", "gõ lại đúng tên user để xác nhận")
    _transition(ctx, u, "user.delete")
    return u


@op("user.delete", group="user_delete", risk="critical", params=DeleteP, scope="user", target=lambda p: p.user, wait_after_approve_s=86400,
    precheck=_delete_pre)
def user_delete(ctx, p: DeleteP) -> dict[str, Any]:
    """Xoá hẳn (chỉ sau thu hồi): sao lưu mã hoá → xác minh → `sysadminctl -deleteUser`. Four-eyes + chờ 24 giờ sau duyệt."""
    return _submit(ctx, "user.delete", _delete_pre(ctx, p))


@op("user.backup", group="user_lifecycle", risk="normal", params=UserRefP, scope="user", target=lambda p: p.user)
def user_backup(ctx, p: UserRefP) -> dict[str, Any]:
    """Sao lưu home của user ngay (mã hoá, kho quản trị)."""
    return _submit(ctx, "user.backup", find_user(ctx, p.user))


# ============================================================================ điều khiển hub (HUB)
class RestartP(Params):
    users: list[str] = Field(min_length=1, max_length=100)
    part: Literal["daemon", "web", "both"] = "both"


def _active(ctx, key: str) -> dict[str, Any]:
    u = find_user(ctx, key)
    if u["state"] not in ("active",):
        raise OpError("conflict", f"{u['os_user']} không ở trạng thái hoạt động")
    return u


def _check_users_scope(ctx, keys: list[str]) -> list[dict[str, Any]]:
    from . import rbac

    out = []
    for k in keys:
        u = _active(ctx, k)
        if ctx.account and not rbac.unrestricted(ctx.account) and not rbac.in_scope(ctx.account, ctx.user_subject(u["id"])):
            raise OpError("forbidden", "ngoài phạm vi được giao")
        out.append(u)
    return out


@op("hub.restart", group="hub", risk="normal", params=RestartP, target=lambda p: ",".join(p.users)[:200])
def hub_restart(ctx, p: RestartP) -> dict[str, Any]:
    """Khởi động lại daemon / web (một hoặc nhiều hub). Run đang chạy sẽ được daemon tự tiếp tục (auto-resume)."""
    done = []
    for u in _check_users_scope(ctx, p.users):
        for part in (("daemon", "web") if p.part == "both" else (p.part,)):
            r = ctx.state.runner.run(["launchctl", "kickstart", "-k", f"system/com.cfharness.{part}.{u['os_user']}"], timeout=60)
            if not r.ok:
                raise OpError("failed", f"kickstart {part} {u['os_user']} lỗi: {(r.err or r.out).strip()[:200]}")
        done.append(u["os_user"])
    return {"restarted": done}


class StartStopP(Params):
    user: str = Field(min_length=1, max_length=64)
    part: Literal["daemon", "web", "both"] = "both"


@op("hub.stop", group="hub", risk="normal", params=StartStopP, scope="user", target=lambda p: p.user)
def hub_stop(ctx, p: StartStopP) -> dict[str, Any]:
    """Dừng daemon/web (không gỡ LaunchDaemon; khởi động máy lại sẽ chạy lại)."""
    u = find_user(ctx, p.user)
    for part in (("web", "daemon") if p.part == "both" else (p.part,)):
        ctx.state.runner.run(["launchctl", "kill", "TERM", f"system/com.cfharness.{part}.{u['os_user']}"], timeout=60)
    return {"ok": True}


@op("hub.start", group="hub", risk="normal", params=StartStopP, scope="user", target=lambda p: p.user)
def hub_start(ctx, p: StartStopP) -> dict[str, Any]:
    """Chạy daemon/web."""
    u = find_user(ctx, p.user)
    for part in (("daemon", "web") if p.part == "both" else (p.part,)):
        ctx.state.runner.run(["launchctl", "kickstart", f"system/com.cfharness.{part}.{u['os_user']}"], timeout=60)
    return {"ok": True}


@op("hub.doctor", group="hub", risk="normal", params=UserRefP, scope="user", target=lambda p: p.user)
def hub_doctor(ctx, p: UserRefP) -> dict[str, Any]:
    """Chẩn đoán từ xa (`cfharness doctor`, không tốn tiền) — kết quả đã che."""
    u = find_user(ctx, p.user)
    r = ctx.state.runner.cfh(u["os_user"], "doctor", timeout=180)
    return {"ok": r.ok, "output": redact_str((r.out or "") + ("\n" + r.err if r.err else ""))[-20000:]}


class LogsP(UserRefP):
    which: Literal["daemon", "web", "update"] = "daemon"
    lines: int = Field(default=500, ge=10, le=2000)


@op("hub.logs", group="hub", risk="read", params=LogsP, scope="user", target=lambda p: p.user, audit_read=True)
def hub_logs(ctx, p: LogsP) -> dict[str, Any]:
    """Log hệ thống của hub (tối đa 2.000 dòng cuối, đã lọc redact). Không gồm transcript run."""
    u = find_user(ctx, p.user)
    root = Path(ctx.state.extra.get("users_root") or "/Users")
    f = root / u["os_user"] / ".cfharness" / "logs" / f"{p.which}.out"
    try:
        with open(f, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 2_000_000))
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return {"lines": [], "missing": True}
    return {"lines": [redact_str(x) for x in text.splitlines()[-p.lines:]], "missing": False}


class UpdateP(Params):
    users: list[str] = Field(min_length=1, max_length=100)
    tag: Tag
    batch: int = Field(default=0, ge=0, le=50)   # 0 = tất cả cùng lúc (theo giới hạn 3 job song song)


class UpdateStep(Step):
    name, title = "update", "Cài cfharness bản mới (uv tool) + khởi động lại daemon và web"

    def check(self, jc: JobCtx) -> bool:
        r = jc.runner.cfh(jc.params["os_user"], "--version", timeout=60)
        return r.ok and jc.params["tag"].lstrip("v") in r.out

    def apply(self, jc: JobCtx) -> None:
        u = jc.params["os_user"]
        r = jc.runner.as_user(u, [f"/Users/{u}/.local/bin/uv", "tool", "install", "--force", "--python", "3.13",
                                  "git+https://github.com/lephuong123/agent-hub@" + jc.params["tag"]], timeout=900)
        if not r.ok:
            raise StepError("cài bản mới lỗi: " + (r.err or r.out).strip()[-300:])
        for part in ("daemon", "web"):
            jc.runner.run(["launchctl", "kickstart", "-k", f"system/com.cfharness.{part}.{u}"], timeout=60)
        jc.state.db.exec("UPDATE hubs SET version=? WHERE user_id=?", (jc.params["tag"].lstrip("v"), jc.params["user_id"]))
        jc.log(f"{u} → {jc.params['tag']}")


class DoctorCheck(Step):
    name, title = "doctor", "Chẩn đoán sau cập nhật"

    def apply(self, jc: JobCtx) -> None:
        r = jc.runner.cfh(jc.params["os_user"], "doctor", timeout=180)
        jc.log((r.out or "")[-1500:])
        if not r.ok:
            raise StepError("doctor báo lỗi sau cập nhật — xem log; quay về bản cũ bằng 'Cập nhật' với tag trước")


@kind("hub.update")
def k_update(params: dict[str, Any]) -> list[Step]:
    return [UpdateStep(), DoctorCheck()]


@op("hub.update", group="hub", risk="high", params=UpdateP, target=lambda p: f"{p.tag}:{','.join(p.users)}"[:200])
def hub_update(ctx, p: UpdateP) -> dict[str, Any]:
    """Cập nhật một hoặc nhiều hub lên tag (hub đã ghim phiên bản bị bỏ qua — HUB-04)."""
    jobs, skipped = [], []
    for u in _check_users_scope(ctx, p.users):
        h = ctx.db.one("SELECT pinned_tag FROM hubs WHERE user_id=?", (u["id"],)) or {}
        if h.get("pinned_tag") and h["pinned_tag"] != p.tag:
            skipped.append(u["os_user"])
            continue
        jobs.append(ctx.state.jobs.submit("hub.update", target=u["os_user"], params={"user_id": u["id"], "os_user": u["os_user"], "tag": p.tag},
                                          created_by=ctx.actor_id))
    out: dict[str, Any] = {"jobs": jobs, "skipped_pinned": skipped}
    if len(jobs) == 1:
        out["job_id"] = jobs[0]
    return out


class PinP(UserRefP):
    tag: Tag | None = None


@op("hub.pin", group="hub", risk="high", params=PinP, scope="user", target=lambda p: p.user)
def hub_pin(ctx, p: PinP) -> dict[str, Any]:
    """Ghim hub ở một tag (loại khỏi cập nhật hàng loạt); bỏ trống = bỏ ghim."""
    u = find_user(ctx, p.user)
    ctx.db.exec("UPDATE hubs SET pinned_tag=? WHERE user_id=?", (p.tag, u["id"]))
    return {"pinned_tag": p.tag}


ADMIN_KEYS = {"hub.max_concurrent_runs": r"^\d{1,2}$", "workspace.confine.root": r"^/Users/[A-Za-z0-9_.-]+(/[^\0]{0,200})?$",
              "web.public_origin": r"^https://[a-z0-9.-]+$", "admin_export.enabled": r"^(true|false)$", "admin_export.interval_s": r"^\d{2,4}$"}


class HubConfigP(UserRefP):
    key: str = Field(max_length=64)
    value: str = Field(max_length=300)


@op("hub.config_set", group="hub", risk="high", params=HubConfigP, scope="user", target=lambda p: p.user)
def hub_config_set(ctx, p: HubConfigP) -> dict[str, Any]:
    """Đặt khoá cấu hình thuộc danh sách quản trị được phép (HUB-10); khoá khác thuộc quyền user."""
    if p.key not in ADMIN_KEYS or not re.match(ADMIN_KEYS[p.key], p.value):
        raise OpError("invalid", "khoá không thuộc danh sách quản trị hoặc giá trị sai dạng")
    u = find_user(ctx, p.user)
    r = ctx.state.runner.cfh(u["os_user"], "config", "set", p.key, p.value, timeout=60)
    if not r.ok:
        raise OpError("failed", (r.err or r.out).strip()[:300])
    return {"ok": True}


# ============================================================================ truy cập của user (ACC)
TOTP_LINK = re.compile(r"(https?://\S+/enroll\?t=[A-Za-z0-9_\-]+)")


@op("access.totp_reset", group="access", risk="normal", params=UserRefP, scope="user", target=lambda p: p.user)
def access_totp_reset(ctx, p: UserRefP) -> dict[str, Any]:
    """Cấp lại TOTP: tắt → tạo link mới (hạn 24 giờ, hiện MỘT lần) → khởi động lại web (huỷ mọi phiên). Thiết bị tin cậy cũ tự vô hiệu."""
    u = _active(ctx, p.user)
    r = ctx.state.runner
    r.cfh(u["os_user"], "web", "totp", "disable", timeout=60)
    e = r.cfh(u["os_user"], "web", "totp", "enroll", timeout=60)
    m = TOTP_LINK.search(e.out or "")
    if not e.ok or not m:
        raise OpError("failed", "không tạo được link TOTP: " + (e.err or e.out).strip()[:200])
    r.run(["launchctl", "kickstart", "-k", f"system/com.cfharness.web.{u['os_user']}"], timeout=60)
    return {"totp_url": m.group(1)}


@op("access.login_unlock", group="access", risk="normal", params=UserRefP, scope="user", target=lambda p: p.user)
def access_login_unlock(ctx, p: UserRefP) -> dict[str, Any]:
    """Gỡ khoá do nhập sai (không khởi động lại web nếu hub đã có `web unlock-login`; bản cũ → khởi động lại web)."""
    u = _active(ctx, p.user)
    r = ctx.state.runner.cfh(u["os_user"], "web", "unlock-login", timeout=60)
    if r.ok:
        return {"ok": True, "restarted": False}
    ctx.state.runner.run(["launchctl", "kickstart", "-k", f"system/com.cfharness.web.{u['os_user']}"], timeout=60)
    return {"ok": True, "restarted": True}


@op("access.sessions_revoke", group="access", risk="normal", params=UserRefP, scope="user", target=lambda p: p.user)
def access_sessions_revoke(ctx, p: UserRefP) -> dict[str, Any]:
    """Huỷ mọi phiên web + thiết bị tin cậy của user."""
    u = _active(ctx, p.user)
    ctx.state.runner.cfh(u["os_user"], "web", "devices", "revoke-all", timeout=60)
    ctx.state.runner.run(["launchctl", "kickstart", "-k", f"system/com.cfharness.web.{u['os_user']}"], timeout=60)
    return {"ok": True}
