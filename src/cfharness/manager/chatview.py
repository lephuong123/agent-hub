"""Chat với điều phối viên trên terminal — phần THUẦN dùng chung cho REPL (`cfharness chat`) và TUI (`top` → phím `m`).

- `ChatState`: theo dõi MỘT hội thoại (kênh `cli`): run của hội thoại (kể cả run tiếp nối sau approval, nhận qua
  `run.queued.parent_run_id`), chữ đang chạy (`message.delta`/`message.complete`), tool đang dùng, trạng thái bận/chờ duyệt.
  `apply(ev)` nhận event wire (`events.subscribe` agent = điều phối viên, level detail) → danh sách mục cần in.
- Mục (dict): `kind` ∈ user | manager | notify | action | approval | note | steer. `render(item)` → renderable `rich`.
- Mọi chữ của agent qua `termsafe.clean` + `rich.text.Text` (không markup): không chèn được ANSI/OSC, bidi, `[link=…]`.
Không I/O, không Textual — test thuần được.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from rich.console import Group
from rich.padding import Padding
from rich.text import Text

from ..termsafe import clean, one_line
from ..tui.model import short_tool
from ..ui import theme as th
from ..web.present import mgr_action_item, mgr_message

CHANNEL = "cli"
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
TERMINAL = ("run.completed", "run.failed", "run.cancelled", "run.interrupted")

# Style lấy từ bảng màu chung `ui/theme.py` (docs/dev-notes/cli-design.md §2) — chat, `top` và CLI là một sản phẩm
C_USER = th.USER
C_MGR = th.MGR
# Bảng agent + thẻ duyệt/câu hỏi của REPL (`agentsview.py`) — gom style ở đây để thay theme sau mà không sửa logic
C_DIM = th.MUTED
C_FAINT = th.MUTED
C_MID = th.MUTED
C_SOFT = th.MUTED
C_TOOL = th.INFO
C_BADGE = th.ALARM
C_CARD = f"bold {th.WARN}"
C_CARD_BAR = th.WARN
C_QUESTION = f"bold {th.INFO}"
C_OK = th.OK
C_WARN = th.WARN
C_ERR = th.ERR_STRONG
_AG = th.STATUS["agent"]
_QUEUED = th.STATUS["run"]["queued"]
# trạng thái một dòng agent: (biểu tượng, nhãn, style). Biểu tượng = `th.STATUS` (dùng chung với `top` và CLI)
AGENT_STATE: dict[str, tuple[str, str, str]] = {
    "running": (_AG["working"].icon, "đang chạy", _AG["working"].style),
    "queued": (_QUEUED.icon, "chờ slot", _QUEUED.style),
    "approval": (_AG["waiting"].icon, "chờ duyệt", _AG["waiting"].style),
    "human": (_AG["waiting_human"].icon, "chờ bạn duyệt", _AG["waiting_human"].style),
    "manager": (_AG["waiting"].icon, "điều phối viên đang xử lý", th.MGR),
    "error": (_AG["error"].icon, "lỗi", _AG["error"].style),
}


def _unicode() -> bool:
    """Ký hiệu unicode hay ASCII (`th.detect`): conhost cũ / code page gốc không phải UTF → ASCII (cli-design.md §6)."""
    return th.detect().unicode


def hhmm(ts: Any) -> str:
    """ISO UTC → giờ máy `HH:MM`; rỗng nếu không đọc được."""
    if not ts:
        return datetime.now().strftime("%H:%M")
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return d.astimezone().strftime("%H:%M")
    except ValueError:
        return ""


def history_items(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`manager.history` (cũ → mới) → mục để in lại."""
    out: list[dict[str, Any]] = []
    for r in rows:
        m = mgr_message(r) if isinstance(r, dict) else None
        if not m or m.get("tick"):
            continue
        if m["kind"] == "user":
            out.append({"kind": "user", "text": m["text"], "ts": m.get("ts"), "run_id": m.get("run_id")})
        else:
            out.append({"kind": "notify" if m.get("notify") else "manager", "text": m["text"], "ts": m.get("ts"), "ok": True,
                        "run_id": m.get("run_id")})
    return out


def timeline(rows: list[dict[str, Any]], action_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Tin của hội thoại + thẻ `manager.action` của các run thuộc hội thoại, theo thời gian (thẻ đứng trước trả lời cùng run)."""
    items = history_items(rows)
    runs = {r.get("run_id") for r in rows if isinstance(r, dict) and r.get("run_id")}
    acts = [mgr_action_item(e) for e in action_events if e.get("type") == "manager.action" and e.get("run_id") in runs]
    order = {"user": 0, "action": 1, "approval": 1, "manager": 2, "notify": 2}
    return sorted(items + acts, key=lambda it: (str(it.get("ts") or ""), order.get(str(it.get("kind")), 3)))


LIVE = ("queued", "running", "waiting_human")


def pending_run(rows: list[dict[str, Any]]) -> str | None:
    """Run của tin người CUỐI chưa có trả lời (ứng viên "đang làm" khi mở lại hội thoại)."""
    last_user = next((r for r in reversed(rows) if r.get("role") == "user" and r.get("run_id")), None)
    if last_user is None:
        return None
    if any(r.get("role") == "manager" and int(r.get("id") or 0) > int(last_user.get("id") or 0) for r in rows):
        return None
    return str(last_user["run_id"])


class ChatState:
    def __init__(self, manager_id: str, conversation_id: str | None = None, *, name: str | None = None) -> None:
        self.manager_id = manager_id
        self.name = name or manager_id
        self.conversation_id = conversation_id
        self.runs: set[str] = set()
        self.active_run: str | None = None
        self.busy = False
        self.busy_since = 0.0
        self.draft = ""
        self.activity = ""  # tool đang dùng
        self._msg_done = False
        self.waiting_approval: str | None = None
        self.last_seq = 0
        self.epoch: str | None = None
        # False: không trả mục approval (REPL in thẻ duyệt cho MỌI agent từ firehose — tránh in hai lần); trạng thái vẫn cập nhật
        self.show_approvals = True

    # ------------------------------------------------------------------ hành động của người
    def resume(self, rows: list[dict[str, Any]], run: dict[str, Any] | None) -> None:
        """Mở lại hội thoại: nhận các run đã biết; `run` (từ `run.get` của `pending_run`) còn sống → trạng thái bận."""
        self.runs.update(str(r["run_id"]) for r in rows if isinstance(r, dict) and r.get("run_id"))
        if run and run.get("status") in LIVE:
            self.busy = True
            self.active_run = run["id"]
            self.busy_since = time.monotonic()
            self.runs.add(run["id"])

    def switch(self, conversation_id: str | None) -> None:
        self.conversation_id = conversation_id
        self.runs.clear()
        self._idle()

    def sent(self, res: dict[str, Any], text: str) -> list[dict[str, Any]]:
        """Kết quả `manager.chat` → mục (tin người đã in ở phía nhập; đây chỉ cập nhật trạng thái + dòng steer)."""
        self.conversation_id = res.get("conversation_id") or self.conversation_id
        rid = res.get("run_id")
        if rid:
            self.runs.add(rid)
        if res.get("steered"):
            return [{"kind": "steer", "text": "đã chèn tin vào lượt điều phối viên đang làm"}]
        self.active_run = rid
        self.busy = True
        self.busy_since = time.monotonic()
        self.draft = ""
        self.activity = ""
        out: list[dict[str, Any]] = []
        if res.get("switched_from"):
            out.append({"kind": "note", "text": f"điều phối viên đã đổi — chuyển sang hội thoại {res['conversation_id']}"})
        return out

    def _idle(self) -> None:
        self.busy = False
        self.active_run = None
        self.draft = ""
        self._msg_done = False
        self.activity = ""
        self.waiting_approval = None

    # ------------------------------------------------------------------ event
    def apply(self, ev: dict[str, Any]) -> list[dict[str, Any]]:
        seq = ev.get("seq")
        if isinstance(seq, int) and (ev.get("epoch") in (None, self.epoch) or self.epoch is None):
            self.last_seq = max(self.last_seq, seq)
        t = str(ev.get("type") or "")
        p = ev.get("payload") or {}
        rid = ev.get("run_id")
        if t == "manager.reply":
            kind = p.get("kind") or "reply"
            if kind == "tick":
                return []
            cid = p.get("conversation_id")
            if kind == "notify" and cid is None:
                return [{"kind": "notify", "text": p.get("text"), "ts": ev.get("ts")}]
            if cid != self.conversation_id or self.conversation_id is None:
                return []
            item = {"kind": "notify" if kind == "notify" else "manager", "text": p.get("text"), "ts": ev.get("ts"),
                    "ok": p.get("ok", True) is not False, "outcome": p.get("outcome"), "run_id": p.get("run_id") or rid}
            if kind != "notify":
                self._idle()
            return [item]
        if t == "run.queued" and p.get("parent_run_id") and p.get("parent_run_id") in self.runs and rid:
            self.runs.add(rid)
            self.active_run = rid
            return []
        if rid not in self.runs:
            return []
        if t == "message.delta":
            if not ev.get("subagent_id"):
                if self._msg_done:  # tin mới của cùng lượt (sau tool) → chữ chạy bắt đầu lại
                    self.draft, self._msg_done = "", False
                self.draft += str(p.get("text") or "")
            return []
        if t == "message.complete":
            if not ev.get("subagent_id"):
                self.draft, self._msg_done = str(p.get("text") or ""), True
            return []
        if t == "tool.start":
            self.activity = str(p.get("name") or "")
            return []
        if t == "tool.complete":
            self.activity = ""
            return []
        if t == "manager.action":
            return [mgr_action_item(ev)]
        if t == "approval.open":
            self.waiting_approval = str(p.get("request_id") or "")
            if not self.show_approvals:
                return []
            return [{"kind": "approval", "request_id": self.waiting_approval, "preview": p.get("preview"), "reason": p.get("reason"),
                     "tool": p.get("tool")}]
        if t == "approval.waiting_human":
            if not self.show_approvals:
                return []
            return [{"kind": "note", "text": f"lượt tạm dừng chờ bạn duyệt {p.get('request_id')} — duyệt/từ chối xong điều phối viên tự làm tiếp",
                     "tone": "warn"}]
        if t == "approval.resolved":
            self.waiting_approval = None
            if not self.show_approvals:
                return []
            dec = {"allowed": "đã cho phép", "denied": "đã từ chối", "cancelled": "đã huỷ"}.get(str(p.get("decision")), str(p.get("decision")))
            return [{"kind": "note", "text": f"yêu cầu {p.get('request_id')}: {dec}" + (f" (qua {p.get('channel')})" if p.get("channel") else "")}]
        return []

    # ------------------------------------------------------------------ trạng thái (thanh dưới / spinner)
    def status_text(self, width: int = 100, *, busy_hint: str = "Ctrl-C dừng", idle_hint: str = "/help lệnh · Ctrl-D thoát") -> Text:
        """Một dòng trạng thái: bận → spinner + thời gian + tool/chữ đang chạy; rảnh → gợi ý."""
        t = Text()
        if self.busy:
            el = time.monotonic() - self.busy_since
            t.append(f" {SPINNER[int(el * 8) % len(SPINNER)]} ", style=C_MGR)
            if self.waiting_approval:
                t.append(f"chờ bạn duyệt {self.waiting_approval}", style=f"bold {th.WARN}")
            else:
                t.append("đang nghĩ…", style="magenta")
            t.append(f" {int(el)}s", style=th.MUTED)
            if self.activity:
                t.append(f"  ⚙ {one_line(short_tool(self.activity), 32)}", style=th.MUTED)
            hint = f"  {busy_hint}"
            room = width - len(t.plain) - len(hint) - 3
            tail = one_line(self.draft.rsplit("\n", 1)[-1] if self.draft.strip() else "", room) if room >= 12 else ""
            if tail:
                t.append(f"  {tail}", style=f"italic {th.MUTED}")
            t.append(hint, style=th.MUTED)
        else:
            t.append(" ◆ ", style=C_MGR)
            t.append(self.name, style=th.HEAD)
            t.append(f"  · hội thoại {self.conversation_id or '(mới)'}", style=th.MUTED)
            t.append(f"   {idle_hint}", style=th.MUTED)
        return t


# ---------------------------------------------------------------------- vẽ mục
def _header(label: str, style: str, ts: Any, extra: str = "") -> Text:
    h = Text()
    h.append(label, style=style)
    if extra:
        h.append(f" {extra}", style=th.MUTED)
    tm = hhmm(ts)
    if tm:
        h.append(f"  {tm}", style=th.MUTED)
    return h


def _body(text: Any, style: str = "") -> Padding:
    """Thân tin: chữ đã làm sạch, thụt 2 cột (xuống dòng mềm vẫn thẳng hàng)."""
    return Padding(Text(clean(text).rstrip("\n") or "(trống)", style=style), (0, 0, 0, 2), expand=False)


def render(item: dict[str, Any], *, manager_name: str = "Điều phối viên") -> Any:
    k = item.get("kind")
    if k == "user":
        return Group(Text(""), _header("❯ Bạn", C_USER, item.get("ts")), _body(item.get("text")))
    if k == "manager":
        ok = item.get("ok", True)
        if not ok:
            return Group(Text(""), _header("◆ " + manager_name, th.ERR_STRONG, item.get("ts"), "— không trả lời được"),
                         _body(item.get("text"), th.ERR))
        return Group(Text(""), _header("◆ " + manager_name, C_MGR, item.get("ts")), _body(item.get("text")))
    if k == "notify":
        return Group(Text(""), _header("✉ Thông báo từ " + manager_name, f"bold {th.WARN}", item.get("ts")), _body(item.get("text")))
    if k == "action":
        t = Text("  ")
        ok = item.get("ok", True)
        u = _unicode()
        t.append(f"{th.glyph('ok', u)} " if ok else f"{th.glyph('err', u)} ", style=th.OK if ok else th.ERR)
        t.append(one_line(item.get("summary") or item.get("tool") or "hành động", 140), style=th.MUTED if ok else th.ERR)
        tgt = item.get("target") or {}
        if tgt.get("label"):
            t.append(f"  → {one_line(tgt['label'], 60)}", style=th.MUTED)
        if not ok and item.get("error"):
            t.append(f"  ({one_line(item['error'], 120)})", style=th.MUTED)
        return t
    if k == "approval":
        rid = one_line(item.get("request_id"), 40)
        t = Text(f"  {th.STATUS['approval']['waiting_human'].icon} ", style=f"bold {th.WARN}")
        t.append("Điều phối viên cần bạn duyệt: ", style=th.WARN)
        t.append(one_line(item.get("preview") or item.get("tool") or "", 160))
        if item.get("reason"):
            t.append(f"\n     lý do: {one_line(item['reason'], 160)}", style=th.MUTED)
        t.append("\n     ")
        t.append(th.hint(f"duyệt: `cfharness approve {rid}` · từ chối: `cfharness deny {rid}`", th.detect()))
        return t
    if k == "steer":
        return Text(f"  {th.STATUS['run']['resumed'].icon} {item.get('text')}", style=th.MUTED)
    tone = {"warn": th.WARN, "err": th.ERR}.get(str(item.get("tone") or ""), th.MUTED)
    return Text(f"  {th.glyph('bullet', _unicode())} {clean(item.get('text'))}", style=f"italic {tone}")


REPL_KEYS = (("Enter", "gửi"), ("Ctrl-V", "dán ảnh"), ("/help", "lệnh"), ("Ctrl-C", "dừng lượt"), ("Ctrl-D", "thoát"))
TUI_KEYS = (("Enter", "gửi"), ("Ctrl-V", "dán ảnh"), ("Ctrl-T", "dừng lượt"), ("Ctrl-N", "hội thoại mới"), ("Esc", "về bảng"))


def banner(manager_id: str, conversation_id: str | None, *, title: str | None = None, resumed: int = 0,
           keys: tuple[tuple[str, str], ...] = REPL_KEYS) -> Text:
    t = Text()
    t.append("◆ ", style=C_MGR)
    t.append("Chat với điều phối viên ", style=th.HEAD)
    t.append(manager_id, style=C_MGR)
    t.append("\n  ")
    if conversation_id:
        t.append(f"hội thoại {conversation_id}", style=th.MUTED)
        if title:
            t.append(f" · {one_line(title, 50)}", style=th.MUTED)
        if resumed:
            t.append(f" · {resumed} tin trước", style=th.MUTED)
    else:
        t.append("hội thoại mới", style=th.MUTED)
    t.append("\n  ")
    for i, (k, d) in enumerate(keys):
        if i:
            t.append(" · ", style=th.MUTED)
        t.append(k, style=th.HEAD)
        t.append(f" {d}", style=th.MUTED)
    return t


HELP = [
    ("/new", "mở hội thoại mới"),
    ("/list", "các hội thoại (kênh cli — dùng chung với TUI)"),
    ("/open <id>", "chuyển sang hội thoại"),
    ("/history [n]", "in lại n tin gần nhất (mặc định 20)"),
    ("/stop [agent|run]", "dừng lượt điều phối viên; kèm agent/run = dừng run đó (y/N)"),
    ("/agents", "bảng mọi agent: trạng thái, task, tool, token, $ (= /ps)"),
    ("/attach <agent|run>", "xem trực tiếp run của agent (màn attach của top)"),
    ("/approvals", "yêu cầu duyệt + câu hỏi đang chờ"),
    ("/approve <id> [ghi chú]", "cho phép MỘT lần (hiện lại lệnh đầy đủ, hỏi y/N)"),
    ("/deny <id> [lý do]", "từ chối yêu cầu duyệt"),
    ("/always <id>", "luôn cho phép (chỉ khi hub cho phép; xác nhận 2 bước)"),
    ("/answer <id> 1,2 [chữ]", "trả lời câu hỏi của agent"),
    ("/clear", "xoá màn hình"),
    ("/help", "trợ giúp"),
    ("/quit", "thoát (hoặc Ctrl-D)"),
]
COMMANDS = [c.split()[0] for c, _ in HELP] + ["/exit", "/ps"]


def help_text() -> Text:
    t = Text()
    for c, d in HELP:
        t.append(f"  {c:<24}", style=th.HEAD)
        t.append(f"{d}\n", style=th.MUTED)
    t.append("  Gõ khi điều phối viên đang làm = chèn thêm ý vào lượt đó (steer).\n", style=th.MUTED)
    t.append("  Kéo thư mục/file vào = gửi đường dẫn; kéo file ảnh vào hoặc Ctrl-V (ảnh trong clipboard) = gửi kèm ảnh (≤ 4, ≤ 5 MB).\n", style=th.MUTED)
    t.append("  Muốn gửi chữ bắt đầu bằng `/` mà trùng tên lệnh: gõ `//…`.\n", style=th.MUTED)
    t.append("  ↓ (dòng nhập trống) mở/đóng bảng agent đang chạy; Esc đóng.\n", style=th.MUTED)
    t.append("  Agent cần bạn duyệt/hỏi bạn → thẻ hiện ngay (kèm chuông): lệnh ĐẦY ĐỦ + cách xử lý.\n", style=th.MUTED)
    return t


def conv_list_text(rows: list[dict[str, Any]], current: str | None) -> Text:
    t = Text()
    if not rows:
        t.append("  (chưa có hội thoại nào — gõ tin để bắt đầu)\n", style=th.MUTED)
        return t
    for c in rows:
        mark = "▸ " if c.get("id") == current else "  "
        t.append(mark, style=C_MGR)
        t.append(f"{c.get('id')}", style=th.HEAD if c.get("id") == current else "")
        t.append(f"  {c.get('messages', 0):>3} tin · {hhmm(c.get('updated_at'))} {str(c.get('updated_at') or '')[:10]}", style=th.MUTED)
        if c.get("title"):
            t.append(f"  {one_line(c['title'], 60)}", style=th.MUTED)
        t.append("\n")
    return t


__all__ = ["REPL_KEYS", "TUI_KEYS", "ChatState", "timeline", "pending_run", "render", "banner", "help_text", "conv_list_text", "history_items", "CHANNEL", "COMMANDS", "HELP", "hhmm"]
