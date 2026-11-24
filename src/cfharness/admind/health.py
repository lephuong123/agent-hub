"""Kiểm sức khoẻ tên miền mỗi 10 phút (DOM-07) + đối chiếu lệch (DOM-08).

Mỗi tên miền: DNS (CNAME đúng `<tunnel-id>.cfargotunnel.com` trong ĐÚNG zone — qua API), ingress (hostname có trong file tunnel và trỏ
đúng cổng web của hub), tunnel có connector, HTTPS trả trang (mã 200/30x, không phải lỗi Cloudflare 52x/530/1033), chứng chỉ còn > 14 ngày.
Lệch → cảnh báo `domain_*` (subject = id tên miền); hết lệch → hồi phục.
"""

from __future__ import annotations

import json
import logging
import socket
import ssl
import time
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any, Callable

from .alerts import sync_alerts

if TYPE_CHECKING:
    from .dispatch import State

log = logging.getLogger("cfharness.admind.health")
DOMAIN_CODES = {"domain_dns", "domain_tunnel", "domain_tls", "domain_http"}


def http_probe(host: str, timeout: float = 10.0) -> dict[str, Any]:  # pragma: no cover — mạng
    out: dict[str, Any] = {"http": None, "cert_days": None}
    try:
        req = urllib.request.Request(f"https://{host}/login", method="GET", headers={"User-Agent": "cfharness-admind-health"})
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
            out["http"] = r.status
    except urllib.error.HTTPError as e:
        out["http"] = e.code
    except (urllib.error.URLError, OSError, ValueError):
        out["http"] = 0
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=timeout) as s, ctx.wrap_socket(s, server_hostname=host) as ss:
            na = ss.getpeercert().get("notAfter")
            if na:
                out["cert_days"] = int((ssl.cert_time_to_seconds(na) - time.time()) // 86400)
    except (OSError, ssl.SSLError, ValueError):
        out["cert_days"] = None
    return out


def check_one(state: "State", d: dict[str, Any], *, ingress: Any, cf: Any, tunnel_ok: bool | None, tunnel_id: str,
              probe: Callable[[str], dict[str, Any]]) -> dict[str, Any]:
    fqdn = f"{d['sub']}.{d['zone']}" if d["sub"] != "@" else d["zone"]
    h: dict[str, Any] = {"fqdn": fqdn, "checked_at": state.clock()}
    codes: set[str] = set()
    if cf is not None and d.get("cf_zone_id"):
        try:
            recs = cf.dns_find(d["cf_zone_id"], fqdn)
            want = f"{tunnel_id}.cfargotunnel.com" if tunnel_id else None
            h["dns"] = [{"type": r.get("type"), "content": r.get("content"), "proxied": r.get("proxied")} for r in recs]
            if not recs or (want and not any(r.get("type") == "CNAME" and r.get("content") == want for r in recs)):
                codes.add("domain_dns")
        except Exception as e:  # best-effort: lỗi API ghi vào health.dns_error
            h["dns_error"] = str(e)[:200]
    if ingress is not None:
        svc = ingress.hosts().get(fqdn)
        h["ingress"] = svc
        want_svc = d.get("ingress_service")
        if d["kind"] in ("hub", "sac") and (not svc or (want_svc and svc != want_svc)):
            codes.add("domain_tunnel")
    if tunnel_ok is False:
        codes.add("domain_tunnel")
    pr = probe(fqdn)
    h.update(pr)
    if pr.get("http") is not None and not (200 <= int(pr["http"] or 0) < 400 or int(pr["http"] or 0) in (401, 403)):
        codes.add("domain_http")
    if pr.get("cert_days") is not None and pr["cert_days"] < 14:
        codes.add("domain_tls")
    h["codes"] = sorted(codes)
    return {"health": h, "codes": codes}


def check_domains(state: "State", *, probe: Callable[[str], dict[str, Any]] | None = None, only: str | None = None) -> int:
    from .cloudflare import from_state
    from .machine import machine
    from .ops_system import setting

    rows = state.db.all("SELECT d.*, z.name AS zone, z.cf_zone_id FROM domains d JOIN zones z ON z.id=d.zone_id WHERE d.state NOT IN "
                        "('revoked','revoking')" + (" AND d.id=?" if only else ""), ((only,) if only else ()))
    if not rows:
        return 0
    try:
        ing = machine(state).ingress()
    except Exception:  # best-effort: thiếu file ingress → bỏ kiểm ingress
        ing = None
    try:
        cf = from_state(state)
    except Exception:  # best-effort: chưa có token Cloudflare → bỏ kiểm DNS
        cf = None
    tunnel_id, account = setting(state.db, "cf.tunnel_id"), setting(state.db, "cf.account_id")
    tunnel_ok = None
    if cf is not None and tunnel_id and account:
        try:
            tunnel_ok = bool(cf.tunnel_connections(account, tunnel_id))
        except Exception:  # best-effort: không gọi được API tunnel → không kết luận
            tunnel_ok = None
    pr = probe or state.extra.get("http_probe") or http_probe
    for d in rows:
        r = check_one(state, d, ingress=ing, cf=cf, tunnel_ok=tunnel_ok, tunnel_id=tunnel_id or (ing.tunnel if ing else ""), probe=pr)
        st = "healthy" if not r["codes"] else "unhealthy"
        state.db.exec("UPDATE domains SET health=?, state=?, checked_at=? WHERE id=?", (json.dumps(r["health"], ensure_ascii=False), st,
                                                                                       state.clock(), d["id"]))
        sync_alerts(state, "domain", d["id"], r["codes"], managed=DOMAIN_CODES, level_of={"domain_http": "crit", "domain_dns": "crit"})
    reconcile(state, ing)
    return len(rows)


def reconcile(state: "State", ing: Any) -> list[dict[str, Any]]:
    """DOM-08: hostname trong ingress không có bản ghi SAC (mồ côi), hub không có hostname, cổng trùng."""
    issues: list[dict[str, Any]] = []
    known = {f"{d['sub']}.{d['zone']}": d for d in state.db.all("SELECT d.*, z.name AS zone FROM domains d JOIN zones z ON z.id=d.zone_id "
                                                                 "WHERE d.state NOT IN ('revoked')")}
    if ing is not None:
        for host, svc in ing.hosts().items():
            if host not in known:
                issues.append({"kind": "orphan_ingress", "host": host, "service": svc})
    for u in state.db.all("SELECT u.id, u.os_user FROM hub_users u WHERE u.state='active' AND NOT EXISTS (SELECT 1 FROM domains d WHERE "
                          "d.hub_user_id=u.id AND d.kind='hub' AND d.state!='revoked')"):
        issues.append({"kind": "hub_without_domain", "user": u["os_user"], "user_id": u["id"]})
    ports: dict[int, list[str]] = {}
    for h in state.db.all("SELECT h.web_port, h.mcp_port, u.os_user FROM hubs h JOIN hub_users u ON u.id=h.user_id WHERE u.state!='deleted'"):
        for p in (h["web_port"], h["mcp_port"]):
            if p:
                ports.setdefault(int(p), []).append(h["os_user"])
    for p, us in ports.items():
        if len(us) > 1:
            issues.append({"kind": "port_conflict", "port": p, "users": us})
    state.db.set_setting("reconcile.issues", issues)
    sync_alerts(state, "system", "domains", {"domain_orphan"} if any(i["kind"] == "orphan_ingress" for i in issues) else set(),
                managed={"domain_orphan"})
    sync_alerts(state, "system", "ports", {"port_conflict"} if any(i["kind"] == "port_conflict" for i in issues) else set(),
                managed={"port_conflict"})
    return issues
