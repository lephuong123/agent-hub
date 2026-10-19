"""Báo cáo insights (FR-OB-06) và chi phí theo agent/team/ngày (FR-OB-02/03). Chỉ đọc DB, không gọi LLM.

`report(since)`: số run theo outcome, top tool, top lỗi, chi phí (tổng/agent/team/ngày), tỷ lệ cache
`cache_read / (input + cache_read + cache_creation)`, số signal theo loại, proposal/rollback (bảng của M4 — `null` khi
chưa có), task theo trạng thái.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from .errors import HubError


def parse_since(since: str | None, default_days: int = 7) -> datetime:
    """`7d`, `24h`, `30m`, ngày ISO `2026-10-01`, hoặc None (= `default_days` ngày)."""
    now = datetime.now(timezone.utc)
    if not since:
        return now - timedelta(days=default_days)
    m = re.fullmatch(r"\s*(\d+)\s*([dhm])\s*", since)
    if m:
        n, u = int(m.group(1)), m.group(2)
        return now - {"d": timedelta(days=n), "h": timedelta(hours=n), "m": timedelta(minutes=n)}[u]
    try:
        dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
    except ValueError:
        raise HubError(f"--since không hợp lệ: {since} (vd 7d, 24h, 2026-10-01)") from None
    return dt if dt.tzinfo else dt.astimezone()


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _table_exists(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def cost_breakdown(conn, since: datetime, by: str = "agent") -> list[dict[str, Any]]:
    col = {"agent": "agent_id", "team": "COALESCE(team_id, '(adhoc)')", "day": "substr(ended_at, 1, 10)", "kind": "kind", "model": "COALESCE(model, '-')"}.get(by)
    if col is None:
        raise HubError("--by phải là agent|team|day|kind|model")
    rows = conn.execute(
        f"SELECT {col} AS k, COUNT(*), COALESCE(SUM(cost_usd),0) FROM runs WHERE ended_at >= ? GROUP BY k ORDER BY 3 DESC", (_iso(since),)
    ).fetchall()
    return [{by: r[0], "runs": r[1], "cost_usd": round(float(r[2]), 8)} for r in rows]


def report(conn, since: datetime, *, top: int = 10) -> dict[str, Any]:
    s = _iso(since)
    out: dict[str, Any] = {"since": s, "until": _iso(datetime.now(timezone.utc))}
    out["runs_by_outcome"] = {r[0] or "(running)": r[1] for r in conn.execute("SELECT COALESCE(outcome, status), COUNT(*) FROM runs WHERE queued_at >= ? GROUP BY 1 ORDER BY 2 DESC", (s,))}
    out["runs_total"] = sum(out["runs_by_outcome"].values())
    out["runs_by_kind"] = {r[0]: r[1] for r in conn.execute("SELECT kind, COUNT(*) FROM runs WHERE queued_at >= ? GROUP BY 1", (s,))}
    tools: Counter[str] = Counter()
    tool_err: Counter[str] = Counter()
    for (p,) in conn.execute("SELECT payload FROM events WHERE type='tool.start' AND ts >= ?", (s,)):
        try:
            tools[json.loads(p).get("name") or "?"] += 1
        except ValueError:
            pass
    for (p,) in conn.execute("SELECT payload FROM events WHERE type='tool.complete' AND ts >= ?", (s,)):
        try:
            d = json.loads(p)
        except ValueError:
            continue
        if d.get("is_error"):
            tool_err[d.get("name") or "?"] += 1
    out["top_tools"] = [{"tool": k, "calls": v, "errors": tool_err.get(k, 0)} for k, v in tools.most_common(top)]
    errs: Counter[str] = Counter()
    for (e,) in conn.execute("SELECT error FROM runs WHERE queued_at >= ? AND error IS NOT NULL AND error != ''", (s,)):
        key = re.sub(r"\b(r|t|ap|sg)_[0-9a-f]{6,}\b", "<id>", e.splitlines()[0])[:160]
        errs[key] += 1
    out["top_errors"] = [{"error": k, "count": v} for k, v in errs.most_common(top)]
    total = conn.execute("SELECT COALESCE(SUM(cost_usd),0), COUNT(*) FROM runs WHERE ended_at >= ?", (s,)).fetchone()
    out["cost"] = {
        "total_usd": round(float(total[0]), 8),
        "by_agent": cost_breakdown(conn, since, "agent"),
        "by_team": cost_breakdown(conn, since, "team"),
        "by_day": cost_breakdown(conn, since, "day"),
    }
    inp = cr = cc = 0
    for (u,) in conn.execute("SELECT usage FROM runs WHERE ended_at >= ? AND usage IS NOT NULL", (s,)):
        try:
            d = json.loads(u) or {}
        except ValueError:
            continue
        inp += int(d.get("input_tokens") or 0)
        cr += int(d.get("cache_read_input_tokens") or 0)
        cc += int(d.get("cache_creation_input_tokens") or 0)
    denom = inp + cr + cc
    out["cache"] = {"input_tokens": inp, "cache_read_tokens": cr, "cache_creation_tokens": cc, "hit_ratio": round(cr / denom, 4) if denom else None}
    out["signals"] = {r[0]: r[1] for r in conn.execute("SELECT kind, COUNT(*) FROM signals WHERE created_at >= ? AND kind != 'failure_obs' GROUP BY 1", (s,))} if _table_exists(conn, "signals") else None
    out["proposals"] = (
        {r[0]: r[1] for r in conn.execute("SELECT status, COUNT(*) FROM proposals WHERE created_at >= ? GROUP BY 1", (s,))} if _table_exists(conn, "proposals") else None
    )
    out["rollbacks"] = (
        conn.execute("SELECT COUNT(*) FROM ledger_entries WHERE op='rollback' AND applied_at >= ?", (s,)).fetchone()[0] if _table_exists(conn, "ledger_entries") else None
    )
    out["tasks_by_status"] = {r[0]: r[1] for r in conn.execute("SELECT status, COUNT(*) FROM tasks WHERE created_at >= ? GROUP BY 1", (s,))} if _table_exists(conn, "tasks") else None
    return out


def render_text(r: dict[str, Any]) -> str:
    lines = [f"Báo cáo từ {r['since']} tới {r['until']}", ""]
    lines.append(f"Run: {r['runs_total']}  " + ", ".join(f"{k}={v}" for k, v in r["runs_by_outcome"].items()))
    c = r["cost"]
    lines.append(f"Chi phí: ${c['total_usd']:.4f}")
    for k in ("by_agent", "by_team"):
        if c[k]:
            key = k.split("_")[1]
            lines.append(f"  theo {key}: " + ", ".join(f"{x[key]}=${x['cost_usd']:.4f}" for x in c[k][:8]))
    ca = r["cache"]
    lines.append(f"Cache: hit_ratio={ca['hit_ratio']} (read {ca['cache_read_tokens']}, creation {ca['cache_creation_tokens']}, input {ca['input_tokens']})")
    if r["top_tools"]:
        lines.append("Top tool: " + ", ".join(f"{t['tool']}×{t['calls']}" + (f" (lỗi {t['errors']})" if t["errors"] else "") for t in r["top_tools"]))
    if r["top_errors"]:
        lines.append("Top lỗi:")
        lines += [f"  {e['count']}× {e['error']}" for e in r["top_errors"]]
    lines.append(f"Signal: {r['signals'] or {}}   Proposal: {r['proposals'] if r['proposals'] is not None else 'n/a (M4)'}   Rollback: {r['rollbacks'] if r['rollbacks'] is not None else 'n/a (M4)'}")
    if r.get("tasks_by_status"):
        lines.append("Task: " + ", ".join(f"{k}={v}" for k, v in r["tasks_by_status"].items()))
    return "\n".join(lines)
