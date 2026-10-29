"""Màn xem agent kiểu Claude Code (transcript của subagent trong Claude Code CLI) — mở từ `cfharness chat` khi click agent đang chạy.

Cùng nguồn dữ liệu với `AttachScreen` (tui/app.py): nạp `run.get {events: true}` từ DB, rồi `events.subscribe {run_id, since_seq,
epoch, level: detail}` (ring buffer bù phần thiếu, kể cả delta) → live; khử trùng theo (epoch, seq); bị ngắt → đăng ký lại từ seq cuối.
Khác AttachScreen ở CÁCH VẼ: mọi mục qua `cc_render` (cùng hình với chat fullscreen):

- Đầu màn: khung bo cam — `<tên agent> · <run>` + chữ trạng thái màu; dòng mờ `N tool · token · thời gian · $` (cập nhật mỗi giây);
  dòng mờ đầu prompt. Bên phải: `[x dừng]` (chỉ khi đang chạy) và `[esc quay lại]` — click được.
- Thân: `VerticalScroll`, MỖI mục một `Static` chứa MỘT `Text` (kéo chọn được → copy): prompt `> …`, lời agent `● …` (delta cập nhật
  cùng mục, `message.complete` thay bản chuẩn), suy nghĩ `✻` thu gọn (click mở), tool `● Tên(args)` + `⎿` (đang chạy → cập nhật tại chỗ
  khi xong; click / Ctrl-O mở input JSON + kết quả đầy đủ), subagent: dòng `● Agent(mô tả)` rồi các mục của nó thụt vào dưới,
  duyệt: hộp viền tím (tool + preview + lý do + `a cho phép · d từ chối`), steer `> …`, kết thúc `⎿ Xong · 12s · 5 lượt · $0.012`.
- Phím: esc/← quay lại · x dừng (xác nhận) · a cho phép · d từ chối (ghi chú) · s chỉ đạo · ctrl+o mở rộng mọi tool · PgUp/PgDn.

Chữ của agent là DỮ LIỆU: `cc_render` đã qua `termsafe.clean/one_line` + `rich.text.Text` (không markup, không link).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.markup import escape
from textual.screen import Screen
from textual.widgets import Input, Static

from ..manager import attachments as A
from ..rpc import DaemonUnavailable, RpcError
from ..termsafe import clean, one_line
from . import cc_render as R
from .client import AsyncHubClient, subscribe
from .model import TopModel, parse_ts

MAX_ITEMS = 1500
TICK_S = 0.2
END_TYPES = ("run.completed", "run.failed", "run.cancelled", "run.interrupted", "run.waiting_human")
ENDED = {"completed", "failed", "cancelled", "interrupted", "waiting_human", "crashed", "blocked"}
AGENT_TOOLS = ("Task", "Agent")
NOISE = ("usage.tick", "agent.status", "status.update", "hook.event", "tool.generating", "control.response", "control.request", "tool.progress", "raw.unknown",
         "task.background", "guard.decision", "turn.start", "run.started")
ERROR_TYPES = ("error", "isolation.violation", "permission.denied", "guard.blocked", "auth.blocked", "budget.exceeded")
STATUS_WORD = {
    "running": ("đang chạy", R.ACCENT),
    "approval": ("chờ duyệt", R.PERMISSION),
    "completed": ("xong", R.OK),
    "failed": ("lỗi", R.ERR),
    "interrupted": ("lỗi", R.ERR),
    "crashed": ("lỗi", R.ERR),
    "blocked": ("xong", R.OK),
    "cancelled": ("đã dừng", R.WARN),
    "waiting_human": ("chờ duyệt", R.PERMISSION),
}

CSS = """
AgentViewScreen { layout: vertical; }
AgentViewScreen #av-head { height: auto; border: round #D97757; padding: 0 1; margin: 0 1; }
AgentViewScreen #av-head-left { width: 1fr; height: auto; }
AgentViewScreen #av-actions { width: auto; height: 1; }
AgentViewScreen .av-btn { width: auto; height: 1; margin-left: 2; color: $text-muted; }
AgentViewScreen .av-btn:hover { color: $text; background: $boost; }
AgentViewScreen #av-log { height: 1fr; padding: 0 2; scrollbar-size-vertical: 1; }
AgentViewScreen #av-log > .av-item { width: 100%; margin-top: 1; }
AgentViewScreen #av-log > .av-nested { margin-top: 0; }
AgentViewScreen #av-log > .av-click:hover { background: $boost; }
AgentViewScreen #av-log > .av-perm { border: round #B1B9F9; padding: 0 1; width: 100%; }
AgentViewScreen #av-log > .av-perm-done { border: round $panel-lighten-2; }
AgentViewScreen #av-spinner { height: 1; padding: 0 2; }
AgentViewScreen #av-steer { display: none; margin: 0 1; border: round $primary-darken-1; }
AgentViewScreen #av-steer:focus { border: round #D97757; }
AgentViewScreen #av-hint { height: 1; padding: 0 2; color: $text-muted; }
"""


def _err(e: Exception) -> str:
    return e.message if isinstance(e, RpcError) else str(e)


def _styled(s: str, style: str) -> Text:
    """Kiểu qua `append` (Rich `Style`): kiểu GỐC `Text(s, style="grey50")` bị Textual bỏ (nó tự parse chuỗi màu, không biết grey50)."""
    t = Text()
    t.append(s, style=style)
    return t


def _indent(t: Text, pad: str = "    ") -> Text:
    out = Text()
    for i, ln in enumerate(t.split("\n", allow_blank=True)):
        if i:
            out.append("\n")
        out.append(pad)
        out.append_text(ln)
    return out


class AvItem(Static):
    """Một mục transcript. `meta` = dữ liệu để vẽ lại (kind, name, status…)."""

    def __init__(self, meta: dict[str, Any], *, classes: str = "av-item") -> None:
        super().__init__("", classes=classes)
        self.meta = meta

    def on_click(self, _event: events.Click) -> None:
        if self.screen.get_selected_text():  # vừa kéo chọn chữ — không coi là click
            return
        self.screen.item_clicked(self)  # type: ignore[attr-defined]


class HeadButton(Static):
    def __init__(self, label: str, action: str, **kw: Any) -> None:
        super().__init__(_styled(label, R.SUBTLE), classes="av-btn", **kw)
        self.action = action

    async def on_click(self, _event: events.Click) -> None:
        await self.app.run_action(self.action, self.screen)


class AgentViewScreen(Screen):
    DEFAULT_CSS = CSS
    AUTO_FOCUS = ""  # ô chỉ đạo ẩn không được giữ focus (nuốt phím a/d/x/s)
    BINDINGS = [
        Binding("escape", "back", "Quay lại"),
        Binding("left", "back", "Quay lại", show=False),
        Binding("x", "stop", "Dừng"),
        Binding("a", "approve", "Cho phép"),
        Binding("d", "deny", "Từ chối"),
        Binding("s", "steer", "Chỉ đạo"),
        Binding("ctrl+o", "expand_all", "Mở rộng tool"),
        Binding("pageup", "page(-1)", "Cuộn lên", show=False),
        Binding("pagedown", "page(1)", "Cuộn xuống", show=False),
        Binding("end", "to_end", "Xuống cuối", show=False),
    ]

    def __init__(self, agent_id: str, run_id: str) -> None:
        super().__init__()
        self.agent_id = agent_id
        self.run_id = run_id
        self.run: dict[str, Any] = {}
        self.seen: set[tuple[str, int]] = set()
        self.sub = None
        self.last_seq = 0
        self.replayed = 0
        self.live_events = 0
        self.loaded = False
        self.tokens = 0
        self.cost = 0.0
        self.tool_uses = 0
        self.outcome: str | None = None
        self.end_ms: float | None = None
        self.t0: float | None = None
        self.activity = "Đang nghĩ"
        self.expand_all = False
        self.frame = 0
        self.tools: dict[str, AvItem] = {}
        self.groups: dict[str, AvItem] = {}  # subagent_id → mục cuối của nhóm (để chèn mục con ngay sau)
        self.headers: dict[str, AvItem] = {}  # subagent_id (= tool_id của Task/Agent) → dòng `● Agent(…)`
        self.perms: dict[str, AvItem] = {}
        self._cur: AvItem | None = None
        self._cur_kind: str | None = None
        self._cur_buf = ""
        self._prompt_shown = False

    # ------------------------------------------------------------------ dựng
    @property
    def model(self) -> TopModel:
        return self.app.model  # type: ignore[attr-defined]

    def compose(self) -> ComposeResult:
        with Horizontal(id="av-head"):
            with Vertical(id="av-head-left"):
                yield Static(id="av-title")
                yield Static(id="av-stats")
                yield Static(id="av-prompt")
            with Horizontal(id="av-actions"):
                yield HeadButton("[x dừng]", "stop", id="av-stop")
                yield HeadButton("[esc quay lại]", "back", id="av-back")
        yield VerticalScroll(id="av-log", can_focus=False)
        yield Static(id="av-spinner")
        yield Input(placeholder="Chỉ đạo agent đang chạy — Enter gửi, Esc huỷ…", id="av-steer")
        hint = Text()
        for i, (key, label) in enumerate((("esc", "quay lại"), ("ctrl+o", "mở rộng tool"), ("a", "cho phép"), ("d", "từ chối"),
                                          ("s", "chỉ đạo"), ("x", "dừng"), ("kéo chọn", "copy"))):
            hint.append(" · " if i else "", style=R.DIM)
            hint.append(key, style=R.SUBTLE)
            hint.append(f" {label}", style=R.DIM)
        yield Static(hint, id="av-hint")

    def on_mount(self) -> None:
        self.run_worker(self._stream(), exclusive=True, group="agent-view")
        self.set_interval(TICK_S, self._tick)
        self._render_head()

    def on_unmount(self) -> None:
        if self.sub is not None:
            self.sub.close()  # chỉ đóng luồng xem; run vẫn chạy

    # ------------------------------------------------------------------ dữ liệu: DB → live (như AttachScreen._stream)
    async def _stream(self) -> None:
        client: AsyncHubClient = self.app.client  # type: ignore[attr-defined]
        try:
            run = await client.call("run.get", {"id": self.run_id, "events": True})
        except Exception as e:
            self._add({"kind": "note", "text": f"không nạp được run {self.run_id}: {_err(e)}", "tone": "err"})
            return
        events_ = run.pop("events", []) or []
        self.run = run
        self.run_id = run["id"]
        self.agent_id = run.get("agent_id") or self.agent_id
        self.t0 = parse_ts(run.get("started_at"))
        if run.get("status") in ENDED:
            self.outcome = self.outcome or run["status"]
            self.end_ms = run.get("duration_ms")
            if not self.end_ms and self.t0 and parse_ts(run.get("ended_at")):
                self.end_ms = (parse_ts(run.get("ended_at")) - self.t0) * 1000  # type: ignore[operator]
            self.cost = float(run.get("cost_usd") or 0)
        status = await client.call("hub.status")
        epoch = status["epoch"]
        since = 0
        for ev in events_:
            self._show(ev)
            if ev.get("epoch") == epoch:
                since = max(since, int(ev.get("seq") or 0))
        self.replayed = len(events_)
        if not self._prompt_shown and run.get("prompt"):
            self._prompt(run["prompt"], first=True)
        self.loaded = True
        self._render_head()
        self._scroll_end()
        while True:
            self.sub = subscribe(client, {"run_id": self.run_id, "since_seq": since, "epoch": epoch, "level": "detail"})
            reason = "?"
            try:
                async for msg in self.sub:
                    if "subscribed" in msg:
                        if msg["subscribed"].get("truncated"):
                            self._add({"kind": "note", "text": "ring buffer đã bị cắt — phần thiếu đã nạp từ DB", "tone": "warn"})
                        continue
                    if "end" in msg:
                        reason = msg["end"].get("reason", "?")
                        break
                    self.live_events += 1
                    self._show(msg)
                    if msg.get("epoch") == epoch:
                        since = max(since, int(msg.get("seq") or 0))
            except (DaemonUnavailable, ConnectionError, OSError) as e:
                reason = f"mất kết nối: {e}"
            if reason in ("closed", "client_closed") or not self.is_mounted:
                return
            self._add({"kind": "note", "text": f"luồng bị ngắt ({reason}) — đăng ký lại từ seq {since}", "tone": "warn"})
            await asyncio.sleep(0.5)
            try:
                st = await client.call("hub.status")
                if st["epoch"] != epoch:
                    epoch, since = st["epoch"], 0
            except Exception:
                await asyncio.sleep(2)

    def _show(self, ev: dict[str, Any]) -> None:
        key = (str(ev.get("epoch")), int(ev.get("seq") or 0))
        if key in self.seen:
            return
        self.seen.add(key)
        self.last_seq = max(self.last_seq, key[1])
        try:
            self.handle(ev)
        except NoMatches:  # màn đang đóng
            return
        note = getattr(self.app, "note_event", None)
        if note is not None:
            note(ev)

    # ------------------------------------------------------------------ event → mục
    def handle(self, ev: dict[str, Any]) -> None:
        p = ev.get("payload") or {}
        t = ev.get("type", "")
        sid = ev.get("subagent_id")
        if t == "usage.tick":
            self.tokens = int(p.get("total_tokens") or self.tokens)
            self.cost = float(p.get("cost_usd") or self.cost)
            return
        if t == "run.started":
            self.t0 = self.t0 or parse_ts(ev.get("ts")) or time.time()
            return
        if sid:
            self._handle_nested(ev, t, p, sid)
            return
        if t == "message.delta":
            if self._cur_kind != "text":
                self._end_block()
                self._cur_kind = "text"
                self._cur = self._add({"kind": "text", "text": "", "live": True})
            self._cur_buf += p.get("text", "")
            assert self._cur is not None
            self._cur.meta["text"] = self._cur_buf
            self._redraw(self._cur)
            self.activity = "Đang viết"
            return
        if t == "message.complete":
            text = p.get("text") or ""
            if self._cur_kind == "text" and self._cur is not None:
                self._cur.meta.update(text=text, live=False)
                self._redraw(self._cur)
                self._end_block()
            else:
                self._end_block()
                self._add({"kind": "text", "text": text})
            return
        if t in ("reasoning.delta", "reasoning.progress"):
            if self._cur_kind != "think":
                self._end_block()
                self._cur_kind = "think"
                self._cur = self._add({"kind": "think", "text": "", "expanded": False}, click=True)
            assert self._cur is not None
            self._cur.meta["text"] += p.get("text") or ""
            self._redraw(self._cur)
            self.activity = "Đang nghĩ"
            return
        if t in NOISE:
            return
        self._end_block()
        if t == "run.queued":
            self._prompt(self.run.get("prompt") or p.get("prompt_preview") or "", first=True)
        elif t == "tool.start":
            self.tool_uses += 1
            name = p.get("name") or "?"
            meta = {"kind": "tool", "tool_id": p.get("tool_id") or "", "name": name, "args": p.get("args_preview") or "",
                    "input": p.get("input"), "status": "running", "started": parse_ts(ev.get("ts")) or time.time(),
                    "agent": name in AGENT_TOOLS, "sub_tools": 0}
            w = self._add(meta, click=True)
            self.tools[meta["tool_id"]] = w
            if meta["agent"]:
                self.headers[meta["tool_id"]] = w
                self.groups[meta["tool_id"]] = w
            self.activity = name
        elif t == "tool.complete":
            self._tool_done(p, nested=False)
        elif t == "subagent.spawn":
            hdr = self.headers.get(p.get("parent_tool_use_id") or "")
            if hdr is not None:
                hdr.meta.update(name="Agent", args=p.get("description") or p.get("type") or "")
                self._redraw(hdr)
        elif t in ("approval.open", "approval.waiting_human"):
            rid = p.get("request_id") or ""
            w = self.perms.get(rid)
            meta = {"kind": "perm", "request_id": rid, "tool": p.get("tool"), "preview": p.get("preview"),
                    "reason": p.get("reason"), "rule": p.get("matched_rule"), "human": t == "approval.waiting_human"}
            if w is None:
                self.perms[rid] = self._add(meta, classes="av-item av-perm")
            else:
                w.meta.update({k: v for k, v in meta.items() if v is not None})
                self._redraw(w)
        elif t == "approval.resolved":
            rid = p.get("request_id") or ""
            w = self.perms.get(rid)
            if w is None:
                w = self._add({"kind": "perm", "request_id": rid, "tool": p.get("tool"), "preview": p.get("preview")}, classes="av-item av-perm")
                self.perms[rid] = w
            w.meta.update(resolved=p.get("decision"), by=p.get("by"), channel=p.get("channel"), note=p.get("note"))
            w.set_class(True, "av-perm-done")
            self._redraw(w)
        elif t == "user.message":
            self._add({"kind": "user", "text": p.get("text", "")})
        elif t == "steer.ack":
            self._add({"kind": "note", "text": "agent đã nhận chỉ đạo"})
        elif t in END_TYPES:
            oc = p.get("outcome") or t.split(".")[1]
            dur_ms = p.get("duration_ms")
            t_end = parse_ts(ev.get("ts"))
            if not dur_ms and self.t0 and t_end:  # run bị dừng: không có duration_ms từ CLI → tính từ lúc bắt đầu
                dur_ms = max(0.0, (t_end - self.t0) * 1000)
            if ev.get("source", "hub") == "hub":
                self.outcome = oc
                self.run["status"] = oc
                self.end_ms = dur_ms or self.end_ms
                if p.get("cost_usd") is not None:
                    self.cost = float(p.get("cost_usd") or 0)
            self._add({"kind": "end", "outcome": oc, "duration_ms": dur_ms, "turns": p.get("num_turns"),
                       "cost": p.get("cost_usd"), "error": p.get("error")})
            for w in self.tools.values():  # tool chưa có kết quả khi run đã kết thúc → không quay mãi
                if w.meta.get("status") == "running":
                    w.meta["status"] = "error" if oc != "completed" else "done"
                    self._redraw(w)
            self._render_head()
        elif t in ERROR_TYPES:
            msg = p.get("message") or p.get("detail") or p.get("name") or p.get("tool_name") or p
            self._add({"kind": "note", "text": f"{t}: {one_line(msg, 300)}", "tone": "err"})
        elif t == "run.takeover":
            self._add({"kind": "note", "text": "người tiếp quản bằng Claude Code"})

    def _handle_nested(self, ev: dict[str, Any], t: str, p: dict[str, Any], sid: str) -> None:
        """Event của subagent (`subagent_id` = tool_id của Task/Agent cha): chèn thụt vào ngay sau nhóm của nó."""
        if t == "tool.start":
            self.tool_uses += 1
            hdr = self._header(sid)
            hdr.meta["sub_tools"] = int(hdr.meta.get("sub_tools") or 0) + 1
            self._redraw(hdr)
            meta = {"kind": "tool", "tool_id": p.get("tool_id") or "", "name": p.get("name") or "?", "args": p.get("args_preview") or "",
                    "input": p.get("input"), "status": "running", "started": parse_ts(ev.get("ts")) or time.time(), "nested": True}
            w = self._add(meta, click=True, group=sid)
            self.tools[meta["tool_id"]] = w
            self.activity = f"Agent · {p.get('name') or '?'}"
        elif t == "tool.complete":
            self._tool_done(p, nested=True, group=sid)
        elif t == "message.complete" and (p.get("text") or "").strip():
            self._header(sid)
            self._add({"kind": "text", "text": p.get("text"), "nested": True}, group=sid)
        elif t == "subagent.spawn":  # subagent lồng subagent: chỉ đổi nhãn nếu có dòng cha
            hdr = self.headers.get(p.get("parent_tool_use_id") or "")
            if hdr is not None:
                hdr.meta.update(name="Agent", args=p.get("description") or "")
                self._redraw(hdr)
        # delta/suy nghĩ/khác của subagent: bỏ (Claude Code cũng chỉ hiện tool + lời cuối)

    def _header(self, sid: str) -> AvItem:
        hdr = self.headers.get(sid)
        if hdr is None:  # thiếu tool.start của Task (vd ring buffer bị cắt) → tạo dòng cha
            hdr = self._add({"kind": "tool", "tool_id": sid, "name": "Agent", "args": "", "status": "running", "started": time.time(),
                             "agent": True, "sub_tools": 0}, click=True)
            self.headers[sid] = hdr
            self.groups[sid] = hdr
            self.tools[sid] = hdr
        return hdr

    def _tool_done(self, p: dict[str, Any], *, nested: bool, group: str | None = None) -> None:
        tid = p.get("tool_id") or ""
        w = self.tools.get(tid)
        if w is None:
            w = self._add({"kind": "tool", "tool_id": tid, "name": p.get("name") or "?", "args": "", "nested": nested,
                           "agent": (p.get("name") in AGENT_TOOLS) and not nested}, click=True, group=group)
            self.tools[tid] = w
        w.meta.update(status="error" if p.get("is_error") else "done", result=p.get("result_preview") or "",
                      duration_s=p.get("duration_s"))
        self._redraw(w)

    def _prompt(self, text: str, *, first: bool = False) -> None:
        if first and self._prompt_shown:
            return
        self._prompt_shown = True
        self._add({"kind": "user", "text": text})

    def _end_block(self) -> None:
        self._cur, self._cur_kind, self._cur_buf = None, None, ""

    # ------------------------------------------------------------------ vẽ
    def render_item(self, meta: dict[str, Any]) -> Text:
        k = meta.get("kind")
        if k == "user":
            return R.user_text(meta.get("text"))
        if k == "text":
            t = R.assistant_text(meta.get("text"), nested=bool(meta.get("nested")), live=bool(meta.get("live")))
            return _indent(t) if meta.get("nested") else t
        if k == "think":
            return R.thinking_text(meta.get("text"), expanded=bool(meta.get("expanded")))
        if k == "tool":
            return self._tool_render(meta)
        if k == "perm":
            return self._perm_render(meta)
        if k == "end":
            return self._end_render(meta)
        return R.note_text(meta.get("text"), meta.get("tone"))

    def _tool_render(self, m: dict[str, Any]) -> Text:
        expanded = bool(m.get("expanded") or self.expand_all)
        status = m.get("status") or "done"
        now = time.time()
        if m.get("agent"):
            head = R.tool_text(m.get("name") or "Agent", m.get("args") or "", status=status, started=m.get("started"), now=now,
                               duration_s=m.get("duration_s"), blink=(self.frame // 3) % 2 == 0)
            t = head.split("\n")[0]
            n = int(m.get("sub_tools") or 0)
            t.append("\n" + R.GUTTER, style=R.DIM)
            if status == "running":
                t.append(f"Đang chạy… ({n} tool)" if n else "Đang chạy…", style=R.DIM)
            else:
                word = "Lỗi" if status == "error" else "Xong"
                bits = [f"{n} tool"] if n else []
                if m.get("duration_s") is not None:
                    bits.append(f"{float(m['duration_s']):.1f}s")
                t.append(f"{word}" + (f" ({' · '.join(bits)})" if bits else ""), style=R.ERR if status == "error" else R.DIM)
                if expanded:
                    for ln in clean(m.get("result") or "").rstrip("\n").split("\n"):
                        t.append("\n" + R.INDENT, style=R.DIM)
                        t.append(clean(ln))
                elif (m.get("result") or "").strip():
                    t.append("  (click để xem kết quả)", style=R.DIM)
            return t
        t = R.tool_text(m.get("name") or "?", m.get("args") or "", status=status, result=m.get("result"), expanded=expanded,
                        duration_s=m.get("duration_s"), started=m.get("started"), now=now, tool_input=m.get("input"),
                        blink=(self.frame // 3) % 2 == 0)
        # thụt mục của subagent ở đây (tool_text bỏ qua `nested` khi đang chạy)
        return _indent(t) if m.get("nested") else t

    def _perm_render(self, m: dict[str, Any]) -> Text:
        t = Text()
        dec = m.get("resolved")
        if dec:
            ok = dec == "allowed"
            t.append("✓ Đã cho phép" if ok else ("✗ Đã từ chối" if dec == "denied" else f"■ {one_line(dec, 30)}"),
                     style=f"bold {R.OK if ok else R.ERR}")
            t.append(f" · {one_line(m.get('tool') or '?', 60)}", style="bold")
            if m.get("by") or m.get("channel"):
                t.append(f"  bởi {one_line(m.get('by') or '?', 30)} qua {one_line(m.get('channel') or '?', 20)}", style=R.DIM)
            if m.get("preview"):
                t.append("\n")
                t.append(one_line(m["preview"], 300), style=R.DIM)
            if m.get("note"):
                t.append("\n")
                t.append(f"ghi chú: {one_line(m['note'], 300)}", style=R.DIM)
            return t
        t.append("Chờ người duyệt" if m.get("human") else "Cần duyệt", style=f"bold {R.PERMISSION}")
        t.append(" · ", style=R.DIM)
        t.append(one_line(m.get("tool") or "?", 60), style="bold")
        t.append("\n\n")
        t.append(clean(m.get("preview") or "").strip() or "(không có preview)")
        if m.get("reason") or m.get("rule"):
            t.append("\n\n")
            t.append(one_line(m.get("reason") or "", 300), style=R.SUBTLE)
            if m.get("rule"):
                t.append(f"  ({one_line(m['rule'], 80)})", style=R.DIM)
        t.append("\n\n")
        t.append("a", style=f"bold {R.PERMISSION}")
        t.append(" cho phép · ", style=R.DIM)
        t.append("d", style=f"bold {R.PERMISSION}")
        t.append(" từ chối", style=R.DIM)
        return t

    def _end_render(self, m: dict[str, Any]) -> Text:
        oc = m.get("outcome") or "?"
        word = {"completed": "Xong", "failed": "Lỗi", "cancelled": "Đã dừng", "interrupted": "Bị ngắt", "waiting_human": "Chờ người"}.get(oc, oc)
        bits = [word]
        if m.get("duration_ms"):
            bits.append(R.fmt_elapsed(float(m["duration_ms"]) / 1000))
        if m.get("turns"):
            bits.append(f"{m['turns']} lượt")
        if m.get("cost"):
            bits.append(f"${float(m['cost']):.3f}")
        text = " · ".join(str(b) for b in bits)
        if oc in ("failed", "interrupted", "crashed"):
            t = R.note_text(text, "err")
            if m.get("error"):
                t.append(f"\n  {one_line(m['error'], 300)}", style=R.ERR)
            return t
        if oc in ("cancelled", "waiting_human"):
            return R.note_text(text, "warn")
        return R.note_text(text)

    def _redraw(self, w: AvItem) -> None:
        w.update(self.render_item(w.meta))

    @property
    def tlog(self) -> VerticalScroll:
        return self.query_one("#av-log", VerticalScroll)

    def _at_end(self) -> bool:
        log = self.tlog
        return log.scroll_y >= log.max_scroll_y - 1

    def _scroll_end(self) -> None:
        self.call_after_refresh(lambda: self.tlog.scroll_end(animate=False))

    def _add(self, meta: dict[str, Any], *, click: bool = False, classes: str | None = None, group: str | None = None) -> AvItem:
        cls = classes or "av-item"
        if group:
            cls += " av-nested"
        if click:
            cls += " av-click"
        w = AvItem(meta, classes=cls)
        w.update(self.render_item(meta))
        log = self.tlog
        follow = self._at_end()
        anchor = self.groups.get(group) if group else None
        if anchor is not None and anchor.parent is log:
            log.mount(w, after=anchor)
        else:
            log.mount(w)
        if group:
            self.groups[group] = w
        kids = [k for k in log.children if isinstance(k, AvItem)]
        for k in kids[: max(0, len(kids) - MAX_ITEMS)]:
            self.tools.pop(str(k.meta.get("tool_id")), None)
            k.remove()
        if follow or not self.loaded:
            self._scroll_end()
        return w

    # ------------------------------------------------------------------ đầu màn / spinner
    def _status(self) -> str:
        if self.outcome:
            return self.outcome
        if self._pending():
            return "approval"
        st = self.run.get("status")
        return st if st in ENDED else "running"

    def running(self) -> bool:
        return self._status() in ("running", "approval")

    def _elapsed(self) -> float | None:
        if not self.running():
            if self.end_ms:
                return float(self.end_ms) / 1000
            return None
        return (time.time() - self.t0) if self.t0 else None

    def _render_head(self) -> None:
        try:
            title = self.query_one("#av-title", Static)
        except NoMatches:
            return
        row = self.model.rows.get(self.agent_id) if self.model is not None else None
        name = (row["name"] if row is not None else None) or self.agent_id
        t = Text()
        t.append(one_line(name, 60), style="bold")
        t.append(f" · {self.run_id[:10]}", style=f"bold {R.DIM}")
        word, color = STATUS_WORD.get(self._status(), (self._status(), R.DIM))
        t.append("   ")
        t.append(word, style=f"bold {color}")
        title.update(t)
        self.query_one("#av-stats", Static).update(
            _styled(R.stats_text(tool_uses=self.tool_uses, tokens=self.tokens, elapsed_s=self._elapsed(), cost_usd=self.cost) or "—", R.DIM))
        prompt = clean(self.run.get("prompt") or "").strip()
        self.query_one("#av-prompt", Static).update(_styled(one_line(prompt.split("\n")[0], 160) if prompt else "", R.DIM))
        self.query_one("#av-stop").display = self.running()
        spin = self.query_one("#av-spinner", Static)
        if self.running() and self.loaded:
            spin.display = True
            act = "Chờ duyệt" if self._status() == "approval" else self.activity
            spin.update(R.spinner_text(act, elapsed_s=self._elapsed() or 0, tokens=self.tokens, frame=self.frame, interrupt="x để dừng"))
        else:
            spin.display = False

    def _tick(self) -> None:
        self.frame += 1
        self._render_head()
        if self.frame % 3 == 0:
            for w in list(self.tools.values()):
                if w.meta.get("status") == "running" and w.is_mounted:
                    self._redraw(w)

    # ------------------------------------------------------------------ click / chọn chữ
    def item_clicked(self, w: AvItem) -> None:
        k = w.meta.get("kind")
        if k in ("tool", "think"):
            w.meta["expanded"] = not w.meta.get("expanded")
            self._redraw(w)

    def on_text_selected(self, _ev: events.TextSelected) -> None:
        text = self.get_selected_text()
        if text and text.strip():
            self.copy(text)

    def copy(self, text: str) -> None:
        self.app.copy_to_clipboard(text)  # OSC 52
        self.run_worker(asyncio.to_thread(A.clipboard_set, text), group="clip", exit_on_error=False)  # dự phòng: clip.exe/pbcopy/wl-copy
        self.notify(f"Đã copy {len(text)} ký tự", timeout=2)

    # ------------------------------------------------------------------ thao tác
    def _pending(self) -> list[dict[str, Any]]:
        model = self.model
        pend = sorted((a for a in (model.approvals if model is not None else []) if a.get("run_id") == self.run_id),
                      key=lambda a: a.get("opened_at") or "")
        if pend:
            return pend
        # model (snapshot ~1 s) chưa kịp có → dùng hộp duyệt đang mở trong transcript
        return [{"id": w.meta["request_id"], "preview": w.meta.get("preview")} for w in self.perms.values()
                if not w.meta.get("resolved") and w.meta.get("request_id")]

    def action_back(self) -> None:
        inp = self.query_one("#av-steer", Input)
        if inp.display:
            inp.display = False
            self.set_focus(None)
            return
        self.app.pop_screen()

    def action_stop(self) -> None:
        if not self.running():
            self.notify("Run này không còn chạy", severity="warning")
            return
        from .app import ConfirmScreen

        def done(ok: bool | None) -> None:
            if ok:
                self.run_worker(self.app.stop_run(self.run_id))  # type: ignore[attr-defined]

        self.app.push_screen(ConfirmScreen(f"Dừng run [b]{escape(self.run_id)}[/b]?", yes="Dừng (y)"), done)

    async def action_approve(self) -> None:
        pend = self._pending()
        if not pend:
            self.notify("Run này không có thao tác chờ duyệt", severity="warning")
            return
        await self.app.decide(pend[0], "approve", None)  # type: ignore[attr-defined]

    def action_deny(self) -> None:
        pend = self._pending()
        if not pend:
            self.notify("Run này không có thao tác chờ duyệt", severity="warning")
            return
        from .app import PromptScreen

        def done(note: str | None) -> None:
            if note is not None:
                self.run_worker(self.app.decide(pend[0], "deny", note or None))  # type: ignore[attr-defined]

        self.app.push_screen(PromptScreen("Từ chối — ghi chú cho agent (tuỳ chọn)", ""), done)

    def action_steer(self) -> None:
        inp = self.query_one("#av-steer", Input)
        inp.display = True
        inp.focus()

    def action_expand_all(self) -> None:
        self.expand_all = not self.expand_all
        for w in self.tlog.children:
            if isinstance(w, AvItem) and w.meta.get("kind") == "tool":
                self._redraw(w)

    def action_page(self, direction: int) -> None:
        log = self.tlog
        log.scroll_to(y=log.scroll_y + direction * max(1, log.size.height - 2), animate=False)

    def action_to_end(self) -> None:
        self.tlog.scroll_end(animate=False)

    @on(Input.Submitted, "#av-steer")
    async def _steer_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        event.input.display = False
        self.set_focus(None)
        if text:
            await self.app.steer(self.run_id, text)  # type: ignore[attr-defined]


async def open_agent_view(app: Any, ref: str) -> None:
    """Mở `AgentViewScreen` cho `ref` = run id, hoặc agent id (run đang chạy / chờ người / gần nhất) — như `TopApp._open_attach`."""
    try:
        run = await app.client.call("run.get", {"id": ref})
    except RpcError:
        r = app.model.rows.get(ref) if getattr(app, "model", None) is not None else None
        rid = r and (r["current_run_id"] or r["waiting_human"] or r["last_run_id"])
        if not rid:
            app.notify(f"không tìm thấy run/agent '{ref}'", severity="error")
            return
        run = {"id": rid, "agent_id": ref}
    await app.push_screen(AgentViewScreen(run["agent_id"], run["id"]))


__all__ = ["AgentViewScreen", "open_agent_view"]
