"""Báo cáo hằng ngày (FR-SA-RPT, luồng 9.4): gom `report/<u>/<ngày>.json` của hub → `daily_reports` + bất thường → gửi lúc `report.send_time`.

`tick(state)` mỗi phút: (1) nạp báo cáo hôm qua + hôm nay của mọi user đang hoạt động (đã đọc rồi mà file không đổi thì bỏ); (2) sau 00:15 mà
hôm qua chưa có → cảnh báo `missing_report`; (3) đến giờ gửi và chưa gửi hôm nay → gửi tóm tắt toàn hệ + từng team; (4) dọn quá `keep_days`.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from .alerts import sync_alerts
from .db import dumps, loads

if TYPE_CHECKING:
    from .dispatch import State

INACTIVE_DAYS = 3


def _day(ts: float, delta: int = 0) -> str:
    return (datetime.fromtimestamp(ts) + timedelta(days=delta)).strftime("%Y-%m-%d")


def anomalies(state: "State", user_id: str, day: str, data: dict[str, Any]) -> list[str]:
    """RPT-09."""
    out = []
    prev = [loads(r["data"], {}) for r in state.db.all("SELECT data FROM daily_reports WHERE user_id=? AND day<? ORDER BY day DESC LIMIT 7",
                                                         (user_id, day))]
    runs = data.get("runs") or {}
    total = int(runs.get("total") or 0)
    if total == 0 and len(prev) >= INACTIVE_DAYS - 1 and all(int((p.get("runs") or {}).get("total") or 0) == 0 for p in prev[:INACTIVE_DAYS - 1]):
        out.append("inactive")
    costs = [float(p.get("cost_usd") or 0) for p in prev]
    avg = sum(costs) / len(costs) if costs else 0
    if avg > 0 and float(data.get("cost_usd") or 0) >= 2 * avg:
        out.append("cost_spike")
    if total >= 5 and int(runs.get("failed") or 0) / total >= 0.3:
        out.append("failure_rate")
    if int((data.get("tasks") or {}).get("stuck") or 0) > 0:
        out.append("tasks_stuck")
    return out


def ingest(state: "State", user: dict[str, Any], day: str) -> bool:
    from ..admin_profile import read_report

    r = read_report(user["os_user"], day)
    if not r:
        return False
    h = hashlib.sha256(dumps(r).encode()).hexdigest()
    cur = state.db.one("SELECT source_hash FROM daily_reports WHERE user_id=? AND day=?", (user["id"], day))
    if cur and cur["source_hash"] == h:
        return True
    an = anomalies(state, user["id"], day, r)
    state.db.exec("INSERT OR REPLACE INTO daily_reports(user_id, day, level, data, anomalies, source_hash, ingested_at) VALUES (?,?,?,?,?,?,?)",
                  (user["id"], day, r["level"], dumps(r), json.dumps(an), h, state.clock()))
    return True


def tick(state: "State") -> dict[str, Any]:
    from .ops_system import setting

    now = state.clock()
    lt = time.localtime(now)
    users = state.db.all("SELECT id, os_user FROM hub_users WHERE host_id=? AND state='active'", (state.db.host_id,))
    yday, today = _day(now, -1), _day(now)
    got = 0
    for u in users:
        for d in (yday, today):
            got += ingest(state, u, d)
        if (lt.tm_hour, lt.tm_min) >= (0, 15):
            have = state.db.one("SELECT 1 FROM daily_reports WHERE user_id=? AND day=?", (u["id"], yday))
            sync_alerts(state, "user", u["id"], set() if have else {"missing_report"}, managed={"missing_report"})
    sent = False
    send_at = setting(state.db, "report.send_time")
    if time.strftime("%H:%M", lt) >= send_at and state.db.get_setting("report.last_sent") != today:
        state.db.set_setting("report.last_sent", today)
        send_digest(state, yday)
        sent = True
    keep = int(setting(state.db, "report.keep_days"))
    state.db.exec("DELETE FROM daily_reports WHERE day<?", (_day(now, -keep),))
    return {"ingested": got, "sent": sent}


def day_rows(state: "State", day: str, user_ids: set[str] | None = None) -> list[dict[str, Any]]:
    """RPT-05: bảng user × chỉ số của một ngày, so với hôm trước và trung bình 7 ngày."""
    prev_day = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    week0 = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")
    out = []
    for u in state.db.all("SELECT id, os_user, full_name, state FROM hub_users WHERE state IN ('active','suspended') ORDER BY os_user"):
        if user_ids is not None and u["id"] not in user_ids:
            continue
        r = state.db.one("SELECT * FROM daily_reports WHERE user_id=? AND day=?", (u["id"], day))
        p = state.db.one("SELECT data FROM daily_reports WHERE user_id=? AND day=?", (u["id"], prev_day))
        wk = [loads(x["data"], {}) for x in state.db.all("SELECT data FROM daily_reports WHERE user_id=? AND day>=? AND day<?", (u["id"], week0, day))]
        d = loads(r["data"], {}) if r else {}
        pd = loads(p["data"], {}) if p else {}
        runs = d.get("runs") or {}
        out.append({"user_id": u["id"], "os_user": u["os_user"], "full_name": u["full_name"], "has_report": bool(r),
                    "runs": runs.get("total"), "completed": runs.get("completed"), "failed": runs.get("failed"),
                    "tasks_done": (d.get("tasks") or {}).get("done"), "tasks_stuck": (d.get("tasks") or {}).get("stuck"),
                    "hours": len(d.get("active_hours") or []), "approvals": d.get("approvals"), "cost": d.get("cost_usd"),
                    "cost_prev": pd.get("cost_usd"), "cost_avg7": round(sum(float(x.get("cost_usd") or 0) for x in wk) / len(wk), 4) if wk else None,
                    "runs_avg7": round(sum(int((x.get("runs") or {}).get("total") or 0) for x in wk) / len(wk), 1) if wk else None,
                    "anomalies": loads(r["anomalies"], []) if r else ["missing"]})
    return out


def digest_text(state: "State", day: str, rows: list[dict[str, Any]], title: str) -> str:
    tot_runs = sum(int(r["runs"] or 0) for r in rows)
    tot_cost = sum(float(r["cost"] or 0) for r in rows)
    lines = [f"📊 {title} — {day}", f"{len(rows)} user · {tot_runs} run · ${tot_cost:.2f}"]
    for r in rows:
        flag = (" ⚠ " + ", ".join(r["anomalies"])) if r["anomalies"] else ""
        lines.append(f"• {r['os_user']}: {r['runs'] or 0} run, {r['tasks_done'] or 0} task xong, ${float(r['cost'] or 0):.2f}{flag}")
    return "\n".join(lines[:80])


def send_digest(state: "State", day: str) -> None:
    from .notify import send

    rows = day_rows(state, day)
    if rows:
        send(state, digest_text(state, day, rows, "Báo cáo ngày toàn hệ"), subject=f"Báo cáo ngày {day}")
    for t in state.db.all("SELECT id, name, channel FROM teams WHERE state='active'"):
        ids = {m["user_id"] for m in state.db.all("SELECT user_id FROM team_members WHERE team_id=?", (t["id"],))}
        trows = [r for r in rows if r["user_id"] in ids]
        if trows:
            ch = loads(t["channel"], {}) or {}
            text = digest_text(state, day, trows, f"Báo cáo team {t['name']}")
            sink = state.extra.get("notify_sink")
            if sink is not None:
                sink.append(text)
            elif ch:
                from .teams_notify import send_team

                send_team(state, ch, text)
