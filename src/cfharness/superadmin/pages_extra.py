"""Trang SAC cho SA5: tài nguyên máy + lịch sử, nhập user CSV, cập nhật theo đợt, báo cáo tuần/tháng, xuất hồ sơ."""

from __future__ import annotations

from starlette.responses import Response

from .pages_core import _try
from .views import page


def _bars(series, key, w=600, h=100):
    if not series:
        return []
    mx = max(float(x[key]) for x in series) or 1.0
    bw = w / len(series)
    return [{"x": round(i * bw + 1, 1), "w": round(max(bw - 2, 1), 1), "h": round(float(x[key]) / mx * (h - 8), 1),
             "y": round(h - float(x[key]) / mx * (h - 8), 1), "label": f"{x['day']}: {x[key]}"} for i, x in enumerate(series)]


@page(r"^/system$", group="monitor")
async def system(app, request, s, m):
    res = await _try(app, request, s, "sys.resources", default={})
    hist = await _try(app, request, s, "sys.history", {"days": 90}, default={"series": []})
    return app.render(request, "system.html", s, res=res, hist=hist, cost_bars=_bars(hist["series"], "cost"),
                      run_bars=_bars(hist["series"], "running"))


@page(r"^/users/bulk$", group="user_lifecycle")
async def users_bulk(app, request, s, m):
    zones = await _try(app, request, s, "zone.list", default=[])
    accounts = await _try(app, request, s, "claude.list", default=[])
    return app.render(request, "users_bulk.html", s, zones=zones, accounts=accounts)


@page(r"^/users/export$", group="user_view")
async def users_export(app, request, s, m):
    ids = [x for x in request.query_params.getlist("u") if x][:500]
    if not ids:
        rows = await app.call(request, "user.list", sess=s)
        ids = [r["id"] for r in rows]
    if not ids:
        return Response("", media_type="text/csv; charset=utf-8")
    r = await app.call(request, "user.export", {"users": ids}, sess=s)
    return Response(r["csv"], media_type="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=ho-so-user.csv"})


@page(r"^/updates$", group="hub")
async def updates(app, request, s, m):
    users = await _try(app, request, s, "user.list", {"state": "active"}, default=[])
    jobs = [j for j in await _try(app, request, s, "job.list", {"limit": 50}, default=[]) if j["kind"] in ("hub.update", "hub.rollout")]
    return app.render(request, "updates.html", s, users=users, jobs=jobs)


@page(r"^/reports/period$", group="report")
async def report_period(app, request, s, m):
    q = request.query_params
    params = {"period": "month" if q.get("period") == "month" else "week"}
    if q.get("team_id"):
        params["team_id"] = q["team_id"]
    r = await app.call(request, "report.period", params, sess=s)
    teams = await _try(app, request, s, "team.list", default=[])
    return app.render(request, "report_period.html", s, r=r, teams=teams, q=dict(q), bars=_bars(r["trend"], "cost"))
