"""Trạng thái thuần của màn `top` (không phụ thuộc Textual — test được trực tiếp).

`TopModel` giữ ảnh chụp `tui.snapshot` (nạp lại ≥ 1 Hz) và áp event firehose ngay khi tới để dòng agent
cập nhật LAST TOOL/TOK/$/STATUS không chờ lượt nạp kế (FR-UI-01, NFR-01). Đo độ trễ event→màn hình
(`latencies_ms`) = lúc TUI áp event − `ts` của envelope (adapter gán `ts` ngay khi đọc dòng stdout).
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from rich.text import Text

# BRD FR-UI-01: ●working ◐waiting ⏸waiting_human ○idle ✖error ‖paused
STATUS_STYLE: dict[str, tuple[str, str, str]] = {
    "working": ("●", "working", "bold green"),
    "waiting": ("◐", "waiting", "bold yellow"),
    "waiting_human": ("⏸", "waiting_human", "bold red"),
    "idle": ("○", "idle", "grey62"),
    "error": ("✖", "error", "bold red"),
    "paused": ("‖", "paused", "grey50"),
}
STATUS_ORDER = {"waiting_human": 0, "waiting": 1, "working": 2, "error": 3, "paused": 4, "idle": 5}
SORT_KEYS = ("status", "cost", "time")
SORT_LABEL = {"status": "trạng thái", "cost": "$", "time": "thời gian chạy"}
# Event làm thay đổi tập dữ liệu cần ảnh chụp mới (approval, vòng đời run/agent)
SNAPSHOT_TRIGGERS = (
    "run.queued", "run.started", "run.completed", "run.failed", "run.cancelled", "run.interrupted", "run.waiting_human",
    "run.resumed", "run.takeover", "approval.open", "approval.resolved", "approval.waiting_human", "agent.created",
    "agent.updated", "agent.archived", "agent.deleted", "agent.restored", "hub.estop", "auth.blocked",
)


def parse_ts(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def status_text(status: str) -> Text:
    icon, label, style = STATUS_STYLE.get(status, ("?", status, ""))
    return Text(f"{icon} {label}", style=style)


def fmt_tokens(n: int | float | None) -> str:
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n / 1_000:.0f}k"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def fmt_cost(c: float | None) -> str:
    c = float(c or 0.0)
    return f"${c:.2f}" if c >= 1 else f"${c:.3f}"


def fmt_dur(s: float | None) -> str:
    if s is None:
        return "—"
    s = max(0, int(s))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}"
    return f"{s // 3600}h{(s % 3600) // 60:02d}"


def short_tool(name: str | None) -> str:
    if not name:
        return "—"
    if name.startswith("mcp__"):
        parts = name.split("__")
        return f"{parts[-1]}@{parts[1]}" if len(parts) >= 3 else parts[-1]
    return name


@dataclass
class Row:
    """Một dòng agent (ảnh chụp + phần live từ event)."""

    data: dict[str, Any]
    tool_running: bool = False
    last_tool_dur: float | None = None

    def __getitem__(self, k: str) -> Any:
        return self.data.get(k)

    @property
    def id(self) -> str:
        return self.data["id"]


@dataclass
class TopModel:
    rows: dict[str, Row] = field(default_factory=dict)
    header: dict[str, Any] = field(default_factory=dict)
    approvals: list[dict[str, Any]] = field(default_factory=list)
    epoch: str | None = None
    last_seq: int = 0
    text_filter: str = ""
    team_filter: str | None = None
    sort_key: str = "status"
    latencies_ms: deque = field(default_factory=lambda: deque(maxlen=20_000))
    events_seen: int = 0
    measure_after: int = 0  # chỉ đo độ trễ cho event có seq > mốc này (bỏ phần replay khi đăng ký)
    snapshots: int = 0
    snapshot_ms: deque = field(default_factory=lambda: deque(maxlen=2_000))

    # ------------------------------------------------------------------ snapshot
    def load(self, snap: dict[str, Any]) -> None:
        old = self.rows
        self.rows = {}
        for a in snap.get("agents") or []:
            prev = old.get(a["id"])
            r = Row(a)
            if prev is not None and prev["last_tool"] == a.get("last_tool") and prev["last_tool_at"] == a.get("last_tool_at"):
                r.tool_running, r.last_tool_dur = prev.tool_running, prev.last_tool_dur
            self.rows[a["id"]] = r
        self.header = snap.get("hub") or {}
        self.approvals = snap.get("approvals") or []
        if snap.get("epoch") != self.epoch:
            self.epoch = snap.get("epoch")
            self.last_seq = int(snap.get("last_seq") or 0)  # daemon mới khởi động (epoch mới) → reset mốc
        elif not self.events_seen:
            self.last_seq = max(self.last_seq, int(snap.get("last_seq") or 0))
        self.snapshots += 1

    # ------------------------------------------------------------------ live
    def apply(self, ev: dict[str, Any], now: float | None = None) -> tuple[str | None, bool]:
        """Áp một event (dict wire). Trả (agent_id có dòng thay đổi, cần nạp ảnh chụp mới)."""
        now = time.time() if now is None else now
        self.events_seen += 1
        if ev.get("epoch") == self.epoch or self.epoch is None:
            self.last_seq = max(self.last_seq, int(ev.get("seq") or 0))
        ts = parse_ts(ev.get("ts"))
        if ts is not None and int(ev.get("seq") or 0) > self.measure_after:
            self.latencies_ms.append((now - ts) * 1000.0)
        t = ev.get("type", "")
        p = ev.get("payload") or {}
        aid = ev.get("agent_id")
        need = t in SNAPSHOT_TRIGGERS or t.startswith(("task.", "team."))
        r = self.rows.get(aid) if aid else None
        if r is None:
            return None, need or bool(aid and t in ("tool.start", "agent.status"))
        d = r.data
        if t == "tool.start":
            d["last_tool"] = p.get("name")
            d["last_tool_preview"] = p.get("args_preview")
            d["last_tool_at"] = ev.get("ts")
            r.tool_running, r.last_tool_dur = True, None
        elif t == "tool.complete":
            if p.get("name") == d.get("last_tool"):
                r.tool_running, r.last_tool_dur = False, p.get("duration_s")
        elif t == "usage.tick":
            d["run_tokens"] = int(p.get("total_tokens") or d.get("run_tokens") or 0)
            d["run_cost_usd"] = float(p.get("cost_usd") or d.get("run_cost_usd") or 0.0)
        elif t == "agent.status":
            st = p.get("status")
            if st and d.get("status") not in ("waiting_human", "waiting", "paused"):
                d["status"] = st
            need = True
        elif t == "run.started":
            d["run_started_at"] = ev.get("ts")
            d["status"] = "working" if d.get("status") in ("idle", "error") else d.get("status")
        else:
            return aid, need
        return aid, need

    # ------------------------------------------------------------------ view
    def teams(self) -> list[str]:
        out: set[str] = set()
        for r in self.rows.values():
            for t in (r["team"] or "").split(","):
                if t:
                    out.add(t)
        return sorted(out)

    def visible(self, now: float | None = None) -> list[Row]:
        now = time.time() if now is None else now
        out = []
        f = self.text_filter.lower().strip()
        for r in self.rows.values():
            if self.team_filter and self.team_filter not in (r["team"] or "").split(","):
                continue
            if f:
                hay = " ".join(str(r[k] or "") for k in ("id", "name", "role", "team", "status", "task", "last_tool", "model")).lower()
                if f not in hay:
                    continue
            out.append(r)
        if self.sort_key == "cost":
            out.sort(key=lambda r: (-(float(r["cost_today_usd"] or 0) + float(r["run_cost_usd"] or 0)), r.id))
        elif self.sort_key == "time":
            out.sort(key=lambda r: (-(self.elapsed(r, now) or -1), r.id))
        else:
            out.sort(key=lambda r: (STATUS_ORDER.get(r["status"], 9), r["team"] or "~", r.id))
        return out

    @staticmethod
    def elapsed(r: Row, now: float) -> float | None:
        t0 = parse_ts(r["run_started_at"])
        return None if t0 is None or r["status"] not in ("working", "waiting") else now - t0

    def pending_for(self, agent_id: str) -> list[dict[str, Any]]:
        return sorted((a for a in self.approvals if a["agent_id"] == agent_id), key=lambda a: a.get("opened_at") or "")

    def latency_stats(self) -> dict[str, float]:
        xs = sorted(self.latencies_ms)
        if not xs:
            return {"n": 0}

        def q(p: float) -> float:
            return round(xs[min(len(xs) - 1, int(p * len(xs)))], 1)

        return {"n": len(xs), "p50": q(0.50), "p95": q(0.95), "p99": q(0.99), "max": round(xs[-1], 1)}


# ============================================================================ hiển thị ô
def task_cell(r: Row) -> Text:
    if r["status"] == "waiting_human":
        return Text("⏸ chờ người duyệt — " + (r["task"] or ""), style="bold red")
    if r["status"] == "waiting":
        return Text("◐ chờ duyệt — " + (r["task"] or ""), style="yellow")
    if r["status"] in ("idle", "paused", "error"):
        return Text(r["task"] or "—", style="grey50")
    return Text(r["task"] or "—")


def last_tool_cell(r: Row, now: float | None = None) -> Text:
    name = r["last_tool"]
    if not name:
        return Text("—", style="grey50")
    now = time.time() if now is None else now
    t = Text(short_tool(name), style="cyan" if r.tool_running else "")
    if r.tool_running:
        t0 = parse_ts(r["last_tool_at"])
        t.append(f" …{fmt_dur(now - t0) if t0 else ''}", style="cyan")
    elif r.last_tool_dur is not None:
        t.append(f" ({r.last_tool_dur:.1f}s)", style="grey62")
    else:
        t0 = parse_ts(r["last_tool_at"])
        if t0:
            t.append(f" ({fmt_dur(now - t0)} trước)", style="grey50")
    return t


def tok_cell(r: Row) -> Text:
    n = r["run_tokens"] if r["status"] in ("working", "waiting") else r["tokens_today"]
    return Text(fmt_tokens(n), justify="right")


def cost_cell(r: Row) -> Text:
    c = float(r["cost_today_usd"] or 0) + (float(r["run_cost_usd"] or 0) if r["status"] in ("working", "waiting") else 0)
    return Text(fmt_cost(c), justify="right")


def header_text(m: TopModel, *, now: float | None = None) -> Text:
    h = m.header
    t = Text()
    t.append(" agent-hub top ", style="bold reverse #7aa2f7")
    t.append(f"  {h.get('agents', 0)} agent · ", style="bold")
    t.append(f"{h.get('running', 0)} đang chạy", style="bold green")
    if h.get("queued"):
        t.append(f" · {h['queued']} chờ slot", style="yellow")
    t.append(f" (tối đa {h.get('max_concurrent_runs', '?')})  ", style="grey62")
    n = h.get("approvals_pending", 0)
    if n:
        t.append(f" ⚠ {n} thao tác phá huỷ chờ người · lâu nhất {fmt_dur(h.get('approvals_longest_wait_s'))} ", style="bold white on red")
        t.append("  ")
    t.append(f"học hôm nay {h.get('learnings_today', 0)}  ", style="magenta")
    t.append(f"{fmt_cost(h.get('cost_today_usd'))} hôm nay · {fmt_tokens(h.get('tokens_today'))} tok", style="bold magenta")
    t.append("\n")
    rl = h.get("rate_limit") or {}
    rl_status = rl.get("status") if isinstance(rl, dict) else None
    t.append(" rate limit: ", style="grey62")
    t.append(str(rl_status or "—"), style="bold red" if rl_status and rl_status not in ("allowed", "ok") else "grey70")
    es = h.get("estop") or {}
    if es.get("on"):
        t.append("  ■ ESTOP BẬT ", style="bold white on red")
        if es.get("reason"):
            t.append(f" {es['reason']}", style="red")
    if h.get("auth_block"):
        t.append("  ✖ claude chưa đăng nhập", style="bold red")
    t.append(f"  · CLI {h.get('cli_version') or '?'}", style="grey62")
    if h.get("cli_untested"):
        t.append(" (chưa kiểm!)", style="bold yellow")
    ua = h.get("cli_unknown_alert")
    if ua:
        t.append(f"  ✖ {float(ua.get('rate') or 0) * 100:.0f}% event lạ/1h — cân nhắc ESTOP", style="bold white on red")
    t.append(f"  · sắp xếp: {SORT_LABEL.get(m.sort_key, m.sort_key)}", style="grey62")
    if m.team_filter:
        t.append(f"  · team: {m.team_filter}", style="grey70")
    if m.text_filter:
        t.append(f"  · lọc: “{m.text_filter}”", style="grey70")
    t.append(f"  · {datetime.now().strftime('%H:%M:%S')}", style="grey42")
    return t


ACTIVITY_SKIP = frozenset({"usage.tick", "agent.status", "ratelimit.update", "hub.config_changed"})


def activity_line(ev: dict[str, Any], names: dict[str, str]) -> Text | None:
    """Một dòng firehose (FR-UI-09) cho event mức summary; None = không hiển thị."""
    t = ev.get("type", "")
    if t in ACTIVITY_SKIP:
        return None
    p = ev.get("payload") or {}
    if t == "tool.start":
        body = Text.assemble(("⚡ ", "yellow"), (short_tool(p.get("name")), "bold"), (f" {p.get('args_preview') or ''}"[:80], "grey70"))
    elif t == "tool.complete":
        if not p.get("is_error"):
            return None
        body = Text(f"✖ {short_tool(p.get('name'))} lỗi: {(p.get('result_preview') or '')[:80]}", style="red")
    elif t == "run.queued":
        body = Text(f"… xếp hàng: {(p.get('prompt_preview') or '')[:80]}", style="grey62")
    elif t == "run.started":
        body = Text(f"▶ bắt đầu ({p.get('model') or '?'})", style="#7aa2f7")
    elif t in ("run.completed", "run.failed", "run.cancelled", "run.interrupted"):
        style = {"run.completed": "green", "run.cancelled": "grey62", "run.interrupted": "yellow"}.get(t, "bold red")
        body = Text(f"■ {p.get('outcome') or t.split('.')[1]} · ${float(p.get('cost_usd') or 0):.4f}", style=style)
        if p.get("error") and t != "run.completed":
            body.append(f" — {str(p['error'])[:80]}", style="red")
    elif t == "run.waiting_human":
        body = Text("⏸ run tạm dừng, chờ người duyệt", style="bold red")
    elif t in ("approval.open", "approval.waiting_human"):
        label = "⏸ chờ NGƯỜI duyệt" if t == "approval.waiting_human" else "◐ xin duyệt"
        body = Text(f"{label} [{p.get('matched_rule') or p.get('destructive_class') or p.get('request_id') or '?'}] {(p.get('preview') or p.get('tool') or '')[:80]}", style="bold red" if t == "approval.waiting_human" else "bold yellow")
    elif t == "approval.resolved":
        dec = p.get("decision")
        body = Text(f"  {'✓ cho phép' if dec == 'allowed' else '✖ từ chối' if dec == 'denied' else dec} {p.get('request_id') or ''} · {p.get('by')} qua {p.get('channel')}", style="green" if dec == "allowed" else "yellow")
    elif t == "guard.blocked":
        body = Text(f"⛔ guard chặn [{p.get('rule') or p.get('reason') or ''}]: {(p.get('preview') or p.get('detail') or '')[:80]}", style="bold red")
    elif t == "user.message":
        body = Text(f"› steer ({p.get('channel')}): {(p.get('text') or '')[:80]}", style="cyan")
    elif t == "steer.ack":
        body = Text("✓ agent đã nhận steer", style="cyan")
    elif t == "subagent.spawn":
        body = Text(f"⤷ subagent {p.get('type')}: {(p.get('description') or '')[:70]}", style="magenta")
    elif t == "message.complete":
        if ev.get("subagent_id"):
            return None
        body = Text(f"✎ {(p.get('text') or '').strip().splitlines()[0][:90] if (p.get('text') or '').strip() else ''}", style="grey70")
    elif t in ("error", "isolation.violation", "permission.denied", "auth.blocked", "budget.exceeded", "notify.failed"):
        body = Text(f"✖ {t}: {str(p.get('message') or p.get('name') or p.get('tool_name') or p)[:90]}", style="bold red")
    elif t.startswith("learning."):
        body = Text(f"✎ {t}", style="magenta")
    elif t.startswith(("task.", "team.")):
        body = Text(f"▣ {t} {p.get('task_id') or ''} {p.get('from') or ''}→{p.get('to') or ''}".rstrip("→ "), style="#bb9af7")
    elif t in ("hub.estop",):
        body = Text(f"■ ESTOP {'BẬT' if p.get('on') else 'tắt'} {p.get('reason') or ''}", style="bold red" if p.get("on") else "green")
    elif t == "run.takeover":
        body = Text("⇄ người tiếp quản bằng Claude Code", style="magenta")
    else:
        return None
    ts = parse_ts(ev.get("ts"))
    line = Text(datetime.fromtimestamp(ts).strftime("%H:%M:%S ") if ts else "         ", style="grey42")
    aid = ev.get("agent_id") or "hub"
    line.append(f"{aid[:14]:<14} ", style="grey62")
    line.append_text(body)
    return line
