"""Zone + tên miền (FR-SA-DOM): khai zone, bảng tên miền trung tâm, cấp / thu hồi / đổi tên, kiểm sức khoẻ, đối chiếu, cấp cổng."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from . import domain_steps  # noqa: F401 — đăng ký loại job domain.*
from .db import loads, new_id
from .ops import OpError, op
from .ptypes import DnsLabel, ObjId, Params, Zone

LABEL_MAX = 63


def reserved(db: Any) -> set[str]:
    from .ops_system import setting

    return {x.lower() for x in setting(db, "domain.reserved")}


def check_sub(db: Any, sub: str, zone_id: str) -> None:
    if len(sub) > LABEL_MAX:
        raise OpError("invalid", "tên miền con dài quá 63 ký tự")
    if sub in reserved(db):
        raise OpError("invalid", f"'{sub}' là tên dành riêng")
    if db.one("SELECT id FROM domains WHERE zone_id=? AND sub=? AND state NOT IN ('revoked')", (zone_id, sub)):
        raise OpError("conflict", "tên miền đã được dùng")


def free_ports(db: Any, n: int = 2) -> list[int]:
    """Cấp cổng trống trong dải cấu hình (DOM-09): mỗi hub một cặp web/MCP liền nhau."""
    from .ops_system import setting

    lo, hi = int(setting(db, "defaults.port_min")), int(setting(db, "defaults.port_max"))
    used = set()
    for h in db.all("SELECT web_port, mcp_port FROM hubs"):
        used |= {int(x) for x in (h["web_port"], h["mcp_port"]) if x}
    sac = setting(db, "sac.service")
    try:
        used.add(int(sac.rsplit(":", 1)[1]))
    except (ValueError, IndexError):
        pass
    for p in range(lo, hi):
        cand = list(range(p, p + n))
        if all(c not in used and c <= hi for c in cand):
            return cand
    raise OpError("conflict", f"hết cổng trống trong dải {lo}..{hi}")


# ============================================================================ zone (DOM-01)
@op("zone.list", group="domain", risk="read")
def zone_list(ctx, p) -> list[dict[str, Any]]:
    """Zone đã khai + số tên miền."""
    return ctx.db.all("SELECT z.*, (SELECT COUNT(*) FROM domains d WHERE d.zone_id=z.id AND d.state!='revoked') AS domains FROM zones z ORDER BY name")


class ZoneP(Params):
    name: Zone


@op("zone.add", group="domain", risk="high", params=ZoneP, target=lambda p: p.name)
def zone_add(ctx, p: ZoneP) -> dict[str, Any]:
    """Khai zone Cloudflare dùng được — kiểm token API thấy zone."""
    from .cloudflare import from_state

    cf = from_state(ctx.state)
    try:
        zid = cf.zone_id(p.name)
    except Exception as e:  # noqa: BLE001
        raise OpError("failed", str(e)) from None
    if not zid:
        raise OpError("not_found", f"token Cloudflare không thấy zone {p.name}")
    cur = ctx.db.one("SELECT id FROM zones WHERE name=?", (p.name,))
    if cur:
        ctx.db.exec("UPDATE zones SET cf_zone_id=?, token_state='ok', checked_at=? WHERE id=?", (zid, ctx.state.clock(), cur["id"]))
        return {"id": cur["id"], "cf_zone_id": zid}
    i = new_id("z")
    ctx.db.exec("INSERT INTO zones(id, name, cf_zone_id, token_state, checked_at, created_at) VALUES (?,?,?,'ok',?,?)",
                (i, p.name, zid, ctx.state.clock(), ctx.state.clock()))
    return {"id": i, "cf_zone_id": zid}


class ZoneIdP(Params):
    id: ObjId
    reason: str = Field(default="", max_length=500)


@op("zone.verify", group="domain", risk="normal", params=ZoneIdP, target=lambda p: p.id)
def zone_verify(ctx, p: ZoneIdP) -> dict[str, Any]:
    """Kiểm lại token còn thấy zone."""
    z = ctx.db.one("SELECT * FROM zones WHERE id=?", (p.id,))
    if not z:
        raise OpError("not_found", "không có zone")
    from .cloudflare import from_state

    try:
        zid = from_state(ctx.state).zone_id(z["name"])
    except Exception as e:  # noqa: BLE001
        zid = None
        ctx.db.exec("UPDATE zones SET token_state='error', checked_at=? WHERE id=?", (ctx.state.clock(), p.id))
        raise OpError("failed", str(e)) from None
    ctx.db.exec("UPDATE zones SET cf_zone_id=?, token_state=?, checked_at=? WHERE id=?", (zid, "ok" if zid else "no_access", ctx.state.clock(), p.id))
    return {"ok": bool(zid)}


def _zone_remove_pre(ctx, p: ZoneIdP) -> None:
    if not ctx.db.one("SELECT id FROM zones WHERE id=?", (p.id,)):
        raise OpError("not_found", "không có zone")
    if ctx.db.one("SELECT id FROM domains WHERE zone_id=? AND state!='revoked'", (p.id,)):
        raise OpError("conflict", "zone còn tên miền đang dùng")


@op("zone.remove", group="domain", risk="critical", params=ZoneIdP, target=lambda p: p.id, precheck=_zone_remove_pre)
def zone_remove(ctx, p: ZoneIdP) -> dict[str, Any]:
    """Bỏ zone khỏi SAC (không xoá trên Cloudflare). Zone còn tên miền → không bỏ được."""
    _zone_remove_pre(ctx, p)
    ctx.db.exec("DELETE FROM zones WHERE id=?", (p.id,))
    return {"ok": True}


# ============================================================================ tên miền (DOM-02, 07, 08)
class DomainListP(Params):
    zone_id: str | None = Field(default=None, max_length=40)
    state: str | None = Field(default=None, max_length=20)
    q: str | None = Field(default=None, max_length=120)


@op("domain.list", group="domain", risk="read", params=DomainListP)
def domain_list(ctx, p: DomainListP) -> list[dict[str, Any]]:
    """Bảng trung tâm: hostname · zone · user · cổng · DNS · ingress · Access · HTTP · TLS · lần kiểm."""
    rows = ctx.db.all("SELECT d.*, z.name AS zone, u.os_user, h.web_port FROM domains d JOIN zones z ON z.id=d.zone_id LEFT JOIN hub_users u ON "
                      "u.id=d.hub_user_id LEFT JOIN hubs h ON h.user_id=d.hub_user_id ORDER BY z.name, d.sub")
    out = []
    for r in rows:
        r["fqdn"] = r["zone"] if r["sub"] == "@" else f"{r['sub']}.{r['zone']}"
        r["health"] = loads(r["health"], {}) or {}
        if p.zone_id and r["zone_id"] != p.zone_id or p.state and r["state"] != p.state or p.q and p.q.lower() not in r["fqdn"]:
            continue
        out.append(r)
    return out


class DomainIdP(Params):
    id: ObjId


@op("domain.check_now", group="domain", risk="normal", params=DomainIdP, target=lambda p: p.id)
def domain_check_now(ctx, p: DomainIdP) -> dict[str, Any]:
    """Kiểm sức khoẻ ngay một tên miền."""
    from .health import check_domains

    check_domains(ctx.state, only=p.id)
    r = ctx.db.one("SELECT state, health FROM domains WHERE id=?", (p.id,))
    if not r:
        raise OpError("not_found", "không có tên miền")
    return {"state": r["state"], "health": loads(r["health"], {})}


@op("domain.reconcile", group="domain", risk="read")
def domain_reconcile(ctx, p) -> list[dict[str, Any]]:
    """Lệch giữa máy thật và SAC (lần kiểm gần nhất): ingress mồ côi, hub không tên miền, cổng trùng."""
    return ctx.db.get_setting("reconcile.issues", []) or []


@op("port.next", group="domain", risk="read")
def port_next(ctx, p) -> dict[str, Any]:
    """Cặp cổng web/MCP trống tiếp theo."""
    w, m = free_ports(ctx.db)
    return {"web": w, "mcp": m}


# ============================================================================ cấp / thu hồi / đổi tên (DOM-03..05)
class DomainCreateP(Params):
    user: str = Field(min_length=1, max_length=64)
    zone_id: ObjId
    sub: DnsLabel


@op("domain.create", group="domain", risk="high", params=DomainCreateP, scope="user", target=lambda p: p.user)
def domain_create(ctx, p: DomainCreateP) -> dict[str, Any]:
    """Cấp tên miền con cho hub: DNS (đúng zone) → ingress → public_origin → kiểm HTTPS. Chạy thành job."""
    from .ops_users import find_user

    u = find_user(ctx, p.user)
    if ctx.db.one("SELECT id FROM domains WHERE hub_user_id=? AND kind='hub' AND state NOT IN ('revoked')", (u["id"],)):
        raise OpError("conflict", "hub đã có tên miền (đổi tên thay vì cấp thêm — DOM-11)")
    if not ctx.db.one("SELECT id FROM zones WHERE id=?", (p.zone_id,)):
        raise OpError("not_found", "không có zone")
    check_sub(ctx.db, p.sub, p.zone_id)
    did = new_id("d")
    ctx.db.exec("INSERT INTO domains(id, host_id, hub_user_id, zone_id, sub, kind, state, created_at) VALUES (?,?,?,?,?,'hub','creating',?)",
                (did, ctx.db.host_id, u["id"], p.zone_id, p.sub, ctx.state.clock()))
    jid = ctx.state.jobs.submit("domain.create", target=u["os_user"], params={"domain_id": did, "kind": "hub"}, created_by=ctx.actor_id)
    ctx.job_id = jid
    return {"job_id": jid, "domain_id": did}


class DomainRevokeP(Params):
    id: ObjId


def _domain_owner(ctx, did: str) -> dict[str, Any]:
    d = ctx.db.one("SELECT d.*, u.os_user FROM domains d LEFT JOIN hub_users u ON u.id=d.hub_user_id WHERE d.id=?", (did,))
    if not d:
        raise OpError("not_found", "không có tên miền")
    return d


@op("domain.revoke", group="domain", risk="high", params=DomainRevokeP, target=lambda p: p.id)
def domain_revoke(ctx, p: DomainRevokeP) -> dict[str, Any]:
    """Thu hồi: gỡ ingress → gỡ DNS → gỡ Access → xoá public_origin. Hub vẫn chạy cục bộ."""
    d = _domain_owner(ctx, p.id)
    if d["kind"] == "sac":
        raise OpError("forbidden", "không thu hồi tên miền của chính SAC ở đây")
    jid = ctx.state.jobs.submit("domain.revoke", target=d["os_user"] or p.id, params={"domain_id": p.id}, created_by=ctx.actor_id)
    ctx.job_id = jid
    return {"job_id": jid}


class DomainRenameP(Params):
    id: ObjId
    sub: DnsLabel
    zone_id: ObjId | None = None


@op("domain.rename", group="domain", risk="high", params=DomainRenameP, target=lambda p: p.id)
def domain_rename(ctx, p: DomainRenameP) -> dict[str, Any]:
    """Đổi tên miền con: tạo tên mới → public_origin mới → tên cũ chuyển hướng N ngày → huỷ phiên web."""
    d = _domain_owner(ctx, p.id)
    zone = p.zone_id or d["zone_id"]
    check_sub(ctx.db, p.sub, zone)
    nid = new_id("d")
    ctx.db.exec("INSERT INTO domains(id, host_id, hub_user_id, zone_id, sub, kind, state, created_at) VALUES (?,?,?,?,?,'hub','creating',?)",
                (nid, ctx.db.host_id, d["hub_user_id"], zone, p.sub, ctx.state.clock()))
    jid = ctx.state.jobs.submit("domain.rename", target=d["os_user"] or p.id, params={"domain_id": nid, "old_domain_id": p.id},
                                created_by=ctx.actor_id)
    ctx.job_id = jid
    return {"job_id": jid, "domain_id": nid}


class SacDomainP(Params):
    zone_id: ObjId
    sub: DnsLabel = "superadmin"


@op("domain.sac_setup", group="system", risk="high", params=SacDomainP, target=lambda p: p.sub)
def domain_sac_setup(ctx, p: SacDomainP) -> dict[str, Any]:
    """Tạo tên miền cho chính SAC (`superadmin.<zone>`) → ingress tới web SAC loopback. Access cấu hình trên dashboard Cloudflare."""
    from .ops_system import setting

    if ctx.db.one("SELECT id FROM domains WHERE kind='sac' AND state!='revoked'"):
        raise OpError("conflict", "SAC đã có tên miền")
    did = new_id("d")
    ctx.db.exec("INSERT INTO domains(id, host_id, zone_id, sub, kind, ingress_service, state, created_at) VALUES (?,?,?,?,'sac',?,'creating',?)",
                (did, ctx.db.host_id, p.zone_id, p.sub, setting(ctx.db, "sac.service"), ctx.state.clock()))
    z = ctx.db.one("SELECT name FROM zones WHERE id=?", (p.zone_id,))
    ctx.db.set_setting("sac.hostname", f"{p.sub}.{z['name']}", ctx.actor_id)
    jid = ctx.state.jobs.submit("domain.create", target="sac", params={"domain_id": did, "kind": "sac"}, created_by=ctx.actor_id)
    ctx.job_id = jid
    return {"job_id": jid}

