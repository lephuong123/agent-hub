"""`cfharness chat` fullscreen (TTY mặc định; `--classic` = REPL prompt_toolkit cũ) — chat với điều phối viên, NHÌN như Claude Code CLI.

Kế thừa `TopApp` (cùng `AttachScreen`, `TasksScreen`, hộp xác nhận, `decide/steer/stop_run`) nhưng màn gốc là `ChatScreen`; kênh `cli`
(dùng chung hội thoại với REPL và màn `m` của `top`). Lõi phiên là `manager.repl.ChatSession` (lệnh `/`, thẻ duyệt/câu hỏi, firehose
mọi agent qua `AgentMonitor`) — chỉ thay đầu ra (`ScreenOut`), dòng chào (`banner` → hộp chào), hỏi y/N (hộp xác nhận) và `/attach`.
`TopApp.model` = model của `AgentMonitor` → một firehose + một vòng `tui.snapshot` cho cả chat, bảng agent và màn attach.

Hình (kiểu vẽ chung ở `tui/cc_render.py`):
- Không thanh tiêu đề, không Footer. Mục đầu transcript = hộp chào viền tròn cam `✻ Chào mừng tới cfharness chat!`.
- Tin người `> chữ` (xám); lời điều phối viên `● chữ`; tool `● Tên(args)` + `  ⎿  ` kết quả (click / Ctrl-O mở rộng; chấm nhấp nháy
  khi đang chạy); thẻ hành động `● tóm tắt` + `  ⎿  → đích` (click → mở đích, `tui.manager.open_target`); ghi chú `  ⎿  …`.
- Thẻ duyệt/câu hỏi trong hộp viền tròn tím (như hộp xin phép của Claude Code); click → ĐIỀN SẴN `/approve <id> ` / `/answer <id> `,
  không làm gì cho tới khi người Enter + y/N.
- Spinner `✻ Đang nghĩ… (12s · esc để dừng)` ngay TRÊN ô nhập khi bận (ẩn khi rảnh). Ô nhập viền tròn xám có `> `; dưới là MỘT dòng
  mờ `? phím tắt` … `<điều phối viên> · hội thoại <id>`, hoặc menu lệnh khi gõ `/`; `?` ở ô nhập trống = bảng phím tắt.
- Bảng agent đang chạy dưới cùng (như cây tác vụ nền của Claude Code): click hoặc ↓ (ô nhập trống) + Enter → `AttachScreen`.
- Kéo chuột chọn chữ → tự copy (OSC 52 + `attachments.clipboard_set` dự phòng). Ctrl-C: có vùng chọn → copy; đang làm → dừng lượt;
  rảnh → bấm lần nữa để thoát.
Chữ của agent là DỮ LIỆU: `termsafe.clean/one_line` + `rich.text.Text` (không markup, không link). Mục phải là MỘT `Text` (`flatten`)
thì Textual mới cho kéo chọn.
"""

from __future__ import annotations

import asyncio
import io
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.console import Group
from rich.padding import Padding
from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.markup import escape
from textual.screen import Screen
from textual.suggester import SuggestFromList
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from ..manager import agentsview as G
from ..manager import attachments as A
from ..manager import chatview as V
from ..manager import repl as R
from ..termsafe import clean, one_line
from . import cc_render as cc
from .app import ConfirmScreen, HtopScreen, TopApp
from .client import AsyncHubClient
from .manager import open_target
from .model import TopModel, short_tool

MAX_ITEMS = 1500  # như Transcript của màn attach
PANEL_ROWS = 6
TICK_S = 0.1
CTRL_C_WINDOW_S = 2.0
HISTORY_MAX = 500
SELECT = "#5FD7FF"  # dòng đang chọn (menu lệnh, bảng agent) — xanh lơ như Claude Code
INPUT_BORDER = "#5F5F5F"
INPUT_BORDER_FOCUS = "#8A8A8A"
PLACEHOLDER = "Nhắn điều phối viên… (gõ / để xem lệnh)"

CSS = f"""
ChatScreen {{ layout: vertical; background: $background; }}
ChatScreen #chat-log {{ height: 1fr; padding: 0 1; scrollbar-size-vertical: 1; background: $background;
    scrollbar-background: $background; scrollbar-background-hover: $background; scrollbar-background-active: $background; }}
ChatScreen #chat-log > .item {{ width: 100%; margin: 1 0 0 0; }}
ChatScreen #chat-log > .tight {{ margin: 0; }}
ChatScreen #chat-log > .welcome {{ width: auto; min-width: 52; max-width: 100%; border: round {cc.ACCENT}; padding: 0 1; margin: 0; }}
ChatScreen #chat-log > .welcome.item {{ margin: 1 0 0 0; }}
ChatScreen #chat-log > .card {{ border: round {cc.PERMISSION}; padding: 0 1; }}
ChatScreen #chat-log > .tool:hover, ChatScreen #chat-log > .clickable:hover {{ background: $boost; }}
ChatScreen #chat-draft {{ width: 100%; margin: 1 0 0 0; }}
ChatScreen #chat-jump {{ height: 1; padding: 0 2; display: none; color: #7F7F7F; }}
ChatScreen #chat-status {{ height: 1; padding: 0 1; margin: 1 0 0 0; display: none; }}
ChatScreen #chat-input-box {{ height: auto; margin: 1 1 0 1; padding: 0 1; border: round {INPUT_BORDER}; background: $background; }}
ChatScreen #chat-input-box:focus-within {{ border: round {INPUT_BORDER_FOCUS}; }}
ChatScreen #chat-prompt {{ width: 2; height: 1; color: #B2B2B2; }}
ChatScreen #chat-input {{ width: 1fr; height: 1; border: none; padding: 0; background: transparent; }}
ChatScreen #chat-input:focus {{ border: none; background: transparent; background-tint: $foreground 0%; }}
ChatScreen #chat-hint {{ height: auto; max-height: 9; padding: 0 3; display: none; }}
ChatScreen #chat-keys {{ height: auto; padding: 0 3; display: none; }}
ChatScreen #chat-foot {{ height: 1; padding: 0 3; }}
ChatScreen #chat-foot-left {{ width: 1fr; color: #7F7F7F; }}
ChatScreen #chat-foot-right {{ width: auto; color: #7F7F7F; }}
ChatScreen #chat-agents-box {{ height: auto; margin: 1 0 0 0; padding: 0 2; display: none; }}
ChatScreen #chat-agents-head {{ height: 1; padding: 0; }}
ChatScreen #chat-agents {{ height: auto; max-height: 8; border: none; padding: 0; background: transparent; }}
ChatScreen #chat-agents:focus {{ border: none; background-tint: $foreground 0%; }}
ChatScreen #chat-agents > .option-list--option-highlighted {{ background: transparent; color: $foreground; text-style: none; }}
ChatScreen #chat-agents:focus > .option-list--option-highlighted {{ background: transparent; color: $foreground; text-style: none; }}
ChatScreen #chat-agents > .option-list--option-hover {{ background: $boost; }}
BoardScreen #summary {{ height: 2; }}
"""


# ============================================================================ vẽ (thuần)
def flatten(r: Any) -> Any:
    """`Group`/`Padding` của `Text` (cách `chatview`/`agentsview` vẽ mục) → MỘT `Text`: Textual chỉ cho kéo chọn chữ trên
    `Text`/`Content`, không trên renderable Rich bất kỳ. Thứ khác giữ nguyên."""
    if isinstance(r, Text):
        return r
    if isinstance(r, Padding) and isinstance(r.renderable, Text):
        out = Text()
        for i, ln in enumerate(r.renderable.split("\n", allow_blank=True)):
            if i:
                out.append("\n")
            out.append(" " * r.left)
            out.append_text(ln)
        return out
    if isinstance(r, Group):
        parts = [flatten(x) for x in r.renderables]
        if all(isinstance(p, Text) for p in parts):
            out = Text()
            for i, p in enumerate(parts):
                if i:
                    out.append("\n")
                out.append_text(p)
            return out
    return r


def _strip_blank_head(r: Any) -> Any:
    """Thẻ của `agentsview` mở đầu bằng một dòng trống (khoảng cách trong REPL) — trong hộp viền thì bỏ."""
    while isinstance(r, Text) and r.plain.startswith("\n"):
        r = r[1:]
    return r


def tool_text(it: dict[str, Any], *, expanded: bool, now: float | None = None, blink: bool = True) -> Text:
    """Một tool của điều phối viên (mục `tool` của `LiveChatState`) → `cc_render.tool_text`."""
    done, err = bool(it.get("done")), bool(it.get("is_error"))
    status = "error" if err else ("done" if done else "running")
    started = it.get("started")
    return cc.tool_text(short_tool(str(it.get("name") or "?")), it.get("args") or "", status=status, result=it.get("result"),
                        expanded=expanded, duration_s=it.get("duration_s"), started=float(started) if started is not None else None,
                        now=now, tool_input=it.get("input"), nested=bool(it.get("sub")), blink=blink)


def text_item(text: Any, *, live: bool = False) -> Text:
    """Chữ điều phối viên viết giữa các tool (và chữ đang chạy, `live`): `● chữ`."""
    return cc.assistant_text(text, live=live)


def _bullet_lines(text: Any, bullet: Text, style: str = "") -> Text:
    t = Text()  # KHÔNG `Text(…, style=)`: style đó là nền của cả đoạn
    t.append_text(bullet)
    lines = clean(text).replace("\t", "    ").rstrip("\n").split("\n")
    for i, ln in enumerate(lines):
        if i:
            t.append("\n  ")
        t.append(ln, style=style)
    return t


def item_text(it: dict[str, Any]) -> Text:
    """Mục hội thoại (`chatview`: user | manager | notify | action | note | steer | approval) → MỘT `Text` kiểu Claude Code."""
    k = it.get("kind")
    if k == "user":
        return cc.user_text(it.get("text"))
    if k == "manager":
        if it.get("ok", True) is False:
            return _bullet_lines(it.get("text") or "không trả lời được", Text().append("● ", style=cc.ERR))
        return cc.assistant_text(it.get("text"))
    if k == "notify":
        head = Text().append("● ✉ ", style=cc.WARN)
        return _bullet_lines(it.get("text"), head, cc.WARN)
    if k == "action":
        ok = it.get("ok", True) is not False
        t = Text().append("● ", style=cc.OK if ok else cc.ERR)
        t.append(one_line(it.get("summary") or it.get("tool") or "hành động", 160), style="bold")
        tgt = it.get("target") or {}
        if tgt.get("label"):
            t.append(f"\n{cc.GUTTER}", style=cc.DIM)
            t.append(f"→ {one_line(tgt['label'], 80)}", style=cc.DIM)
        if not ok and it.get("error"):
            t.append(f"\n{cc.GUTTER if not tgt.get('label') else cc.INDENT}", style=cc.DIM)
            t.append(one_line(it["error"], 160), style=cc.ERR)
        return t
    if k == "approval":  # chỉ khi phiên không có AgentMonitor (thẻ đầy đủ đến từ firehose)
        return cc.note_text(f"điều phối viên cần bạn duyệt {one_line(it.get('request_id'), 40)}: "
                            f"{one_line(it.get('preview') or it.get('tool') or '', 160)}", "warn")
    if k in ("note", "steer"):
        return cc.note_text(it.get("text"), it.get("tone") if k == "note" else None)
    return flatten(V.render(it))


def welcome_text(name: str, conversation_id: str | None, *, title: str | None = None, resumed: int = 0) -> Text:
    """Nội dung hộp chào (viền do CSS `.welcome` vẽ) — như `✻ Welcome to Claude Code!`."""
    t = Text()
    t.append("✻ ", style=cc.ACCENT)
    t.append("Chào mừng tới cfharness chat!", style="bold")
    t.append("\n\n")
    t.append("  /help để xem lệnh, ? để xem phím tắt", style=cc.DIM)
    t.append("\n\n")
    t.append(f"  điều phối viên: {one_line(name, 40)}", style=cc.DIM)
    t.append(f"\n  hội thoại: {one_line(conversation_id, 40) if conversation_id else '(mới)'}", style=cc.DIM)
    if title:
        t.append(f"\n  chủ đề: {one_line(title, 50)}", style=cc.DIM)
    if resumed:
        t.append(f"\n  {resumed} tin trước bên dưới", style=cc.DIM)
    return t


def slash_menu(prefix: str, *, limit: int = 7) -> Text | None:
    """Menu lệnh khi gõ `/` (như Claude Code): dòng đầu (sẽ chọn) xanh lơ, `/lệnh   mô tả`."""
    rows = [(c, d) for c, d in V.HELP if c.split()[0].startswith(prefix.lower())]
    if not rows:
        return None
    w = max(len(c) for c, _ in rows[:limit]) + 3
    t = Text()
    for i, (c, d) in enumerate(rows[:limit]):
        if i:
            t.append("\n")
        if i == 0:
            t.append(f"{c:<{w}}", style=f"bold {SELECT}")
            t.append(d, style=SELECT)
        else:
            t.append(f"{c:<{w}}")
            t.append(d, style=cc.DIM)
    if len(rows) > limit:
        t.append(f"\n… +{len(rows) - limit} lệnh", style=cc.DIM)
    return t


SHORTCUTS = (
    (("/ cho lệnh", ""), ("esc", "dừng lượt / đóng menu")),
    (("↑ ↓", "lịch sử tin đã gửi"), ("ctrl + o", "mở rộng mọi tool")),
    (("↓ (ô trống)", "chọn agent đang chạy"), ("ctrl + g", "bảng agent đầy đủ")),
    (("click", "mở tool / thẻ / agent"), ("ctrl + n", "hội thoại mới")),
    (("kéo chuột", "chọn chữ = copy"), ("ctrl + v", "dán ảnh")),
    (("pgup / pgdn", "cuộn"), ("ctrl + c ×2 / ctrl + d", "thoát")),
)


def shortcuts_text() -> Text:
    """Bảng phím tắt 2 cột (như `? for shortcuts` của Claude Code)."""
    t = Text()
    for i, (left, right) in enumerate(SHORTCUTS):
        if i:
            t.append("\n")
        lk, ld = left
        cell = f"{lk} {ld}".strip() if ld else lk
        t.append(f"{cell:<40}", style=cc.DIM)
        t.append(f"{right[0]} {right[1]}", style=cc.DIM)
    return t


_GLYPH = {
    "running": ("◐", cc.ACCENT),
    "queued": ("○", cc.DIM),
    "approval": ("⚠", cc.WARN),
    "human": ("⚠", cc.WARN),
    "manager": ("◐", cc.PERMISSION),
    "error": ("✗", cc.ERR),
}


def agent_line(mon: G.AgentMonitor, state: str, r: Any, width: int, *, selected: bool = False, now: float | None = None) -> Text:
    """Một dòng bảng agent: `  ◐ tên  task  tool cuối · thống kê`; dòng đang chọn `❯` xanh lơ."""
    now = time.time() if now is None else now
    glyph, gstyle = _GLYPH.get(state, ("·", cc.DIM))
    t = Text().append("❯ " if selected else "  ", style=f"bold {SELECT}" if selected else "")
    t.append(f"{glyph} ", style=gstyle)
    t.append(one_line(r["name"] or r.id, 30), style=f"bold {SELECT}" if selected else "bold")
    working = r["status"] in ("working", "waiting")
    tool = short_tool(r["last_tool"]) if r["last_tool"] else ""
    stats = cc.stats_text(tokens=r["run_tokens"] if working else None, elapsed_s=TopModel.elapsed(r, now),
                          cost_usd=float(r["run_cost_usd"] or 0) if working else None)
    tail = " · ".join(x for x in (one_line(tool, 30), stats) if x)
    room = width - t.cell_len - (len(tail) + 2 if tail else 0) - 2
    if room >= 8:
        task = Text(G._task(mon, state, r), style=cc.WARN if state in ("human", "error") else cc.SUBTLE)
        task.truncate(room, overflow="ellipsis")
        t.append("  ")
        t.append_text(task)
    if tail:
        t.append("  ")
        t.append(tail, style=cc.DIM)
    t.truncate(max(1, width), overflow="ellipsis")
    return t


def panel_head(mon: G.AgentMonitor, *, focused: bool = False, now: float | None = None) -> Text | None:
    """`2 agent đang chạy · ⚠ 1 chờ bạn duyệt · ↓ để chọn · click để xem`; None khi không có gì."""
    act = mon.active(now)
    c = mon.counts()
    if not (act or c["approvals_human"] or c["questions_human"] or c["manager"]):
        return None
    running = sum(1 for s, _ in act if s in ("running", "approval", "human", "manager"))
    queued = sum(1 for s, _ in act if s == "queued")
    errors = sum(1 for s, _ in act if s == "error")
    t = Text()
    parts: list[tuple[str, str]] = [(f"{running} agent đang chạy", cc.DIM)]
    if queued:
        parts.append((f"{queued} chờ slot", cc.DIM))
    if errors:
        parts.append((f"{errors} lỗi", cc.ERR))
    if c["approvals_human"]:
        parts.append((f"⚠ {c['approvals_human']} chờ bạn duyệt", cc.WARN))
    if c["questions_human"]:
        parts.append((f"? {c['questions_human']} câu hỏi chờ bạn", cc.WARN))
    if c["manager"]:
        parts.append((f"{c['manager']} điều phối viên đang xử lý", cc.DIM))
    parts.append(("enter để xem · esc về ô nhập" if focused else "↓ để chọn · click để xem", cc.DIM))
    for i, (s, st) in enumerate(parts):
        if i:
            t.append(" · ", style=cc.DIM)
        t.append(s, style=st)
    return t


# ============================================================================ lịch sử ô nhập (cùng tệp/định dạng FileHistory của REPL)
def load_history(path: Path) -> list[str]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[str] = []
    cur: list[str] = []
    for ln in raw.split("\n"):
        if ln.startswith("+"):
            cur.append(ln[1:])
        elif cur:
            out.append("\n".join(cur))
            cur = []
    if cur:
        out.append("\n".join(cur))
    return out[-HISTORY_MAX:]


def append_history(path: Path, text: str) -> None:
    try:  # nhị phân, "\n" như `FileHistory` (text mode trên Windows ghi "\r\n" → REPL đọc ra chữ có "\r")
        with open(path, "ab") as f:
            f.write((f"\n# {datetime.now()}\n" + "".join(f"+{ln}\n" for ln in text.split("\n"))).encode("utf-8"))
    except OSError:
        pass


# ============================================================================ trạng thái + phiên
class LiveChatState(V.ChatState):
    """`ChatState` + dòng tool của điều phối viên (`tool`/`tool_done`) và đoạn chữ viết trước mỗi tool (`interim`).
    REPL không cần (chỉ hiện tool đang chạy ở thanh trạng thái)."""

    def apply(self, ev: dict[str, Any]) -> list[dict[str, Any]]:
        t = str(ev.get("type") or "")
        p = ev.get("payload") or {}
        rid = ev.get("run_id")
        pre: list[dict[str, Any]] = []
        if rid in self.runs and t == "tool.start":
            if not ev.get("subagent_id") and self.draft.strip():
                pre.append({"kind": "interim", "text": self.draft, "run_id": rid})
                self.draft, self._msg_done = "", False
            pre.append({"kind": "tool", "tool_id": str(p.get("tool_id") or ""), "name": p.get("name"), "args": p.get("args_preview"),
                        "input": p.get("input"), "run_id": rid, "sub": bool(ev.get("subagent_id")), "started": time.monotonic()})
        elif rid in self.runs and t == "tool.complete":
            pre.append({"kind": "tool_done", "tool_id": str(p.get("tool_id") or ""), "name": p.get("name"), "duration_s": p.get("duration_s"),
                        "is_error": bool(p.get("is_error")), "result": p.get("result_preview"), "run_id": rid, "sub": bool(ev.get("subagent_id"))})
        return pre + super().apply(ev)


class ScreenOut(R.Out):
    """Đầu ra của `ChatSession` → widget của `ChatScreen`."""

    def __init__(self, screen: "ChatScreen") -> None:
        super().__init__(color=True, file=io.StringIO(), width=120)
        self.screen = screen

    def write(self, renderable: Any, meta: dict[str, Any] | None = None) -> None:
        self.screen.add(renderable, meta)

    def hold(self) -> None:  # `/attach` mở màn ngay trong app — không nhường terminal
        pass

    def release(self) -> None:
        pass

    def clear(self) -> None:
        self.screen.clear_log()


class TuiChatSession(R.ChatSession):
    state_cls = LiveChatState

    def __init__(self, screen: "ChatScreen", client: AsyncHubClient, **kw: Any) -> None:
        super().__init__(client, ScreenOut(screen), **kw)
        self.screen = screen

    def banner(self, manager_id: str, conversation_id: str | None, *, title: str | None = None, resumed: int = 0) -> None:
        name = self.state.name if self.state is not None else manager_id
        self.screen.add_welcome(welcome_text(name, conversation_id, title=title, resumed=resumed))

    def _emit(self, item: dict[str, Any]) -> None:
        self.screen.add_item(item)

    def on_hub_event(self, ev: dict[str, Any]) -> None:
        super().on_hub_event(ev)
        self.screen.app.board_event(ev)  # type: ignore[attr-defined]

    async def cmd_attach(self, arg: str) -> None:
        ref = arg.strip()
        if not ref or ref.startswith("-") or " " in ref:
            self._note("cần id agent hoặc run: /attach <agent|run>", "warn")
            return
        if self.mon is not None:
            r = self.mon.find_agent(ref)
            if r is not None:
                ref = r.id
        await self.screen.app._open_attach(ref)  # type: ignore[attr-defined]


# ============================================================================ widget
class Item(Static):
    """Một mục của transcript; `meta` = mục gốc (kind, id…) để biết click làm gì."""

    def __init__(self, renderable: Any, meta: dict[str, Any] | None = None, *, classes: str = "item") -> None:
        super().__init__(renderable, classes=classes)
        self.meta = meta or {}

    async def on_click(self, event: events.Click) -> None:
        if self.screen.get_selected_text():  # vừa kéo chọn chữ — không coi là click
            return
        await self.screen.item_clicked(self)  # type: ignore[attr-defined]


class ToolItem(Item):
    def __init__(self, meta: dict[str, Any]) -> None:
        super().__init__("", meta, classes="item tool")
        self.expanded = False
        self.redraw()

    def redraw(self, *, expand_all: bool = False, blink: bool = True) -> None:
        self.update(tool_text(self.meta, expanded=self.expanded or expand_all, blink=blink))


class JumpBar(Static):
    def on_click(self) -> None:
        self.screen.action_to_end()  # type: ignore[attr-defined]


class ChatInput(Input):
    """Ô nhập: `?` khi ô TRỐNG → bật/tắt bảng phím tắt (như Claude Code) thay vì gõ chữ `?`."""

    async def _on_key(self, event: events.Key) -> None:
        if event.character == "?" and not self.value:
            event.prevent_default()  # chặn `Input._on_key` (handler của lớp cha trong MRO) — không chèn `?`
            event.stop()
            self.screen.toggle_shortcuts()  # type: ignore[attr-defined]


class AgentList(OptionList):
    """Bảng agent đang chạy (dưới ô nhập). ↑ ở dòng đầu / Esc → về ô nhập."""

    BINDINGS = [Binding("escape", "leave", "Về ô nhập", show=False)]

    def action_leave(self) -> None:
        self.screen.focus_input()  # type: ignore[attr-defined]

    def action_cursor_up(self) -> None:
        if self.highlighted in (None, 0):
            self.screen.focus_input()  # type: ignore[attr-defined]
            return
        super().action_cursor_up()

    def on_focus(self) -> None:
        self.screen.render_panel()  # type: ignore[attr-defined]

    def on_blur(self) -> None:
        self.screen.render_panel()  # type: ignore[attr-defined]


class BoardScreen(HtopScreen):
    """Bảng agent đầy đủ của `top` mở từ chat (Ctrl-G); Esc/q/m → về chat (không thoát app)."""

    BINDINGS = [
        Binding("escape", "back", "Về chat"),
        Binding("q", "back", "Về chat"),
        Binding("m", "back", "Về chat", show=False),
    ]

    def action_back(self) -> None:
        self.app.pop_screen()


# ============================================================================ màn chat
class ChatScreen(Screen):
    DEFAULT_CSS = CSS
    BINDINGS = [
        Binding("ctrl+c", "ctrl_c", "Copy / dừng / thoát", priority=True, show=False),
        Binding("ctrl+d", "app.quit", "Thoát", show=False),
        Binding("escape", "escape", "Dừng lượt", show=False),
        Binding("ctrl+o", "expand_all", "Mở rộng tool", show=False),
        Binding("ctrl+g", "board", "Bảng agent", show=False),
        Binding("ctrl+n", "new_conv", "Hội thoại mới", show=False),
        Binding("ctrl+v", "paste_image", "Dán ảnh", priority=True, show=False),
        Binding("pageup", "page(-1)", "Cuộn lên", show=False),
        Binding("pagedown", "page(1)", "Cuộn xuống", show=False),
        Binding("ctrl+end", "to_end", "Xuống cuối", show=False),
    ]

    def __init__(self, *, home: Path | None = None, conversation_id: str | None = None, new: bool = False) -> None:
        super().__init__()
        self.home = home
        self.want_conv = conversation_id
        self.new = new
        self.session: TuiChatSession | None = None
        self.tools: dict[str, ToolItem] = {}
        self.expand_all = False
        self._unseen = False
        self._last_ctrl_c = 0.0
        self._send_lock = asyncio.Lock()
        self._panel_ids: list[str] = []
        self._panel_at = 0.0
        hist = Path(R._history_file(home)) if home else None
        self.hist_path = hist
        self.history = load_history(hist) if hist else []
        self.hist_pos: int | None = None
        self.hist_draft = ""

    # ------------------------------------------------------------------ dựng
    def compose(self) -> ComposeResult:
        with VerticalScroll(id="chat-log", can_focus=False):
            yield Static(id="chat-draft")
        yield JumpBar(id="chat-jump")
        yield Static(id="chat-status")
        with Horizontal(id="chat-input-box"):
            yield Static("> ", id="chat-prompt")
            yield ChatInput(placeholder=PLACEHOLDER, id="chat-input", compact=True,
                            suggester=SuggestFromList(sorted(set(R.COMMANDS)), case_sensitive=False))
        yield Static(id="chat-hint")
        yield Static(shortcuts_text(), id="chat-keys")
        with Horizontal(id="chat-foot"):
            yield Static(Text("? phím tắt"), id="chat-foot-left")
            yield Static(id="chat-foot-right")
        with Vertical(id="chat-agents-box"):
            yield Static(id="chat-agents-head")
            yield AgentList(id="chat-agents", compact=True)

    async def on_mount(self) -> None:
        self.query_one("#chat-draft").display = False
        self.focus_input()
        self.set_interval(TICK_S, self._tick)
        app = self.app
        s = TuiChatSession(self, app.client, conversation_id=self.want_conv, new=self.new, home=self.home)  # type: ignore[attr-defined]
        s.ask, s.bell = self._ask, app.bell
        self.session = s
        try:
            ok = await s.open()
        except Exception as e:  # daemon lỗi / hội thoại không có
            self.add_item({"kind": "note", "text": R._err(e), "tone": "err"})
            ok = False
        if not ok:
            self.query_one("#chat-input", Input).disabled = True
        elif s.mon is not None:
            app.model = s.mon.model  # type: ignore[attr-defined]  # bảng agent / attach đọc cùng model với phiên chat
        self._render_foot()
        self._scroll_end()

    def focus_input(self) -> None:
        try:
            self.query_one("#chat-input", Input).focus()
        except NoMatches:
            pass

    # ------------------------------------------------------------------ transcript
    @property
    def chat_log(self) -> VerticalScroll:
        return self.query_one("#chat-log", VerticalScroll)

    def _at_end(self) -> bool:
        log = self.chat_log
        return log.scroll_y >= log.max_scroll_y - 1

    def _scroll_end(self) -> None:
        self.call_after_refresh(lambda: self.chat_log.scroll_end(animate=False))

    def _mount(self, w: Static) -> None:
        log = self.chat_log
        follow = self._at_end()
        log.mount(w, before=self.query_one("#chat-draft"))
        kids = [k for k in log.children if isinstance(k, Item)]
        for k in kids[: max(0, len(kids) - MAX_ITEMS)]:
            if isinstance(k, ToolItem):
                self.tools.pop(str(k.meta.get("tool_id")), None)
            k.remove()
        if follow:
            self._scroll_end()
        else:
            self._unseen = True

    def add_welcome(self, text: Text) -> None:
        """Hộp chào (thay dòng `banner` của REPL); mục đầu transcript, viền tròn cam."""
        has_items = any(isinstance(k, Item) for k in self.chat_log.children)
        self._mount(Item(text, {"kind": "welcome"}, classes="item welcome" if has_items else "welcome"))

    def add(self, renderable: Any, meta: dict[str, Any] | None = None) -> None:
        """Đầu ra trực tiếp của phiên (thẻ duyệt/câu hỏi, bảng /agents, /help…). Thẻ duyệt/câu hỏi: hộp viền như hộp xin phép
        của Claude Code, click được."""
        kind = (meta or {}).get("kind")
        r = flatten(renderable)
        cls = "item"
        if kind in ("approval_card", "question_card"):
            cls = "item clickable" if (meta or {}).get("routed") == "manager" else "item clickable card"
            r = _strip_blank_head(r)
        elif kind == "action":
            cls = "item clickable"
        self._mount(Item(r, meta, classes=cls))

    def add_item(self, it: dict[str, Any]) -> None:
        k = it.get("kind")
        if k == "tool":
            w = ToolItem(dict(it))
            self.tools[str(it.get("tool_id"))] = w
            if self.expand_all:
                w.redraw(expand_all=True)
            self._mount(w)
            return
        if k == "tool_done":
            w = self.tools.get(str(it.get("tool_id")))
            if w is None:  # mất tool.start (vd nối lại giữa chừng)
                w = ToolItem({**it, "kind": "tool"})
                self.tools[str(it.get("tool_id"))] = w
                self._mount(w)
            w.meta.update({k2: v for k2, v in it.items() if k2 not in ("kind", "tool_id")}, done=True)
            w.redraw(expand_all=self.expand_all)
            if self._at_end():
                self._scroll_end()
            return
        if k in ("interim", "manager"):  # chữ đang chạy vừa thành mục cố định → ẩn ngay (không đợi nhịp vẽ, tránh hiện hai lần)
            self.query_one("#chat-draft").display = False
        if k == "interim":
            self._mount(Item(text_item(it.get("text")), it))
            return
        cls = "item"
        if k == "action":
            cls = "item clickable"
        elif k in ("note", "steer") and not (k == "note" and it.get("tone") in ("warn", "err")):
            cls = "item tight"  # `  ⎿  ghi chú` dính vào mục trên, như Claude Code
        self._mount(Item(item_text(it), it, classes=cls))
        if k == "manager":  # lượt xong → spinner tắt ngay, không đợi nhịp vẽ
            self._tick()

    def clear_log(self) -> None:
        for k in [k for k in self.chat_log.children if isinstance(k, Item)]:
            k.remove()
        self.tools.clear()

    # ------------------------------------------------------------------ nhịp vẽ
    def _tick(self) -> None:
        s = self.session
        st = s.state if s else None
        try:
            width = max(40, self.size.width - 2)
            status = self.query_one("#chat-status", Static)
            draft = self.query_one("#chat-draft", Static)
        except NoMatches:
            return
        now = time.monotonic()
        if st is not None and st.busy:
            el = now - st.busy_since
            if st.waiting_approval:
                spin = cc.spinner_text(f"Chờ bạn duyệt {one_line(st.waiting_approval, 40)}", elapsed_s=el, frame=int(el * 8))
            else:
                hint = f"⚙ {one_line(short_tool(st.activity), 32)}" if st.activity else None
                spin = cc.spinner_text("Đang nghĩ", elapsed_s=el, frame=int(el * 8), hint=hint)
            status.update(spin)
            status.display = True
            live = st.draft.strip()
            if live:
                follow = self._at_end()
                draft.update(text_item(st.draft, live=True))
                draft.display = True
                if follow:
                    self._scroll_end()
            elif draft.display:
                draft.display = False
        else:
            if status.display:
                status.display = False
            if draft.display:
                draft.display = False
        blink = int(now * 2) % 2 == 0
        for w in self.tools.values():
            if not w.meta.get("done"):
                w.redraw(expand_all=self.expand_all, blink=blink)
        if self._at_end():
            self._unseen = False
        jump = self.query_one("#chat-jump", Static)
        jump.display = self._unseen
        if self._unseen:
            jump.update(Text("↓ có tin mới bên dưới · click hoặc ctrl+end", style=cc.DIM))
        if now - self._panel_at >= 0.5:
            self._panel_at = now
            self.render_panel(width)
            self._render_foot()
        if s is not None:
            s.out.console.width = width

    def _render_foot(self) -> None:
        s = self.session
        st = s.state if s else None
        t = Text()
        if st is not None:
            t.append(f"{st.name} · hội thoại {st.conversation_id or '(mới)'}", style=cc.DIM)
        try:
            self.query_one("#chat-foot-right", Static).update(t)
        except NoMatches:
            pass

    def _sync_foot(self) -> None:
        """Dòng dưới ô nhập: menu lệnh / bảng phím tắt thay chỗ dòng `? phím tắt`."""
        try:
            hint = self.query_one("#chat-hint", Static)
            keys = self.query_one("#chat-keys", Static)
            foot = self.query_one("#chat-foot")
            left = self.query_one("#chat-foot-left", Static)
            inp = self.query_one("#chat-input", Input)
        except NoMatches:
            return
        foot.display = not (hint.display or keys.display)
        left.update(Text("? phím tắt" if not inp.value else "", style=cc.DIM))

    def toggle_shortcuts(self) -> None:
        keys = self.query_one("#chat-keys", Static)
        keys.display = not keys.display
        if keys.display:
            self.query_one("#chat-hint").display = False
        self._sync_foot()

    def render_panel(self, width: int | None = None) -> None:
        width = width or max(40, self.size.width - 2)
        s = self.session
        mon = s.mon if s else None
        try:
            box = self.query_one("#chat-agents-box")
            ol = self.query_one("#chat-agents", AgentList)
        except NoMatches:
            return
        head = panel_head(mon, focused=ol.has_focus) if mon is not None else None
        if head is None:
            if box.display:
                box.display = False
                if ol.has_focus:
                    self.focus_input()
            return
        box.display = True
        self.query_one("#chat-agents-head", Static).update(head)
        rows = mon.active()  # type: ignore[union-attr]
        ids = [r.id for _, r in rows]
        shown = ids[:PANEL_ROWS]
        hl = ol.highlighted if ol.has_focus else None
        prompts = [agent_line(mon, st_, r, width - 2, selected=(i == hl)) for i, (st_, r) in enumerate(rows[:PANEL_ROWS])]  # type: ignore[arg-type]
        if shown != self._panel_ids:
            old = ol.highlighted
            ol.clear_options()
            for aid, p in zip(shown, prompts):
                ol.add_option(Option(p, id=aid))
            if len(ids) > PANEL_ROWS:
                ol.add_option(Option(Text(f"    +{len(ids) - PANEL_ROWS} nữa · ctrl+g bảng đầy đủ", style=cc.DIM), id="__more__"))
            self._panel_ids = shown
            if old is not None and ol.option_count:
                ol.highlighted = min(old, ol.option_count - 1)
        else:
            for i, p in enumerate(prompts):
                ol.replace_option_prompt_at_index(i, p)

    @on(OptionList.OptionHighlighted, "#chat-agents")
    def _agent_highlighted(self, _ev: OptionList.OptionHighlighted) -> None:
        self.render_panel()

    # ------------------------------------------------------------------ click
    async def item_clicked(self, w: Item) -> None:
        m = w.meta
        k = m.get("kind")
        if isinstance(w, ToolItem):
            w.expanded = not w.expanded
            w.redraw(expand_all=self.expand_all)
        elif k == "action":
            await open_target(self, m.get("target") or {})
        elif k == "approval_card":
            self.prefill(f"/approve {m.get('id')} ")
        elif k == "question_card":
            self.prefill(f"/answer {m.get('id')} ")

    def prefill(self, text: str) -> None:
        inp = self.query_one("#chat-input", Input)
        inp.value = text
        inp.cursor_position = len(text)
        inp.focus()

    @on(OptionList.OptionSelected, "#chat-agents")
    async def _open_agent(self, ev: OptionList.OptionSelected) -> None:
        aid = ev.option.id or ""
        if aid == "__more__":
            self.action_board()
        elif aid:
            await self.app._open_attach(aid)  # type: ignore[attr-defined]

    # ------------------------------------------------------------------ chọn chữ → copy
    def on_text_selected(self, _ev: events.TextSelected) -> None:
        text = self.get_selected_text()
        if text and text.strip():
            self.copy(text)

    def copy(self, text: str) -> None:
        self.app.copy_to_clipboard(text)  # OSC 52 (Windows Terminal, iTerm2, kitty, VS Code…)
        self.run_worker(asyncio.to_thread(A.clipboard_set, text), group="clip", exit_on_error=False)  # dự phòng: clip.exe/pbcopy/wl-copy
        n = len(text)
        self.notify(f"Đã copy {n} ký tự", timeout=2)

    # ------------------------------------------------------------------ ô nhập
    @on(Input.Changed, "#chat-input")
    def _changed(self, ev: Input.Changed) -> None:
        hint = self.query_one("#chat-hint", Static)
        v = ev.value
        if v:
            self.query_one("#chat-keys").display = False
        menu = slash_menu(v) if v.startswith("/") and " " not in v and not v.startswith("//") else None
        if menu is not None:
            hint.update(menu)
        hint.display = menu is not None
        self._sync_foot()

    @on(Input.Submitted, "#chat-input")
    async def _submit(self, ev: Input.Submitted) -> None:
        text = ev.value
        if not text.strip() or self.session is None or self.session.state is None:
            return
        ev.input.value = ""
        self.hist_pos = None
        if not self.history or self.history[-1] != text:
            self.history.append(text)
            if self.hist_path:
                append_history(self.hist_path, text)
        self._scroll_end()
        self.run_worker(self._handle(text), group="chat-send", exit_on_error=False)

    async def _handle(self, text: str) -> None:
        s = self.session
        assert s is not None
        async with self._send_lock:
            keep = await s.handle(text)
        self._render_foot()
        if not keep:
            await self.app.action_quit()

    def on_key(self, event: events.Key) -> None:
        inp = self.query_one("#chat-input", Input)
        if not inp.has_focus or event.key not in ("up", "down"):
            return
        event.prevent_default()
        event.stop()
        if event.key == "up":
            if not self.history:
                return
            if self.hist_pos is None:
                self.hist_draft, self.hist_pos = inp.value, len(self.history)
            self.hist_pos = max(0, self.hist_pos - 1)
            self._set_input(self.history[self.hist_pos])
            return
        if self.hist_pos is not None:  # đang lướt lịch sử → đi tới
            self.hist_pos += 1
            if self.hist_pos >= len(self.history):
                self.hist_pos = None
                self._set_input(self.hist_draft)
            else:
                self._set_input(self.history[self.hist_pos])
            return
        if not inp.value and self.query_one("#chat-agents-box").display:
            ol = self.query_one("#chat-agents", AgentList)
            ol.focus()
            if ol.option_count and ol.highlighted is None:
                ol.highlighted = 0

    def _set_input(self, text: str) -> None:
        inp = self.query_one("#chat-input", Input)
        inp.value = text
        inp.cursor_position = len(text)

    async def _ask(self, prompt: str) -> str:
        """y/N của `ChatSession` (/approve, /always, /answer xin phép, /stop agent) → hộp xác nhận. Chữ có id/tên do agent đặt → escape markup."""
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()

        def done(ok: bool | None) -> None:
            if not fut.done():
                fut.set_result("y" if ok else "")

        self.app.push_screen(ConfirmScreen(escape(prompt.strip()), yes="Đồng ý (y)"), done)
        return await fut

    # ------------------------------------------------------------------ phím
    async def action_ctrl_c(self) -> None:
        sel = self.get_selected_text()
        if sel:
            self.copy(sel)
            self.clear_selection()
            return
        st = self.session.state if self.session else None
        if st is not None and st.busy:
            await self.session.stop_turn()  # type: ignore[union-attr]
            return
        now = time.monotonic()
        if now - self._last_ctrl_c < CTRL_C_WINDOW_S:
            await self.app.action_quit()
            return
        self._last_ctrl_c = now
        self.notify("Ctrl-C lần nữa (hoặc Ctrl-D) để thoát", timeout=CTRL_C_WINDOW_S)

    async def action_escape(self) -> None:
        for wid in ("#chat-hint", "#chat-keys"):
            w = self.query_one(wid)
            if w.display:
                w.display = False
                self._sync_foot()
                return
        st = self.session.state if self.session else None
        if st is not None and st.busy:
            await self.session.stop_turn()  # type: ignore[union-attr]

    def action_expand_all(self) -> None:
        self.expand_all = not self.expand_all
        for w in self.tools.values():
            w.redraw(expand_all=self.expand_all)

    def action_board(self) -> None:
        self.app.open_board()  # type: ignore[attr-defined]

    async def action_new_conv(self) -> None:
        if self.session is not None and self.session.state is not None:
            await self.session.handle("/new")
            self._render_foot()

    def action_page(self, direction: int) -> None:
        log = self.chat_log
        log.scroll_to(y=log.scroll_y + direction * max(1, log.size.height - 2), animate=False)

    def action_to_end(self) -> None:
        self._unseen = False
        self.chat_log.scroll_end(animate=False)

    def action_paste_image(self) -> None:
        """Ctrl-V: ảnh trong clipboard → `[Ảnh #n]` (gửi kèm tin); không có ảnh → dán chữ."""
        s = self.session
        if s is None:
            return
        inp = self.query_one("#chat-input", Input)
        img = A.clipboard_image()
        if img:
            if len(img) > A.MAX_IMAGE_BYTES:
                self.add_item({"kind": "note", "text": f"ảnh trong clipboard quá lớn ({len(img) // 1024} KB)", "tone": "warn"})
                return
            n = max(s.pasted, default=0) + 1
            s.pasted[n] = A.Attachment(f"clipboard-{n}.png", img)
            inp.insert_text_at_cursor(A.MARKER.format(n=n) + " ")
        else:
            txt = A.clipboard_text()
            if txt:
                inp.insert_text_at_cursor(txt)
        inp.focus()


# ============================================================================ app
class ChatApp(TopApp):
    SUB_TITLE = "chat"
    CHANNEL = "cli"
    # CSS của app thắng DEFAULT_CSS của màn → nền màn chat (TopApp đặt `Screen { background: $surface }`) phải ghi đè ở đây
    CSS = TopApp.CSS + "\nChatScreen { background: $background; }\n"

    def __init__(self, sock_path: Path | str, *, home: Path | None = None, conversation_id: str | None = None, new: bool = False,
                 refresh_s: float = 1.0, takeover_exec: bool | None = None) -> None:
        super().__init__(sock_path, refresh_s=refresh_s, takeover_exec=takeover_exec)
        self.client = AsyncHubClient(sock_path, channel="cli")
        self.chat_args: dict[str, Any] = {"home": home, "conversation_id": conversation_id, "new": new}
        self.chat: ChatScreen | None = None
        self.htop: HtopScreen | None = None  # type: ignore[assignment]  # bảng agent khi đang mở (Ctrl-G)

    async def on_mount(self, event: events.Mount) -> None:
        # Textual gọi `on_mount` của MỌI lớp trong MRO → chặn của TopApp (nó đẩy bảng htop + chạy firehose/snapshot riêng).
        # `ChatSession` đã có firehose + snapshot (AgentMonitor) và `self.model` trỏ vào đó.
        event.prevent_default()
        self.chat = ChatScreen(**self.chat_args)
        await self.push_screen(self.chat)

    def open_board(self) -> None:
        self.htop = BoardScreen()
        self.push_screen(self.htop)

    async def refresh_snapshot(self) -> None:
        s = self.chat.session if self.chat else None
        if s is not None:
            await s.refresh_snapshot()

    async def _open_attach(self, ref: str) -> None:
        """Xem agent/run (bảng agent, `/attach`, thẻ hành động) bằng màn kiểu Claude Code thay cho `AttachScreen` của `top`."""
        from .agent_view import open_agent_view

        await open_agent_view(self, ref)

    def board_event(self, ev: dict[str, Any]) -> None:
        """Event firehose (đã áp vào model bởi `AgentMonitor`) → dòng hoạt động của bảng agent khi đang mở."""
        if self.htop is not None and self.htop.is_mounted:
            try:
                self.htop.hub_event(ev, ev.get("agent_id"))
            except NoMatches:
                pass

    async def action_quit(self) -> None:
        s = self.chat.session if self.chat else None
        if s is not None:
            await s.close()
        self.exit()


def run_chat(sock: Path, home: Path, *, conversation_id: str | None, new: bool) -> int:
    ChatApp(sock, home=home, conversation_id=conversation_id, new=new).run()
    return 0


__all__ = ["ChatApp", "ChatScreen", "LiveChatState", "flatten", "tool_text", "text_item", "item_text", "welcome_text", "slash_menu",
           "shortcuts_text", "agent_line", "panel_head", "load_history", "append_history", "run_chat"]
