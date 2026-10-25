"""RPC phía daemon của M2: `tui.snapshot`, `run.steer`, `template.*`, `agent.create_from_template`,
`run.takeover`, `run.takeover_record`.

Chữ ký:
- `tui.snapshot {}` → ảnh chụp cho màn `top` trong MỘT lần gọi (FR-UI-01):
  `{hub: {...header...}, agents: [row], approvals: [pending], epoch, last_seq, server_ts}`.
  `row` = `{id, name, role, model, team, status, raw_status, paused, current_run_id, task, run_started_at,
  last_tool, last_tool_preview, last_tool_at, run_tokens, run_cost_usd, tokens_today, cost_today_usd,
  pending_approvals, waiting_human, last_run_id, last_run_status, session_id}`.
  `status` hiển thị ∈ working | waiting | waiting_human | idle | error | paused.
- `run.steer {id (run hoặc agent), text, by?, channel?}` → `{run_id, sent, seq, steer_count}` (FR-RT-06).
- `template.list {}` / `template.get {name}`; `agent.create_from_template {id, template, overrides?, soul?}` (FR-AG-07).
- `run.takeover {id}` → argv/cwd để chạy `claude --resume` tương tác; `run.takeover_record {id, exit_code?, started_at?,
  launched?}` → Run `kind=human_takeover` (FR-UI-10).
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from .. import templates
from ..errors import HubError
from ..events import iso
from ..steer import steer
from . import takeover

if TYPE_CHECKING:
    from ..daemon import Daemon
    from ..hub import Hub

USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def _req(p: dict[str, Any], key: str) -> Any:
    if p.get(key) in (None, ""):
        raise HubError(f"thiếu tham số '{key}'", code=-32602)
    return p[key]


def _today_start_utc() -> str:
    start = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    return iso(start.astimezone(timezone.utc))


def _tokens(usage: Any) -> int:
    if isinstance(usage, str):
        try:
            usage = json.loads(usage)
        except json.JSONDecodeError:
            return 0
    if not isinstance(usage, dict):
        return 0
    return sum(int(usage.get(k) or 0) for k in USAGE_KEYS)


def _teams_by_agent(hub: "Hub") -> dict[str, str]:
    """agent_id → tên team (bảng của M3; chưa có → rỗng)."""
    try:
        rows = hub.store.conn.execute(
            "SELECT m.agent_id, COALESCE(t.name, m.team_id) AS team FROM team_members m LEFT JOIN teams t ON t.id = m.team_id "
            "WHERE t.archived_at IS NULL ORDER BY m.created_at"
        ).fetchall()
    except Exception:
        return {}
    out: dict[str, str] = {}
    for r in rows:
        out[r["agent_id"]] = f"{out[r['agent_id']]},{r['team']}" if r["agent_id"] in out else r["team"]
    return out


def _task_title(hub: "Hub", task_id: str | None) -> str | None:
    if not task_id:
        return None
    try:
        r = hub.store.conn.execute("SELECT title FROM tasks WHERE id=?", (task_id,)).fetchone()
    except Exception:
        return None
    return r["title"] if r else None


def _first_line(s: str | None, n: int = 120) -> str:
    s = (s or "").strip().splitlines()[0] if (s or "").strip() else ""
    return s[:n]


def _age_s(ts: str | None, now: float) -> float | None:
    if not ts:
        return None
    try:
        return max(0.0, now - datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def snapshot(hub: "Hub", p: dict[str, Any] | None = None) -> dict[str, Any]:
    now = time.time()
    store = hub.store
    agents = hub.agents.list()
    approvals = hub.guard.annotate_always(hub.approvals.list("pending", limit=500))  # `always_offer` cho nút "Luôn cho phép"
    appr_by_agent: dict[str, list[dict[str, Any]]] = {}
    for a in approvals:
        a["wait_s"] = round(_age_s(a.get("opened_at"), now) or 0.0, 1)
        appr_by_agent.setdefault(a["agent_id"], []).append(a)
    teams = _teams_by_agent(hub)
    since = _today_start_utc()
    tok_today: dict[str, int] = {}
    cost_today: dict[str, float] = {}
    for r in store.conn.execute("SELECT agent_id, usage, cost_usd FROM runs WHERE ended_at >= ?", (since,)):
        tok_today[r["agent_id"]] = tok_today.get(r["agent_id"], 0) + _tokens(r["usage"])
        cost_today[r["agent_id"]] = cost_today.get(r["agent_id"], 0.0) + float(r["cost_usd"] or 0.0)
    waiting_runs: dict[str, dict[str, Any]] = {}
    for r in store.list_runs(status="waiting_human", limit=500):
        waiting_runs.setdefault(r["agent_id"], r)
    active_by_agent: dict[str, list[str]] = {}
    for ar in hub.runs.active.values():
        active_by_agent.setdefault(ar.agent_id, []).append(ar.run_id)

    rows = []
    running_tokens = 0
    sbx = hub.guard.sandbox_overrides()  # SEC: nhãn cảnh báo ngoại lệ sandbox theo agent
    for a in agents:
        aid = a["id"]
        live = hub.aggregator.agents.get(aid)
        run_id = a.get("current_run_id") or (active_by_agent.get(aid) or [None])[0]
        run = store.get_run(run_id) if run_id else None
        last = run or (store.list_runs(agent_id=aid, limit=1) or [None])[0]
        pend = appr_by_agent.get(aid, [])
        wh = waiting_runs.get(aid)
        raw = a["status"]
        if raw == "paused":
            disp = "paused"
        elif wh is not None or any(x["status"] == "waiting_human" for x in pend):
            disp = "waiting_human"
        elif pend:
            disp = "waiting"
        else:
            disp = raw if raw in ("working", "idle", "error", "waiting") else ("idle" if raw != "disabled" else "paused")
        if disp in ("idle", "error") and active_by_agent.get(aid):
            disp = "working"
        task_src = run or wh
        task = None
        if task_src:
            task = _task_title(hub, task_src.get("task_id")) or _first_line(task_src.get("prompt"))
        is_running = bool(run and run["status"] == "running")
        run_tokens = int(live.run_tokens) if (live and is_running) else 0
        run_cost = float(live.run_cost_est) if (live and is_running) else 0.0
        running_tokens += run_tokens
        rows.append(
            {
                "id": aid,
                "name": a.get("name") or aid,
                "sandbox_localhost": bool((sbx.get(aid) or {}).get("allow_localhost")),
                "sandbox_domains": list((sbx.get(aid) or {}).get("allow_domains") or []),
                "role": a.get("role") or "",
                "model": a.get("model"),
                "team": teams.get(aid),
                "status": disp,
                "raw_status": raw,
                "paused": raw == "paused",
                "current_run_id": run["id"] if run and run["status"] in ("running", "queued") else None,
                "run_status": run["status"] if run else None,
                "task": task,
                "run_started_at": run.get("started_at") if is_running else None,
                "last_tool": live.last_tool if live else None,
                "last_tool_preview": live.last_tool_preview if live else None,
                "last_tool_at": iso(live.last_tool_at) if live and live.last_tool_at else None,
                "run_tokens": run_tokens,
                "run_cost_usd": round(run_cost, 6),
                "tokens_today": tok_today.get(aid, 0) + run_tokens,
                "cost_today_usd": round(cost_today.get(aid, 0.0), 6),
                "pending_approvals": [x["id"] for x in pend],
                "waiting_human": wh["id"] if wh else None,
                "last_run_id": (last or {}).get("id") or (wh or {}).get("id"),
                "last_run_status": (last or {}).get("status"),
                "session_id": (live.session_id if live else None) or (last or {}).get("session_id"),
            }
        )

    st = hub.status()
    cli = store.conn.execute("SELECT cli_version FROM runs WHERE cli_version IS NOT NULL ORDER BY queued_at DESC LIMIT 1").fetchone()
    cli_version = cli["cli_version"] if cli else None
    tested = hub.config.get("runtime.claude.tested_versions") or []
    compat = store.state_get("cli_compat") or {}  # H-10: kiểm phiên bản lúc daemon khởi động (runtime/compat.py)
    if compat.get("version"):
        cli_version = compat["version"]
    if st["runtime"] == "fake":
        cli_version = cli_version or "fake"
    unknown = store.state_get("cli_unknown_rate") or {}
    longest = max((x["wait_s"] for x in approvals), default=0.0)
    learn_today = store.conn.execute(
        "SELECT COUNT(*) FROM events WHERE ts >= ? AND type LIKE 'learning.%'", (since,)
    ).fetchone()[0]
    header = {
        "agents": len(agents),
        "running": st["running"],
        "queued": st["queued"],
        "max_concurrent_runs": st["max_concurrent_runs"],
        "approvals_pending": len(approvals),
        "approvals_waiting_human": sum(1 for x in approvals if x["status"] == "waiting_human"),
        "approvals_longest_wait_s": longest,
        "learnings_today": int(learn_today),
        "cost_today_usd": round(sum(cost_today.values()), 6),
        "tokens_today": sum(tok_today.values()) + running_tokens,
        "rate_limit": hub.aggregator.rate_limit,
        "estop": st["estop"],
        "auth_block": st["auth_block"],
        "runtime": st["runtime"],
        "cli_version": cli_version,
        "cli_untested": bool(cli_version and st["runtime"] != "fake" and cli_version not in tested),
        "cli_unknown_alert": unknown if unknown.get("alert") else None,
        "subscribers": st["subscribers"],
        "version": st["version"],
    }
    return {"hub": header, "agents": rows, "approvals": approvals, "epoch": hub.bus.epoch, "last_seq": hub.bus.last_seq, "server_ts": iso()}


def register_rpc(d: "Daemon") -> None:
    hub = d.hub

    async def run_steer(p: dict[str, Any]) -> dict[str, Any]:
        return await steer(hub, str(_req(p, "id")), str(_req(p, "text")), by=str(p.get("by") or "user"), channel=str(p.get("channel") or "cli"))

    def create_from_template(p: dict[str, Any]) -> dict[str, Any]:
        cfg, soul = templates.render(str(_req(p, "template")), str(_req(p, "id")), p.get("overrides") or {})
        return hub.agents.create(cfg, p.get("soul") or soul)

    d.methods.update(
        {
            "tui.snapshot": lambda p: snapshot(hub, p),
            "run.steer": run_steer,
            "template.list": lambda p: templates.list_templates(),
            "template.get": lambda p: templates.load(str(_req(p, "name"))),
            "agent.create_from_template": create_from_template,
            "run.takeover": lambda p: takeover.prepare(hub, str(_req(p, "id"))),
            "run.takeover_record": lambda p: takeover.record(
                hub, str(_req(p, "id")), exit_code=p.get("exit_code"), started_at=p.get("started_at"), launched=bool(p.get("launched", True))
            ),
        }
    )
