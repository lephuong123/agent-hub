"""Thao tác báo cáo (FR-SA-RPT-01..09) và chi phí (CLA-04)."""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any

from pydantic import Field

from . import rbac
from .db import loads
from .ops import OpError, op
from .ptypes import Params
from .reports import day_rows

DAY = r"^\d{4}-\d{2}-\d{2}$"


def _activity_ok(ctx) -> bool:
    from .ops_system import setting

    return ctx.can("user_activity") and bool(setting(ctx.db, "visibility.activity"))


def _allowed_users(ctx, team_id: str | None = None) -> set[str] | None:
    """Team Lead: chỉ thành viên các team mình; Operator có phạm vi: theo phạm vi; còn lại: mọi user. `team_id` lọc thêm."""
    ids = ctx.scoped_user_ids()
    if ctx.account and ctx.account["role"] == rbac.TEAM_LEAD:
        teams = rbac.scope_of(ctx.account).get("teams", [])
        ids = {m["user_id"] for t in teams for m in ctx.db.all("SELECT user_id FROM team_members WHERE team_id=?", (t,))}
    if team_id:
        members = {m["user_id"] for m in ctx.db.all("SELECT user_id FROM team_members WHERE team_id=?", (team_id,))}
        ids = members if ids is None else ids & members
    return ids


class DayP(Params):
    day: str | None = Field(default=None, pattern=DAY)
    team_id: str | None = Field(default=None, max_length=40)


@op("report.day", group="report", risk="read", params=DayP)
def report_day(ctx, p: DayP) -> dict[str, Any]:
    """Báo cáo gộp một ngày (toàn hệ / team): user × chỉ số, so với hôm trước + trung bình 7 ngày (RPT-05)."""
    day = p.day or (datetime.fromtimestamp(ctx.state.clock()) - timedelta(days=1)).strftime("%Y-%m-%d")
    rows = day_rows(ctx.state, day, _allowed_users(ctx, p.team_id))
    return {"day": day, "rows": rows, "totals": {"runs": sum(int(r["runs"] or 0) for r in rows), "cost": round(sum(float(r["cost"] or 0) for r in rows), 4),
                                                 "missing": sum(not r["has_report"] for r in rows),
                                                 "anomalies": sum(1 for r in rows if r["anomalies"] and r["has_report"])}}


class UserDaysP(Params):
    user: str = Field(min_length=1, max_length=64)
    days: int = Field(default=14, ge=1, le=365)


def _check_user(ctx, key: str) -> dict[str, Any]:
    u = ctx.db.one("SELECT * FROM hub_users WHERE id=? OR os_user=?", (key, key))
    if not u:
        raise OpError("not_found", "không có user")
    allow = _allowed_users(ctx)
    if allow is not None and u["id"] not in allow:
        raise OpError("forbidden", "ngoài phạm vi")
    return u


def _strip(ctx, data: dict[str, Any]) -> dict[str, Any]:
    if not _activity_ok(ctx):
        data = {k: v for k, v in data.items() if k != "items"}
    return data


@op("report.user", group="report", risk="read", params=UserDaysP)
def report_user(ctx, p: UserDaysP) -> list[dict[str, Any]]:
    """Báo cáo ngày của một user trong N ngày."""
    u = _check_user(ctx, p.user)
    since = (datetime.fromtimestamp(ctx.state.clock()) - timedelta(days=p.days)).strftime("%Y-%m-%d")
    return [{"day": r["day"], "level": r["level"], "data": _strip(ctx, loads(r["data"], {})), "anomalies": loads(r["anomalies"], [])}
            for r in ctx.db.all("SELECT * FROM daily_reports WHERE user_id=? AND day>=? ORDER BY day DESC", (u["id"], since))]


class GetP(Params):
    user: str = Field(min_length=1, max_length=64)
    day: str = Field(pattern=DAY)


@op("report.get", group="report", risk="read", params=GetP, audit_read=True)
def report_get(ctx, p: GetP) -> dict[str, Any]:
    """Một báo cáo ngày (mức Hoạt động chỉ khi được xem — ghi audit vì có thể lộ tiêu đề việc)."""
    u = _check_user(ctx, p.user)
    r = ctx.db.one("SELECT * FROM daily_reports WHERE user_id=? AND day=?", (u["id"], p.day))
    if not r:
        raise OpError("not_found", "không có báo cáo")
    ctx.audit_target = u["os_user"]
    return {"user": {"id": u["id"], "os_user": u["os_user"], "full_name": u["full_name"]}, "day": p.day, "data": _strip(ctx, loads(r["data"], {})),
            "anomalies": loads(r["anomalies"], []), "activity_visible": _activity_ok(ctx)}


class SearchP(Params):
    q: str = Field(min_length=2, max_length=80)
    since: str | None = Field(default=None, pattern=DAY)
    until: str | None = Field(default=None, pattern=DAY)


@op("report.search", group="user_activity", risk="read", params=SearchP, audit_read=True)
def report_search(ctx, p: SearchP) -> list[dict[str, Any]]:
    """Tìm theo từ khoá tiêu đề việc trong báo cáo (RPT-08) — cần mức Hoạt động."""
    if not _activity_ok(ctx):
        raise OpError("forbidden", "mức xem Hoạt động đang tắt")
    allow = _allowed_users(ctx)
    out = []
    q = p.q.lower()
    for r in ctx.db.all("SELECT r.*, u.os_user FROM daily_reports r JOIN hub_users u ON u.id=r.user_id WHERE r.day>=? AND r.day<=? ORDER BY r.day DESC",
                        (p.since or "0000-00-00", p.until or "9999-99-99")):
        if allow is not None and r["user_id"] not in allow:
            continue
        for it in loads(r["data"], {}).get("items") or []:
            if q in str(it.get("title") or "").lower() or q in str(it.get("summary") or "").lower():
                out.append({"day": r["day"], "os_user": r["os_user"], "user_id": r["user_id"], **it})
        if len(out) >= 500:
            break
    return out


@op("report.send_now", group="system", risk="normal", params=DayP)
def report_send_now(ctx, p: DayP) -> dict[str, Any]:
    """Gửi lại báo cáo một ngày ngay (kiểm kênh gửi)."""
    from .reports import send_digest

    day = p.day or (datetime.fromtimestamp(ctx.state.clock()) - timedelta(days=1)).strftime("%Y-%m-%d")
    send_digest(ctx.state, day)
    return {"day": day}


# ============================================================================ chi phí (CLA-04)
class CostP(Params):
    days: int = Field(default=30, ge=1, le=365)
    by: str = Field(default="user", pattern=r"^(user|group|team|claude|day)$")


@op("cost.report", group="cost", risk="read", params=CostP)
def cost_report(ctx, p: CostP) -> dict[str, Any]:
    """Chi phí theo user / nhóm / team / tài khoản Claude / ngày (từ mẫu lịch sử: max chi phí-hôm-nay mỗi ngày mỗi user)."""
    since = ctx.state.clock() - p.days * 86400
    allow = _allowed_users(ctx)
    per: dict[tuple[str, str], float] = {}
    for r in ctx.db.all("SELECT ts, user_id, cost_today FROM samples WHERE ts>=?", (since,)):
        if allow is not None and r["user_id"] not in allow:
            continue
        k = (r["user_id"], time.strftime("%Y-%m-%d", time.localtime(r["ts"])))
        per[k] = max(per.get(k, 0.0), float(r["cost_today"] or 0))
    users = {u["id"]: u for u in ctx.db.all("SELECT id, os_user, group_id FROM hub_users")}
    groups = {g["id"]: g["name"] for g in ctx.db.all("SELECT id, name FROM groups_")}
    acct = {r["user_id"]: r["account_id"] for r in ctx.db.all("SELECT user_id, account_id FROM user_claude")}
    accts = {a["id"]: a["name"] for a in ctx.db.all("SELECT id, name FROM claude_accounts")}
    teams: dict[str, list[str]] = {}
    for m in ctx.db.all("SELECT m.user_id, t.name FROM team_members m JOIN teams t ON t.id=m.team_id"):
        teams.setdefault(m["user_id"], []).append(m["name"])
    agg: dict[str, float] = {}
    for (uid, day), v in per.items():
        u = users.get(uid, {})
        keys = {"user": [u.get("os_user", uid)], "group": [groups.get(u.get("group_id") or "", "(không nhóm)")],
                "team": teams.get(uid) or ["(không team)"], "claude": [accts.get(acct.get(uid, ""), "(chưa gán)")], "day": [day]}[p.by]
        for k in keys:
            agg[k] = agg.get(k, 0.0) + v
    rows = sorted(({"key": k, "usd": round(v, 4)} for k, v in agg.items()), key=lambda x: (x["key"] if p.by == "day" else -x["usd"]))
    return {"by": p.by, "days": p.days, "rows": rows, "total": round(sum(agg.values()) if p.by != "team" else sum(per.values()), 4)}
