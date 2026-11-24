"""SA5 (mức S): tự gỡ ESTOP theo hẹn (EST-03), tài nguyên máy (MON-04), lịch sử 90 ngày (MON-05), nhập user từ CSV (JOB-04),
cập nhật theo đợt (HUB-03), báo cáo tuần/tháng (RPT-07), xuất hồ sơ nhiều user (PRO-10)."""

from __future__ import annotations

import csv
import io
import time
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import Field

from .db import loads
from .jobs import JobCtx, Step, StepError, kind
from .ops import OpError, op
from .ptypes import Params, Tag


# ============================================================================ EST-03: tự gỡ ESTOP theo hẹn
class EstopTimedP(Params):
    scope: Literal["all", "user", "group", "team"]
    ids: list[str] = Field(default_factory=list, max_length=200)
    reason_code: Literal["maintenance", "budget", "security", "policy", "incident", "other"] = "maintenance"
    hours: float = Field(gt=0, le=168)


@op("estop.on_timed", group="estop_on", risk="normal", params=EstopTimedP)
def estop_on_timed(ctx, p: EstopTimedP) -> dict[str, Any]:
    """Bật ESTOP và hẹn tự gỡ sau N giờ (bảo trì có lịch). Việc gỡ do admind thực hiện, ghi audit."""
    from .ops_monitor import EstopP, _set

    r = _set(ctx, True, EstopP(scope=p.scope, ids=p.ids, reason_code=p.reason_code))
    timers = ctx.db.get_setting("estop.timers", []) or []
    timers.append({"scope": p.scope, "ids": p.ids, "until": ctx.state.clock() + p.hours * 3600, "by": ctx.actor_id})
    ctx.db.set_setting("estop.timers", timers)
    return {**r, "until": timers[-1]["until"]}


def run_estop_timers(state: Any) -> int:
    from .. import admin_control as AC
    from .ops_monitor import _targets

    timers = state.db.get_setting("estop.timers", []) or []
    keep, n = [], 0
    for t in timers:
        if t["until"] > state.clock():
            keep.append(t)
            continue

        class _C:  # ngữ cảnh tối thiểu cho _targets
            db = state.db

        for u in _targets(_C, t["scope"], t["ids"]):
            AC.set_flag(False, u, by="sac")
            n += 1
        state.audit.record(op="estop.auto_off", outcome="ok", actor={"id": "timer", "role": "system"}, target=",".join(t["ids"]) or "*",
                           params={"scope": t["scope"]})
    state.db.set_setting("estop.timers", keep)
    return n


# ============================================================================ MON-04 / MON-05
@op("sys.resources", group="monitor", risk="read")
def resources(ctx, p) -> dict[str, Any]:
    """CPU (tải), RAM, đĩa (tổng + theo user, từ số liệu hub), số tiến trình claude, tunnel có connector."""
    import os

    from .collector import snapshot
    from .machine import machine

    out: dict[str, Any] = {"disk": machine(ctx.state).disk()}
    try:
        out["load"] = [round(x, 2) for x in os.getloadavg()]  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        out["load"] = None
    out["cpus"] = os.cpu_count()
    try:
        r = ctx.state.runner.run(["pgrep", "-c", "-f", "claude"], timeout=10)
        out["claude_procs"] = int((r.out or "0").strip() or 0) if r.ok else 0
    except (ValueError, Exception):  # best-effort: pgrep không có / không đếm được
        out["claude_procs"] = None
    try:
        r = ctx.state.runner.run(["sysctl", "-n", "hw.memsize"], timeout=10)
        out["ram_gb"] = round(int(r.out.strip()) / 2**30, 1) if r.ok and r.out.strip().isdigit() else None
    except Exception:  # best-effort: không phải macOS
        out["ram_gb"] = None
    out["users_disk"] = [{"os_user": h["os_user"], "free_gb": (snapshot(h).get("disk") or {}).get("free_gb")}
                         for h in ctx.db.all("SELECT h.*, u.os_user FROM hubs h JOIN hub_users u ON u.id=h.user_id")]
    out["tunnel"] = ctx.db.one("SELECT COUNT(*) AS n FROM alerts WHERE code='domain_tunnel' AND state!='resolved'")["n"] == 0
    return out


class HistoryP(Params):
    days: int = Field(default=30, ge=1, le=90)
    user: str | None = Field(default=None, max_length=64)


@op("sys.history", group="monitor", risk="read", params=HistoryP)
def history(ctx, p: HistoryP) -> dict[str, Any]:
    """Chuỗi theo ngày: chi phí (tổng max-trong-ngày mỗi user), run đang chạy lớn nhất, số lỗi."""
    since = ctx.state.clock() - p.days * 86400
    uid = None
    if p.user:
        u = ctx.db.one("SELECT id FROM hub_users WHERE id=? OR os_user=?", (p.user, p.user))
        uid = u["id"] if u else "-"
    allow = ctx.scoped_user_ids()
    per: dict[tuple[str, str], dict[str, float]] = {}
    for r in ctx.db.all("SELECT * FROM samples WHERE ts>=?" + (" AND user_id=?" if uid else ""), (since, uid) if uid else (since,)):
        if allow is not None and r["user_id"] not in allow:
            continue
        k = (time.strftime("%Y-%m-%d", time.localtime(r["ts"])), r["user_id"])
        x = per.setdefault(k, {"cost": 0.0, "running": 0, "failed": 0})
        x["cost"] = max(x["cost"], float(r["cost_today"] or 0))
        x["running"] = max(x["running"], int(r["running"] or 0))
        x["failed"] = max(x["failed"], int(r["failed_today"] or 0))
    days: dict[str, dict[str, float]] = {}
    for (day, _u), x in per.items():
        d = days.setdefault(day, {"cost": 0.0, "running": 0, "failed": 0})
        d["cost"] += x["cost"]
        d["running"] += x["running"]
        d["failed"] += x["failed"]
    return {"series": [{"day": k, **{kk: round(vv, 4) for kk, vv in v.items()}} for k, v in sorted(days.items())]}


# ============================================================================ JOB-04: nhập user hàng loạt từ CSV
CSV_COLS = ("os_user", "full_name", "email", "group", "team", "sub")


class CsvP(Params):
    csv: str = Field(min_length=10, max_length=200_000)
    zone_id: str | None = Field(default=None, max_length=40)
    claude_account_id: str | None = Field(default=None, max_length=40)
    dry_run: bool = True


@op("user.import_csv", group="user_lifecycle", risk="high", params=CsvP, target=lambda p: "csv")
def import_csv(ctx, p: CsvP) -> dict[str, Any]:
    """Tạo nhiều user từ CSV (cột: os_user, full_name, email, group, team, sub). `dry_run` = chỉ kiểm, báo dòng lỗi."""
    from .ops_lifecycle import CreateP, user_create

    rows = list(csv.DictReader(io.StringIO(p.csv)))
    if not rows or "os_user" not in rows[0]:
        raise OpError("invalid", f"CSV cần dòng tiêu đề với các cột: {', '.join(CSV_COLS)}")
    if len(rows) > 100:
        raise OpError("invalid", "tối đa 100 dòng mỗi lần")
    groups = {g["name"]: g["id"] for g in ctx.db.all("SELECT id, name FROM groups_")}
    teams = {t["slug"]: t["id"] for t in ctx.db.all("SELECT id, slug FROM teams")}
    report, ok = [], []
    seen: set[str] = set()
    for i, r in enumerate(rows, start=2):
        errs = []
        u = (r.get("os_user") or "").strip()
        if u in seen:
            errs.append("trùng trong file")
        seen.add(u)
        if r.get("group") and r["group"].strip() not in groups:
            errs.append(f"không có nhóm '{r['group']}'")
        if r.get("team") and r["team"].strip() not in teams:
            errs.append(f"không có team '{r['team']}'")
        params = {"os_user": u, "full_name": (r.get("full_name") or u).strip(), "email": (r.get("email") or "").strip() or None,
                  "group_id": groups.get((r.get("group") or "").strip()), "teams": [teams[r["team"].strip()]] if r.get("team") and r["team"].strip() in teams else [],
                  "claude_account_id": p.claude_account_id}
        if p.zone_id and (r.get("sub") or "").strip():
            params.update(zone_id=p.zone_id, sub=r["sub"].strip().lower())
        try:
            cp = CreateP.model_validate({k: v for k, v in params.items() if v is not None})
        except Exception as e:  # noqa: BLE001 — lỗi kiểm dòng CSV trả về cho người
            errs.append(str(e).splitlines()[0][:200])
            cp = None
        if ctx.db.one("SELECT 1 FROM hub_users WHERE os_user=? AND state!='deleted'", (u,)):
            errs.append("user đã có")
        report.append({"line": i, "os_user": u, "errors": errs})
        if not errs and cp is not None:
            ok.append(cp)
    if p.dry_run or any(x["errors"] for x in report):
        return {"dry_run": True, "rows": report, "valid": len(ok)}
    jobs = []
    for cp in ok:
        jobs.append(user_create(ctx, cp)["job_id"])
    return {"dry_run": False, "rows": report, "jobs": jobs}


# ============================================================================ HUB-03: cập nhật theo đợt
class RolloutP(Params):
    tag: Tag
    canary: str = Field(min_length=1, max_length=64)
    batch: int = Field(default=3, ge=1, le=20)
    soak_minutes: int = Field(default=10, ge=0, le=240)


class RolloutStep(Step):
    def __init__(self, idx: int, users: list[str]) -> None:
        self.users = users
        self.name, self.title = f"wave_{idx}", f"Đợt {idx}: {', '.join(users)}"

    def check(self, jc: JobCtx) -> bool:
        return all(u in jc.data.get("done", []) for u in self.users)

    def apply(self, jc: JobCtx) -> None:
        from .ops_lifecycle import DoctorCheck, UpdateStep

        for u in self.users:
            row = jc.state.db.one("SELECT id FROM hub_users WHERE os_user=?", (u,))
            sub = JobCtx(state=jc.state, job_id=jc.job_id, kind="hub.update", target=u, params={"user_id": row["id"], "os_user": u, "tag": jc.params["tag"]},
                         secrets={}, data={})
            try:
                if not UpdateStep().check(sub):
                    UpdateStep().apply(sub)
                DoctorCheck().apply(sub)
            finally:
                for line in sub._lines:
                    jc.log(line)
            jc.data.setdefault("done", []).append(u)


class SoakStep(Step):
    def __init__(self, minutes: int) -> None:
        self.minutes = minutes
        self.name, self.title = "soak", f"Chờ {minutes} phút không lỗi trên hub thử"

    def check(self, jc: JobCtx) -> bool:
        return self.minutes == 0 or bool(jc.data.get("soaked"))

    def apply(self, jc: JobCtx) -> None:
        canary = jc.params["canary"]
        uid = jc.state.db.one("SELECT id FROM hub_users WHERE os_user=?", (canary,))["id"]
        deadline = time.time() + self.minutes * 60
        while time.time() < deadline and not jc.state.extra.get("fast_soak"):
            time.sleep(min(30, max(0, deadline - time.time())))
        bad = jc.state.db.one("SELECT code FROM alerts WHERE subject_kind='user' AND subject=? AND state='on' AND code IN "
                              "('stale','stopped','auth_block','malformed')", (uid,))
        if bad:
            raise StepError(f"hub thử {canary} có cảnh báo '{bad['code']}' — dừng đợt cập nhật")
        jc.data["soaked"] = True


def _rollout_steps(params: dict[str, Any]) -> list[Step]:
    waves: list[list[str]] = params.get("waves") or []
    steps: list[Step] = [RolloutStep(1, [params["canary"]]), SoakStep(int(params.get("soak_minutes", 10)))]
    for i, w in enumerate(waves, start=2):
        steps.append(RolloutStep(i, w))
    return steps


kind("hub.rollout")(_rollout_steps)


@op("hub.rollout", group="hub", risk="high", params=RolloutP, target=lambda p: p.tag)
def rollout(ctx, p: RolloutP) -> dict[str, Any]:
    """Cập nhật theo đợt: 1 hub thử → doctor xanh + chờ N phút không lỗi → phần còn lại theo lô; lỗi → dừng đợt."""
    users = [u["os_user"] for u in ctx.db.all("SELECT u.os_user FROM hub_users u JOIN hubs h ON h.user_id=u.id WHERE u.state='active' AND "
                                              "(h.pinned_tag IS NULL OR h.pinned_tag=?) ORDER BY u.os_user", (p.tag,))]
    if p.canary not in users:
        raise OpError("invalid", "hub thử phải đang hoạt động và không bị ghim bản khác")
    rest = [u for u in users if u != p.canary]
    waves = [rest[i:i + p.batch] for i in range(0, len(rest), p.batch)]
    jid = ctx.state.jobs.submit("hub.rollout", target=f"rollout:{p.tag}", params={"tag": p.tag, "canary": p.canary, "waves": waves,
                                                                                  "soak_minutes": p.soak_minutes}, created_by=ctx.actor_id)
    ctx.job_id = jid
    return {"job_id": jid, "waves": [[p.canary], *waves]}


# ============================================================================ RPT-07: tuần / tháng
class PeriodP(Params):
    period: Literal["week", "month"] = "week"
    end: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    team_id: str | None = Field(default=None, max_length=40)


@op("report.period", group="report", risk="read", params=PeriodP)
def report_period(ctx, p: PeriodP) -> dict[str, Any]:
    """Báo cáo gộp tuần (7 ngày) / tháng (30 ngày) kết thúc ở `end`, có chuỗi xu hướng theo ngày."""
    from .ops_reports import _allowed_users

    end = datetime.strptime(p.end, "%Y-%m-%d") if p.end else datetime.fromtimestamp(ctx.state.clock()) - timedelta(days=1)
    n = 7 if p.period == "week" else 30
    start = (end - timedelta(days=n - 1)).strftime("%Y-%m-%d")
    allow = _allowed_users(ctx, p.team_id)
    users = {u["id"]: u for u in ctx.db.all("SELECT id, os_user, full_name FROM hub_users")}
    per_user: dict[str, dict[str, float]] = {}
    trend: dict[str, dict[str, float]] = {}
    for r in ctx.db.all("SELECT * FROM daily_reports WHERE day>=? AND day<=?", (start, end.strftime("%Y-%m-%d"))):
        if allow is not None and r["user_id"] not in allow:
            continue
        d = loads(r["data"], {})
        runs = d.get("runs") or {}
        x = per_user.setdefault(r["user_id"], {"runs": 0, "failed": 0, "tasks_done": 0, "cost": 0.0, "days": 0})
        y = trend.setdefault(r["day"], {"runs": 0, "cost": 0.0})
        x["runs"] += int(runs.get("total") or 0)
        x["failed"] += int(runs.get("failed") or 0)
        x["tasks_done"] += int((d.get("tasks") or {}).get("done") or 0)
        x["cost"] += float(d.get("cost_usd") or 0)
        x["days"] += 1 if int(runs.get("total") or 0) else 0
        y["runs"] += int(runs.get("total") or 0)
        y["cost"] += float(d.get("cost_usd") or 0)
    rows = [{"os_user": users.get(k, {}).get("os_user", k), "full_name": users.get(k, {}).get("full_name", ""), **{kk: round(vv, 4) for kk, vv in v.items()}}
            for k, v in per_user.items()]
    rows.sort(key=lambda r: -r["cost"])
    return {"period": p.period, "start": start, "end": end.strftime("%Y-%m-%d"), "rows": rows,
            "trend": [{"day": k, **v} for k, v in sorted(trend.items())], "totals": {"runs": sum(r["runs"] for r in rows),
                                                                                       "cost": round(sum(r["cost"] for r in rows), 4)}}


# ============================================================================ PRO-10: xuất hồ sơ
class ExportP(Params):
    users: list[str] = Field(min_length=1, max_length=500)


@op("user.export", group="user_view", risk="read", params=ExportP, audit_read=True, target=lambda p: ",".join(p.users)[:200])
def user_export(ctx, p: ExportP) -> dict[str, Any]:
    """Xuất hồ sơ (mức Số liệu/Hồ sơ) của các user đã chọn thành CSV."""
    from .ops_users import user_profile, UserP

    buf = io.StringIO()
    w = csv.writer(buf)
    cols = ["os_user", "full_name", "email", "group", "state", "hub_status", "version", "web_port", "cost_today", "cost_month", "runs_today",
            "agents", "domains", "teams"]
    w.writerow(cols)
    for key in p.users:
        prof = user_profile(ctx, UserP(id=key))
        i, h = prof["identity"], prof["hub"]
        w.writerow([i["os_user"], i["full_name"], i["email"], i["group"], i["state"], h["status"], h["version"], h["web_port"],
                    prof["cost"]["usd"].get("today"), prof["cost"]["usd"].get("month"), prof["metrics"]["runs"].get("today_total"),
                    prof["metrics"]["agents"].get("total"), " ".join(f"{d['sub']}.{d['zone']}" for d in prof["domains"]),
                    " ".join(t["name"] for t in i["teams"])])
    return {"csv": buf.getvalue()}

