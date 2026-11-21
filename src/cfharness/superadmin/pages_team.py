"""Trang SAC: team (danh sách, bảng việc kanban, thành viên), báo cáo ngày (toàn hệ/team/user, tìm kiếm), chi phí."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta

from starlette.responses import Response

from .pages_core import _try
from .views import page


@page(r"^/teams$", group="team_board")
async def teams(app, request, s, m):
    rows = await app.call(request, "team.list", sess=s)
    users = await _try(app, request, s, "user.list", default=[])
    return app.render(request, "teams.html", s, rows=rows, users=users)


@page(r"^/teams/(?P<id>[a-z]+_[0-9a-f]+)$", group="team_board")
async def team(app, request, s, m):
    t = await app.call(request, "team.get", {"id": m["id"]}, sess=s)
    users = await _try(app, request, s, "user.list", default=[]) if s.account["role"] == "super_admin" else []
    by_col = {c["key"]: [x for x in t["tasks"] if x["state"] == c["key"]] for c in t["columns"]}
    return app.render(request, "team.html", s, t=t, by_col=by_col, users=users)


def _yday(app) -> str:
    return (datetime.fromtimestamp(app.clock()) - timedelta(days=1)).strftime("%Y-%m-%d")


@page(r"^/reports$", group="report")
async def reports(app, request, s, m):
    q = request.query_params
    params = {"day": q.get("day") or _yday(app)}
    if q.get("team_id"):
        params["team_id"] = q["team_id"]
    r = await app.call(request, "report.day", params, sess=s)
    if q.get("format") == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        cols = ["os_user", "full_name", "runs", "completed", "failed", "tasks_done", "tasks_stuck", "hours", "approvals", "cost", "cost_prev", "cost_avg7"]
        w.writerow(cols + ["anomalies"])
        for x in r["rows"]:
            w.writerow([x.get(c) for c in cols] + [";".join(x["anomalies"])])
        return Response(buf.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f"attachment; filename=report-{r['day']}.csv"})
    teams_ = await _try(app, request, s, "team.list", default=[])
    found = None
    if q.get("search"):
        found = await _try(app, request, s, "report.search", {"q": q["search"]}, default=[])
    return app.render(request, "reports.html", s, r=r, teams=teams_, q=dict(q), found=found)


@page(r"^/reports/(?P<day>\d{4}-\d{2}-\d{2})/(?P<uid>[A-Za-z0-9_.-]+)$", group="report")
async def report_one(app, request, s, m):
    r = await app.call(request, "report.get", {"user": m["uid"], "day": m["day"]}, sess=s)
    return app.render(request, "report.html", s, r=r)


@page(r"^/costs$", group="cost")
async def costs(app, request, s, m):
    q = request.query_params
    by = q.get("by") if q.get("by") in ("user", "group", "team", "claude", "day") else "user"
    days = int(q.get("days") or 30) if (q.get("days") or "30").isdigit() else 30
    r = await app.call(request, "cost.report", {"by": by, "days": max(1, min(days, 365))}, sess=s)
    if q.get("format") == "csv":
        return Response("key,usd\n" + "\n".join(f"{x['key']},{x['usd']}" for x in r["rows"]), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename=costs-{by}.csv"})
    mx = max([x["usd"] for x in r["rows"]] or [1]) or 1
    return app.render(request, "costs.html", s, r=r, by=by, days=days, mx=mx)
