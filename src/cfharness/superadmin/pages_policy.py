"""Trang SAC: chính sách (danh sách phạm vi, soạn + mô phỏng, lịch sử/rollback), kho tài khoản Claude."""

from __future__ import annotations

from typing import Any

from .pages_core import _try
from .views import page


def content_from_query(q: Any) -> dict[str, Any]:
    """Tham số GET `c.<sec>.<key>` → nội dung chính sách (dùng cho mô phỏng trước khi lưu)."""
    out: dict[str, Any] = {}
    for sec, key, kind in (("budget", "usd_per_day", float), ("budget", "usd_per_run_max", float), ("runs", "max_concurrent", int)):
        v = q.get(f"c.{sec}.{key}")
        if v not in (None, ""):
            try:
                out.setdefault(sec, {})[key] = kind(v)
            except ValueError:
                pass
    for key in ("sandbox", "confine", "export"):
        if q.get(f"c.require.{key}") == "true":
            out.setdefault("require", {})[key] = True
    allow = [x.strip() for x in (q.get("c.models.allow") or "").replace(",", "\n").splitlines() if x.strip()]
    if allow:
        out["models"] = {"allow": allow}
    if q.get("c.min_version"):
        out["min_version"] = q["c.min_version"]
    if q.get("c.policy_required") == "true":
        out["policy_required"] = True
    return out


@page(r"^/policy$", group="policy")
async def policy(app, request, s, m):
    q = request.query_params
    rows = await app.call(request, "policy.list", sess=s)
    scope = q.get("scope") or "default"
    cur = await _try(app, request, s, "policy.get", {"scope": scope}, default=None)
    sim = None
    content = cur["content"] if cur else {}
    if q.get("sim") == "1":
        content = content_from_query(q)
        sim = await _try(app, request, s, "policy.simulate", {"scope": scope, "content": content}, default=None)
    users = await _try(app, request, s, "user.list", default=[])
    groups = await _try(app, request, s, "group.list", default=[])
    teams = await _try(app, request, s, "team.list", default=[])
    eff = None
    if q.get("user"):
        eff = await _try(app, request, s, "policy.effective", {"user": q["user"]}, default=None)
    return app.render(request, "policy.html", s, rows=rows, scope=scope, cur=cur, content=content, sim=sim, users=users, groups=groups,
                      teams=teams, eff=eff, q=dict(q))


@page(r"^/claude$", group="claude")
async def claude(app, request, s, m):
    rows = await app.call(request, "claude.list", sess=s)
    users = await _try(app, request, s, "user.list", default=[])
    return app.render(request, "claude.html", s, rows=rows, users=users)
