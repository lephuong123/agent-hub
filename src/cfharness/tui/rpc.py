"""RPC phía daemon của M2: `tui.snapshot`, `run.steer`, `template.*`, `agent.create_from_template`,
`run.takeover`, `run.takeover_record`.

Chữ ký:
- `tui.snapshot {}` → ảnh chụp cho màn `top` trong MỘT lần gọi (FR-UI-01):
  `{hub: {...header...}, agents: [row], approvals: [pending], epoch, last_seq, server_ts}`.
  `row` = `{id, name, role, model, team, status, raw_status, paused, current_run_id, task, run_started_at,
  last_tool, last_tool_preview, last_tool_at, run_tokens, run_cost_usd, tokens_today, cost_today_usd,
  pending_approvals, waiting_human, last_run_id, last_run_status, session_id}`.
  `status` hiển thị ∈ working | waiting | waiting_human | idle | error | paused.
  RPC đi qua `SnapshotCache` (TUI, web, REPL dùng chung một lần tính; cũ tối đa `SNAPSHOT_TTL_S`, tính lại ngay khi có event/ghi DB).
  Dữ liệu từng agent nạp theo lô (`_prefetch`) — không truy vấn trong vòng lặp agent.
- `tui.agent_state {id}` → `{agent: row|None}`: một dòng như trên, chỉ truy vấn của agent đó (trang Điều phối). Chỉ đọc, ai gọi cũng được
  như `tui.snapshot` (không nhạy cảm, không phụ thuộc người gọi).
- `run.steer {id (run hoặc agent), text, by?, channel?}` → `{run_id, sent, seq, steer_count}` (FR-RT-06).
- `template.list {}` / `template.get {name}`; `agent.create_from_template {id, template, overrides?, soul?}` (FR-AG-07).
- `run.takeover {id}` → argv/cwd để chạy `claude --resume` tương tác; `run.takeover_record {id, exit_code?, started_at?,
  launched?}` → Run `kind=human_takeover` (FR-UI-10).
"""

from __future__ import annotations

import copy
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
    except Exception:  # best-effort: bảng team chưa có (DB cũ) → không nhãn team
        return {}
    out: dict[str, str] = {}
    for r in rows:
        out[r["agent_id"]] = f"{out[r['agent_id']]},{r['team']}" if r["agent_id"] in out else r["team"]
    return out


def _task_titles(hub: "Hub", task_ids: list[str | None]) -> dict[str, str]:
    """task_id → tiêu đề cho cả danh sách trong MỘT truy vấn (thay `_task_title` gọi từng agent)."""
    ids = sorted({t for t in task_ids if t})
    if not ids:
        return {}
    try:
        rows = hub.store.conn.execute("SELECT id, title FROM tasks WHERE id IN (SELECT value FROM json_each(?))", (json.dumps(ids),))
        return {r["id"]: r["title"] for r in rows}
    except Exception:  # best-effort: bảng tasks chưa có (DB cũ) → không tiêu đề
        return {}


def _runs_by_id(hub: "Hub", run_ids: list[str | None]) -> dict[str, dict[str, Any]]:
    """run_id → dòng run (KHÔNG cột `diff`) cho cả danh sách trong MỘT truy vấn (thay `get_run` từng agent — `get_run` kéo cả diff)."""
    ids = sorted({r for r in run_ids if r})
    if not ids:
        return {}
    store = hub.store
    sql = f"SELECT {store._runs_list_cols()} FROM runs WHERE id IN (SELECT value FROM json_each(?))"
    return {r["id"]: r for r in (store._row("runs", x) for x in store.conn.execute(sql, (json.dumps(ids),))) if r}


LAST_RUNS_SQL = ("SELECT {cols} FROM runs WHERE rowid IN "
                 "(SELECT (SELECT rowid FROM runs WHERE agent_id=j.value ORDER BY queued_at DESC LIMIT 1) FROM json_each(?) j)")
"""Run mới nhất (theo `queued_at`) của TỪNG agent trong danh sách — MỘT truy vấn; truy vấn con tương quan đi theo index
`runs_agent(agent_id, queued_at)` (giống `list_runs(agent_id=…, limit=1)`, cùng thứ tự khi trùng `queued_at`)."""


def _last_runs(hub: "Hub", agent_ids: list[str]) -> dict[str, dict[str, Any]]:
    """agent_id → run mới nhất (không cột `diff`), thay N lần `list_runs(agent_id=…, limit=1)`."""
    ids = list(dict.fromkeys(agent_ids))
    if not ids:
        return {}
    store = hub.store
    rows = store.conn.execute(LAST_RUNS_SQL.format(cols=store._runs_list_cols()), (json.dumps(ids),))
    return {r["agent_id"]: r for r in (store._row("runs", x) for x in rows) if r}


TODAY_USAGE_SQL = (
    "SELECT agent_id, COALESCE(SUM(cost_usd), 0) AS cost, COALESCE(SUM(CASE WHEN json_valid(usage) THEN "
    + " + ".join(f"CAST(COALESCE(json_extract(usage, '$.{k}'), 0) AS INTEGER)" for k in USAGE_KEYS)
    + " END), 0) AS tok FROM runs WHERE ended_at >= ?{agent} GROUP BY +agent_id"
)
"""Token + chi phí theo agent của các run kết thúc từ một mốc — cộng NGAY TRONG SQLite (trước đây kéo từng dòng `usage` về
`json.loads` trong Python). `+agent_id`: giữ khoảng `ended_at` trên index `runs_ended` (như `Store.cost_since_by_agent`)."""


def _today_usage(hub: "Hub", since: str, agent_id: str | None = None) -> tuple[dict[str, int], dict[str, float]]:
    """(token, chi phí) hôm nay theo agent. Cùng kết quả với cách cũ (`_tokens` từng dòng): usage hỏng/không phải object → 0 token."""
    if agent_id is None:
        rows = hub.store.conn.execute(TODAY_USAGE_SQL.format(agent=""), (since,))
    else:
        rows = hub.store.conn.execute(TODAY_USAGE_SQL.format(agent=" AND agent_id=?"), (since, agent_id))
    tok: dict[str, int] = {}
    cost: dict[str, float] = {}
    for r in rows:
        tok[r["agent_id"]] = int(r["tok"] or 0)
        cost[r["agent_id"]] = float(r["cost"] or 0.0)
    return tok, cost


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


LEARN_COUNT_SQL = "SELECT COUNT(*) FROM events WHERE type >= 'learning.' AND type < 'learning/' AND ts >= ?"
"""Đếm event `learning.*` từ một mốc. Dùng KHOẢNG trên `type` ('/' là ký tự ngay sau '.') thay cho `LIKE 'learning.%'` để dùng được index
`events_type_ts(type, ts)` — LIKE không dùng index (quét ~157 ms trên DB thật)."""


def _row_run_id(a: dict[str, Any], active: list[str]) -> str | None:
    return a.get("current_run_id") or (active or [None])[0]


def _prefetch(hub: "Hub", agents: list[dict[str, Any]], active_by_agent: dict[str, list[str]],
              waiting_runs: dict[str, dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, str]]:
    """Nạp trước, theo lô, dữ liệu mà `_agent_row` cần cho mọi agent: (run hiện tại theo id, run mới nhất theo agent — chỉ agent
    không có run hiện tại, tiêu đề task). Ba truy vấn cho cả bảng thay vì tới 3 truy vấn MỖI agent (N+1)."""
    runs = _runs_by_id(hub, [_row_run_id(a, active_by_agent.get(a["id"]) or []) for a in agents])
    no_run = [a["id"] for a in agents if _row_run_id(a, active_by_agent.get(a["id"]) or []) not in runs]
    last = _last_runs(hub, no_run)
    task_ids: list[str | None] = []
    for a in agents:
        src = runs.get(_row_run_id(a, active_by_agent.get(a["id"]) or []) or "") or waiting_runs.get(a["id"])
        task_ids.append((src or {}).get("task_id"))
    return runs, last, _task_titles(hub, task_ids)


def _agent_row(hub: "Hub", a: dict[str, Any], *, now: float, pend: list[dict[str, Any]], wh: dict[str, Any] | None, active: list[str],
               team: str | None, tokens_today: int, cost_today: float, sbx: dict[str, Any], trust: Any,
               runs: dict[str, dict[str, Any]], last_runs: dict[str, dict[str, Any]], titles: dict[str, str]) -> dict[str, Any]:
    """Một dòng agent của `tui.snapshot` / `tui.agent_state` (cùng định dạng). `runs`/`last_runs`/`titles` từ `_prefetch` (không truy vấn ở đây)."""
    aid = a["id"]
    live = hub.aggregator.agents.get(aid)
    run_id = _row_run_id(a, active)
    run = runs.get(run_id) if run_id else None
    last = run or last_runs.get(aid)
    raw = a["status"]
    if raw == "paused":
        disp = "paused"
    elif wh is not None or any(x["status"] == "waiting_human" for x in pend):
        disp = "waiting_human"
    elif pend:
        disp = "waiting"
    else:
        disp = raw if raw in ("working", "idle", "error", "waiting") else ("idle" if raw != "disabled" else "paused")
    if disp in ("idle", "error") and active:
        disp = "working"
    task_src = run or wh
    task = None
    if task_src:
        task = titles.get(task_src.get("task_id") or "") or _first_line(task_src.get("prompt"))
    is_running = bool(run and run["status"] == "running")
    run_tokens = int(live.run_tokens) if (live and is_running) else 0
    run_cost = float(live.run_cost_est) if (live and is_running) else 0.0
    return {
        "id": aid,
        "name": a.get("name") or aid,
        "sandbox_localhost": bool(sbx.get("allow_localhost")),
        "sandbox_domains": list(sbx.get("allow_domains") or []),
        "trust": trust,
        "role": a.get("role") or "",
        "model": a.get("model"),
        "team": team,
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
        "tokens_today": tokens_today + run_tokens,
        "cost_today_usd": round(cost_today, 6),
        "pending_approvals": [x["id"] for x in pend],
        "waiting_human": wh["id"] if wh else None,
        "last_run_id": (last or {}).get("id") or (wh or {}).get("id"),
        "last_run_status": (last or {}).get("status"),
        "session_id": (live.session_id if live else None) or (last or {}).get("session_id"),
    }


def agent_state(hub: "Hub", p: dict[str, Any] | None = None) -> dict[str, Any]:
    """`tui.agent_state {id}` → `{agent: row|None}`: MỘT dòng agent như trong `tui.snapshot` nhưng chỉ truy vấn của agent đó (trang Điều phối
    chỉ cần dòng của điều phối viên — không dựng cả ảnh chụp mọi agent). Agent không có/đã lưu trữ → `agent: None`."""
    aid = str(_req(p or {}, "id"))
    store = hub.store
    a = store.get_agent(aid)
    if a is None or a.get("archived_at"):
        return {"agent": None}
    now = time.time()
    pend = hub.guard.annotate_always(hub.approvals.list("pending", agent_id=aid, limit=500))
    for x in pend:
        x["wait_s"] = round(_age_s(x.get("opened_at"), now) or 0.0, 1)
    whs = store.list_runs(agent_id=aid, status="waiting_human", limit=1)
    active = [ar.run_id for ar in hub.runs.active.values() if ar.agent_id == aid]
    tok, cost = _today_usage(hub, _today_start_utc(), aid)
    wh = whs[0] if whs else None
    runs, last, titles = _prefetch(hub, [a], {aid: active}, {aid: wh} if wh else {})
    row = _agent_row(hub, a, now=now, pend=pend, wh=wh, active=active, team=_teams_by_agent(hub).get(aid),
                     tokens_today=tok.get(aid, 0), cost_today=cost.get(aid, 0.0), sbx=hub.guard.sandbox_overrides().get(aid) or {},
                     trust=hub.guard.trust_all().get(aid), runs=runs, last_runs=last, titles=titles)
    return {"agent": row}


def _status_lite(hub: "Hub", cfg: dict[str, Any]) -> dict[str, Any]:
    """Phần của `hub.status()` mà header snapshot dùng — bỏ các phần nặng snapshot không cần (đếm agent, `cost_today` — snapshot đã tự
    tính —, bộ đếm lỗi log, usage theo epoch, tài khoản) và đọc config MỘT lần (`cfg`)."""
    from .. import __version__

    runs = hub.runs
    return {
        "version": __version__,
        "runtime": cfg["runtime"]["kind"],
        "max_concurrent_runs": cfg["hub"]["max_concurrent_runs"],
        "running": len(runs.active),
        "queued": len(runs.queue),
        "estop": runs.estop,
        "auth_block": runs.auth_block,
        "subscribers": hub.bus.subscriber_count,
        "migrated_to": runs.frozen,
    }


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
    tok_today, cost_today = _today_usage(hub, since)
    waiting_runs: dict[str, dict[str, Any]] = {}
    for r in store.list_runs(status="waiting_human", limit=500):
        waiting_runs.setdefault(r["agent_id"], r)
    active_by_agent: dict[str, list[str]] = {}
    for ar in hub.runs.active.values():
        active_by_agent.setdefault(ar.agent_id, []).append(ar.run_id)
    runs, last_runs, titles = _prefetch(hub, agents, active_by_agent, waiting_runs)

    rows = []
    running_tokens = 0
    sbx = hub.guard.sandbox_overrides()  # SEC: nhãn cảnh báo ngoại lệ sandbox theo agent
    trusts = hub.guard.trust_all()  # nhãn ⚠TRUST: agent chạy không rào chắn (guard/service.trust_set)
    for a in agents:
        aid = a["id"]
        row = _agent_row(hub, a, now=now, pend=appr_by_agent.get(aid, []), wh=waiting_runs.get(aid), active=active_by_agent.get(aid) or [],
                         team=teams.get(aid), tokens_today=tok_today.get(aid, 0), cost_today=cost_today.get(aid, 0.0),
                         sbx=sbx.get(aid) or {}, trust=trusts.get(aid), runs=runs, last_runs=last_runs, titles=titles)
        running_tokens += row["run_tokens"]
        rows.append(row)

    cfg = hub.config.data  # đọc config MỘT lần (mỗi lần đọc là một bản trộn DEFAULTS mới)
    st = _status_lite(hub, cfg)
    cli = store.conn.execute("SELECT cli_version FROM runs WHERE cli_version IS NOT NULL ORDER BY queued_at DESC LIMIT 1").fetchone()
    cli_version = cli["cli_version"] if cli else None
    tested = ((cfg.get("runtime") or {}).get("claude") or {}).get("tested_versions") or []
    compat = store.state_get("cli_compat") or {}  # H-10: kiểm phiên bản lúc daemon khởi động (runtime/compat.py)
    if compat.get("version"):
        cli_version = compat["version"]
    if st["runtime"] == "fake":
        cli_version = cli_version or "fake"
    unknown = store.state_get("cli_unknown_rate") or {}
    longest = max((x["wait_s"] for x in approvals), default=0.0)
    learn_today = store.conn.execute(
        LEARN_COUNT_SQL, (since,)
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
        "migrated_to": st.get("migrated_to"),  # chuyển nhà (hub_freeze.py)
        "runtime": st["runtime"],
        "cli_version": cli_version,
        "cli_untested": bool(cli_version and st["runtime"] != "fake" and cli_version not in tested),
        "cli_unknown_alert": unknown if unknown.get("alert") else None,
        "subscribers": st["subscribers"],
        "version": st["version"],
    }
    # task `ready` không claim được (wait_reason: workspace_locked, agent_busy…) — agent nhận nó vẫn idle nên bảng agent không thấy
    waiting_tasks = [
        {"id": r["id"], "title": _first_line(r["title"]), "assignee": r["assignee_agent_id"], "team_id": r["team_id"], "wait_reason": r["wait_reason"]}
        for r in store.conn.execute(
            "SELECT id, title, assignee_agent_id, team_id, wait_reason FROM tasks WHERE status='ready' AND wait_reason IS NOT NULL "
            "ORDER BY updated_at LIMIT 100"
        )
    ]
    return {"hub": header, "agents": rows, "approvals": approvals, "waiting_tasks": waiting_tasks, "epoch": hub.bus.epoch,
            "last_seq": hub.bus.last_seq, "server_ts": iso()}


SNAPSHOT_TTL_S = 1.0
"""Tuổi tối đa của ảnh chụp dùng chung (plan-api-caching PR3; quyết định đã chốt: snapshot được phép cũ 1–3 s)."""


class SnapshotCache:
    """Cache `tui.snapshot` đầy đủ ở daemon: TUI (mỗi 1 s), web (mỗi trang + poll) và REPL điều phối dùng chung MỘT lần tính.

    Trúng khi CẢ HAI: tuổi < `ttl` VÀ khoá không đổi. Khoá = (epoch, `bus.last_seq`, số dòng kết nối GHI đã đổi — `Store.write_changes`,
    số run đang chạy/chờ, mtime config). Daemon là writer duy nhất của hub.db nên mọi ghi (approval, câu hỏi, task, run, agent…) đều đổi
    khoá → lần gọi sau tính lại ngay; thứ ghi NGOÀI daemon (CLI `hub_freeze`…) hay trạng thái chỉ trong RAM chỉ cũ tối đa `ttl`.
    Khoá đọc SAU khi tính (nếu chính lúc tính có ghi thì lần sau vẫn trúng). Trả bản sao sâu — người gọi sửa thoải mái, cache không đổi.
    Hàm `snapshot()` vẫn không cache (test/đường gọi trực tiếp)."""

    def __init__(self, hub: "Hub", *, ttl: float = SNAPSHOT_TTL_S, clock: Any = time.monotonic) -> None:
        self.hub = hub
        self.ttl = ttl
        self._clock = clock
        self._key: tuple[Any, ...] | None = None
        self._at = 0.0
        self._val: dict[str, Any] | None = None
        self.hits = 0
        self.misses = 0

    def _key_now(self) -> tuple[Any, ...]:
        h = self.hub
        return (h.bus.epoch, h.bus.last_seq, h.store.write_changes, len(h.runs.active), len(h.runs.queue), getattr(h.config, "_mtime", None))

    def invalidate(self) -> None:
        self._val = None

    def get(self) -> dict[str, Any]:
        if self._val is not None and self._clock() - self._at < self.ttl and self._key_now() == self._key:
            self.hits += 1
            return copy.deepcopy(self._val)
        self.misses += 1
        val = snapshot(self.hub)
        self._val, self._key, self._at = val, self._key_now(), self._clock()
        return copy.deepcopy(val)


def register_rpc(d: "Daemon") -> None:
    hub = d.hub
    cache = d.snapshot_cache = SnapshotCache(hub)

    async def run_steer(p: dict[str, Any]) -> dict[str, Any]:
        return await steer(hub, str(_req(p, "id")), str(_req(p, "text")), by=str(p.get("by") or "user"), channel=str(p.get("channel") or "cli"))

    def create_from_template(p: dict[str, Any]) -> dict[str, Any]:
        cfg, soul = templates.render(str(_req(p, "template")), str(_req(p, "id")), p.get("overrides") or {})
        return hub.agents.create(cfg, p.get("soul") or soul)

    d.methods.update(
        {
            "tui.snapshot": lambda p: cache.get(),  # không tham số (params bị bỏ qua như trước)
            "tui.agent_state": lambda p: agent_state(hub, p),
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
