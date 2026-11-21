"""Trang lõi SAC: tổng quan, quản trị viên, phiên, nhật ký, job, chờ duyệt, cấu hình."""

from __future__ import annotations

from typing import Any

from ..admind.client import AdmindError
from .views import api_view, page


async def _try(app, request, s, op: str, params: dict[str, Any] | None = None, default: Any = None) -> Any:
    try:
        return await app.call(request, op, params or {}, sess=s)
    except AdmindError as e:
        if e.code in ("unavailable", "unauthenticated"):
            raise
        return default


@page(r"^/$", group="user_view")
async def overview(app, request, s, m):
    ov = await _try(app, request, s, "overview", default=None)
    return app.render(request, "overview.html", s, ov=ov)


@api_view("overview", group="user_view")
async def overview_api(app, request, s):
    return await _try(app, request, s, "overview", default={})


@page(r"^/admins$", group="admin_accounts")
async def admins(app, request, s, m):
    rows = await app.call(request, "admin.list", sess=s)
    return app.render(request, "admins.html", s, rows=rows)


@page(r"^/sessions$", group="self")
async def sessions(app, request, s, m):
    rows = await app.call(request, "auth.sessions", {"all": s.account["role"] == "super_admin"}, sess=s)
    return app.render(request, "sessions.html", s, rows=rows)


@page(r"^/audit$", group="audit")
async def audit(app, request, s, m):
    q = request.query_params
    params: dict[str, Any] = {"limit": 300}
    for k in ("target", "op_prefix", "actor_id"):
        if q.get(k):
            params[k] = q[k]
    rows = await app.call(request, "audit.search", params, sess=s)
    chain = await _try(app, request, s, "audit.verify", default=None)
    if q.get("format") == "csv":
        import csv
        import io

        from starlette.responses import Response

        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["seq", "ts", "actor_email", "role", "ip", "op", "target", "outcome", "error_code", "job_id", "approver_id"])
        for r in rows:
            w.writerow([r.get(k) for k in ("seq", "ts", "actor_email", "role", "ip", "op", "target", "outcome", "error_code", "job_id", "approver_id")])
        return Response(buf.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=audit.csv"})
    return app.render(request, "audit.html", s, rows=rows, chain=chain, q=dict(q))


@page(r"^/approvals$", group="approval")
async def approvals(app, request, s, m):
    rows = await app.call(request, "approval.list", sess=s)
    return app.render(request, "approvals.html", s, rows=rows)


@page(r"^/jobs$", group="job")
async def jobs(app, request, s, m):
    rows = await app.call(request, "job.list", {"limit": 200}, sess=s)
    return app.render(request, "jobs.html", s, rows=rows)


@page(r"^/jobs/(?P<id>[a-z]+_[0-9a-f]+)$", group="job")
async def job(app, request, s, m):
    j = await app.call(request, "job.get", {"id": m["id"]}, sess=s)
    return app.render(request, "job.html", s, j=j)


@api_view("job", group="job")
async def job_api(app, request, s):
    jid = request.query_params.get("id", "")
    return await _try(app, request, s, "job.get", {"id": jid}, default={})


@page(r"^/settings$", group="system")
async def settings(app, request, s, m):
    cfg = await app.call(request, "sys.config_get", sess=s)
    return app.render(request, "settings.html", s, cfg=cfg)
