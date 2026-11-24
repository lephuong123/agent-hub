"""Tổng quan, cảnh báo, ESTOP (FR-SA-MON-01..03, FR-SA-EST)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from .. import admin_control as AC
from ..errors import HubError
from . import rbac
from .alerts import list_alerts, raise_alert, resolve_alert
from .collector import snapshot
from .ops import OpError, op
from .ptypes import ObjId, Params

REASON = Literal["maintenance", "budget", "security", "policy", "revoked", "incident", "other"]


@op("overview", group="user_view", risk="read")
def overview(ctx, p) -> dict[str, Any]:
    """Số liệu trang tổng quan (MON-01) — trong phạm vi của tài khoản."""
    allow = ctx.scoped_user_ids()
    users = [u for u in ctx.db.all("SELECT id, state FROM hub_users WHERE host_id=? AND state!='deleted'", (ctx.db.host_id,))
             if allow is None or u["id"] in allow]
    ids = {u["id"] for u in users}
    hubs = [h for h in ctx.db.all("SELECT * FROM hubs WHERE host_id=?", (ctx.db.host_id,)) if h["user_id"] in ids]
    cost_t = cost_m = 0.0
    running = queued = approvals = 0
    up = 0
    for h in hubs:
        d = snapshot(h)
        c, r = d.get("cost_usd") or {}, d.get("runs") or {}
        cost_t += float(c.get("today") or 0)
        cost_m += float(c.get("month") or 0)
        approvals += int((d.get("approvals") or {}).get("open") or 0)
        if h["status"] == "ok":
            up += 1
            running += int(r.get("running") or 0)
            queued += int(r.get("queued") or 0)
    alerts = [a for a in ctx.db.all("SELECT subject_kind, subject FROM alerts WHERE state!='resolved' AND (muted_until IS NULL OR muted_until<?)",
                                    (ctx.state.clock(),)) if allow is None or (a["subject_kind"] == "user" and a["subject"] in ids)]
    bad_domains = ctx.db.one("SELECT COUNT(*) AS n FROM domains WHERE state='unhealthy'")["n"] if allow is None else 0
    flags = AC.list_flags()
    return {"users": {"total": len(users), "active": sum(u["state"] == "active" for u in users),
                      "suspended": sum(u["state"] == "suspended" for u in users), "revoked": sum(u["state"] == "revoked" for u in users)},
            "hubs": {"up": up, "down": len([h for h in hubs if h["status"] != "ok"])}, "runs": {"running": running, "queued": queued},
            "approvals": approvals, "cost_today": round(cost_t, 4), "cost_month": round(cost_m, 4), "alerts": len(alerts),
            "domains_bad": int(bad_domains), "estop": any(f["scope"] == "all" for f in flags), "estop_users": len([f for f in flags if f["user"]]),
            "jobs_running": len(ctx.state.jobs.list(state="running")), "approvals_pending": len(ctx.state.approvals.list(state="pending"))
            if ctx.can("approval") else 0}


# ============================================================================ cảnh báo
class AlertListP(Params):
    include_resolved: bool = False


@op("alert.list", group="monitor", risk="read", params=AlertListP)
def alert_list(ctx, p: AlertListP) -> list[dict[str, Any]]:
    """Cảnh báo (mở / cả đã xử lý). Kèm tên user cho cảnh báo theo user."""
    rows = list_alerts(ctx.state, include_resolved=p.include_resolved, limit=500)
    names = {u["id"]: u["os_user"] for u in ctx.db.all("SELECT id, os_user FROM hub_users")}
    for r in rows:
        r["subject_name"] = names.get(r["subject"], r["subject"]) if r["subject_kind"] == "user" else r["subject"]
    return rows


class AlertIdP(Params):
    id: ObjId
    note: str = Field(default="", max_length=500)


@op("alert.ack", group="monitor", risk="normal", params=AlertIdP, target=lambda p: p.id)
def alert_ack(ctx, p: AlertIdP) -> dict[str, Any]:
    """Xác nhận đang xử lý (+ ghi chú)."""
    n = ctx.db.exec("UPDATE alerts SET state='ack', acked_by=?, note=CASE WHEN ?!='' THEN ? ELSE note END, updated_at=? WHERE id=? AND state='on'",
                    (ctx.actor_id, p.note, p.note, ctx.state.clock(), p.id))
    if not n:
        raise OpError("not_found", "không có cảnh báo đang mở")
    return {"ok": True}


class MuteP(Params):
    id: ObjId
    hours: int = Field(ge=1, le=168)


@op("alert.mute", group="monitor", risk="normal", params=MuteP, target=lambda p: p.id)
def alert_mute(ctx, p: MuteP) -> dict[str, Any]:
    """Tắt tiếng N giờ (không gửi tin, vẫn hiện)."""
    ctx.db.exec("UPDATE alerts SET muted_until=?, updated_at=? WHERE id=?", (ctx.state.clock() + p.hours * 3600, ctx.state.clock(), p.id))
    return {"ok": True}


@op("alert.resolve", group="monitor", risk="normal", params=AlertIdP, target=lambda p: p.id)
def alert_resolve(ctx, p: AlertIdP) -> dict[str, Any]:
    """Đóng tay một cảnh báo (nếu điều kiện còn, lần thu thập sau sẽ mở lại)."""
    a = ctx.db.one("SELECT * FROM alerts WHERE id=?", (p.id,))
    if not a:
        raise OpError("not_found", "không có cảnh báo")
    resolve_alert(ctx.state, a["subject_kind"], a["subject"], a["code"])
    return {"ok": True}


# ============================================================================ ESTOP
def _team_users(ctx, team_ids: list[str]) -> list[str]:
    out: list[str] = []
    for t in team_ids:
        out += [r["os_user"] for r in ctx.db.all("SELECT u.os_user FROM team_members m JOIN hub_users u ON u.id=m.user_id WHERE m.team_id=?", (t,))]
    return sorted(set(out))


def _targets(ctx, scope: str, ids: list[str]) -> list[str | None]:
    if scope == "all":
        return [None]
    if scope == "user":
        out = []
        for i in ids:
            u = ctx.db.one("SELECT os_user FROM hub_users WHERE (id=? OR os_user=?) AND host_id=?", (i, i, ctx.db.host_id))
            if not u:
                raise OpError("not_found", f"không có user {i}")
            out.append(u["os_user"])
        return out
    if scope == "group":
        return [u["os_user"] for i in ids for u in ctx.db.all("SELECT os_user FROM hub_users WHERE group_id=? AND state!='deleted'", (i,))]
    return _team_users(ctx, ids)  # type: ignore[return-value]


class EstopP(Params):
    scope: Literal["all", "user", "group", "team"]
    ids: list[str] = Field(default_factory=list, max_length=200)
    reason_code: REASON = "other"


def _check_estop_scope(ctx, p: EstopP) -> None:
    acct = ctx.account
    if acct is None or rbac.unrestricted(acct):
        return
    if p.scope == "all":
        raise OpError("forbidden", "ngoài phạm vi được giao")
    if acct["role"] == rbac.TEAM_LEAD:
        if p.scope != "team" or not set(p.ids) <= set(rbac.scope_of(acct).get("teams", [])):
            raise OpError("forbidden", "Team Lead chỉ bật ESTOP cho team của mình")
        return
    for os_user in _targets(ctx, p.scope, p.ids):
        if not rbac.in_scope(acct, ctx.user_subject(os_user or "")):
            raise OpError("forbidden", "ngoài phạm vi được giao")


def _set(ctx, on: bool, p: EstopP) -> dict[str, Any]:
    _check_estop_scope(ctx, p)
    users = _targets(ctx, p.scope, p.ids)
    if not users:
        raise OpError("invalid", "không có đối tượng")
    done = []
    for u in users:
        try:
            AC.set_flag(on, u, reason_code=p.reason_code, by="sac")
        except HubError as e:
            raise OpError("failed", str(e)) from None
        done.append(u or "*")
        if u:
            sid = ctx.db.one("SELECT id FROM hub_users WHERE os_user=? AND host_id=?", (u, ctx.db.host_id))
            if sid:
                (raise_alert(ctx.state, "user", sid["id"], "admin_estop_on", "warn") if on else
                 resolve_alert(ctx.state, "user", sid["id"], "admin_estop_on"))
    ctx.audit_target = ",".join(done)[:200]
    return {"on": on, "targets": done}


@op("estop.on", group="estop_on", risk="normal", params=EstopP)
def estop_on(ctx, p: EstopP) -> dict[str, Any]:
    """Bật ESTOP quản trị (toàn máy / user / nhóm / team). Hub dừng run đang chạy, user không tự gỡ được."""
    return _set(ctx, True, p)


@op("estop.off", group="estop_off", risk="high", params=EstopP)
def estop_off(ctx, p: EstopP) -> dict[str, Any]:
    """Gỡ ESTOP quản trị."""
    return _set(ctx, False, p)


@op("estop.list", group="estop_view", risk="read")
def estop_list(ctx, p) -> list[dict[str, Any]]:
    """Cờ ESTOP đang bật + hub đã áp chưa (EST-04)."""
    out = []
    seen = {}
    for h in ctx.db.all("SELECT u.os_user, h.snapshot FROM hubs h JOIN hub_users u ON u.id=h.user_id"):
        seen[h["os_user"]] = bool((snapshot(h).get("admin_estop") or {}).get("seen"))
    for f in AC.list_flags():
        f["applied"] = seen.get(f["user"]) if f["user"] else None
        out.append(f)
    return out
