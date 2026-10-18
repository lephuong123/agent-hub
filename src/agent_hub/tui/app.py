"""`agent-hub top` — màn "htop" cho đội agent (FR-UI-01..05, 09, 10), Textual.

TUI là CLIENT THUẦN của daemon (RPC qua Unix socket): mọi trạng thái nằm ở daemon; thoát TUI, Esc khỏi màn attach
hay mất kết nối KHÔNG ảnh hưởng run (NFR-05). Port từ `demo/agent_hub_demo/app.py` (không import demo).

Luồng dữ liệu:
- Bảng agent: `tui.snapshot` mỗi `refresh_s` (mặc định 1 s, NFR-03) + áp ngay event firehose (`events.subscribe`
  mức summary, không có delta token — FR-UI-09) để LAST TOOL/TOK/$ cập nhật tức thì (NFR-01).
- Attach (Enter): nạp transcript của run từ DB (`run.get events`), rồi `events.subscribe {run_id, since_seq, level:
  detail}` → ring buffer bù phần còn thiếu (kể cả delta) rồi nối live; khử trùng theo (epoch, seq) (FR-UI-03).
- Subscriber chậm bị daemon ngắt (`slow_consumer`, NFR-08) → TUI tự đăng ký lại từ seq cuối đã thấy.

Phím màn bảng: ↑↓ chọn · Enter attach · a approve · d deny · N báo nhầm (allow + đánh dấu) · s steer · x stop
(xác nhận) · X stop ngay · T takeover · n tạo · e sửa · Del xoá · / lọc · g team · o sắp xếp · t tasks · l learnings ·
f feedback (good|wrong|partial, H-16) · E estop · q thoát. Có `learning.applied` → thông báo ngắn "X learned: …".
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from rich.markdown import Markdown
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DataTable, Footer, Input, Label, RichLog, Select, SelectionList, Static

from ..rpc import DaemonUnavailable, RpcError
from .client import AsyncHubClient, subscribe
from .model import (
    STATUS_STYLE,
    SORT_KEYS,
    Row,
    TopModel,
    activity_line,
    cost_cell,
    fmt_cost,
    fmt_dur,
    fmt_tokens,
    header_text,
    last_tool_cell,
    parse_ts,
    short_tool,
    status_text,
    task_cell,
    tok_cell,
)

MODELS = ["haiku", "sonnet", "opus", "fable"]
PERMISSION_MODES = ["default", "acceptEdits", "plan", "auto", "dontAsk"]
COLUMNS = [
    ("ID", "id", 14), ("NAME", "name", 14), ("TEAM", "team", 10), ("STATUS", "status", 15), ("TASK", "task", 36),
    ("LAST TOOL", "tool", 20), ("T", "t", 6), ("TOK", "tok", 6), ("$", "cost", 7),
]
MAX_TRANSCRIPT_WIDGETS = 1500


def _err(e: Exception) -> str:
    return e.message if isinstance(e, RpcError) else str(e)


# ============================================================================ màn bảng (htop)
class HtopScreen(Screen):
    BINDINGS = [
        Binding("enter", "attach", "Attach", show=True),
        Binding("a", "approve", "Duyệt"),
        Binding("d", "deny", "Từ chối"),
        Binding("N", "false_positive", "Báo nhầm", show=False),
        Binding("s", "steer", "Steer"),
        Binding("x", "stop", "Dừng"),
        Binding("X", "stop_now", "Dừng ngay", show=False),
        Binding("T", "takeover", "Takeover", show=False),
        Binding("n", "new_agent", "Tạo"),
        Binding("e", "edit_agent", "Sửa"),
        Binding("delete", "delete_agent", "Xoá"),
        Binding("slash", "search", "Lọc"),
        Binding("g", "cycle_team", "Team", show=False),
        Binding("o", "cycle_sort", "Sắp xếp", show=False),
        Binding("t", "tasks", "Tasks"),
        Binding("l", "learnings", "Learnings"),
        Binding("f", "feedback", "Feedback"),  # hardening H-16 (FR-UI-01)
        Binding("E", "estop", "ESTOP", show=False),
        Binding("q", "app.quit", "Thoát"),
    ]

    def compose(self) -> ComposeResult:
        yield Static(id="summary")
        yield Input(placeholder="Lọc theo id / tên / task / team / trạng thái… (Enter áp dụng, Esc huỷ)", id="filter")
        yield DataTable(id="agents", cursor_type="row", zebra_stripes=True)
        yield Static(id="detail")
        yield Static(Text(" Firehose — event tóm tắt của mọi agent (không gồm delta token)", style="bold grey70"), id="activity-title")
        yield RichLog(id="activity", max_lines=1000, wrap=False, markup=False)
        yield Footer()

    @property
    def model(self) -> TopModel:
        return self.app.model  # type: ignore[attr-defined]

    @property
    def client(self) -> AsyncHubClient:
        return self.app.client  # type: ignore[attr-defined]

    def on_mount(self) -> None:
        table = self.query_one("#agents", DataTable)
        for label, key, width in COLUMNS:
            table.add_column(label, key=key, width=width)
        self.query_one("#filter").display = False
        self._row_order: list[str] = []
        self._snap_pending = False
        table.focus()
        self.render_all()
        self.set_interval(min(0.5, self.app.refresh_s), self.render_tick)  # type: ignore[attr-defined]  # NFR-03: ≥ 1 Hz có dư

    # -- dữ liệu
    def render_tick(self) -> None:
        """≥ 1 Hz: vẽ lại cột thời gian/header (dữ liệu đã có từ snapshot/event)."""
        try:
            self.render_all()
        except NoMatches:  # đang thoát app
            pass

    def render_all(self) -> None:
        m = self.model
        self.query_one("#summary", Static).update(header_text(m))
        table = self.query_one("#agents", DataTable)
        prev = self.selected_id()
        rows = m.visible()
        now = time.time()
        order = [r.id for r in rows]
        if order != self._row_order:
            table.clear()
            for r in rows:
                table.add_row(*self._cells(r, now), key=r.id)
            self._row_order = order
            if prev in order:
                table.move_cursor(row=order.index(prev), animate=False)
        else:
            for r in rows:
                for key, val in zip([c[1] for c in COLUMNS], self._cells(r, now)):
                    table.update_cell(r.id, key, val)
        self.update_detail()
        self.app.note_render()  # type: ignore[attr-defined]

    def _cells(self, r: Row, now: float) -> list[Any]:
        return [
            Text(r.id, style="grey62"),
            Text(r["name"] or r.id, style="bold"),
            Text(r["team"] or "—", style="grey70"),
            status_text(r["status"]),
            task_cell(r),
            last_tool_cell(r, now),
            Text(fmt_dur(TopModel.elapsed(r, now)), justify="right"),
            tok_cell(r),
            cost_cell(r),
        ]

    def update_row(self, aid: str) -> None:
        """Cập nhật ngay một dòng khi có event (không chờ nhịp vẽ)."""
        r = self.model.rows.get(aid)
        if r is None or aid not in self._row_order:
            return
        table = self.query_one("#agents", DataTable)
        now = time.time()
        for key, val in zip([c[1] for c in COLUMNS], self._cells(r, now)):
            table.update_cell(aid, key, val)
        if aid == self.selected_id():
            self.update_detail()

    def hub_event(self, ev: dict[str, Any], aid: str | None) -> None:
        if aid:
            self.update_row(aid)
        line = activity_line(ev, {})
        if line is not None:
            self.query_one("#activity", RichLog).write(line)

    def selected_id(self) -> str | None:
        table = self.query_one("#agents", DataTable)
        if table.row_count == 0:
            return None
        try:
            key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        except Exception:
            return None
        return key.value

    def selected(self) -> Row | None:
        aid = self.selected_id()
        return self.model.rows.get(aid) if aid else None

    def update_detail(self) -> None:
        r = self.selected()
        detail = self.query_one("#detail", Static)
        if r is None:
            detail.update(Text(" Chưa có agent nào khớp. Bấm n để tạo agent (có template researcher/coder/reviewer/ops).", style="grey50"))
            return
        t = Text()
        t.append(f" {r['name']}", style="bold")
        t.append(f" · {r['role'] or '—'} · model {r['model']}", style="grey70")
        if r["current_run_id"]:
            t.append(f" · run {r['current_run_id']}", style="grey62")
        elif r["last_run_id"]:
            t.append(f" · run cuối {r['last_run_id']} ({r['last_run_status']})", style="grey50")
        if r["last_tool_preview"]:
            t.append(f"\n ┊ {short_tool(r['last_tool'])}: {r['last_tool_preview']}"[:160], style="grey62")
        pend = self.model.pending_for(r.id)
        if pend:
            a = pend[0]
            t.append("\n ⏸ CHỜ NGƯỜI DUYỆT " if a["status"] == "waiting_human" else "\n ◐ CHỜ DUYỆT ", style="bold black on yellow")
            t.append(f" {a.get('preview') or a.get('tool_name')} ", style="bold yellow")
            t.append(f"· luật {a.get('matched_rule') or a.get('destructive_class') or '?'} · chờ {fmt_dur(a.get('wait_s'))} ", style="grey70")
            if a.get("reason"):
                t.append(f"· {str(a['reason'])[:80]} ", style="grey62")
            t.append("[a] cho phép  [d] từ chối  [N] báo nhầm", style="bold")
        detail.update(t)

    @on(DataTable.RowHighlighted)
    def _row_highlighted(self) -> None:
        self.update_detail()

    @on(DataTable.RowSelected)
    def _row_selected(self, event: DataTable.RowSelected) -> None:
        self.action_attach()

    # -- hành động
    def action_attach(self) -> None:
        r = self.selected()
        if r is None:
            return
        run_id = r["current_run_id"] or r["waiting_human"] or r["last_run_id"]
        if not run_id:
            self.notify(f"{r.id} chưa có run nào", severity="warning")
            return
        self.app.push_screen(AttachScreen(r.id, run_id))

    async def _decide(self, decision: str, note: str | None = None) -> None:
        r = self.selected()
        pend = self.model.pending_for(r.id) if r else []
        if not pend:
            pend = sorted(self.model.approvals, key=lambda a: a.get("opened_at") or "")
            if not pend:
                self.notify("Không có thao tác nào đang chờ duyệt", severity="warning")
                return
        await self.app.decide(pend[0], decision, note)  # type: ignore[attr-defined]

    async def action_approve(self) -> None:
        await self._decide("approve")

    def action_deny(self) -> None:
        def done(note: str | None) -> None:
            if note is not None:
                self.run_worker(self._decide("deny", note or None))

        self.app.push_screen(PromptScreen("Từ chối thao tác — ghi chú cho agent (tuỳ chọn), Enter để gửi", ""), done)

    async def action_false_positive(self) -> None:
        await self._decide("approve", "[false_positive] người dùng báo phân loại nhầm (NFR-31)")

    def action_steer(self) -> None:
        r = self.selected()
        if r is None:
            return
        if not r["current_run_id"]:
            self.notify(f"{r.id} không có run đang chạy để steer", severity="warning")
            return
        rid = r["current_run_id"]

        def done(text: str | None) -> None:
            if text:
                self.run_worker(self.app.steer(rid, text))  # type: ignore[attr-defined]

        self.app.push_screen(PromptScreen(f"Steer run {rid} ({r.id}) — tin gửi vào phiên đang chạy", ""), done)

    def _stop_target(self) -> str | None:
        r = self.selected()
        if r is None:
            return None
        rid = r["current_run_id"] or r["waiting_human"]
        if not rid:
            self.notify(f"{r.id} không có run đang chạy", severity="warning")
        return rid

    def action_stop(self) -> None:
        rid = self._stop_target()
        if not rid:
            return

        def done(ok: bool | None) -> None:
            if ok:
                self.run_worker(self.app.stop_run(rid))  # type: ignore[attr-defined]

        self.app.push_screen(ConfirmScreen(f"Dừng run [b]{rid}[/b]?\nRun sẽ `cancelled`, session giữ để resume.", yes="Dừng (y)"), done)

    async def action_stop_now(self) -> None:
        rid = self._stop_target()
        if rid:
            await self.app.stop_run(rid)  # type: ignore[attr-defined]

    async def action_takeover(self) -> None:
        r = self.selected()
        if r is not None:
            await self.app.takeover(r.id)  # type: ignore[attr-defined]

    # -- H-16: f feedback (FR-UI-01) → feedback.record (Signal good/correction, audit channel=tui)
    def feedback_target(self) -> tuple[str, str] | None:
        r = self.selected()
        if r is None:
            return None
        rid = r["current_run_id"] or r["last_run_id"]
        return (r.id, rid) if rid else None

    def action_feedback(self) -> None:
        tgt = self.feedback_target()
        if tgt is None:
            self.notify("Agent này chưa có run nào để feedback", severity="warning")
            return
        agent_id, run_id = tgt

        def done(text: str | None) -> None:
            if text:
                self.run_worker(self.app.feedback(run_id, text))  # type: ignore[attr-defined]

        self.app.push_screen(PromptScreen(f"Feedback run {run_id} ({agent_id}): good | wrong | partial [ghi chú]", ""), done)

    def action_new_agent(self) -> None:
        self.run_worker(self._agent_form(None))

    def action_edit_agent(self) -> None:
        r = self.selected()
        if r is not None:
            self.run_worker(self._agent_form(r.id))

    async def _agent_form(self, agent_id: str | None) -> None:
        app = self.app
        try:
            templates = await app.client.call("template.list")  # type: ignore[attr-defined]
            mcp = [m["name"] for m in await app.client.call("mcp.list") or []]  # type: ignore[attr-defined]
        except Exception:
            templates, mcp = [], []
        agent = None
        if agent_id:
            try:
                agent = await app.client.call("agent.get", {"id": agent_id})  # type: ignore[attr-defined]
            except Exception as e:
                self.notify(_err(e), severity="error")
                return

        async def save(data: dict[str, Any]) -> str | None:
            try:
                if agent_id:
                    await app.client.call("agent.update", {"id": agent_id, "changes": data["changes"]}, audit=True)  # type: ignore[attr-defined]
                    self.notify(f"Đã sửa agent {agent_id}")
                elif data.get("template"):
                    await app.client.call(  # type: ignore[attr-defined]
                        "agent.create_from_template", {"id": data["id"], "template": data["template"], "overrides": data["changes"]}, audit=True
                    )
                    self.notify(f"Đã tạo agent {data['id']} từ template {data['template']}")
                else:
                    await app.client.call("agent.create", {"config": {"id": data["id"], **data["changes"]}}, audit=True)  # type: ignore[attr-defined]
                    self.notify(f"Đã tạo agent {data['id']}")
            except Exception as e:
                return _err(e)
            await app.refresh_snapshot()  # type: ignore[attr-defined]
            return None

        app.push_screen(AgentFormScreen(agent, templates, mcp, save))

    def action_delete_agent(self) -> None:
        r = self.selected()
        if r is None:
            return

        def done(ok: bool | None) -> None:
            if ok:
                self.run_worker(self.app.call_and_refresh("agent.delete", {"id": r.id}, f"Đã xoá mềm agent {r.id} (khôi phục: agent-hub agent restore {r.id})"))  # type: ignore[attr-defined]

        self.app.push_screen(ConfirmScreen(f"Xoá mềm agent [b]{r['name']}[/b] ({r.id})?\nChuyển vào thùng rác, khôi phục được.", yes="Xoá (y)"), done)

    def action_cycle_team(self) -> None:
        opts: list[str | None] = [None, *self.model.teams()]
        idx = opts.index(self.model.team_filter) if self.model.team_filter in opts else 0
        self.model.team_filter = opts[(idx + 1) % len(opts)]
        self.render_all()

    def action_cycle_sort(self) -> None:
        i = SORT_KEYS.index(self.model.sort_key)
        self.model.sort_key = SORT_KEYS[(i + 1) % len(SORT_KEYS)]
        self.render_all()

    def action_search(self) -> None:
        inp = self.query_one("#filter", Input)
        inp.display = True
        inp.value = self.model.text_filter
        inp.focus()

    @on(Input.Submitted, "#filter")
    def _filter_submitted(self, event: Input.Submitted) -> None:
        self.model.text_filter = event.value.strip()
        event.input.display = False
        self.query_one("#agents").focus()
        self.render_all()

    def key_escape(self) -> None:
        inp = self.query_one("#filter", Input)
        if inp.display:
            inp.display = False
            self.query_one("#agents").focus()
        elif self.model.text_filter or self.model.team_filter:
            self.model.text_filter, self.model.team_filter = "", None
            self.render_all()

    def action_tasks(self) -> None:
        self.app.push_screen(TasksScreen())

    def action_learnings(self) -> None:
        self.app.push_screen(LearningsScreen())

    def action_estop(self) -> None:
        on_now = bool((self.model.header.get("estop") or {}).get("on"))

        def done(ok: bool | None) -> None:
            if ok:
                params = {"on": not on_now, "reason": "bật từ TUI" if not on_now else None, "stop_running": not on_now}
                self.run_worker(self.app.call_and_refresh("estop.set", params, "ESTOP " + ("TẮT" if on_now else "BẬT")))  # type: ignore[attr-defined]

        msg = "TẮT ESTOP — cho phép khởi chạy run mới?" if on_now else "BẬT ESTOP — không khởi chạy run mới và DỪNG mọi run đang chạy?"
        self.app.push_screen(ConfirmScreen(msg, yes="Đồng ý (y)"), done)


# ============================================================================ transcript
class Transcript(VerticalScroll):
    """Luồng event của MỘT run: chữ stream, suy nghĩ thu gọn, dòng tool `┊ tên (args) (2.1s)`, kết quả rút gọn,
    diff inline cho Edit/Write, approval, steer…"""

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self._cur_kind: str | None = None
        self._cur_widget: Static | None = None
        self._cur_buf = ""
        self._tools: dict[str, tuple[Static, str, str]] = {}
        self._reasoning: list[tuple[Static, list[str], list[int]]] = []
        self.expand_reasoning = False
        self.count = 0

    def _add(self, renderable: Any, classes: str = "") -> Static:
        w = Static(renderable, classes=classes)
        self.mount(w)
        self.count += 1
        if self.count > MAX_TRANSCRIPT_WIDGETS:
            kids = list(self.children)
            for k in kids[: len(kids) - MAX_TRANSCRIPT_WIDGETS]:
                k.remove()
            self.count = MAX_TRANSCRIPT_WIDGETS
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
            t.append(f" · {len(text)} ký tự · " if text else f" · ~{est} token · ", style="grey42")
            t.append((snippet + "…") if snippet else "", style="italic grey42")
        return t

    def toggle_reasoning(self) -> None:
        self.expand_reasoning = not self.expand_reasoning
        for w, parts, est in self._reasoning:
            w.update(self._render_reasoning("".join(parts), est[0]))

    def note(self, text: str, style: str = "grey50") -> None:
        self._end_block()
        self._add(Text(text, style=style), "note")

    def handle(self, ev: dict[str, Any]) -> None:
        p = ev.get("payload") or {}
        t = ev.get("type", "")
        if ev.get("subagent_id") and t in ("message.delta", "reasoning.delta", "message.complete"):
            return  # chữ của subagent: chỉ hiện dòng tool của nó
        if t == "message.delta":
            if self._cur_kind != "text":
                self._end_block()
                self._cur_kind = "text"
                self._cur_widget = self._add("", "msg")
            self._cur_buf += p.get("text", "")
            assert self._cur_widget is not None
            self._cur_widget.update(Markdown(self._cur_buf))
            return
        if t == "message.complete":
            text = p.get("text") or ""
            if self._cur_kind == "text" and self._cur_widget is not None:
                self._cur_widget.update(Markdown(text))  # bản chuẩn thay phần delta đã stream
                self._end_block()
            else:
                self._end_block()
                self._add(Markdown(text), "msg")
            return
        if t in ("reasoning.delta", "reasoning.progress"):
            if self._cur_kind != "reasoning":
                self._end_block()
                self._cur_kind = "reasoning"
                w = self._add("", "reasoning")
                self._cur_widget = w
                self._reasoning.append((w, [], [0]))
            w, parts, est = self._reasoning[-1]
            parts.append(p.get("text") or "")
            est[0] = int(p.get("estimated_tokens") or est[0] or 0)
            w.update(self._render_reasoning("".join(parts), est[0]))
            return
        if t in ("usage.tick", "agent.status", "status.update", "hook.event", "tool.generating", "control.response", "raw.unknown", "task.background", "guard.decision"):
            return
        self._end_block()
        if t == "run.queued":
            budget = p.get("max_budget_usd")
            self._add(Text(f"… xếp hàng · {p.get('kind')} · ngân sách {'$' + str(budget) if budget else '—'}" + (" · resume" if p.get("resume") else ""), style="grey50"), "note")
            if p.get("prompt_preview"):
                line = Text("› Prompt: ", style="bold #7aa2f7")
                line.append(p["prompt_preview"], style="#7aa2f7")
                self._add(line, "user")
        elif t == "run.started":
            line = Text(f"── Bắt đầu · {p.get('model')} · CLI {p.get('claude_code_version') or '?'} · {p.get('permission_mode')} ", style="bold #7aa2f7")
            mcp = [m.get("name") for m in p.get("mcp_servers") or [] if isinstance(m, dict)]
            if mcp:
                line.append(f"\n   MCP: {', '.join(str(x) for x in mcp)}", style="grey50")
            self._add(line, "turn")
        elif t == "turn.start":
            return
        elif t == "tool.start":
            name, summ = p.get("name", "?"), p.get("args_preview") or ""
            w = self._add(self._tool_line(name, summ, None, running=True, sub=bool(ev.get("subagent_id"))), "tool")
            self._tools[p.get("tool_id") or ""] = (w, name, summ)
            diff = self._diff(name, p.get("input") or {})
            if diff is not None:
                self._add(diff, "tool-result")
        elif t == "tool.complete":
            tid = p.get("tool_id") or ""
            w, name, summ = self._tools.pop(tid, (None, p.get("name", "?"), ""))
            line = self._tool_line(name, summ, p.get("duration_s"), running=False, error=bool(p.get("is_error")), sub=bool(ev.get("subagent_id")))
            if w is None:
                self._add(line, "tool")
            else:
                w.update(line)
            prev = (p.get("result_preview") or "").strip()
            if prev:
                res = Text("┊    ↳ ", style="grey42")
                first = prev.splitlines()
                res.append(" ⏎ ".join(first[:3])[:200] + (" …" if len(first) > 3 or len(prev) > 200 else ""), style="red" if p.get("is_error") else "grey62")
                self._add(res, "tool-result")
        elif t == "subagent.spawn":
            self._add(Text(f"┊ ⤷ subagent {p.get('type')}: {p.get('description')}", style="magenta"), "tool")
        elif t in ("approval.open", "approval.waiting_human"):
            body = Text()
            body.append(" ⏸ CHỜ NGƯỜI DUYỆT " if t == "approval.waiting_human" else " ◐ CHỜ DUYỆT ", style="bold black on yellow")
            body.append(f" {p.get('tool') or ''}: ", style="bold yellow")
            body.append(f"{p.get('preview')}\n", style="yellow")
            if p.get("matched_rule") or p.get("reason"):
                body.append(f"   luật {p.get('matched_rule') or '?'} · {p.get('reason') or ''}   ", style="grey70")
            body.append("[a] cho phép  [d] từ chối", style="bold")
            self._add(body, "request")
        elif t == "approval.resolved":
            dec = p.get("decision")
            label, style = {"allowed": ("✓ Đã cho phép", "green"), "denied": ("✖ Đã từ chối", "red")}.get(dec, (f"■ {dec}", "grey50"))
            self._add(Text(f"   {label} bởi {p.get('by')} qua {p.get('channel')}" + (f" — {p['note']}" if p.get("note") else ""), style=style), "note")
        elif t == "user.message":
            line = Text(f"› Steer ({p.get('by')} qua {p.get('channel')}): ", style="bold cyan")
            line.append(p.get("text", ""), style="cyan")
            self._add(line, "user")
        elif t == "steer.ack":
            self._add(Text("  ✓ steer.ack — agent đã nhận tin vào ngữ cảnh", style="cyan"), "note")
        elif t in ("run.completed", "run.failed", "run.cancelled", "run.interrupted", "run.waiting_human"):
            cost = float(p.get("cost_usd") or 0)
            dur = (p.get("duration_ms") or 0) / 1000
            oc = p.get("outcome") or t.split(".")[1]
            style = {"completed": "green", "cancelled": "grey62", "waiting_human": "bold red"}.get(oc, "bold red")
            txt = Text(f"■ Run {oc} · {fmt_dur(dur) if dur else '—'} · {p.get('num_turns') or '?'} lượt · ${cost:.4f}", style=style)
            if p.get("error") and oc != "completed":
                txt.append(f"\n   {str(p['error'])[:300]}", style="red")
            self._add(txt, "turn-end")
        elif t in ("error", "isolation.violation", "permission.denied", "guard.blocked", "auth.blocked", "budget.exceeded"):
            self._add(Text(f"✖ {t}: {str(p.get('message') or p.get('detail') or p.get('name') or p.get('tool_name') or p)[:300]}", style="bold red"), "error")
        elif t == "run.takeover":
            self._add(Text("⇄ người tiếp quản bằng Claude Code", style="magenta"), "note")

    @staticmethod
    def _diff(name: str, inp: dict[str, Any]) -> Text | None:
        if name in ("Edit", "MultiEdit") and isinstance(inp, dict):
            edits = inp.get("edits") or [{"old_string": inp.get("old_string"), "new_string": inp.get("new_string")}]
            t = Text()
            for e in edits[:3]:
                for ln in str(e.get("old_string") or "").splitlines()[:8]:
                    t.append(f"┊    - {ln}\n", style="red")
                for ln in str(e.get("new_string") or "").splitlines()[:8]:
                    t.append(f"┊    + {ln}\n", style="green")
            return t if t.plain else None
        if name == "Write" and isinstance(inp, dict) and inp.get("content"):
            lines = str(inp["content"]).splitlines()
            t = Text()
            for ln in lines[:8]:
                t.append(f"┊    + {ln}\n", style="green")
            if len(lines) > 8:
                t.append(f"┊    … (+{len(lines) - 8} dòng)\n", style="grey50")
            return t
        return None

    @staticmethod
    def _tool_line(name: str, summ: str, dur: float | None, *, running: bool, error: bool = False, sub: bool = False) -> Text:
        t = Text("┊ " + ("  ⤷ " if sub else ""), style="grey42")
        t.append(short_tool(name), style="bold red" if error else "bold")
        if summ:
            t.append(f" ({summ})", style="grey70")
        if running:
            t.append("  đang chạy…", style="cyan")
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
        Binding("T", "takeover", "Takeover", show=False),
        Binding("end", "scroll_end", "Cuối", show=False),
    ]

    def __init__(self, agent_id: str, run_id: str) -> None:
        super().__init__()
        self.agent_id = agent_id
        self.run_id = run_id
        self.seen: set[tuple[str, int]] = set()
        self.run: dict[str, Any] = {}
        self.sub = None
        self.tokens = 0
        self.cost = 0.0
        self.model_name: str | None = None
        self.last_seq = 0
        self.replayed = 0
        self.live_events = 0
        self._bars_at = 0.0

    @property
    def model(self) -> TopModel:
        return self.app.model  # type: ignore[attr-defined]

    def compose(self) -> ComposeResult:
        yield Static(id="attach-head")
        yield Transcript(id="transcript")
        yield Static(id="banner")
        yield Input(placeholder="Gửi chỉ đạo (steer) cho agent đang chạy — Enter gửi, Esc huỷ…", id="steer")
        yield Static(id="statusbar")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#steer").display = False
        self.query_one("#banner").display = False
        self.run_worker(self._stream(), exclusive=True, group="attach")
        self.set_interval(1.0, self.refresh_bars)
        self.refresh_bars()

    async def _stream(self) -> None:
        app = self.app
        tr = self.query_one(Transcript)
        client: AsyncHubClient = app.client  # type: ignore[attr-defined]
        try:
            run = await client.call("run.get", {"id": self.run_id, "events": True})
        except Exception as e:
            tr.note(f"✖ không nạp được run {self.run_id}: {_err(e)}", "bold red")
            return
        self.run = run
        self.run_id = run["id"]
        events = run.pop("events", []) or []
        status = await client.call("hub.status")
        epoch = status["epoch"]
        since = 0
        for ev in events:
            self._show(ev, tr)
            if ev.get("epoch") == epoch:
                since = max(since, int(ev.get("seq") or 0))
        self.replayed = len(events)
        tr.note(
            f"↺ Attach run {self.run_id}: phát lại {len(events)} event từ DB (epoch {epoch}); tiếp theo nối live từ seq {since}",
            "italic grey50",
        )
        tr.scroll_end(animate=False)
        self.refresh_bars()
        # live: ring buffer bù phần sau `since` (gồm delta) rồi tiếp tục; tự đăng ký lại nếu bị ngắt
        while True:
            self.sub = subscribe(client, {"run_id": self.run_id, "since_seq": since, "epoch": epoch, "level": "detail"})
            reason = "?"
            try:
                async for msg in self.sub:
                    if "subscribed" in msg:
                        if msg["subscribed"].get("truncated"):
                            tr.note("⚠ ring buffer đã bị cắt — phần thiếu đã nạp từ DB", "yellow")
                        continue
                    if "end" in msg:
                        reason = msg["end"].get("reason", "?")
                        break
                    self.live_events += 1
                    self._show(msg, tr)
                    if msg.get("type") in ("run.completed", "run.failed", "run.cancelled", "run.interrupted", "run.waiting_human") and msg.get("source") == "hub":
                        self.run["status"] = (msg.get("payload") or {}).get("outcome") or msg["type"].split(".")[1]
                    now = time.monotonic()
                    if now - self._bars_at > 0.25:
                        self._bars_at = now
                        self.refresh_bars()
                    if msg.get("epoch") == epoch:
                        since = max(since, int(msg.get("seq") or 0))
                    tr.scroll_end(animate=False)
            except (DaemonUnavailable, ConnectionError, OSError) as e:
                reason = f"mất kết nối: {e}"
            if reason in ("closed", "client_closed") or not self.is_mounted:
                return
            tr.note(f"↻ luồng bị ngắt ({reason}) — đăng ký lại từ seq {since}", "yellow")
            await asyncio.sleep(0.5)
            try:
                st = await client.call("hub.status")
                if st["epoch"] != epoch:
                    epoch, since = st["epoch"], 0
            except Exception:
                await asyncio.sleep(2)

    def _show(self, ev: dict[str, Any], tr: Transcript) -> None:
        key = (str(ev.get("epoch")), int(ev.get("seq") or 0))
        if key in self.seen:
            return
        self.seen.add(key)
        self.last_seq = max(self.last_seq, key[1])
        p = ev.get("payload") or {}
        if ev.get("type") == "usage.tick":
            self.tokens = int(p.get("total_tokens") or self.tokens)
            self.cost = float(p.get("cost_usd") or self.cost)
        elif ev.get("type") == "run.started":
            self.model_name = p.get("model")
        tr.handle(ev)
        self.app.note_event(ev)  # type: ignore[attr-defined]

    def on_unmount(self) -> None:
        if self.sub is not None:
            self.sub.close()  # chỉ đóng luồng xem; run vẫn chạy (FR-UI-02)

    def refresh_bars(self) -> None:
        r = self.model.rows.get(self.agent_id)
        head = self.query_one("#attach-head", Static)
        bar = self.query_one("#statusbar", Static)
        banner = self.query_one("#banner", Static)
        h = Text()
        h.append(f" {self.agent_id} ", style="bold reverse #7aa2f7")
        h.append(f" run {self.run_id} ", style="bold")
        live_run = r is not None and r["current_run_id"] == self.run_id and self.run.get("status") in ("running", "queued", None)
        st = r["status"] if live_run else (self.run.get("status") or "?")
        if st in STATUS_STYLE:
            h.append_text(status_text(st))
        else:
            h.append(st, style="grey70")
        h.append(f"   {(self.run.get('prompt') or '').strip().splitlines()[0][:90] if (self.run.get('prompt') or '').strip() else ''}", style="grey70")
        head.update(h)
        pend = [a for a in self.model.approvals if a.get("run_id") == self.run_id]
        if pend:
            a = pend[0]
            b = Text()
            b.append(" ⏸ CHỜ NGƯỜI DUYỆT " if a["status"] == "waiting_human" else " ◐ CHỜ DUYỆT ", style="bold black on yellow")
            b.append(f" {a.get('preview')}  ", style="bold yellow")
            b.append(f"luật {a.get('matched_rule') or '?'} · chờ {fmt_dur(a.get('wait_s'))}  [a] cho phép · [d] từ chối", style="bold")
            banner.update(b)
            banner.display = True
        else:
            banner.display = False
        s = Text()
        s.append(f" {self.model_name or self.run.get('model') or '?'} ", style="bold")
        ctx = min(100.0, self.tokens / 2000.0)
        s.append(f"· ctx≈{ctx:.0f}% ", style="grey62")
        t0 = parse_ts(self.run.get("started_at"))
        el = (time.time() - t0) if (t0 and (r is not None and r["current_run_id"] == self.run_id)) else (self.run.get("duration_ms") or 0) / 1000
        if el:
            s.append(f"· {self.tokens / el:.0f} tok/s ", style="cyan")
        s.append(f"· {fmt_tokens(self.tokens)} tok ", style="grey70")
        s.append(f"· {fmt_cost(self.cost or self.run.get('cost_usd'))} ", style="bold magenta")
        s.append(f"· {fmt_dur(el)} ", style="grey70")
        s.append(f"· seq {self.last_seq} · phiên {(self.run.get('session_id') or '—')[:8]} · replay {self.replayed} + live {self.live_events}", style="grey42")
        bar.update(s)

    def action_back(self) -> None:
        inp = self.query_one("#steer", Input)
        if inp.display:
            inp.display = False
            return
        self.app.pop_screen()

    async def action_approve(self) -> None:
        pend = sorted((a for a in self.model.approvals if a.get("run_id") == self.run_id), key=lambda a: a.get("opened_at") or "")
        if not pend:
            self.notify("Run này không có thao tác chờ duyệt", severity="warning")
            return
        await self.app.decide(pend[0], "approve", None)  # type: ignore[attr-defined]

    def action_deny(self) -> None:
        pend = sorted((a for a in self.model.approvals if a.get("run_id") == self.run_id), key=lambda a: a.get("opened_at") or "")
        if not pend:
            self.notify("Run này không có thao tác chờ duyệt", severity="warning")
            return

        def done(note: str | None) -> None:
            if note is not None:
                self.run_worker(self.app.decide(pend[0], "deny", note or None))  # type: ignore[attr-defined]

        self.app.push_screen(PromptScreen("Từ chối — ghi chú cho agent (tuỳ chọn)", ""), done)

    def action_stop(self) -> None:
        def done(ok: bool | None) -> None:
            if ok:
                self.run_worker(self.app.stop_run(self.run_id))  # type: ignore[attr-defined]

        self.app.push_screen(ConfirmScreen(f"Dừng run [b]{self.run_id}[/b]?", yes="Dừng (y)"), done)

    def action_steer(self) -> None:
        inp = self.query_one("#steer", Input)
        inp.display = True
        inp.focus()

    async def action_takeover(self) -> None:
        await self.app.takeover(self.run_id)  # type: ignore[attr-defined]

    def action_toggle_reasoning(self) -> None:
        self.query_one(Transcript).toggle_reasoning()

    def action_scroll_end(self) -> None:
        self.query_one(Transcript).scroll_end(animate=False)

    @on(Input.Submitted, "#steer")
    async def _steer_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        event.input.display = False
        if text:
            await self.app.steer(self.run_id, text)  # type: ignore[attr-defined]


# ============================================================================ learnings (M4) / tasks (M3)
# Tab Learnings (FR-UI-06) + màn golden task: M4-B — tui/learnings.py (import cuối file, tránh vòng import)


TASK_STATUSES = ["triage", "todo", "ready", "running", "review", "blocked", "done"]
TASK_STYLE = {"running": "bold green", "ready": "cyan", "review": "magenta", "blocked": "bold red", "done": "grey50", "triage": "yellow", "todo": "white"}


class TasksScreen(Screen):
    """Tab Tasks (FR-UI-07) dựa trên RPC `task.list|get` của M3 (docs/dev-notes/M3.md)."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Về bảng"),
        Binding("enter", "attach", "Attach run"),
        Binding("c", "create_task", "Tạo task"),
        Binding("f", "toggle_done", "Ẩn/hiện done"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.tasks: list[dict[str, Any]] = []
        self.available: bool | None = None
        self.hide_done = True

    def compose(self) -> ComposeResult:
        yield Static(id="tasks-head")
        yield DataTable(id="tasks", cursor_type="row", zebra_stripes=True)
        yield Static(id="task-detail")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#tasks", DataTable)
        for label, key, w in [("ID", "id", 12), ("STATUS", "status", 9), ("P", "pri", 2), ("TEAM", "team", 10), ("ASSIGNEE", "who", 12), ("TITLE", "title", 40), ("RUN", "run", 14), ("$", "cost", 7), ("CHỜ", "wait", 14)]:
            table.add_column(label, key=key, width=w)
        table.focus()
        self.run_worker(self.reload(), exit_on_error=False)
        self.set_interval(2.0, lambda: self.run_worker(self.reload(), exclusive=True, group="tasks", exit_on_error=False))

    async def reload(self) -> None:
        try:
            await self._reload()
        except NoMatches:  # màn đã đóng giữa chừng
            pass

    async def _reload(self) -> None:
        client: AsyncHubClient = self.app.client  # type: ignore[attr-defined]
        head = self.query_one("#tasks-head", Static)
        try:
            self.tasks = await client.call("task.list", {"limit": 500}) or []
            self.available = True
        except RpcError as e:
            if e.code == -32601:
                self.available = False
                head.update(Text(" Tasks — daemon chưa có RPC task.* (M3). Tab sẽ hoạt động khi M3 được cài.", style="bold yellow"))
                return
            head.update(Text(f" Tasks — lỗi: {e.message}", style="bold red"))
            return
        except Exception as e:
            head.update(Text(f" Tasks — lỗi: {e}", style="bold red"))
            return
        counts: dict[str, int] = {}
        for t in self.tasks:
            counts[t["status"]] = counts.get(t["status"], 0) + 1
        h = Text()
        h.append(" Tasks ", style="bold reverse #bb9af7")
        h.append("  ")
        for st in TASK_STATUSES:
            h.append(f" {st} {counts.get(st, 0)} ", style=TASK_STYLE.get(st, ""))
            h.append("│", style="grey42")
        h.append(f"   {'(ẩn done — f để hiện)' if self.hide_done else ''}", style="grey50")
        head.update(h)
        table = self.query_one("#tasks", DataTable)
        prev = self._selected_id()
        table.clear()
        order = {s: i for i, s in enumerate(["running", "review", "ready", "blocked", "todo", "triage", "done", "archived"])}
        rows = sorted((t for t in self.tasks if not (self.hide_done and t["status"] in ("done", "archived"))), key=lambda t: (order.get(t["status"], 9), -int(t.get("priority") or 0), t.get("created_at") or ""))
        for t in rows:
            table.add_row(
                Text(t["id"][:12], style="grey62"),
                Text(t["status"], style=TASK_STYLE.get(t["status"], "")),
                str(t.get("priority") or 0),
                t.get("team_id") or "—",
                t.get("assignee_agent_id") or (f"@{t['role']}" if t.get("role") else "—"),
                (("  " if t.get("origin_task_id") else "") + (t.get("title") or ""))[:60],
                t.get("current_run_id") or "—",
                Text(fmt_cost(t.get("cost_usd")), justify="right"),
                Text(t.get("wait_reason") or t.get("block_reason") or "", style="yellow"),
                key=t["id"],
            )
        ids = [t["id"] for t in rows]
        if prev in ids:
            table.move_cursor(row=ids.index(prev), animate=False)
        await self.show_detail()

    def _selected_id(self) -> str | None:
        table = self.query_one("#tasks", DataTable)
        if table.row_count == 0:
            return None
        try:
            return table.coordinate_to_cell_key(table.cursor_coordinate)[0].value
        except Exception:
            return None

    @on(DataTable.RowHighlighted)
    async def _hl(self) -> None:
        try:
            await self.show_detail()
        except NoMatches:
            pass

    async def show_detail(self) -> None:
        tid = self._selected_id()
        w = self.query_one("#task-detail", Static)
        if not tid:
            w.update(Text(" (chưa có task)", style="grey50"))
            return
        try:
            t = await self.app.client.call("task.get", {"id": tid})  # type: ignore[attr-defined]
        except Exception as e:
            w.update(Text(f" {_err(e)}", style="red"))
            return
        x = Text()
        x.append(f" {t.get('title')}\n", style="bold")
        x.append(f" {t['id']} · {t['status']} · team {t.get('team_id') or '—'} · {t.get('assignee_agent_id') or t.get('role') or '—'} · lần thử {t.get('attempts')}\n", style="grey62")
        for label, key in (("cha", "parents"), ("con", "children")):
            items = t.get(key) or []
            if items:
                x.append(f" {label}: ", style="grey70")
                x.append(", ".join(f"{i['id'][:10]}({i['status']})" for i in items) + "\n", style="grey70")
        for c in (t.get("comments") or [])[-3:]:
            x.append(f" 💬 {c.get('author')}: {str(c.get('text') or '')[:100]}\n", style="grey62")
        runs = t.get("runs") or []
        if runs:
            x.append(" runs: " + ", ".join(f"{r['id']}({r['status']})" for r in runs[-4:]), style="grey62")
        w.update(x)

    def action_create_task(self) -> None:
        """Tạo task nhanh: `<agent|team:ID>: <tiêu đề>` → `task.create` (M3)."""

        def done(text: str | None) -> None:
            if not text or ":" not in text:
                if text:
                    self.notify("Cú pháp: <agent>: <tiêu đề>  hoặc  team:<id>: <tiêu đề>", severity="warning")
                return
            if text.startswith("team:"):
                _, team, title = text.split(":", 2)
                params = {"team": team.strip(), "title": title.strip()}
            else:
                who, title = text.split(":", 1)
                params = {"assignee": who.strip(), "title": title.strip()}
            self.run_worker(self._create(params))

        self.app.push_screen(PromptScreen("Task mới — `<agent>: <tiêu đề>` hoặc `team:<id>: <tiêu đề>`", ""), done)

    async def _create(self, params: dict[str, Any]) -> None:
        try:
            t = await self.app.client.call("task.create", params, audit=True)  # type: ignore[attr-defined]
        except Exception as e:
            self.notify(_err(e), severity="error")
            return
        self.notify(f"Đã tạo task {t['id']}")
        await self.reload()

    def action_toggle_done(self) -> None:
        self.hide_done = not self.hide_done
        self.run_worker(self.reload())

    def action_attach(self) -> None:
        tid = self._selected_id()
        t = next((x for x in self.tasks if x["id"] == tid), None)
        if t and t.get("current_run_id") and t.get("assignee_agent_id"):
            self.app.push_screen(AttachScreen(t["assignee_agent_id"], t["current_run_id"]))
        else:
            self.notify("Task không có run đang chạy", severity="warning")


# ============================================================================ form / hộp thoại
class AgentFormScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape", "cancel", "Huỷ")]

    def __init__(self, agent: dict[str, Any] | None, templates: list[dict[str, Any]], mcp: list[str], save) -> None:
        super().__init__()
        self.agent = agent
        self.templates = templates
        self.mcp = mcp
        self.save = save

    def compose(self) -> ComposeResult:
        a = self.agent or {}
        tp = a.get("tool_policy") or {}
        lim = a.get("limits") or {}
        with Vertical(id="form"):
            yield Label(f"Sửa agent {a['id']}" if a else "Tạo agent mới", id="form-title")
            if not a:
                with Horizontal(classes="row"):
                    with Vertical():
                        yield Label("ID (slug)")
                        yield Input(placeholder="vd: reviewer-1", id="f-id")
                    with Vertical():
                        yield Label("Template (FR-AG-07)")
                        yield Select([(f"{t['name']} — {t['model']}/{t['permission_mode']}", t["name"]) for t in self.templates], prompt="(không dùng template)", id="f-template")
            yield Label("Tên hiển thị")
            yield Input(value=a.get("name") or "", placeholder="(mặc định = id)", id="f-name")
            yield Label("Vai trò (role, tiếng Anh)")
            yield Input(value=a.get("role") or "", placeholder="(template điền sẵn nếu bỏ trống)", id="f-role")
            with Horizontal(classes="row"):
                with Vertical():
                    yield Label("Model")
                    yield Select([(m, m) for m in MODELS], value=a.get("model") if a.get("model") in MODELS else Select.NULL, prompt="(theo template/mặc định)", id="f-model")
                with Vertical():
                    yield Label("Permission mode")
                    pm = tp.get("permission_mode")
                    yield Select([(m, m) for m in PERMISSION_MODES], value=pm if pm in PERMISSION_MODES else Select.NULL, prompt="(theo template/mặc định)", id="f-pm")
                with Vertical():
                    yield Label("Ngân sách $/run")
                    v = lim.get("max_budget_usd_per_run")
                    yield Input(value="" if v is None else str(v), placeholder="vd 0.3", id="f-budget")
            yield Label("MCP servers (Space để chọn)")
            chosen = set(a.get("mcp_servers") or [])
            yield SelectionList[str](*[(m, m, m in chosen) for m in self.mcp], id="f-mcp")
            yield Label("", id="f-error")
            with Horizontal(id="buttons"):
                yield Button("Lưu", variant="primary", id="save")
                yield Button("Huỷ", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#f-id" if not self.agent else "#f-name", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    def _error(self, msg: str) -> None:
        self.query_one("#f-error", Label).update(Text(msg, style="bold red"))

    @on(Button.Pressed, "#save")
    @on(Input.Submitted)
    async def _save(self) -> None:
        changes: dict[str, Any] = {}
        name = self.query_one("#f-name", Input).value.strip()
        role = self.query_one("#f-role", Input).value.strip()
        if name:
            changes["name"] = name
        if role:
            changes["role"] = role
        model = self.query_one("#f-model", Select).value
        if model not in (Select.NULL, None) and isinstance(model, str):
            changes["model"] = model
        pm = self.query_one("#f-pm", Select).value
        if pm not in (Select.NULL, None) and isinstance(pm, str):
            changes["tool_policy"] = {"permission_mode": pm}
        b = self.query_one("#f-budget", Input).value.strip()
        if b:
            try:
                changes["limits"] = {"max_budget_usd_per_run": float(b)}
            except ValueError:
                self._error("Ngân sách phải là số")
                return
        mcp = list(self.query_one("#f-mcp", SelectionList).selected)
        if self.mcp and (mcp or (self.agent or {}).get("mcp_servers")):
            changes["mcp_servers"] = mcp
        data: dict[str, Any] = {"changes": changes}
        if not self.agent:
            aid = self.query_one("#f-id", Input).value.strip()
            if not aid:
                self._error("ID không được để trống")
                return
            data["id"] = aid
            tpl = self.query_one("#f-template", Select).value
            data["template"] = tpl if isinstance(tpl, str) else None
            changes.setdefault("name", aid)
        elif not changes:
            self.dismiss(None)
            return
        err = await self.save(data)
        if err:
            self._error(err)
        else:
            self.dismiss(None)


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "no", "Huỷ"), Binding("y", "yes", "Đồng ý")]

    def __init__(self, message: str, *, yes: str = "Đồng ý (y)") -> None:
        super().__init__()
        self.message = message
        self.yes = yes

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm"):
            yield Label(self.message)
            with Horizontal(id="buttons"):
                yield Button(self.yes, variant="error", id="yes")
                yield Button("Huỷ (Esc)", id="no")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


class PromptScreen(ModalScreen[str | None]):
    """Ô nhập một dòng (steer, ghi chú deny). Esc → None; Enter → chuỗi (có thể rỗng)."""

    BINDINGS = [Binding("escape", "cancel", "Huỷ")]

    def __init__(self, title: str, value: str = "") -> None:
        super().__init__()
        self.title_text = title
        self.value = value

    def compose(self) -> ComposeResult:
        with Vertical(id="prompt"):
            yield Label(self.title_text, id="prompt-title")
            yield Input(value=self.value, id="prompt-input")

    def on_mount(self) -> None:
        self.query_one("#prompt-input", Input).focus()

    @on(Input.Submitted)
    def _submit(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip())

    def action_cancel(self) -> None:
        self.dismiss(None)


class InfoScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape", "close", "Đóng"), Binding("enter", "close", "Đóng")]

    def __init__(self, text: Text | str) -> None:
        super().__init__()
        self.text = text

    def compose(self) -> ComposeResult:
        with Vertical(id="info"):
            yield Static(self.text)
            yield Label(Text("Esc/Enter để đóng", style="grey50"))

    def action_close(self) -> None:
        self.dismiss(None)


# ============================================================================ app
CSS = """
Screen { background: $surface; }
#summary { height: 2; padding: 0 1; background: $panel; }
#filter { height: 3; margin: 0 1; }
#agents { height: auto; max-height: 55%; margin: 1 1 0 1; }
#detail { height: 4; padding: 0 1; margin-top: 1; border-top: solid $primary-darken-2; }
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

#learn-body { height: 1fr; padding: 1 2; }
#tasks-head { height: 1; padding: 0 1; background: $panel; }
#tasks { height: 1fr; margin: 1 1 0 1; }
#task-detail { height: 8; padding: 0 1; border-top: solid $primary-darken-2; }

AgentFormScreen, ConfirmScreen, PromptScreen, InfoScreen { align: center middle; background: $background 60%; }
#form { width: 90; height: auto; padding: 1 2; border: thick $accent; background: $panel; }
#form-title { text-style: bold; color: $accent; margin-bottom: 1; }
#form Label { margin-top: 1; color: $text-muted; }
#form .row { height: auto; }
#form .row > Vertical { width: 1fr; height: auto; }
#f-mcp { height: 6; }
#buttons { height: auto; margin-top: 1; align-horizontal: right; }
#buttons Button { margin-left: 1; }
#confirm { width: 64; height: auto; padding: 1 2; border: thick $error; background: $panel; }
#prompt { width: 90; height: auto; padding: 1 2; border: thick $accent; background: $panel; }
#prompt-title { margin-bottom: 1; }
#info { width: 100; height: auto; padding: 1 2; border: thick $accent; background: $panel; }
"""


class HelpScreen(ModalScreen[None]):
    """NFR-26: màn trợ giúp `?` — liệt kê MỌI phím của màn đang mở (kể cả phím ẩn khỏi footer) + phím toàn cục."""

    BINDINGS = [Binding("escape", "close", "Đóng"), Binding("question_mark", "close", "Đóng", show=False), Binding("q", "close", "Đóng", show=False)]

    def __init__(self, screen: Screen | None) -> None:
        super().__init__()
        self.target = screen

    @staticmethod
    def rows_for(screen: Any, app: Any) -> list[tuple[str, str, str]]:
        out: list[tuple[str, str, str]] = []
        seen: set[str] = set()
        for owner, binds in ((type(screen).__name__ if screen is not None else "", getattr(screen, "BINDINGS", []) or []), ("TopApp", getattr(app, "BINDINGS", []) or [])):
            for b in binds:
                if not isinstance(b, Binding) or b.key in seen:
                    continue
                seen.add(b.key)
                out.append((b.key, b.description or b.action, owner))
        return out

    def compose(self) -> ComposeResult:
        rows = self.rows_for(self.target, self.app)
        lines = [f"[b]Phím tắt — {type(self.target).__name__ if self.target is not None else ''}[/b]  (Esc để đóng)", ""]
        lines += [f"  [b]{k:<12}[/b] {d}" for k, d, _ in rows]
        with Vertical(id="confirm"):
            yield Label("\n".join(lines))

    def action_close(self) -> None:
        self.dismiss(None)


class TopApp(App):
    TITLE = "agent-hub"
    SUB_TITLE = "top"
    CSS = CSS
    BINDINGS = [Binding("question_mark", "help", "Trợ giúp (?)")]  # NFR-26

    def action_help(self) -> None:
        if isinstance(self.screen, HelpScreen):
            return
        self.push_screen(HelpScreen(self.screen))

    def __init__(
        self,
        sock_path: Path | str,
        *,
        attach: str | None = None,
        refresh_s: float = 1.0,
        takeover_exec: bool | None = None,
        firehose_backlog: int = 200,
    ) -> None:
        super().__init__()
        self.client = AsyncHubClient(sock_path, channel="tui")
        self.model = TopModel()
        self.refresh_s = refresh_s
        self.attach_ref = attach
        self.takeover_exec = sys.stdin.isatty() if takeover_exec is None else takeover_exec
        self.firehose_backlog = firehose_backlog
        self.firehose_sub = None
        self.render_times: list[float] = []
        self.connected = False
        self._snap_task: asyncio.Task | None = None
        self.last_error: str | None = None

    # -- vòng đời
    async def on_mount(self) -> None:
        self.htop = HtopScreen()
        await self.push_screen(self.htop)
        await self.refresh_snapshot()
        self.run_worker(self._firehose(), group="firehose")
        self.set_interval(self.refresh_s, self._snapshot_tick)
        if self.attach_ref:
            await self._open_attach(self.attach_ref)
        from .learnings import maybe_show_digest  # M4-B FR-LN-13: digest khi mở TUI lần đầu trong ngày

        self.run_worker(maybe_show_digest(self), exit_on_error=False)

    async def _open_attach(self, ref: str) -> None:
        try:
            run = await self.client.call("run.get", {"id": ref})
        except RpcError:
            r = self.model.rows.get(ref)
            rid = r and (r["current_run_id"] or r["waiting_human"] or r["last_run_id"])
            if not rid:
                self.notify(f"không tìm thấy run/agent '{ref}'", severity="error")
                return
            run = {"id": rid, "agent_id": ref}
        self.push_screen(AttachScreen(run["agent_id"], run["id"]))

    def _snapshot_tick(self) -> None:
        if self._snap_task is None or self._snap_task.done():
            self._snap_task = asyncio.ensure_future(self.refresh_snapshot())

    async def refresh_snapshot(self) -> None:
        t0 = time.perf_counter()
        try:
            snap = await self.client.call("tui.snapshot")
        except Exception as e:
            self.connected = False
            self.last_error = _err(e)
            try:
                self.htop.query_one("#summary", Static).update(Text(f" ✖ không kết nối được daemon: {self.last_error}", style="bold red"))
            except NoMatches:
                pass
            return
        self.connected = True
        self.model.snapshot_ms.append((time.perf_counter() - t0) * 1000)
        self.model.load(snap)
        if self.htop.is_mounted:
            try:
                self.htop.render_all()
            except NoMatches:  # đang thoát app
                pass

    async def _firehose(self) -> None:
        """Firehose mức summary (FR-UI-09). Bị ngắt (slow_consumer/daemon restart) → đăng ký lại từ seq cuối."""
        first = True
        while True:
            # lần đầu: nạp sẵn ~`firehose_backlog` event gần nhất từ ring buffer; các lần sau: nối từ seq cuối đã thấy
            since = max(0, self.model.last_seq - self.firehose_backlog) if first else self.model.last_seq
            self.model.measure_after = self.model.last_seq  # chỉ đo độ trễ cho event live, không đo phần replay
            first = False
            params: dict[str, Any] = {"level": "summary", "since_seq": since, "epoch": self.model.epoch}
            self.firehose_sub = subscribe(self.client, params)
            reason = "?"
            try:
                async for msg in self.firehose_sub:
                    if "subscribed" in msg:
                        continue
                    if "end" in msg:
                        reason = msg["end"].get("reason", "?")
                        break
                    self.on_hub_event(msg)
            except (DaemonUnavailable, ConnectionError, OSError) as e:
                reason = f"mất kết nối: {e}"
            except RpcError as e:
                reason = e.message
            if reason == "client_closed":
                return
            self.notify(f"firehose bị ngắt ({reason}) — đăng ký lại", severity="warning", timeout=3)
            await asyncio.sleep(1.0)
            await self.refresh_snapshot()

    def on_hub_event(self, ev: dict[str, Any]) -> None:
        if ev.get("type") == "learning.applied":  # H-16 (FR-LN-13): thông báo ngắn "X learned: …" khi tự áp dụng
            p = ev.get("payload") or {}
            msg = p.get("summary") or f"{ev.get('agent_id') or p.get('scope') or '?'} learned: {p.get('op') or ''} {p.get('target') or ''} {p.get('target_ref') or ''}".strip()
            self.notify(str(msg)[:200] + (f"\nrollback: {p['rollback_cmd']}" if p.get("rollback_cmd") else ""), title="learning", timeout=6)
        aid, need = self.model.apply(ev)
        try:
            self.htop.hub_event(ev, aid)
        except NoMatches:  # đang thoát app
            return
        if need:
            self._snapshot_tick()

    def note_event(self, ev: dict[str, Any]) -> None:
        """Màn attach nhận event (detail) — chỉ đo độ trễ, không đếm hai lần vào bảng."""

    def note_render(self) -> None:
        self.render_times.append(time.monotonic())
        if len(self.render_times) > 5000:
            del self.render_times[:2500]

    async def on_unmount(self) -> None:
        if self.firehose_sub is not None:
            self.firehose_sub.close()

    # -- thao tác dùng chung giữa các màn
    async def call_and_refresh(self, method: str, params: dict[str, Any], ok: str) -> bool:
        try:
            await self.client.call(method, params, audit=True)
        except Exception as e:
            self.notify(_err(e), severity="error")
            return False
        self.notify(ok)
        await self.refresh_snapshot()
        return True

    async def decide(self, approval: dict[str, Any], decision: str, note: str | None) -> None:
        method = "approval.approve" if decision == "approve" else "approval.deny"
        try:
            await self.client.call(method, {"id": approval["id"], "note": note, "channel": "tui", "by": "user"})
        except Exception as e:
            self.notify(_err(e), severity="error")  # vd viewer khác đã quyết trước (FR-UI-04)
            await self.refresh_snapshot()
            return
        self.notify(("Đã cho phép " if decision == "approve" else "Đã từ chối ") + f"{approval.get('preview') or approval['id']}", severity="information" if decision == "approve" else "warning")
        await self.refresh_snapshot()

    async def steer(self, run_id: str, text: str) -> bool:
        try:
            r = await self.client.call("run.steer", {"id": run_id, "text": text, "channel": "tui", "by": "user"}, audit=True)
        except Exception as e:
            self.notify(_err(e), severity="error", timeout=8)
            return False
        self.notify(f"Đã gửi steer vào {r['run_id']} (lần {r['steer_count']}) — chờ steer.ack")
        return True

    async def feedback(self, run_id: str, text: str) -> bool:
        kind, _, note = text.strip().partition(" ")
        kind = kind.lower()
        if kind not in ("good", "wrong", "partial"):
            kind, note = "wrong", text.strip()  # chỉ ghi chú → coi là sửa sai (correction)
        try:
            await self.client.call("feedback.record", {"run_id": run_id, "kind": kind, "note": note or None, "source": "tui"}, audit=True)
        except Exception as e:
            self.notify(_err(e), severity="error")
            return False
        self.notify(f"Đã ghi feedback '{kind}' cho run {run_id} — vòng học sẽ xem xét")
        return True

    async def stop_run(self, run_id: str) -> bool:
        return await self.call_and_refresh("run.stop", {"id": run_id}, f"Đã gửi lệnh dừng {run_id}")

    async def takeover(self, ref: str) -> None:
        try:
            info = await self.client.call("run.takeover", {"id": ref})
        except Exception as e:
            self.notify(_err(e), severity="error", timeout=8)
            return
        if not self.takeover_exec:
            t = Text()
            t.append("Takeover (FR-UI-10)\n\n", style="bold")
            t.append(f"Run {info['run_id']} · session {info['session_id']}\nChạy lệnh sau trong terminal:\n\n", style="grey70")
            t.append(info["command"], style="bold cyan")
            t.append("\n\nKhi xong: agent-hub takeover " + info["run_id"] + " --record", style="grey62")
            self.push_screen(InfoScreen(t))
            return
        env = {k: v for k, v in os.environ.items() if k not in set(info.get("env_strip") or [])}
        started = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"
        with self.suspend():
            print(f"\n[agent-hub] takeover run {info['run_id']} — {info['command']}\n", flush=True)
            try:
                rc = subprocess.call(info["argv"], cwd=info["cwd"] if info.get("cwd_exists") else None, env=env)
            except OSError as e:
                print(f"không chạy được claude: {e}")
                rc = 127
        try:
            await self.client.call("run.takeover_record", {"id": info["run_id"], "exit_code": rc, "started_at": started}, audit=True)
        except Exception as e:
            self.notify(_err(e), severity="error")
        await self.refresh_snapshot()


from .learnings import GoldenScreen, LearningsScreen  # noqa: E402  M4-B (FR-UI-06)


def run_top(sock_path: Path | str, *, attach: str | None = None) -> None:
    TopApp(sock_path, attach=attach).run()
