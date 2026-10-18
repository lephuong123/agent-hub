"""TUI "htop cho agent" — Textual. Chỉ là VIEWER: mọi trạng thái nằm ở hub (bus + aggregator);
đóng màn attach hay thoát app không giết agent (ở bản thật, hub là daemon riêng)."""

from __future__ import annotations

import sys
import time
from datetime import datetime
from typing import Any

from rich.markdown import Markdown
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DataTable, Footer, Input, Label, RichLog, Select, SelectionList, Static

from .bus import STATUS_ORDER, AgentState, Proposal
from .events import HubEvent
from .simulator import MCP_CATALOG, MODELS, TEAMS, FakeHub

# ============================================================================ định dạng
STATUS_STYLE = {
    "working": ("●", "làm việc", "bold green"),
    "waiting": ("◐", "chờ duyệt", "bold yellow"),
    "idle": ("○", "rảnh", "grey62"),
    "error": ("✖", "lỗi", "bold red"),
    "stopped": ("■", "đã dừng", "grey50"),
}


def status_text(status: str) -> Text:
    icon, label, style = STATUS_STYLE.get(status, ("?", status, ""))
    return Text(f"{icon} {label}", style=style)


def fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def fmt_cost(c: float) -> str:
    return f"${c:.2f}"


def fmt_dur(s: float | None) -> str:
    if s is None:
        return "—"
    s = max(0, int(s))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s // 3600}h{(s % 3600) // 60:02d}"


def short_tool(name: str | None) -> str:
    if not name:
        return "—"
    if name.startswith("mcp__"):
        parts = name.split("__")
        return f"{parts[-1]}"
    return name


def since(dt: datetime | None) -> float | None:
    if dt is None:
        return None
    return time.time() - dt.timestamp()


def task_cell(a: AgentState) -> Text:
    req = a.pending_request
    if a.status == "waiting" and req:
        return Text(f"duyệt: {req.get('command') or req.get('tool')}", style="yellow")
    if a.status == "error" and a.last_error:
        return Text(a.last_error, style="red")
    if a.status in ("idle", "stopped"):
        return Text(a.task or "—", style="grey50")
    return Text(a.task or "—")


def last_tool_cell(a: AgentState) -> Text:
    if not a.last_tool:
        return Text("—", style="grey50")
    name = short_tool(a.last_tool)
    if a.tool_running:
        return Text.assemble((name, "cyan"), (" …", "cyan"))
    return Text.assemble(name, (f" ({a.last_tool_dur:.1f}s)" if a.last_tool_dur is not None else "", "grey62"))


PROPOSAL_STATUS = {
    "pending": ("⏳ chờ duyệt", "bold yellow"),
    "applied": ("✓ đã áp dụng", "green"),
    "rejected": ("✖ từ chối", "red"),
    "rolled_back": ("↺ đã rollback", "grey50"),
}


def proposal_status_text(p: Proposal) -> Text:
    label, style = PROPOSAL_STATUS[p.status]
    if p.status == "applied" and p.applied_by and p.applied_by.startswith("auto"):
        label = "✓ tự áp dụng"
    return Text(label, style=style)


def diff_text(diff: str) -> Text:
    t = Text()
    for line in diff.splitlines():
        style = "green" if line.startswith("+") else "red" if line.startswith("-") else "grey62"
        t.append(line + "\n", style=style)
    return t


LEARN_ACTION = {"applied": "đã áp dụng", "rejected": "bị từ chối", "rolled_back": "đã rollback"}


def activity_line(ev: HubEvent, a: AgentState | None) -> Text | None:
    p = ev.payload
    t = ev.type
    if t == "tool.start":
        body = Text.assemble(("⚡ ", "yellow"), (short_tool(p.get("name")), "bold white"), (f" {p.get('summary', '')}"[:70], "grey70"))
    elif t == "tool.complete" and p.get("is_error"):
        body = Text(f"✖ {short_tool(p.get('name'))} lỗi: {p.get('summary')}", style="red")
    elif t == "turn.start":
        body = Text(f"▶ bắt đầu: {p.get('task') or ''}", style="#7aa2f7")
    elif t == "turn.complete":
        st = p.get("status")
        style = {"success": "green", "interrupted": "grey62"}.get(st, "red")
        body = Text(f"■ kết thúc ({st}) · {fmt_tokens(p.get('total_tokens') or 0)} tok · ${p.get('cost_usd') or 0:.4f}", style=style)
    elif t == "request.open":
        body = Text(f"◐ xin duyệt {p.get('tool')}: {p.get('command')}", style="bold yellow")
    elif t == "request.cancel":
        body = Text(f"  yêu cầu {p.get('request_id')}: {p.get('decision')} ({p.get('by')})", style="yellow")
    elif t == "error":
        body = Text(f"✖ {p.get('message')}", style="bold red")
    elif t == "learning.proposal":
        body = Text(f"✎ đề xuất ({p.get('scope')}/{p.get('kind')}): {p.get('title')}", style="magenta")
    elif t == "learning.applied":
        body = Text(f"✎ {p.get('proposal_id')} {LEARN_ACTION.get(p.get('action'), p.get('action'))} · {p.get('by')}", style="grey62")
    elif t == "user.message":
        body = Text(f"› steer: {p.get('text')}", style="cyan")
    elif t == "subagent.spawn":
        body = Text(f"⤷ subagent {p.get('name')}: {p.get('task')}", style="magenta")
    else:
        return None
    line = Text(ev.ts.astimezone().strftime("%H:%M:%S "), style="grey42")
    line.append(f"{ev.agent_id:<3} {(a.name if a else '?'):<11} ", style="grey62")
    line.append_text(body)
    return line


# ============================================================================ màn htop
class HtopScreen(Screen):
    BINDINGS = [
        Binding("enter", "attach", "Xem"),
        Binding("a", "approve", "Duyệt"),
        Binding("d", "deny", "Từ chối"),
        Binding("s", "steer", "Steer"),
        Binding("x", "stop", "Dừng"),
        Binding("l", "learnings", "Learnings"),
        Binding("n", "new_agent", "Tạo"),
        Binding("e", "edit_agent", "Sửa"),
        Binding("delete", "delete_agent", "Xoá"),
        Binding("t", "cycle_team", "Team"),
        Binding("slash", "search", "Lọc"),
        Binding("q", "app.quit", "Thoát"),
    ]

    team_filter: str | None = None
    text_filter: str = ""

    def compose(self) -> ComposeResult:
        yield Static(id="summary")
        yield Input(placeholder="Lọc theo tên / task / team… (Enter áp dụng, Esc huỷ)", id="filter")
        table = DataTable(id="agents", cursor_type="row", zebra_stripes=True)
        yield table
        yield Static(id="detail")
        yield Static(Text(" Hoạt động gần đây (kênh firehose — không gồm delta token)", style="bold grey70"), id="activity-title")
        yield RichLog(id="activity", max_lines=500, wrap=False, markup=False)
        yield Footer()

    def on_mount(self) -> None:
        # firehose: chỉ event tóm tắt của MỌI agent; nạp sẵn ít dòng gần nhất từ ring buffer
        bus = self.hub.bus
        recent: list[HubEvent] = []
        for aid in list(self.hub.agg.agents):
            evs, _ = bus.replay(aid)
            recent.extend(e for e in evs if not e.is_delta)
        recent.sort(key=lambda e: e.ts)
        log = self.query_one("#activity", RichLog)
        for ev in recent[-60:]:
            self.log_activity(ev, log)
        self.firehose = bus.subscribe(None, include_deltas=False)
        self.set_interval(0.3, self.drain_activity)
        table = self.query_one("#agents", DataTable)
        for label, key, width in [
            ("ID", "id", 4), ("TÊN", "name", 12), ("TEAM", "team", 11), ("TRẠNG THÁI", "status", 12),
            ("TASK", "task", 38), ("TOOL CUỐI", "tool", 20), ("T", "t", 4), ("TOK", "tok", 5), ("$", "cost", 6),
        ]:
            table.add_column(label, key=key, width=width)
        self.query_one("#filter").display = False
        self.refresh_view()
        self.set_interval(0.5, self.refresh_view)
        table.focus()

    @property
    def hub(self) -> FakeHub:
        return self.app.hub  # type: ignore[attr-defined]

    def visible_agents(self) -> list[AgentState]:
        out = []
        for a in self.hub.agg.agents.values():
            if self.team_filter and a.team != self.team_filter:
                continue
            if self.text_filter:
                hay = f"{a.id} {a.name} {a.team} {a.task or ''} {a.role}".lower()
                if self.text_filter.lower() not in hay:
                    continue
            out.append(a)
        return out

    def selected_id(self) -> str | None:
        table = self.query_one("#agents", DataTable)
        if table.row_count == 0:
            return None
        try:
            key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        except Exception:
            return None
        return key.value

    def refresh_view(self) -> None:
        agg = self.hub.agg
        counts = agg.count_by_status()
        summary = Text()
        summary.append(" agent-hub ", style="bold reverse #7aa2f7")
        summary.append(f"  {len(agg.agents)} agent  ", style="bold")
        for st in ("working", "waiting", "idle", "error", "stopped"):
            if counts.get(st):
                icon, label, style = STATUS_STYLE[st]
                summary.append(f"{icon} {counts[st]} {label}  ", style=style)
        summary.append("│ ", style="grey42")
        summary.append(f"{fmt_tokens(agg.total_tokens)} tok", style="bold cyan")
        summary.append("  ·  ", style="grey42")
        summary.append(f"{fmt_cost(agg.total_cost)} hôm nay", style="bold magenta")
        if agg.pending_proposals:
            summary.append("  ·  ", style="grey42")
            summary.append(f"✎ {agg.pending_proposals} đề xuất học chờ duyệt (l)", style="yellow")
        summary.append("\n")
        flt = self.team_filter or "Tất cả"
        summary.append(" ↑↓ chọn · Enter xem chi tiết", style="grey62")
        summary.append(f"  ·  Team: {flt}", style="grey70")
        if self.text_filter:
            summary.append(f"  ·  Lọc: “{self.text_filter}”", style="grey70")
        summary.append(f"  ·  epoch {self.hub.bus.epoch}  ·  {datetime.now().strftime('%H:%M:%S')}", style="grey42")
        self.query_one("#summary", Static).update(summary)

        table = self.query_one("#agents", DataTable)
        prev = self.selected_id()
        table.clear()
        agents = sorted(self.visible_agents(), key=lambda a: (a.team, a.id))
        for a in agents:
            active = a.status in ("working", "waiting")
            table.add_row(
                Text(a.id, style="grey62"),
                Text(a.name, style="bold"),
                a.team,
                status_text(a.status),
                task_cell(a),
                last_tool_cell(a),
                fmt_dur(since(a.turn_started)) if active else "—",
                Text(fmt_tokens(a.tokens), justify="right"),
                Text(fmt_cost(a.cost), justify="right"),
                key=a.id,
            )
        if prev:
            for i, a in enumerate(agents):
                if a.id == prev:
                    table.move_cursor(row=i, animate=False)
                    break
        self.update_detail()

    def on_unmount(self) -> None:
        self.firehose.close()

    def drain_activity(self) -> None:
        log = self.query_one("#activity", RichLog)
        for ev in self.firehose.drain():
            self.log_activity(ev, log)

    def log_activity(self, ev: HubEvent, log: RichLog) -> None:
        line = activity_line(ev, self.hub.agg.agents.get(ev.agent_id))
        if line is not None:
            log.write(line)

    def update_detail(self) -> None:
        aid = self.selected_id()
        detail = self.query_one("#detail", Static)
        a = self.hub.agg.agents.get(aid) if aid else None
        if a is None:
            detail.update(Text(" Không có agent nào khớp bộ lọc. Bấm n để tạo agent mới.", style="grey50"))
            return
        t = Text()
        t.append(f" {a.name}", style="bold")
        t.append(f" · {a.role or '—'} · model {a.model} · MCP: {', '.join(a.mcp_servers) or '—'}", style="grey70")
        if a.subagents:
            t.append(f" · {a.subagents} subagent", style="grey70")
        req = a.pending_request
        if req:
            t.append("\n ◐ CHỜ DUYỆT ", style="bold black on yellow")
            t.append(f" {req.get('tool')}: {req.get('command')}  ", style="yellow")
            t.append(f"— {req.get('reason')}  ", style="grey70")
            t.append("[a] duyệt  [d] từ chối", style="bold")
        elif a.status == "error" and a.last_error:
            t.append(f"\n ✖ {a.last_error}", style="red")
        detail.update(t)

    @on(DataTable.RowHighlighted)
    def _row_highlighted(self) -> None:
        self.update_detail()

    @on(DataTable.RowSelected)
    def _row_selected(self, event: DataTable.RowSelected) -> None:
        self.app.push_screen(AttachScreen(event.row_key.value))

    # -- actions
    def action_attach(self) -> None:
        aid = self.selected_id()
        if aid:
            self.app.push_screen(AttachScreen(aid))

    def action_approve(self) -> None:
        aid = self.selected_id()
        if aid and self.hub.approve(aid):
            self.notify(f"Đã duyệt yêu cầu của {aid}", severity="information")
        else:
            self.notify("Agent này không có yêu cầu đang chờ", severity="warning")
        self.refresh_view()

    def action_deny(self) -> None:
        aid = self.selected_id()
        if aid and self.hub.deny(aid):
            self.notify(f"Đã từ chối yêu cầu của {aid}", severity="warning")
        else:
            self.notify("Agent này không có yêu cầu đang chờ", severity="warning")
        self.refresh_view()

    def action_steer(self) -> None:
        aid = self.selected_id()
        if aid:
            self.app.push_screen(AttachScreen(aid, steer=True))

    def action_stop(self) -> None:
        aid = self.selected_id()
        if aid and self.hub.stop(aid):
            self.notify(f"Đã dừng {aid}", severity="warning")
        self.refresh_view()

    def action_learnings(self) -> None:
        self.app.push_screen(LearningsScreen())

    def action_new_agent(self) -> None:
        def done(result: dict | None) -> None:
            if result:
                aid = self.hub.create_agent(**result)
                self.notify(f"Đã tạo agent {aid} — {result['name']}")
                self.refresh_view()

        self.app.push_screen(AgentFormScreen(), done)

    def action_edit_agent(self) -> None:
        aid = self.selected_id()
        a = self.hub.agg.agents.get(aid) if aid else None
        if a is None:
            return

        def done(result: dict | None) -> None:
            if result:
                self.hub.update_agent(aid, **result)
                self.notify(f"Đã cập nhật {aid}")
                self.refresh_view()

        self.app.push_screen(AgentFormScreen(a), done)

    def action_delete_agent(self) -> None:
        aid = self.selected_id()
        a = self.hub.agg.agents.get(aid) if aid else None
        if a is None:
            return

        def done(ok: bool | None) -> None:
            if ok:
                self.hub.delete_agent(aid)
                self.notify(f"Đã xoá agent {aid} ({a.name})", severity="warning")
                self.refresh_view()

        self.app.push_screen(ConfirmScreen(f"Xoá agent [b]{a.name}[/b] ({aid})?\nPhiên đang chạy sẽ bị dừng."), done)

    def action_cycle_team(self) -> None:
        options: list[str | None] = [None, *TEAMS]
        teams_present = {a.team for a in self.hub.agg.agents.values()}
        options += sorted(t for t in teams_present if t not in TEAMS)
        idx = options.index(self.team_filter) if self.team_filter in options else 0
        self.team_filter = options[(idx + 1) % len(options)]
        self.refresh_view()

    def action_search(self) -> None:
        inp = self.query_one("#filter", Input)
        inp.display = True
        inp.value = self.text_filter
        inp.focus()

    @on(Input.Submitted, "#filter")
    def _filter_submitted(self, event: Input.Submitted) -> None:
        self.text_filter = event.value.strip()
        event.input.display = False
        self.query_one("#agents").focus()
        self.refresh_view()

    def key_escape(self) -> None:
        inp = self.query_one("#filter", Input)
        if inp.display:
            inp.display = False
            self.query_one("#agents").focus()
        elif self.text_filter or self.team_filter:
            self.text_filter, self.team_filter = "", None
            self.refresh_view()


# ============================================================================ transcript
class Transcript(VerticalScroll):
    """Hiển thị luồng event của MỘT agent: chữ stream, suy nghĩ thu gọn, dòng tool, request…"""

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self._cur_kind: str | None = None
        self._cur_widget: Static | None = None
        self._cur_buf = ""
        self._tools: dict[str, tuple[Static, str, str]] = {}
        self._reasoning: list[tuple[Static, list[str], list[int]]] = []
        self.expand_reasoning = False
        self._turn_t0: datetime | None = None

    def _add(self, renderable: Any, classes: str = "") -> Static:
        w = Static(renderable, classes=classes)
        self.mount(w)
        return w

    def _end_block(self) -> None:
        self._cur_kind, self._cur_widget, self._cur_buf = None, None, ""

    def _render_reasoning(self, text: str, est: int) -> Text:
        t = Text()
        if self.expand_reasoning:
            t.append("▾ Suy nghĩ\n", style="italic grey50")
            t.append(text or "(ẩn nội dung — chỉ có ước lượng token)", style="italic grey42")
        else:
            snippet = " ".join(text.split())[:90]
            t.append("▸ Suy nghĩ", style="italic grey50")
            meta = f" · {len(text)} ký tự" if text else f" · ~{est} token"
            t.append(meta + " · ", style="grey42")
            t.append((snippet + "…") if snippet else "", style="italic grey42")
        return t

    def toggle_reasoning(self) -> None:
        self.expand_reasoning = not self.expand_reasoning
        for w, parts, est in self._reasoning:
            w.update(self._render_reasoning("".join(parts), est[0]))

    def note(self, text: str, style: str = "grey50") -> None:
        self._end_block()
        self._add(Text(text, style=style), "note")

    def handle(self, ev: HubEvent) -> None:
        p = ev.payload
        t = ev.type
        if t == "message.delta":
            if self._cur_kind != "text":
                self._end_block()
                self._cur_kind = "text"
                self._cur_widget = self._add("", "msg")
            self._cur_buf += p.get("text", "")
            assert self._cur_widget is not None
            self._cur_widget.update(Markdown(self._cur_buf))
            return
        if t == "reasoning.delta":
            if self._cur_kind != "reasoning":
                self._end_block()
                self._cur_kind = "reasoning"
                w = self._add("", "reasoning")
                self._cur_widget = w
                self._reasoning.append((w, [], [0]))
            w, parts, est = self._reasoning[-1]
            parts.append(p.get("text") or "")
            est[0] = p.get("estimated_tokens") or est[0]
            w.update(self._render_reasoning("".join(parts), est[0]))
            return
        if t in ("usage.tick", "agent.status"):
            return
        if t in ("learning.proposal", "learning.applied") and self._cur_kind == "text" and self._cur_widget is not None:
            # sự kiện "bên lề" không được cắt đôi đoạn chữ đang stream → chèn phía trên đoạn đó
            line = self._learning_line(ev)
            self.mount(Static(line, classes="learning"), before=self._cur_widget)
            return
        self._end_block()
        if t == "turn.start":
            self._turn_t0 = ev.ts
            line = Text()
            line.append(f"── Lượt mới · {p.get('model')} · {ev.ts.astimezone().strftime('%H:%M:%S')} ", style="bold #7aa2f7")
            if p.get("task"):
                line.append(f"· {p['task']}", style="#7aa2f7")
            mcp = p.get("mcp_servers") or []
            if mcp:
                line.append(f"\n   MCP: {', '.join(mcp)}", style="grey50")
            self._add(line, "turn")
        elif t == "tool.start":
            name, summ = p.get("name", "?"), p.get("summary", "")
            w = self._add(self._tool_line(name, summ, None, running=True), "tool")
            self._tools[p.get("tool_use_id", "")] = (w, name, summ)
        elif t == "tool.complete":
            tid = p.get("tool_use_id", "")
            w, name, summ = self._tools.pop(tid, (None, p.get("name", "?"), ""))
            line = self._tool_line(name, summ, p.get("duration_s"), running=False, error=p.get("is_error"))
            if w is None:
                self._add(line, "tool")
            else:
                w.update(line)
            res = Text("┊    ↳ ", style="grey42")
            res.append(p.get("summary") or "(trống)", style="red" if p.get("is_error") else "grey62")
            self._add(res, "tool-result")
        elif t == "subagent.spawn":
            self._add(Text(f"┊ ⤷ subagent {p.get('name')}: {p.get('task')}", style="magenta"), "tool")
        elif t == "request.open":
            tool = self._tools.get(p.get("tool_use_id") or "")
            if tool is not None:
                tool[0].update(self._tool_line(tool[1], tool[2], None, running=True, label="chờ duyệt…"))
            body = Text()
            body.append(" ◐ CHỜ DUYỆT ", style="bold black on yellow")
            body.append(f" {p.get('tool')}: ", style="bold yellow")
            body.append(f"{p.get('command')}\n", style="yellow")
            body.append(f"   {p.get('reason')}   ", style="grey70")
            body.append("[a] duyệt  [d] từ chối", style="bold")
            self._add(body, "request")
        elif t == "request.cancel":
            dec = p.get("decision")
            label, style = {
                "approved": ("✓ Đã duyệt", "green"),
                "denied": ("✖ Đã từ chối", "red"),
                "cancelled": ("■ Đã huỷ yêu cầu", "grey50"),
            }.get(dec, (str(dec), ""))
            self._add(Text(f"   {label} bởi {p.get('by')}", style=style), "note")
        elif t == "user.message":
            line = Text("› Bạn (steer): ", style="bold cyan")
            line.append(p.get("text", ""), style="cyan")
            self._add(line, "user")
        elif t == "turn.complete":
            status = p.get("status")
            dur = (ev.ts - self._turn_t0).total_seconds() if self._turn_t0 else None
            tok = fmt_tokens(p.get("total_tokens") or 0)
            cost = p.get("cost_usd") or 0
            if status == "success":
                txt = Text(f"✓ Hoàn tất · {fmt_dur(dur)} · {tok} tok · ${cost:.4f}", style="green")
            elif status == "interrupted":
                txt = Text(f"■ Đã dừng giữa chừng · {tok} tok · ${cost:.4f}", style="grey62")
            else:
                txt = Text(f"✖ Lượt kết thúc lỗi ({p.get('subtype')}) · {tok} tok · ${cost:.4f}", style="bold red")
            self._add(txt, "turn-end")
        elif t == "error":
            self._add(Text(f"✖ Lỗi: {p.get('message')}", style="bold red"), "error")
        elif t in ("learning.proposal", "learning.applied"):
            self._add(self._learning_line(ev), "learning")

    @staticmethod
    def _learning_line(ev: HubEvent) -> Text:
        p = ev.payload
        if ev.type == "learning.proposal":
            line = Text(f"✎ Đề xuất học ({p.get('kind')}, phạm vi {p.get('scope')}): ", style="magenta")
            line.append(p.get("title", ""), style="bold magenta")
            return line
        return Text(f"   ✎ {LEARN_ACTION.get(p.get('action'), p.get('action'))} · {p.get('by')}", style="grey62")

    @staticmethod
    def _tool_line(
        name: str, summ: str, dur: float | None, *, running: bool, error: bool = False, label: str = "đang chạy…"
    ) -> Text:
        t = Text("┊ ", style="grey42")
        t.append("⚡ ", style="yellow")
        t.append(short_tool(name), style="bold red" if error else "bold")
        if name.startswith("mcp__"):
            t.append(f" [{name.split('__')[1]}]", style="grey50")
        if summ:
            t.append(f" {summ}", style="grey70")
        if running:
            t.append(f"  ({label})", style="yellow" if label != "đang chạy…" else "cyan")
        elif dur is not None:
            t.append(f" ({dur:.1f}s)", style="grey50")
        return t


# ============================================================================ màn attach
class AttachScreen(Screen):
    BINDINGS = [
        Binding("escape", "back", "Về bảng"),
        Binding("a", "approve", "Duyệt"),
        Binding("d", "deny", "Từ chối"),
        Binding("s", "steer", "Steer"),
        Binding("x", "stop", "Dừng"),
        Binding("r", "toggle_reasoning", "Suy nghĩ"),
        Binding("end", "scroll_end", "Cuối", show=False),
    ]

    def __init__(self, agent_id: str, steer: bool = False) -> None:
        super().__init__()
        self.agent_id = agent_id
        self.open_steer = steer
        self.follow = True

    @property
    def hub(self) -> FakeHub:
        return self.app.hub  # type: ignore[attr-defined]

    def compose(self) -> ComposeResult:
        yield Static(id="attach-head")
        yield Transcript(id="transcript")
        yield Static(id="banner")
        yield Input(placeholder="Gửi chỉ đạo cho agent (Enter gửi, Esc huỷ)…", id="steer")
        yield Static(id="statusbar")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#steer").display = False
        tr = self.query_one(Transcript)
        # attach: replay toàn bộ ring buffer rồi nối live — không hở, không trùng
        self.sub = self.hub.bus.subscribe(self.agent_id, since_seq=0)
        n = self.sub.queue.qsize()
        last = self.hub.bus.last_seq(self.agent_id)
        tr.note(
            f"↺ Attach: phát lại {n} event từ ring buffer (seq 1→{last}, epoch {self.hub.bus.epoch})"
            + (" — đã bị cắt, đọc thêm ở log bền" if self.sub.truncated else ""),
            "italic grey50",
        )
        self.drain()  # phần replay; từ đây là live (không chèn mốc để khỏi cắt đôi đoạn chữ đang stream)
        self.set_interval(0.1, self.drain)
        self.set_interval(0.5, self.refresh_bars)
        self.refresh_bars()
        if self.open_steer:
            self.action_steer()

    def on_unmount(self) -> None:
        self.sub.close()

    def drain(self) -> None:
        evs = self.sub.drain()
        if not evs:
            return
        tr = self.query_one(Transcript)
        for ev in evs:
            tr.handle(ev)
        if self.follow:
            tr.scroll_end(animate=False)
        self.refresh_bars()

    def refresh_bars(self) -> None:
        a = self.hub.agg.agents.get(self.agent_id)
        head = self.query_one("#attach-head", Static)
        bar = self.query_one("#statusbar", Static)
        banner = self.query_one("#banner", Static)
        if a is None:
            head.update(Text(f" Agent {self.agent_id} đã bị xoá", style="bold red"))
            return
        h = Text()
        h.append(f" {a.id} ", style="bold reverse #7aa2f7")
        h.append(f" {a.name} ", style="bold")
        h.append(f"· {a.team} · {a.role or '—'}  ", style="grey70")
        h.append_text(status_text(a.status))
        if a.task:
            h.append(f"   {a.task}", style="grey70")
        head.update(h)

        req = a.pending_request
        if req:
            b = Text()
            b.append(" ◐ CHỜ DUYỆT ", style="bold black on yellow")
            b.append(f" {req.get('tool')}: {req.get('command')}  ", style="bold yellow")
            b.append("[a] duyệt · [d] từ chối", style="bold")
            banner.update(b)
            banner.display = True
        else:
            banner.display = False

        model = a.running_model or a.model
        pct = a.context_percent
        filled = int(round(pct / 10))
        s = Text()
        s.append(f" {model} ", style="bold")
        s.append("· ctx ", style="grey50")
        s.append("█" * filled, style="green" if pct < 60 else "yellow" if pct < 85 else "red")
        s.append("░" * (10 - filled), style="grey35")
        s.append(f" {pct:.0f}% ", style="bold")
        s.append(f"· {a.tps:.0f} tok/s ", style="cyan")
        s.append(f"· {fmt_tokens(a.tokens)} tok ", style="grey70")
        s.append(f"· {fmt_cost(a.cost)} ", style="bold magenta")
        s.append(f"· seq {self.hub.bus.last_seq(self.agent_id)} · phiên {(a.session_id or '—')[:8]}", style="grey42")
        bar.update(s)

    def action_back(self) -> None:
        inp = self.query_one("#steer", Input)
        if inp.display:
            inp.display = False
            return
        self.app.pop_screen()

    def action_approve(self) -> None:
        if self.hub.approve(self.agent_id):
            self.notify("Đã duyệt")
        else:
            self.notify("Không có yêu cầu đang chờ", severity="warning")
        self.drain()

    def action_deny(self) -> None:
        if self.hub.deny(self.agent_id):
            self.notify("Đã từ chối", severity="warning")
        else:
            self.notify("Không có yêu cầu đang chờ", severity="warning")
        self.drain()

    def action_stop(self) -> None:
        self.hub.stop(self.agent_id)
        self.drain()

    def action_steer(self) -> None:
        inp = self.query_one("#steer", Input)
        inp.display = True
        inp.focus()

    def action_toggle_reasoning(self) -> None:
        self.query_one(Transcript).toggle_reasoning()

    def action_scroll_end(self) -> None:
        self.query_one(Transcript).scroll_end(animate=False)

    @on(Input.Submitted, "#steer")
    def _steer_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        event.input.display = False
        if text:
            self.hub.steer(self.agent_id, text)
            self.drain()


# ============================================================================ màn learnings
class LearningsScreen(Screen):
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Về bảng"),
        Binding("a", "approve", "Duyệt"),
        Binding("d", "reject", "Từ chối"),
        Binding("r", "rollback", "Rollback"),
    ]

    @property
    def hub(self) -> FakeHub:
        return self.app.hub  # type: ignore[attr-defined]

    def compose(self) -> ComposeResult:
        yield Static(id="learn-head")
        yield DataTable(id="proposals", cursor_type="row", zebra_stripes=True)
        yield Static(id="diff")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#proposals", DataTable)
        for label, key, width in [
            ("ID", "id", 7), ("AGENT", "agent", 12), ("PHẠM VI", "scope", 8), ("LOẠI", "kind", 7),
            ("ĐỀ XUẤT", "title", 58), ("TRẠNG THÁI", "status", 15),
        ]:
            table.add_column(label, key=key, width=width)
        self.refresh_view()
        self.set_interval(1.0, self.refresh_view)
        table.focus()

    def ordered(self) -> list[Proposal]:
        props = list(self.hub.agg.proposals.values())
        props.sort(key=lambda p: p.created, reverse=True)
        props.sort(key=lambda p: 0 if p.status == "pending" else 1)
        return props

    def selected(self) -> Proposal | None:
        table = self.query_one("#proposals", DataTable)
        if table.row_count == 0:
            return None
        key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        return self.hub.agg.proposals.get(key.value)

    def refresh_view(self) -> None:
        agg = self.hub.agg
        head = Text()
        head.append(" Learnings ", style="bold reverse magenta")
        head.append(f"  {len(agg.proposals)} đề xuất · ", style="bold")
        head.append(f"{agg.pending_proposals} chờ duyệt", style="bold yellow")
        head.append(
            "\n Chính sách: thay đổi nhỏ tầng AGENT (memory/skill của riêng nó) → tự áp dụng sau eval gate, có ledger + rollback.\n"
            " Tầng TEAM/global, hồ sơ user, xoá skill → luôn chờ người duyệt.",
            style="grey62",
        )
        self.query_one("#learn-head", Static).update(head)
        table = self.query_one("#proposals", DataTable)
        prev = self.selected()
        table.clear()
        props = self.ordered()
        for p in props:
            a = agg.agents.get(p.agent_id)
            table.add_row(
                Text(p.proposal_id, style="grey62"),
                f"{p.agent_id} {a.name if a else '(đã xoá)'}",
                Text(p.scope, style="bold cyan" if p.scope == "team" else ""),
                p.kind,
                p.title,
                proposal_status_text(p),
                key=p.proposal_id,
            )
        if prev:
            for i, p in enumerate(props):
                if p.proposal_id == prev.proposal_id:
                    table.move_cursor(row=i, animate=False)
                    break
        self.update_diff()

    def update_diff(self) -> None:
        p = self.selected()
        w = self.query_one("#diff", Static)
        if p is None:
            w.update(Text(" Chưa có đề xuất nào.", style="grey50"))
            return
        t = Text()
        t.append(f" {p.title}\n", style="bold")
        t.append(f" {p.kind} · phạm vi {p.scope} · tạo {p.created.astimezone().strftime('%H:%M:%S')}", style="grey62")
        if p.applied_by:
            t.append(f" · xử lý bởi: {p.applied_by}", style="grey62")
        t.append("\n\n")
        t.append_text(diff_text(p.diff))
        keys = {"pending": "[a] duyệt · [d] từ chối", "applied": "[r] rollback", "rejected": "[a] duyệt lại",
                "rolled_back": ""}[p.status]
        if keys:
            t.append(f"\n {keys}", style="bold")
        w.update(t)

    @on(DataTable.RowHighlighted)
    def _hl(self) -> None:
        self.update_diff()

    def _act(self, fn_name: str, ok_msg: str) -> None:
        p = self.selected()
        if p is None:
            return
        if getattr(self.hub, fn_name)(p.proposal_id):
            self.notify(f"{ok_msg}: {p.title}")
        else:
            self.notify("Thao tác không hợp lệ với trạng thái hiện tại", severity="warning")
        self.refresh_view()

    def action_approve(self) -> None:
        self._act("approve_learning", "Đã duyệt")

    def action_reject(self) -> None:
        self._act("reject_learning", "Đã từ chối")

    def action_rollback(self) -> None:
        self._act("rollback_learning", "Đã rollback")


# ============================================================================ form tạo/sửa agent
class AgentFormScreen(ModalScreen[dict | None]):
    BINDINGS = [Binding("escape", "cancel", "Huỷ")]

    def __init__(self, agent: AgentState | None = None) -> None:
        super().__init__()
        self.agent = agent

    def compose(self) -> ComposeResult:
        a = self.agent
        with Vertical(id="form"):
            yield Label(f"Sửa agent {a.id}" if a else "Tạo agent mới", id="form-title")
            yield Label("Tên")
            yield Input(value=a.name if a else "", placeholder="vd: thu-ky", id="f-name")
            yield Label("Vai trò (persona)")
            yield Input(value=a.role if a else "", placeholder="vd: Tổng hợp công văn đến hằng ngày", id="f-role")
            with Horizontal(classes="row"):
                with Vertical():
                    yield Label("Team")
                    yield Select([(t, t) for t in TEAMS], value=a.team if a else TEAMS[0], allow_blank=False, id="f-team")
                with Vertical():
                    yield Label("Model")
                    yield Select([(m, m) for m in MODELS], value=a.model if a else "sonnet", allow_blank=False, id="f-model")
            yield Label("MCP servers (Space để chọn)")
            chosen = set(a.mcp_servers) if a else set()
            yield SelectionList[str](*[(m, m, m in chosen) for m in MCP_CATALOG], id="f-mcp")
            yield Label("", id="f-error")
            with Horizontal(id="buttons"):
                yield Button("Lưu", variant="primary", id="save")
                yield Button("Huỷ", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#f-name", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#save")
    @on(Input.Submitted)
    def _save(self) -> None:
        name = self.query_one("#f-name", Input).value.strip()
        if not name:
            self.query_one("#f-error", Label).update(Text("Tên không được để trống", style="bold red"))
            self.query_one("#f-name", Input).focus()
            return
        self.dismiss(
            {
                "name": name,
                "role": self.query_one("#f-role", Input).value.strip(),
                "team": self.query_one("#f-team", Select).value,
                "model": self.query_one("#f-model", Select).value,
                "mcp": list(self.query_one("#f-mcp", SelectionList).selected),
            }
        )


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "no", "Huỷ"), Binding("y", "yes", "Đồng ý")]

    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm"):
            yield Label(self.message)
            with Horizontal(id="buttons"):
                yield Button("Xoá (y)", variant="error", id="yes")
                yield Button("Huỷ (Esc)", id="no")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


# ============================================================================ app
CSS = """
Screen { background: $surface; }
#summary { height: 2; padding: 0 1; background: $panel; }
#filter { height: 3; margin: 0 1; }
#agents { height: auto; max-height: 60%; margin: 1 1 0 1; }
#detail { height: 3; padding: 0 1; margin-top: 1; border-top: solid $primary-darken-2; }
#activity-title { height: 1; padding: 0 1; background: $panel; }
#activity { height: 1fr; padding: 0 1; scrollbar-size-vertical: 1; }

#attach-head { height: 1; background: $panel; padding: 0 1; }
#transcript { height: 1fr; padding: 0 1; scrollbar-size-vertical: 1; }
#transcript > Static { margin: 0; }
#transcript > .msg { padding: 0 0 0 2; margin: 1 0 0 0; }
#transcript > .turn { margin: 1 0 0 0; }
#transcript > .turn-end { margin: 0 0 1 0; }
#transcript > .request { border: tall $warning; padding: 0 1; margin: 1 0; }
#transcript > .user { margin: 1 0 0 0; }
#banner { height: auto; padding: 0 1; background: $warning 15%; }
#steer { margin: 0 1; border: tall $accent; }
#statusbar { height: 1; background: $panel; padding: 0 1; }

#learn-head { height: 4; padding: 0 1; background: $panel; }
#proposals { height: 1fr; margin: 1 1 0 1; }
#diff { height: 12; padding: 0 1; border-top: solid $primary-darken-2; }

AgentFormScreen, ConfirmScreen { align: center middle; background: $background 60%; }
#form { width: 76; height: auto; padding: 1 2; border: thick $accent; background: $panel; }
#form-title { text-style: bold; color: $accent; margin-bottom: 1; }
#form Label { margin-top: 1; color: $text-muted; }
#form .row { height: auto; }
#form .row > Vertical { width: 1fr; height: auto; }
#f-mcp { height: 9; }
#buttons { height: auto; margin-top: 1; align-horizontal: right; }
#buttons Button { margin-left: 1; }
#confirm { width: 56; height: auto; padding: 1 2; border: thick $error; background: $panel; }
"""


class AgentHubApp(App):
    TITLE = "agent-hub"
    SUB_TITLE = "htop cho agent (demo, dữ liệu giả lập)"
    CSS = CSS

    def __init__(self, hub: FakeHub | None = None) -> None:
        super().__init__()
        self.hub = hub or FakeHub(live=True)

    async def on_mount(self) -> None:
        self.push_screen(HtopScreen())
        await self.hub.start()

    async def on_unmount(self) -> None:
        await self.hub.shutdown()


def main() -> None:
    speed = 1.0
    if "--fast" in sys.argv:
        speed = 0.3
    AgentHubApp(FakeHub(live=True, speed=speed)).run()


if __name__ == "__main__":
    main()
