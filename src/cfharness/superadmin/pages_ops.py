"""Trang SAC: người dùng (danh sách, hồ sơ 360°, nhập), tên miền, cảnh báo, ESTOP."""

from __future__ import annotations

import csv
import io
from typing import Any

from starlette.responses import Response

from .pages_core import _try
from .views import page


def _csv(rows: list[dict[str, Any]], cols: list[str], name: str) -> Response:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for r in rows:
        w.writerow([r.get(c) for c in cols])
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f"attachment; filename={name}.csv"})


@page(r"^/users$", group="user_view")
async def users(app, request, s, m):
    q = request.query_params
    params = {k: q[k] for k in ("state", "group_id", "q") if q.get(k)}
    rows = await app.call(request, "user.list", params, sess=s)
    sort = q.get("sort") or "os_user"
    if rows and sort in rows[0]:
        rows.sort(key=lambda r: (r.get(sort) is None, r.get(sort) if not isinstance(r.get(sort), str) else r[sort].lower()),
                  reverse=q.get("desc") == "1")
    if q.get("format") == "csv":
        return _csv(rows, ["os_user", "full_name", "email", "group", "hostname", "state", "hub_status", "version", "cost_today", "cost_month",
                           "budget_pct", "running", "approvals", "alerts"], "users")
    groups = await _try(app, request, s, "group.list", default=[])
    return app.render(request, "users.html", s, rows=rows, q=dict(q), groups=groups)


@page(r"^/users/import$", group="user_lifecycle")
async def users_import(app, request, s, m):
    cands = await app.call(request, "user.import_scan", sess=s)
    groups = await _try(app, request, s, "group.list", default=[])
    return app.render(request, "users_import.html", s, cands=cands, groups=groups)


@page(r"^/users/new$", group="user_lifecycle")
async def users_new(app, request, s, m):
    zones = await _try(app, request, s, "zone.list", default=[])
    groups = await _try(app, request, s, "group.list", default=[])
    accounts = await _try(app, request, s, "claude.list", default=[])
    ports = await _try(app, request, s, "port.next", default={})
    defaults = await _try(app, request, s, "user.create_defaults", default={})
    teams = await _try(app, request, s, "team.list", default=[])
    return app.render(request, "user_new.html", s, zones=zones, groups=groups, accounts=accounts, ports=ports, defaults=defaults, teams=teams)


@page(r"^/users/(?P<id>[A-Za-z0-9_.-]+)$", group="user_view")
async def user(app, request, s, m):
    prof = await app.call(request, "user.profile", {"id": m["id"]}, sess=s)
    tab = request.query_params.get("tab") or "overview"
    zones = await _try(app, request, s, "zone.list", default=[]) if tab == "access" or tab == "overview" else []
    groups = await _try(app, request, s, "group.list", default=[])
    policy = await _try(app, request, s, "policy.effective", {"user": prof["identity"]["os_user"]}, default=None) if tab == "policy" else None
    reports = await _try(app, request, s, "report.user", {"user": prof["identity"]["id"], "days": 14}, default=[]) if tab == "reports" else []
    accounts = await _try(app, request, s, "claude.list", default=[]) if tab == "access" else []
    bars = _bars(prof["cost"]["by_day"])
    return app.render(request, "user.html", s, p=prof, tab=tab, zones=zones, groups=groups, policy=policy, reports=reports, bars=bars,
                      accounts=accounts)


def _bars(series: list[dict[str, Any]], w: int = 600, h: int = 110) -> list[dict[str, Any]]:
    if not series:
        return []
    mx = max(float(x["usd"]) for x in series) or 1.0
    bw = w / max(len(series), 1)
    return [{"x": round(i * bw + 1, 1), "w": round(max(bw - 2, 1), 1), "h": round(float(x["usd"]) / mx * (h - 10), 1),
             "y": round(h - float(x["usd"]) / mx * (h - 10), 1), "label": f"{x['day']}: ${float(x['usd']):.2f}"} for i, x in enumerate(series)]


@page(r"^/groups$", group="user_view")
async def groups(app, request, s, m):
    rows = await app.call(request, "group.list", sess=s)
    return app.render(request, "groups.html", s, rows=rows)


@page(r"^/domains$", group="domain")
async def domains(app, request, s, m):
    q = request.query_params
    params = {k: q[k] for k in ("zone_id", "state", "q") if q.get(k)}
    rows = await app.call(request, "domain.list", params, sess=s)
    if q.get("format") == "csv":
        return _csv(rows, ["fqdn", "zone", "os_user", "web_port", "kind", "state", "ingress_service", "checked_at"], "domains")
    zones = await app.call(request, "zone.list", sess=s)
    issues = await _try(app, request, s, "domain.reconcile", default=[])
    return app.render(request, "domains.html", s, rows=rows, zones=zones, issues=issues, q=dict(q))


@page(r"^/alerts$", group="monitor")
async def alerts(app, request, s, m):
    rows = await app.call(request, "alert.list", {"include_resolved": request.query_params.get("all") == "1"}, sess=s)
    return app.render(request, "alerts.html", s, rows=rows, show_all=request.query_params.get("all") == "1")


@page(r"^/estop$", group="estop_view")
async def estop(app, request, s, m):
    flags = await app.call(request, "estop.list", sess=s)
    users = await _try(app, request, s, "user.list", default=[])
    teams = await _try(app, request, s, "team.list", default=[])
    groups = await _try(app, request, s, "group.list", default=[])
    return app.render(request, "estop.html", s, flags=flags, users=users, teams=teams, groups=groups)
