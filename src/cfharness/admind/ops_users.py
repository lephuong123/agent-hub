"""Người dùng hệ thống (FR-SA-USR, PRO): danh sách, hồ sơ 360°, nhập user có sẵn, sửa, nhóm. Vòng đời (tạo/khoá/thu hồi) ở `lifecycle.py`."""

from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import Field

from .. import admin_control as AC
from .collector import snapshot
from .db import loads, new_id
from .ops import OpError, op
from .ptypes import Email, Name, ObjId, OsUserAny, Params, clean_text

USER_STATES = ("creating", "create_failed", "active", "suspended", "revoked", "deleted")


def zone_for_host(db: Any, hostname: str) -> dict[str, Any] | None:
    best = None
    for z in db.all("SELECT * FROM zones"):
        if hostname == z["name"] or hostname.endswith("." + z["name"]):
            if best is None or len(z["name"]) > len(best["name"]):
                best = z
    return best


def find_user(ctx, key: str) -> dict[str, Any]:
    u = ctx.db.one("SELECT * FROM hub_users WHERE (id=? OR os_user=?) AND host_id=?", (key, key, ctx.db.host_id))
    if not u:
        raise OpError("not_found", "không có user")
    return u


def user_domains(db: Any, user_id: str) -> list[dict[str, Any]]:
    return db.all("SELECT d.*, z.name AS zone FROM domains d JOIN zones z ON z.id=d.zone_id WHERE d.hub_user_id=? ORDER BY d.created_at",
                  (user_id,))


def _row(ctx, u: dict[str, Any], hub: dict[str, Any] | None, alerts: dict[str, int], groups: dict[str, str]) -> dict[str, Any]:
    d = snapshot(hub)
    c, r = d.get("cost_usd") or {}, d.get("runs") or {}
    doms = user_domains(ctx.db, u["id"])
    return {
        "id": u["id"], "os_user": u["os_user"], "full_name": u["full_name"], "email": u["email"], "group_id": u["group_id"],
        "group": groups.get(u["group_id"] or "", ""), "state": u["state"], "imported": bool(u["imported"]),
        "hostname": next((f"{x['sub']}.{x['zone']}" for x in doms if x["kind"] == "hub"), None),
        "hub_status": (hub or {}).get("status", "missing"), "version": (hub or {}).get("version"), "web_port": (hub or {}).get("web_port"),
        "cost_today": c.get("today"), "cost_month": c.get("month"), "budget_pct": (d.get("budget") or {}).get("day_pct"),
        "running": r.get("running") if (hub or {}).get("status") == "ok" else 0, "approvals": (d.get("approvals") or {}).get("open"),
        "alerts": alerts.get(u["id"], 0), "estop": bool((d.get("admin_estop") or {}).get("on") or (d.get("estop") or {}).get("on")),
        "last_seen": (hub or {}).get("last_seen"),
    }


class ListP(Params):
    state: Literal["creating", "create_failed", "active", "suspended", "revoked", "deleted"] | None = None
    group_id: str | None = Field(default=None, max_length=40)
    q: str | None = Field(default=None, max_length=80)
    include_deleted: bool = False


@op("user.list", group="user_view", risk="read", params=ListP)
def user_list(ctx, p: ListP) -> list[dict[str, Any]]:
    """Danh sách user (USR-01) — Operator/Team Lead chỉ thấy user trong phạm vi."""
    allow = ctx.scoped_user_ids()
    hubs = {h["user_id"]: h for h in ctx.db.all("SELECT * FROM hubs WHERE host_id=?", (ctx.db.host_id,))}
    alerts: dict[str, int] = {}
    for a in ctx.db.all("SELECT subject, COUNT(*) AS n FROM alerts WHERE subject_kind='user' AND state!='resolved' GROUP BY subject"):
        alerts[a["subject"]] = int(a["n"])
    groups = {g["id"]: g["name"] for g in ctx.db.all("SELECT id, name FROM groups_")}
    out = []
    for u in ctx.db.all("SELECT * FROM hub_users WHERE host_id=? ORDER BY os_user", (ctx.db.host_id,)):
        if allow is not None and u["id"] not in allow:
            continue
        if u["state"] == "deleted" and not p.include_deleted and p.state != "deleted":
            continue
        if p.state and u["state"] != p.state:
            continue
        if p.group_id and u["group_id"] != p.group_id:
            continue
        if p.q and p.q.lower() not in f"{u['os_user']} {u['full_name']} {u['email']}".lower():
            continue
        out.append(_row(ctx, u, hubs.get(u["id"]), alerts, groups))
    return out


class UserP(Params):
    id: str = Field(min_length=1, max_length=64)


@op("user.profile", group="user_view", risk="read", params=UserP, scope="user", target=lambda p: p.id)
def user_profile(ctx, p: UserP) -> dict[str, Any]:
    """Hồ sơ 360° (FR-SA-PRO). Trường mức Hoạt động chỉ trả khi vai trò được xem VÀ hệ đã bật mức này (SYS-03)."""
    from .ops_system import setting

    u = find_user(ctx, p.id)
    hub = ctx.db.one("SELECT * FROM hubs WHERE host_id=? AND user_id=?", (ctx.db.host_id, u["id"]))
    d = snapshot(hub)
    prof = loads((hub or {}).get("profile"), {}) or {}
    groups = {g["id"]: g["name"] for g in ctx.db.all("SELECT id, name FROM groups_")}
    alerts = ctx.db.all("SELECT * FROM alerts WHERE subject_kind='user' AND subject=? ORDER BY updated_at DESC LIMIT 50", (u["id"],))
    activity_ok = ctx.can("user_activity") and bool(setting(ctx.db, "visibility.activity"))
    teams = ctx.db.all("SELECT t.id, t.name, t.slug, m.role FROM team_members m JOIN teams t ON t.id=m.team_id WHERE m.user_id=?", (u["id"],))
    out = {
        "identity": {k: u[k] for k in ("id", "os_user", "full_name", "email", "group_id", "state", "note", "created_at", "created_by", "revoked_at",
                                       "imported")} | {"group": groups.get(u["group_id"] or "", ""), "teams": teams},
        "hub": {"status": (hub or {}).get("status", "missing"), "version": (hub or {}).get("version"), "hub_id": (hub or {}).get("hub_id"),
                "web_port": (hub or {}).get("web_port"), "mcp_port": (hub or {}).get("mcp_port"), "install_kind": (hub or {}).get("install_kind"),
                "pinned_tag": (hub or {}).get("pinned_tag"), "last_seen": (hub or {}).get("last_seen"), "daemon": d.get("daemon") or {},
                "guard": d.get("guard") or {}, "disk": d.get("disk") or {}, "policy": d.get("policy") or {}, "infra": prof.get("infra") or {}},
        "access": {"web": d.get("web") or {}, "chat": d.get("chat") or {}, "claude": d.get("claude") or {}, "access": prof.get("access") or {}},
        "metrics": {"runs": d.get("runs") or {}, "approvals": d.get("approvals") or {}, "questions": d.get("questions") or {},
                    "agents": d.get("agents") or {}, "schedules": d.get("schedules") or {}, "estop": d.get("estop") or {},
                    "admin_estop": d.get("admin_estop") or {}, "tasks": prof.get("tasks") or {}},
        "cost": {"usd": d.get("cost_usd") or {}, "budget": d.get("budget") or {}, "by_agent": prof.get("cost_by_agent") or [],
                 "by_model": prof.get("cost_by_model") or [], "by_day": _cost_by_day(ctx, u["id"])},
        "agents": prof.get("agents") or [],
        "domains": [{k: x[k] for k in ("id", "sub", "zone", "kind", "state", "checked_at", "health")} for x in user_domains(ctx.db, u["id"])],
        "alerts": alerts,
        "jobs": ctx.state.jobs.list(target=u["os_user"], limit=20),
        "audit": ctx.state.audit.search(target=u["os_user"], limit=50) + ctx.state.audit.search(target=u["id"], limit=50),
        "activity_visible": activity_ok,
        "activity": (prof.get("activity") or {}) if activity_ok else None,
        "needs_update": bool(d) and int(d.get("schema") or 0) < 2,
    }
    for x in out["domains"]:
        x["health"] = loads(x["health"], {})
    return out


def _cost_by_day(ctx, user_id: str, days: int = 30) -> list[dict[str, Any]]:
    since = ctx.state.clock() - days * 86400
    rows = ctx.db.all("SELECT ts, cost_today FROM samples WHERE user_id=? AND ts>=? ORDER BY ts", (user_id, since))
    by: dict[str, float] = {}
    for r in rows:
        day = time.strftime("%Y-%m-%d", time.localtime(r["ts"]))
        by[day] = max(by.get(day, 0.0), float(r["cost_today"] or 0))
    return [{"day": k, "usd": round(v, 4)} for k, v in sorted(by.items())]


# ============================================================================ nhập user có sẵn (USR-13)
@op("user.import_scan", group="user_lifecycle", risk="read")
def import_scan(ctx, p) -> list[dict[str, Any]]:
    """Quét máy: thành viên `cfharness-users` + plist LaunchDaemon + thư mục trạng thái + hostname ingress → ứng viên chưa có trong SAC."""
    from .machine import machine

    m = machine(ctx.state)
    have = {r["os_user"] for r in ctx.db.all("SELECT os_user FROM hub_users WHERE host_id=? AND state!='deleted'", (ctx.db.host_id,))}
    members, plists = set(m.users_group()), m.plists()
    ov = m.status()
    status_users = {str(h.get("user")) for h in ov.get("hubs") or []} | set(ov.get("user_dirs") or [])
    try:
        ing = m.ingress()
        ports_host: dict[int, list[str]] = {}
        for h in ing.hosts():
            port = ing.port_of(h)
            if port:
                ports_host.setdefault(port, []).append(h)
    except Exception:  # noqa: BLE001 — chưa có file ingress: vẫn nhập được user, tên miền thêm sau
        ports_host = {}
    out = []
    for u in sorted((members | plists | status_users) - have):
        if not AC.USER_RE.match(u):
            continue
        ports = m.plist_ports(u)
        out.append({"os_user": u, "member": u in members, "plist": u in plists, "status_file": u in status_users,
                    "web_port": ports.get("web"), "hostnames": ports_host.get(ports.get("web") or -1, [])})
    return out


class ImportP(Params):
    os_user: OsUserAny
    full_name: Name | None = None
    email: Email | None = None
    group_id: ObjId | None = None


@op("user.import", group="user_lifecycle", risk="high", params=ImportP, target=lambda p: p.os_user)
def user_import(ctx, p: ImportP) -> dict[str, Any]:
    """Đưa một user có sẵn vào SAC — KHÔNG chạm hub (chỉ ghi sac.db)."""
    cands = {c["os_user"]: c for c in import_scan(ctx, None)}
    c = cands.get(p.os_user)
    if not c:
        raise OpError("not_found", "user không có trên máy hoặc đã có trong SAC")
    now = ctx.state.clock()
    uid = new_id("u")
    old = ctx.db.one("SELECT id FROM hub_users WHERE host_id=? AND os_user=?", (ctx.db.host_id, p.os_user))
    with ctx.db.tx() as cx:
        if old:  # user đã xoá khỏi SAC trước đây → dùng lại bản ghi
            uid = old["id"]
            cx.execute("UPDATE hub_users SET state=?, full_name=?, email=?, group_id=?, imported=1, revoked_at=NULL WHERE id=?",
                       ("active" if c["member"] else "revoked", clean_text(p.full_name or p.os_user), p.email or "", p.group_id, uid))
        else:
            cx.execute("INSERT INTO hub_users(id, host_id, os_user, full_name, email, group_id, state, created_at, created_by, imported) "
                       "VALUES (?,?,?,?,?,?,?,?,?,1)", (uid, ctx.db.host_id, p.os_user, clean_text(p.full_name or p.os_user), p.email or "",
                                                         p.group_id, "active" if c["member"] else "revoked", now, ctx.actor_id))
        cx.execute("INSERT INTO hubs(id, host_id, user_id, web_port, install_kind) VALUES (?,?,?,?,?) ON CONFLICT(host_id, user_id) DO UPDATE "
                   "SET web_port=excluded.web_port", (f"hub_{uid[-16:]}", ctx.db.host_id, uid, c["web_port"], "unknown"))
        for host in c["hostnames"]:
            z = zone_for_host(ctx.db, host)
            if z is None:
                zname = ".".join(host.split(".")[-2:])
                zid = new_id("z")
                cx.execute("INSERT OR IGNORE INTO zones(id, name, created_at) VALUES (?,?,?)", (zid, zname, now))
                z = dict(cx.execute("SELECT * FROM zones WHERE name=?", (zname,)).fetchone())
            sub = host[: -(len(z["name"]) + 1)] if host != z["name"] else "@"
            cx.execute("INSERT OR IGNORE INTO domains(id, host_id, hub_user_id, zone_id, sub, kind, ingress_service, state, created_at) "
                       "VALUES (?,?,?,?,?,'hub',?,'unknown',?)", (new_id("d"), ctx.db.host_id, uid, z["id"], sub,
                                                                  f"http://127.0.0.1:{c['web_port']}", now))
    ctx.audit_target = p.os_user
    return {"id": uid, "os_user": p.os_user, "domains": c["hostnames"]}


class UpdateP(Params):
    id: str = Field(min_length=1, max_length=64)
    full_name: Name | None = None
    email: Email | None = None
    group_id: str | None = Field(default=None, max_length=40)
    note: str | None = Field(default=None, max_length=2000)


@op("user.update", group="user_lifecycle", risk="normal", params=UpdateP, scope="user", target=lambda p: p.id)
def user_update(ctx, p: UpdateP) -> dict[str, Any]:
    """Sửa họ tên, email, nhóm, ghi chú quản trị (USR-06 — ghi chú chỉ SAC thấy, không tới agent)."""
    u = find_user(ctx, p.id)
    sets, vals = [], []
    if p.full_name is not None:
        sets.append("full_name=?"), vals.append(clean_text(p.full_name))
    if p.email is not None:
        sets.append("email=?"), vals.append(p.email)
    if p.group_id is not None:
        if p.group_id and not ctx.db.one("SELECT id FROM groups_ WHERE id=?", (p.group_id,)):
            raise OpError("not_found", "không có nhóm")
        sets.append("group_id=?"), vals.append(p.group_id or None)
    if p.note is not None:
        sets.append("note=?"), vals.append(clean_text(p.note))
    if sets:
        ctx.db.exec(f"UPDATE hub_users SET {', '.join(sets)} WHERE id=?", (*vals, u["id"]))
    return {"ok": True}


# ============================================================================ nhóm quản trị (USR-14)
@op("group.list", group="user_view", risk="read")
def group_list(ctx, p) -> list[dict[str, Any]]:
    """Nhóm quản trị (phòng ban)."""
    return ctx.db.all("SELECT g.*, (SELECT COUNT(*) FROM hub_users u WHERE u.group_id=g.id AND u.state!='deleted') AS members FROM groups_ g "
                      "ORDER BY name")


class GroupP(Params):
    name: Name
    description: str = Field(default="", max_length=500)


@op("group.create", group="user_lifecycle", risk="normal", params=GroupP, target=lambda p: p.name)
def group_create(ctx, p: GroupP) -> dict[str, Any]:
    """Tạo nhóm."""
    if ctx.db.one("SELECT id FROM groups_ WHERE name=?", (p.name,)):
        raise OpError("conflict", "trùng tên nhóm")
    gid = new_id("g")
    ctx.db.exec("INSERT INTO groups_(id, name, description, created_at) VALUES (?,?,?,?)", (gid, clean_text(p.name), clean_text(p.description),
                                                                                           ctx.state.clock()))
    return {"id": gid}


class GroupIdP(Params):
    id: ObjId


@op("group.delete", group="user_lifecycle", risk="normal", params=GroupIdP, target=lambda p: p.id)
def group_delete(ctx, p: GroupIdP) -> dict[str, Any]:
    """Xoá nhóm rỗng."""
    if ctx.db.one("SELECT id FROM hub_users WHERE group_id=? AND state!='deleted'", (p.id,)):
        raise OpError("conflict", "nhóm còn thành viên")
    ctx.db.exec("DELETE FROM groups_ WHERE id=?", (p.id,))
    return {"ok": True}

