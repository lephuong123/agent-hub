"""Đo KPI G-4 (`agent-hub report kpi`) và overhead học NFR-19 (`agent-hub report overhead`) — chỉ ĐỌC DB.

KPI G-4 — "tỷ lệ lặp lại cùng loại Signal correction cho cùng agent + loại việc":
- correction = Signal kind ∈ `CORRECTION_KINDS` của regression monitor (correction/reject/cancel/edit_output/undo);
- loại việc = `task:<role|kind>` nếu run thuộc task, ngược lại `adhoc` (Signal không có run → `adhoc`);
- correction LẶP = đã có correction cùng (agent, loại việc, kind) TRƯỚC đó (kể từ mốc bắt đầu học);
- tỷ lệ lặp theo tuần = correction lặp / run làm việc (adhoc|task) đã kết thúc của tuần đó.
  Mốc = 2 tuần đầu kể từ khi bật học (ledger entry/Signal đầu tiên); so với các tuần ≥ tuần thứ 5 ("sau 4 tuần"). Đạt khi giảm ≥ 30%.
Kèm các chỉ số G-4 khác từ BRD §2: proposal chờ duyệt (=0), tỷ lệ rollback (≤ 15%), rollback thủ công (≤ 5%),
thay đổi làm tụt eval mà không bị rollback (=0).

Overhead NFR-19: chi phí + thời gian run "học" (review, eval, eval_judge, curator) so với run làm việc (adhoc, task) — ≤ 15%.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..hub import Hub

CORRECTION_KINDS = ("correction", "reject", "cancel", "edit_output", "undo")
WORK_KINDS = ("adhoc", "task")
LEARNING_KINDS = ("review", "eval", "eval_judge", "curator")
LEARNING_ACTORS = ("review", "curator", "agent", "learning")
PENDING_STATUSES = ("pending", "pending_approval", "awaiting_approval", "approved")


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _pct(a: float, b: float) -> float | None:
    return None if not b else round(100.0 * a / b, 2)


def learning_start(conn) -> datetime | None:
    vals = [conn.execute("SELECT MIN(applied_at) FROM ledger_entries").fetchone()[0], conn.execute("SELECT MIN(created_at) FROM signals").fetchone()[0]]
    ds = [d for d in (_parse(v) for v in vals) if d is not None]
    return min(ds) if ds else None


def kpi_report(hub: "Hub", *, since: str | None = None, weeks: int | None = None, agent_id: str | None = None, now: datetime | None = None) -> dict[str, Any]:
    conn = hub.store.conn
    now = now or datetime.now(timezone.utc)
    start = _parse(since) if since else learning_start(conn)
    if start is None:
        return {"start": None, "weeks": [], "note": "chưa có dữ liệu học (Signal/ledger)", "metrics": _other_metrics(conn, None, agent_id)}
    nweeks = int(weeks) if weeks else max(1, int((now - start).total_seconds() // (7 * 86400)) + 1)
    end = start + timedelta(days=7 * nweeks)
    task_type: dict[str, str] = {}
    for r in conn.execute("SELECT r.id, t.role, t.kind FROM runs r JOIN tasks t ON t.id=r.task_id WHERE r.task_id IS NOT NULL"):
        task_type[r[0]] = f"task:{r[1] or r[2] or 'task'}"
    q = f"SELECT agent_id, run_id, kind, created_at FROM signals WHERE kind IN ({','.join('?' * len(CORRECTION_KINDS))}) AND created_at>=? AND created_at<?"
    args: list[Any] = [*CORRECTION_KINDS, _iso(start), _iso(end)]
    if agent_id:
        q += " AND agent_id=?"
        args.append(agent_id)
    seen: set[tuple[str, str, str]] = set()
    buckets = [{"week": i + 1, "from": _iso(start + timedelta(days=7 * i)), "corrections": 0, "repeated": 0, "work_runs": 0} for i in range(nweeks)]
    by_key: dict[str, int] = {}
    for a, run_id, kind, ts in conn.execute(q + " ORDER BY created_at, rowid", args):
        d = _parse(ts)
        if d is None:
            continue
        i = int((d - start).total_seconds() // (7 * 86400))
        if not 0 <= i < nweeks:
            continue
        key = (a, task_type.get(run_id or "", "adhoc"), kind)
        buckets[i]["corrections"] += 1
        if key in seen:
            buckets[i]["repeated"] += 1
            by_key[f"{key[0]}/{key[1]}/{key[2]}"] = by_key.get(f"{key[0]}/{key[1]}/{key[2]}", 0) + 1
        seen.add(key)
    rq = f"SELECT queued_at FROM runs WHERE kind IN ({','.join('?' * len(WORK_KINDS))}) AND status NOT IN ('queued','running') AND queued_at>=? AND queued_at<?"
    rargs: list[Any] = [*WORK_KINDS, _iso(start), _iso(end)]
    if agent_id:
        rq += " AND agent_id=?"
        rargs.append(agent_id)
    for (ts,) in conn.execute(rq, rargs):
        d = _parse(ts)
        if d is not None:
            i = int((d - start).total_seconds() // (7 * 86400))
            if 0 <= i < nweeks:
                buckets[i]["work_runs"] += 1
    for b in buckets:
        b["repeat_rate"] = round(b["repeated"] / b["work_runs"], 4) if b["work_runs"] else None
    base = buckets[:2]
    after = buckets[4:]

    def rate(bs: list[dict[str, Any]]) -> float | None:
        runs = sum(b["work_runs"] for b in bs)
        return round(sum(b["repeated"] for b in bs) / runs, 4) if runs else None

    br, ar = rate(base), rate(after)
    reduction = None if (br is None or ar is None or br == 0) else round(100.0 * (br - ar) / br, 1)
    return {
        "start": _iso(start), "weeks": buckets, "agent_id": agent_id,
        "repeat_correction": {"baseline_weeks_1_2": br, "after_week_4": ar, "reduction_pct": reduction, "target_reduction_pct": 30,
                              "met": None if reduction is None else reduction >= 30,
                              "note": None if after else "chưa đủ 4 tuần kể từ khi bật học — chỉ có mốc"},
        "top_repeated": sorted(by_key.items(), key=lambda kv: -kv[1])[:10],
        "metrics": _other_metrics(conn, _iso(start), agent_id),
    }


def _other_metrics(conn, since: str | None, agent_id: str | None) -> dict[str, Any]:
    w, a = ["1=1"], []
    if since:
        w.append("applied_at>=?")
        a.append(since)
    if agent_id:
        w.append("agent_id=?")
        a.append(agent_id)
    acts = ",".join(f"'{x}'" for x in LEARNING_ACTORS)
    base = f"FROM ledger_entries e WHERE {' AND '.join(w)} AND e.op!='rollback' AND e.actor IN ({acts})"
    total = int(conn.execute(f"SELECT COUNT(*) {base}", a).fetchone()[0])
    rolled = int(conn.execute(f"SELECT COUNT(*) {base} AND e.rolled_back_by IS NOT NULL", a).fetchone()[0])
    manual = int(conn.execute(f"SELECT COUNT(*) {base} AND e.rolled_back_by IN (SELECT id FROM ledger_entries WHERE actor='user')", a).fetchone()[0])
    pw, pa = [f"status IN ({','.join('?' * len(PENDING_STATUSES))})"], list(PENDING_STATUSES)
    if agent_id:
        pw.append("agent_id=?")
        pa.append(agent_id)
    pending = int(conn.execute(f"SELECT COUNT(*) FROM proposals WHERE {' AND '.join(pw)}", pa).fetchone()[0])
    not_rb = 0
    try:
        ew, ea = ["kind!='check'", "status='eval_failed'"], []
        if agent_id:
            ew.append("agent_id=?")
            ea.append(agent_id)
        for (ids,) in conn.execute(f"SELECT ledger_entry_ids FROM eval_runs WHERE {' AND '.join(ew)}", ea):
            for eid in json.loads(ids or "[]"):
                r = conn.execute("SELECT rolled_back_by, actor FROM ledger_entries WHERE id=?", (eid,)).fetchone()
                if r is not None and r[0] is None and r[1] in LEARNING_ACTORS:
                    not_rb += 1
    except Exception:  # pragma: no cover - bảng eval chưa có
        not_rb = 0
    return {
        "learning_changes": total,
        "pending_approval_proposals": {"value": pending, "target": 0, "met": pending == 0},
        "rollback_rate_pct": {"value": _pct(rolled, total), "target": "≤ 15", "met": None if not total else 100.0 * rolled / total <= 15},
        "manual_rollback_rate_pct": {"value": _pct(manual, total), "target": "≤ 5", "met": None if not total else 100.0 * manual / total <= 5},
        "eval_drop_not_rolled_back": {"value": not_rb, "target": 0, "met": not_rb == 0},
    }


def overhead_report(hub: "Hub", *, since: str | None = None, agent_id: str | None = None, limit_pct: float = 15.0) -> dict[str, Any]:
    conn = hub.store.conn
    w, a = ["status NOT IN ('queued','running')"], []
    if since:
        w.append("queued_at>=?")
        a.append(since)
    if agent_id:
        w.append("agent_id=?")
        a.append(agent_id)
    rows = conn.execute(f"SELECT agent_id, kind, COUNT(*), COALESCE(SUM(cost_usd),0), COALESCE(SUM(duration_ms),0) FROM runs WHERE {' AND '.join(w)} GROUP BY 1,2", a).fetchall()
    per: dict[str, dict[str, Any]] = {}
    tot = {"work_cost": 0.0, "work_ms": 0, "work_runs": 0, "learn_cost": 0.0, "learn_ms": 0, "learn_runs": 0}
    by_kind: dict[str, dict[str, Any]] = {}
    for ag, kind, n, cost, ms in rows:
        side = "work" if kind in WORK_KINDS else "learn" if kind in LEARNING_KINDS else None
        bk = by_kind.setdefault(kind, {"runs": 0, "cost_usd": 0.0, "duration_ms": 0})
        bk["runs"] += n
        bk["cost_usd"] = round(bk["cost_usd"] + float(cost), 8)
        bk["duration_ms"] += int(ms)
        if side is None:
            continue
        p = per.setdefault(ag, {"work_cost": 0.0, "work_ms": 0, "work_runs": 0, "learn_cost": 0.0, "learn_ms": 0, "learn_runs": 0})
        for d in (p, tot):
            d[f"{side}_cost"] += float(cost)
            d[f"{side}_ms"] += int(ms)
            d[f"{side}_runs"] += int(n)

    def fin(d: dict[str, Any]) -> dict[str, Any]:
        cp, tp = _pct(d["learn_cost"], d["work_cost"]), _pct(d["learn_ms"], d["work_ms"])
        return {**{k: (round(v, 6) if isinstance(v, float) else v) for k, v in d.items()}, "cost_overhead_pct": cp, "time_overhead_pct": tp,
                "met": None if cp is None else (cp <= limit_pct and (tp is None or tp <= limit_pct))}

    return {"since": since, "limit_pct": limit_pct, "total": fin(tot), "by_agent": {k: fin(v) for k, v in sorted(per.items())}, "by_kind": by_kind,
            "learning_kinds": list(LEARNING_KINDS), "work_kinds": list(WORK_KINDS)}


def render_kpi(r: dict[str, Any]) -> str:
    if not r.get("start"):
        return "KPI G-4: " + r.get("note", "chưa có dữ liệu")
    L = [f"KPI G-4 — từ {r['start'][:10]}" + (f" (agent {r['agent_id']})" if r.get("agent_id") else ""), "",
         "TUẦN  CORRECTION  LẶP  RUN  TỶ LỆ LẶP"]
    for b in r["weeks"]:
        rr = "-" if b["repeat_rate"] is None else "%.3f" % b["repeat_rate"]
        L.append(f"{b['week']:>4}  {b['corrections']:>10}  {b['repeated']:>3}  {b['work_runs']:>3}  {rr}")
    rc = r["repeat_correction"]
    L += ["", f"tỷ lệ lặp correction: 2 tuần đầu {rc['baseline_weeks_1_2']} → sau 4 tuần {rc['after_week_4']}; giảm {rc['reduction_pct']}% (mục tiêu ≥ 30%)"
          + ("" if rc["met"] is None else (" ✔" if rc["met"] else " ✘")) + (f" — {rc['note']}" if rc.get("note") else "")]
    m = r["metrics"]
    L.append(f"proposal chờ duyệt: {m['pending_approval_proposals']['value']} (mục tiêu 0)")
    L.append(f"rollback: {m['rollback_rate_pct']['value']}% (≤ 15%) · thủ công: {m['manual_rollback_rate_pct']['value']}% (≤ 5%) · "
             f"tụt eval không rollback: {m['eval_drop_not_rolled_back']['value']} (= 0) · thay đổi học: {m['learning_changes']}")
    if r.get("top_repeated"):
        L.append("lặp nhiều nhất: " + ", ".join(f"{k} ×{v}" for k, v in r["top_repeated"][:5]))
    return "\n".join(L)


def render_overhead(r: dict[str, Any]) -> str:
    t = r["total"]
    L = [f"Overhead học (NFR-19, ≤ {r['limit_pct']:g}%)" + (f" từ {r['since']}" if r.get("since") else ""),
         f"làm việc: {t['work_runs']} run, ${t['work_cost']:.4f}, {t['work_ms'] / 1000:.1f} s",
         f"học (review/eval/judge/curator): {t['learn_runs']} run, ${t['learn_cost']:.4f}, {t['learn_ms'] / 1000:.1f} s",
         f"overhead: chi phí {t['cost_overhead_pct']}% · thời gian {t['time_overhead_pct']}%" + ("" if t["met"] is None else (" ✔" if t["met"] else " ✘ VƯỢT"))]
    for a, d in r["by_agent"].items():
        L.append(f"  {a:<16} chi phí {d['cost_overhead_pct']}%  thời gian {d['time_overhead_pct']}%  ({d['learn_runs']}/{d['work_runs']} run)")
    return "\n".join(L)
