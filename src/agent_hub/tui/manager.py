"""Màn "Điều phối" của TUI `top` (phím `m`): chat với điều phối viên ngay trong htop.

- Dùng CHUNG hội thoại kênh `cli` với `agent-hub chat` (đóng TUI rồi mở chat — hay ngược lại — vẫn tiếp mạch). Mọi ghi gửi
  `_audit.channel=tui` (`AsyncHubClient(channel="tui")` của app).
- Trái: danh sách hội thoại (`+ Hội thoại mới` đầu) và "Hành động" (`manager.action` của hội thoại) — Enter/click trên hành động có
  đích run/task/agent HỢP LỆ (`present.mgr_target`: loại đã biết + id an toàn) → mở Attach/Tasks; đích khác chỉ thông báo.
- Giữa: transcript (`RichLog`, markup tắt) + dòng trạng thái (spinner, tool, đuôi chữ đang viết) + ô nhập dính đáy.
- Luồng: `events.subscribe {agent_id: điều phối viên, level: detail}` → `chatview.ChatState.apply`; tự đăng ký lại khi rớt.
Chữ của điều phối viên là DỮ LIỆU: `termsafe.clean` + `rich.text.Text` (không markup, không link).
"""

from __future__ import annotations

import asyncio
from typing import Any

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.widgets import Footer, Input, OptionList, RichLog, Static
from textual.widgets.option_list import Option

from ..manager import chatview as V
from ..rpc import DaemonUnavailable, RpcError
from ..termsafe import one_line
from .client import subscribe

NEW = "__new__"
ACTIONS_SCAN = 3000
HISTORY = 100
NAVIGABLE = ("run", "task", "agent")

CSS = """
ManagerScreen #mgr-body { height: 1fr; }
ManagerScreen #mgr-side { width: 34; border-right: solid $primary-darken-2; }
ManagerScreen .mgr-cap { height: 1; padding: 0 1; color: $text-muted; text-style: bold; }
ManagerScreen #mgr-convs { height: 1fr; border: none; }
ManagerScreen #mgr-actions { height: 1fr; border: none; }
ManagerScreen #mgr-main { width: 1fr; }
ManagerScreen #mgr-head { height: 1; padding: 0 1; background: $panel; }
ManagerScreen #mgr-log { height: 1fr; padding: 0 1; scrollbar-size-vertical: 1; scrollbar-background: $surface; scrollbar-background-hover: $surface; scrollbar-background-active: $surface; }
ManagerScreen #mgr-status { height: 1; padding: 0 1; }
ManagerScreen #mgr-input { margin: 0 1; }
"""


class ManagerScreen(Screen):
    DEFAULT_CSS = CSS
    BINDINGS = [
        Binding("escape", "back", "Về bảng"),
        Binding("ctrl+n", "new_conv", "Hội thoại mới"),
        Binding("n", "new_conv", "Hội thoại mới", show=False),
        Binding("ctrl+t", "stop_turn", "Dừng lượt"),
        Binding("ctrl+r", "reload", "Tải lại", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.state: V.ChatState | None = None
        self.convs: list[dict[str, Any]] = []
        self.actions: list[dict[str, Any]] = []
        self._sub: Any = None
        self._sending = False
        self._held: list[dict[str, Any]] = []

    @property
    def client(self) -> Any:
        return self.app.client  # type: ignore[attr-defined]

    def compose(self) -> ComposeResult:
        with Horizontal(id="mgr-body"):
            with Vertical(id="mgr-side"):
                yield Static("HỘI THOẠI (kênh cli)", classes="mgr-cap")
                yield OptionList(id="mgr-convs")
                yield Static("HÀNH ĐỘNG  Enter = mở", classes="mgr-cap")
                yield OptionList(id="mgr-actions")
            with Vertical(id="mgr-main"):
                yield Static(id="mgr-head")
                yield RichLog(id="mgr-log", wrap=True, markup=False, highlight=False, auto_scroll=True)
                yield Static(id="mgr-status")
                yield Input(placeholder="Nhắn điều phối viên… (Enter gửi · Esc về bảng · Ctrl-N hội thoại mới)", id="mgr-input")
        yield Footer()

    async def on_mount(self) -> None:
        self.query_one("#mgr-input", Input).focus()
        self.set_interval(0.15, self._tick_status)
        await self.load(None, initial=True)

    # ------------------------------------------------------------------ nạp
    def log_item(self, item: dict[str, Any]) -> None:
        name = self.state.name if self.state else "Điều phối viên"
        self.query_one("#mgr-log", RichLog).write(V.render(item, manager_name=name))

    async def load(self, conversation_id: str | None, *, initial: bool = False, new: bool = False) -> None:
        log = self.query_one("#mgr-log", RichLog)
        log.clear()
        try:
            g = await self.client.call("manager.get") or {}
        except (RpcError, DaemonUnavailable) as e:
            log.write(Text(f"không đọc được điều phối viên: {e}", style="red"))
            return
        mid = g.get("agent_id")
        inp = self.query_one("#mgr-input", Input)
        if not mid:
            self.state = None
            inp.disabled = True
            log.write(Text("◆ ", style="bold magenta") + Text(
                "Chưa có điều phối viên.\n\n  Tạo và chỉ định (chỉ người làm được), rồi mở lại màn này:\n"
                "    agent-hub agent create dieu-phoi --template manager --name \"Điều phối viên\"\n"
                "    agent-hub manager set dieu-phoi\n"))
            self._render_head()
            return
        inp.disabled = False
        name = mid
        try:
            name = str((await self.client.call("agent.get", {"id": mid}) or {}).get("name") or mid)
        except RpcError:
            pass
        await self._load_convs()
        cid = conversation_id
        if cid is None and initial and not new and self.convs:
            cid = self.convs[0]["id"]
        st = await self.client.call("hub.status") or {}
        state = V.ChatState(mid, cid, name=one_line(name, 40))
        state.last_seq, state.epoch = int(st.get("last_seq") or 0), st.get("epoch")
        rows: list[dict[str, Any]] = []
        if cid:
            rows = await self.client.call("manager.history", {"conversation_id": cid, "limit": HISTORY}) or []
        evs: list[dict[str, Any]] = []
        if rows:
            try:
                evs = [e for e in await self.client.call("events.query", {"agent_id": mid, "limit": ACTIONS_SCAN}) or [] if e.get("type") == "manager.action"]
            except RpcError:
                evs = []
        conv = next((c for c in self.convs if c["id"] == cid), None)
        self.state = state
        log.write(V.banner(mid, cid, title=(conv or {}).get("title"), resumed=len(V.history_items(rows)), keys=V.TUI_KEYS))
        for it in V.timeline(rows, evs):
            self.log_item(it)
        rid = V.pending_run(rows)
        run = None
        if rid:
            try:
                run = await self.client.call("run.get", {"id": rid})
            except RpcError:
                run = None
        state.resume(rows, run)
        runs = state.runs
        self.actions = [V.mgr_action_item(e) for e in evs if e.get("run_id") in runs][-50:]
        self._render_actions()
        self._render_convs()
        self._render_head()
        self._restart_stream()

    async def _load_convs(self) -> None:
        try:
            self.convs = await self.client.call("manager.conversations", {"channel": V.CHANNEL}) or []
        except RpcError:
            self.convs = []

    # ------------------------------------------------------------------ vẽ phần phụ
    def _render_head(self) -> None:
        h = Text()
        h.append(" ◆ Điều phối ", style="bold reverse magenta")
        st = self.state
        if st is None:
            h.append("  (chưa chỉ định)", style="grey50")
        else:
            h.append(f"  {st.name}", style="bold")
            h.append(f"  · hội thoại {st.conversation_id or '(mới — tin đầu tiên sẽ tạo)'}", style="grey62")
            h.append("  · dùng chung với `agent-hub chat`", style="grey42")
        self.query_one("#mgr-head", Static).update(h)

    def _render_convs(self) -> None:
        ol = self.query_one("#mgr-convs", OptionList)
        ol.clear_options()
        cur = self.state.conversation_id if self.state else None
        ol.add_option(Option(Text("+ Hội thoại mới", style="bold green"), id=NEW))
        for c in self.convs[:50]:
            t = Text()
            t.append("▸ " if c["id"] == cur else "  ", style="magenta")
            t.append(one_line(c.get("title") or c["id"], 22), style="bold" if c["id"] == cur else "")
            t.append(f"\n  {c['id'][:14]} · {c.get('messages', 0)} tin · {V.hhmm(c.get('updated_at'))}", style="grey50")
            ol.add_option(Option(t, id=f"c:{c['id']}"))

    def _render_actions(self) -> None:
        ol = self.query_one("#mgr-actions", OptionList)
        ol.clear_options()
        if not self.actions:
            ol.add_option(Option(Text("(chưa có)", style="grey50"), id="none", disabled=True))
            return
        for i, a in enumerate(reversed(self.actions)):
            t = Text()
            t.append("✓ " if a.get("ok", True) else "✗ ", style="green" if a.get("ok", True) else "red")
            t.append(one_line(a.get("summary"), 28))
            tgt = a.get("target") or {}
            if tgt.get("label"):
                t.append(f"\n  → {one_line(tgt['label'], 28)}", style="grey50")
            ol.add_option(Option(t, id=f"a:{len(self.actions) - 1 - i}"))

    def _tick_status(self) -> None:
        try:
            w = self.query_one("#mgr-status", Static)
        except NoMatches:
            return
        st = self.state
        if st is None:
            w.update("")
            return
        w.update(st.status_text(max(40, self.size.width - 38), busy_hint="Ctrl-T dừng lượt", idle_hint="Esc về bảng · Tab sang danh sách"))

    # ------------------------------------------------------------------ luồng event
    def _restart_stream(self) -> None:
        if self._sub is not None:
            self._sub.close()
        self.run_worker(self._stream(), exclusive=True, group="mgr-stream", exit_on_error=False)

    async def _stream(self) -> None:
        delay = 0.2
        while self.state is not None and self.is_attached:
            st = self.state
            self._sub = sub = subscribe(self.client, {"agent_id": st.manager_id, "level": "detail", "since_seq": st.last_seq, "epoch": st.epoch})
            try:
                async for msg in sub:
                    if "subscribed" in msg:
                        delay = 0.2
                        continue
                    if "end" in msg:
                        break
                    if self.state is not st:
                        return
                    self._apply(msg)
            except (RpcError, DaemonUnavailable, OSError, ValueError):
                pass
            if self.state is not st:
                return
            await asyncio.sleep(delay)
            delay = min(delay * 2, 3.0)

    def _apply(self, ev: dict[str, Any]) -> None:
        st = self.state
        if st is None:
            return
        if self._sending:
            self._held.append(ev)
            return
        try:
            for it in st.apply(ev):
                self.log_item(it)
                if it.get("kind") == "action":
                    self.actions.append(it)
                    self._render_actions()
            self._tick_status()
            if ev.get("type") == "manager.reply":
                self.run_worker(self._refresh_convs(), group="mgr-convs", exclusive=True, exit_on_error=False)
        except NoMatches:  # màn đã đóng
            pass

    async def _refresh_convs(self) -> None:
        await self._load_convs()
        self._render_convs()

    # ------------------------------------------------------------------ gửi
    @on(Input.Submitted, "#mgr-input")
    async def _submit(self, ev: Input.Submitted) -> None:
        text = ev.value.strip()
        if not text or self.state is None:
            return
        ev.input.value = ""
        await self.send(text)

    async def send(self, text: str) -> None:
        st = self.state
        if st is None:
            return
        self.log_item({"kind": "user", "text": text})
        params: dict[str, Any] = {"channel": V.CHANNEL, "text": text}
        new_conv = st.conversation_id is None
        if st.conversation_id:
            params["conversation_id"] = st.conversation_id
        self._sending = True
        try:
            res = await self.client.call("manager.chat", params, audit=True) or {}
        except (RpcError, DaemonUnavailable) as e:
            self.log_item({"kind": "note", "text": f"chưa gửi được: {e.message if isinstance(e, RpcError) else e}", "tone": "err"})
            return
        finally:
            self._sending = False
            held, self._held = self._held, []
        for it in st.sent(res, text):
            self.log_item(it)
        for e in held:
            self._apply(e)
        if new_conv:
            await self._refresh_convs()
        self._render_head()

    # ------------------------------------------------------------------ phím / chọn
    async def action_new_conv(self) -> None:
        if self.state is None:
            return
        self.state.switch(None)
        self.actions = []
        self.query_one("#mgr-log", RichLog).clear()
        self.query_one("#mgr-log", RichLog).write(V.banner(self.state.manager_id, None, keys=V.TUI_KEYS))
        self._render_actions()
        self._render_convs()
        self._render_head()
        self.query_one("#mgr-input", Input).focus()

    async def action_stop_turn(self) -> None:
        st = self.state
        if st is None or not st.busy or not st.active_run:
            self.notify("Không có lượt nào đang chạy")
            return
        try:
            await self.client.call("run.stop", {"id": st.active_run}, audit=True)
            self.log_item({"kind": "note", "text": f"đã yêu cầu dừng lượt {st.active_run}", "tone": "warn"})
        except (RpcError, DaemonUnavailable) as e:
            self.notify(str(e), severity="error")

    async def action_reload(self) -> None:
        await self.load(self.state.conversation_id if self.state else None)

    def action_back(self) -> None:
        if self._sub is not None:
            self._sub.close()
        self.state = None
        self.app.pop_screen()

    @on(OptionList.OptionSelected, "#mgr-convs")
    async def _pick_conv(self, ev: OptionList.OptionSelected) -> None:
        oid = ev.option.id or ""
        if oid == NEW:
            await self.action_new_conv()
        elif oid.startswith("c:"):
            await self.load(oid[2:])
            self.query_one("#mgr-input", Input).focus()

    @on(OptionList.OptionSelected, "#mgr-actions")
    async def _pick_action(self, ev: OptionList.OptionSelected) -> None:
        oid = ev.option.id or ""
        if not oid.startswith("a:"):
            return
        try:
            a = self.actions[int(oid[2:])]
        except (ValueError, IndexError):
            return
        await self.open_target(a.get("target") or {})

    async def open_target(self, tgt: dict[str, Any]) -> None:
        """Đích của thẻ hành động → màn nội bộ. CHỈ loại đã biết + id an toàn (`href` khác None) — không tin chữ agent."""
        kind, ident = tgt.get("kind"), tgt.get("id")
        if not tgt.get("href") or kind not in NAVIGABLE or not ident:
            self.notify(f"Không mở được đích này trong TUI ({one_line(tgt.get('label') or '?', 40)})", severity="warning")
            return
        from .app import AttachScreen, TasksScreen

        if kind == "task":
            self.app.push_screen(TasksScreen(select=str(ident)))
            return
        if kind == "run":
            try:
                run = await self.client.call("run.get", {"id": ident})
            except RpcError as e:
                self.notify(e.message, severity="error")
                return
            self.app.push_screen(AttachScreen(run["agent_id"], run["id"]))
            return
        await self.app._open_attach(str(ident))  # type: ignore[attr-defined]


__all__ = ["ManagerScreen"]
