"""Bảng agent đang chạy + thẻ duyệt/câu hỏi cho `cfharness chat` — phần THUẦN (không I/O, không prompt_toolkit), test trực tiếp.

Dữ liệu (client thuần của daemon, KHÔNG RPC mới — y như `cfharness top`):
- `tui.snapshot` (~1 s) → `TopModel.load` (tui/model.py), cộng firehose `events.subscribe` mức summary → `TopModel.apply`.
- `question.list {status: pending}` lúc mở + khi có event `question.*` (snapshot không có câu hỏi).
- `AgentMonitor.apply(ev)` trả danh sách THÔNG BÁO cho REPL: `{"kind": "approval"|"question"|"note", "id", "bell", "routed", "header"}`
  — REPL tự lấy chi tiết (`approval.get` / `question.get`) rồi in thẻ.

Định tuyến (hợp đồng với luồng chính sách duyệt — trường/event có thể CHƯA có, code chạy được cả hai trường hợp):
- approval có `routed_to` ∈ "manager" | "human" | None; event `approval.delegated` (giao điều phối viên) / `approval.escalated`
  (điều phối viên → người). `routed_to == "manager"` → "điều phối viên đang xử lý", KHÔNG chuông/huy hiệu. Mọi trường hợp khác
  (None/"human"/không có trường) = cần người.
- câu hỏi: `status == "with_manager"` (hoặc `routed_to == "manager"`) → điều phối viên đang xử lý; `question.escalated` → người.

Mọi chữ của agent (tên, task, lệnh, lý do, câu hỏi) là DỮ LIỆU: qua `termsafe.clean/one_line` + `rich.text.Text` (không markup).
"""

from __future__ import annotations

import time
from typing import Any

from rich.cells import cell_len
from rich.console import Group
from rich.table import Table
from rich.text import Text

from ..termsafe import clean, one_line
from ..tui.model import TopModel, cost_cell, fmt_dur, last_tool_cell, parse_ts, short_tool, tok_cell
from ..web.present import action_view
from . import chatview as V

ERROR_TTL_S = 600.0  # agent lỗi trong phiên này hiện trong bảng 10 phút (tới khi chạy run mới)
MAX_PANEL_ROWS = 12
APPROVAL_EVENTS = ("approval.open", "approval.waiting_human", "approval.delegated", "approval.escalated", "approval.resolved",
                   "approval.reminder")
QUESTION_EVENTS = ("question.open", "question.escalated", "question.answered", "question.cancelled")
_ROW_TEXT = ("name", "task", "last_tool", "last_tool_preview", "team", "role", "model")
YES = ("y", "yes", "c", "co", "có", "ok")


def is_yes(ans: str | None) -> bool:
    return str(ans or "").strip().lower() in YES


def _scrub(d: dict[str, Any]) -> None:
    """Chữ hiển thị của dòng agent → một dòng đã làm sạch (agent đặt được tên/task/tool)."""
    for k in _ROW_TEXT:
        if isinstance(d.get(k), str):
            d[k] = one_line(d[k], 400)


class AgentMonitor:
    """Trạng thái bảng agent + approval/câu hỏi chờ của MỘT phiên chat."""

    def __init__(self, manager_id: str | None = None) -> None:
        self.model = TopModel()
        self.manager_id = manager_id
        self.questions: dict[str, dict[str, Any]] = {}
        self.routed: dict[str, str] = {}  # approval id → "manager"|"human" theo event delegated/escalated (mới hơn snapshot)
        self.carded: dict[str, str] = {}  # id đã in thẻ → "human"|"manager" (không in lại, chỉ in dòng ngắn)
        self.errors: dict[str, tuple[float, str]] = {}  # agent_id → (lúc lỗi, lỗi) thấy TRONG phiên này
        self.loaded = False

    # ------------------------------------------------------------------ nạp
    def load_snapshot(self, snap: dict[str, Any]) -> None:
        self.model.load(snap)
        for r in self.model.rows.values():
            _scrub(r.data)
        live = {a.get("id") for a in self.model.approvals}
        for k in [k for k in self.routed if k not in live]:
            self.routed.pop(k, None)
        self.loaded = True

    def load_questions(self, rows: list[dict[str, Any]]) -> None:
        self.questions = {str(q["id"]): q for q in rows or [] if isinstance(q, dict) and q.get("id") and q.get("status") in ("open", "with_manager")}

    # ------------------------------------------------------------------ định tuyến
    def approval_route(self, ap: dict[str, Any]) -> str:
        """"manager" (điều phối viên đang xử lý) | "human" (cần bạn)."""
        aid = str(ap.get("id") or ap.get("request_id") or "")
        r = self.routed.get(aid) or ap.get("routed_to")
        return "manager" if r == "manager" else "human"

    @staticmethod
    def question_route(q: dict[str, Any]) -> str:
        if q.get("status") == "with_manager" or (q.get("routed_to") == "manager" and q.get("status") != "open"):
            return "manager"
        return "human"

    def pending_approvals(self) -> list[dict[str, Any]]:
        return sorted(self.model.approvals, key=lambda a: str(a.get("opened_at") or ""))

    def counts(self) -> dict[str, int]:
        aps = self.pending_approvals()
        qs = list(self.questions.values())
        return {
            "approvals_human": sum(1 for a in aps if self.approval_route(a) == "human"),
            "questions_human": sum(1 for q in qs if self.question_route(q) == "human"),
            "manager": sum(1 for a in aps if self.approval_route(a) == "manager") + sum(1 for q in qs if self.question_route(q) == "manager"),
        }

    def needs_human(self) -> int:
        c = self.counts()
        return c["approvals_human"] + c["questions_human"]

    # ------------------------------------------------------------------ dòng agent
    def state(self, r: Any, now: float | None = None) -> str | None:
        """Trạng thái hiển thị của một dòng (None = không hiện trong bảng)."""
        now = time.time() if now is None else now
        st = r["status"]
        if st == "waiting_human":
            return "human"
        if st == "waiting":
            pend = [a for a in self.model.approvals if a.get("agent_id") == r.id]
            if pend and all(self.approval_route(a) == "manager" for a in pend):
                return "manager"
            return "approval"
        if st == "working":
            return "queued" if r["run_status"] == "queued" else "running"
        if r["current_run_id"] and r["run_status"] == "queued":
            return "queued"
        if st == "error":
            e = self.errors.get(r.id)
            return "error" if e and now - e[0] < ERROR_TTL_S else None
        return None

    def active(self, now: float | None = None) -> list[tuple[str, Any]]:
        now = time.time() if now is None else now
        order = {"human": 0, "approval": 1, "manager": 2, "running": 3, "queued": 4, "error": 5}
        out = []
        for r in self.model.rows.values():
            if r.id == self.manager_id:
                continue  # điều phối viên đã có thanh trạng thái riêng
            s = self.state(r, now)
            if s:
                out.append((s, r))
        out.sort(key=lambda x: (order.get(x[0], 9), x[1].id))
        return out

    def find_agent(self, ref: str) -> Any:
        ref = ref.strip()
        rows = self.model.rows
        if ref in rows:
            return rows[ref]
        hits = [r for r in rows.values() if r.id.startswith(ref)] or [r for r in rows.values() if str(r["name"] or "").lower() == ref.lower()]
        return hits[0] if len(hits) == 1 else None

    def agent_name(self, aid: str | None) -> str:
        r = self.model.rows.get(aid or "")
        return one_line((r["name"] if r else None) or aid or "?", 40)

    # ------------------------------------------------------------------ event
    def apply(self, ev: dict[str, Any], now: float | None = None) -> tuple[list[dict[str, Any]], bool]:
        """Event firehose → (thông báo cho REPL, cần nạp snapshot/câu hỏi ngay)."""
        now = time.time() if now is None else now
        aid, need = self.model.apply(ev, now)
        r = self.model.rows.get(aid or "")
        if r is not None:
            _scrub(r.data)
        t = str(ev.get("type") or "")
        p = ev.get("payload") or {}
        out: list[dict[str, Any]] = []
        if t == "run.failed" and ev.get("agent_id"):
            self.errors[ev["agent_id"]] = (now, one_line(p.get("error") or p.get("outcome") or "lỗi", 200))
        elif t in ("run.started", "run.queued") and ev.get("agent_id"):
            self.errors.pop(ev["agent_id"], None)
        if t in APPROVAL_EVENTS:
            need = True
            rid = str(p.get("request_id") or p.get("approval_id") or p.get("id") or "")
            if not rid:
                return out, need
            if p.get("routed_to") in ("manager", "human"):
                self.routed[rid] = p["routed_to"]
            if t == "approval.delegated":
                self.routed[rid] = "manager"
            elif t == "approval.escalated":
                self.routed[rid] = "human"
            route = self.routed.get(rid) or ("manager" if p.get("routed_to") == "manager" else "human")
            if t == "approval.resolved":
                if rid in self.carded:
                    out.append({"kind": "note", "id": rid, "resolved": p})
                    self.carded.pop(rid, None)
                self.model.approvals = [a for a in self.model.approvals if a.get("id") != rid]
                self.routed.pop(rid, None)
            elif t == "approval.reminder":
                pass  # nhắc lại đã có ở Slack/Telegram; bảng + huy hiệu vẫn hiện
            elif t == "approval.escalated" or rid not in self.carded or (route == "human" and self.carded.get(rid) == "manager"):
                out.append({"kind": "approval", "id": rid, "routed": route, "bell": route == "human", "event": t, "payload": p,
                            "agent_id": ev.get("agent_id"), "run_id": ev.get("run_id")})
                self.carded[rid] = route
            elif t == "approval.waiting_human" and route == "human":
                out.append({"kind": "note", "id": rid, "waiting_human": True, "bell": True})
            return out, need
        if t in QUESTION_EVENTS:
            need = True
            qid = str(p.get("question_id") or "")
            if not qid:
                return out, need
            if t in ("question.answered", "question.cancelled"):
                self.questions.pop(qid, None)
                if qid in self.carded:
                    out.append({"kind": "note", "id": qid, "question_done": t, "payload": p})
                    self.carded.pop(qid, None)
                return out, need
            q = {**self.questions.get(qid, {}), **{k: v for k, v in p.items() if k != "question_id"}, "id": qid,
                 "agent_id": ev.get("agent_id"), "run_id": ev.get("run_id")}
            if t == "question.escalated":
                q.update({"status": "open", "routed_to": "human"})
            self.questions[qid] = q
            route = self.question_route(q)
            if t == "question.escalated" or qid not in self.carded:
                out.append({"kind": "question", "id": qid, "routed": route, "bell": route == "human", "event": t, "payload": q})
                self.carded[qid] = route
            return out, need
        return out, need


# ============================================================================ bảng (toolbar)
def _fit(parts: list[tuple[str, str]], width: int) -> Text:
    """Ghép các mảnh (chữ, style) — bỏ dần mảnh cuối khi không vừa `width` ô; mảnh đầu luôn giữ (cắt bằng …)."""
    keep = list(parts)
    while len(keep) > 1 and sum(cell_len(s) for s, _ in keep) > width:
        keep.pop()
        while len(keep) > 1 and keep[-1][0] == " · ":  # không để dấu nối treo cuối dòng
            keep.pop()
    t = Text()
    for s, st in keep:
        t.append(s, style=st)
    t.truncate(max(1, width), overflow="ellipsis")
    return t


def summary_line(mon: AgentMonitor, width: int, *, expanded: bool = False, now: float | None = None) -> Text | None:
    """Dòng gọn: `● 3 agent đang chạy · ⚠ 1 chờ bạn duyệt  (↓ xem)`; None khi không có gì chạy/chờ."""
    act = mon.active(now)
    c = mon.counts()
    running = sum(1 for s, _ in act if s in ("running", "approval", "human", "manager"))
    queued = sum(1 for s, _ in act if s == "queued")
    errors = sum(1 for s, _ in act if s == "error")
    if not (act or c["approvals_human"] or c["questions_human"] or c["manager"]):
        return None
    parts: list[tuple[str, str]] = [(" ", "")]
    if c["approvals_human"]:
        parts.append((f"⚠ {c['approvals_human']} chờ bạn duyệt", V.C_BADGE))
        parts.append((" · ", V.C_DIM))
    if c["questions_human"]:
        parts.append((f"❓ {c['questions_human']} câu hỏi chờ bạn", V.C_QUESTION))
        parts.append((" · ", V.C_DIM))
    if running:
        parts.append((f"{V.AGENT_STATE['running'][0]} {running} agent đang chạy", V.AGENT_STATE["running"][2]))
        parts.append((" · ", V.C_DIM))
    if c["manager"]:
        parts.append((f"◐ {c['manager']} điều phối viên đang xử lý", V.AGENT_STATE["manager"][2]))
        parts.append((" · ", V.C_DIM))
    if queued:
        parts.append((f"◌ {queued} chờ slot", V.AGENT_STATE["queued"][2]))
        parts.append((" · ", V.C_DIM))
    if errors:
        parts.append((f"✖ {errors} lỗi", V.AGENT_STATE["error"][2]))
        parts.append((" · ", V.C_DIM))
    if parts[-1][0] == " · ":
        parts.pop()
    if len(parts) > 1 and parts[1][1] == V.C_BADGE:  # huy hiệu nền đỏ: chèn khoảng trắng hai bên cho dễ đọc
        parts[1] = (f" {parts[1][0]} ", V.C_BADGE)
        if len(parts) > 2 and parts[2][0] == " · ":
            parts[2] = (" ", "")
    parts.append(("  (↓/Esc đóng)" if expanded else "  (↓ xem)", V.C_FAINT))
    return _fit(parts, width)


def _elapsed(mon: AgentMonitor, state: str, r: Any, now: float) -> str:
    if state in ("approval", "human", "manager"):
        pend = [a for a in mon.model.approvals if a.get("agent_id") == r.id]
        t0 = min((parse_ts(a.get("opened_at")) or now for a in pend), default=None)
        return fmt_dur(now - t0) if t0 else "—"
    if state == "running":
        e = TopModel.elapsed(r, now)
        return fmt_dur(e) if e is not None else "—"
    if state == "error":
        e = mon.errors.get(r.id)
        return f"{fmt_dur(now - e[0])} trước" if e else "—"
    return "—"


def _task(mon: AgentMonitor, state: str, r: Any) -> str:
    if state in ("approval", "human", "manager"):
        pend = [a for a in mon.pending_approvals() if a.get("agent_id") == r.id]
        if pend:
            return ("duyệt: " if state != "manager" else "điều phối viên xử lý: ") + one_line(pend[0].get("preview") or pend[0].get("tool_name"), 200)
    if state == "error":
        e = mon.errors.get(r.id)
        if e:
            return one_line(e[1], 200)
    return one_line(r["task"] or "—", 200)


def agent_row(mon: AgentMonitor, state: str, r: Any, width: int, now: float | None = None) -> Text:
    """Một dòng: biểu tượng · tên · task · thời gian · tool cuối · token · $ — bỏ dần cột phụ khi hẹp (không bao giờ tràn)."""
    now = time.time() if now is None else now
    icon, _label, style = V.AGENT_STATE.get(state, ("?", state, ""))
    name_w = max(6, min(18, width // 5))
    cols: list[tuple[str, str, int]] = []  # (chữ, style, rộng cố định)
    el = _elapsed(mon, state, r, now)
    tool = last_tool_cell(r, now)
    tok, cost = tok_cell(r).plain, cost_cell(r).plain
    if width >= 34:
        cols.append((el, V.C_MID, 9 if state == "error" else 6))
    if width >= 64:
        cols.append(("⚙ " + one_line(tool.plain, 40) if r["last_tool"] else "", V.C_TOOL if r.tool_running else V.C_MID, min(24, max(12, width // 6))))
    if width >= 48:
        cols.append((tok, V.C_MID, 6))
        cols.append((cost, V.C_MID, 7))
    fixed = 2 + name_w + 1 + sum(w + 1 for _, _, w in cols) + 1
    task_w = width - fixed
    t = Text(" ")
    t.append(icon + " ", style=style)
    n = Text(one_line(r["name"] or r.id, 60), style="bold")
    n.truncate(name_w, overflow="ellipsis", pad=True)
    t.append_text(n)
    t.append(" ")
    if task_w >= 6:
        tk = Text(_task(mon, state, r), style=style if state in ("human", "error") else V.C_SOFT)
        tk.truncate(task_w, overflow="ellipsis", pad=True)
        t.append_text(tk)
        t.append(" ")
    for s, st, w in cols:
        c = Text(s, style=st, justify="right" if w in (6, 7, 9) else "left")
        c.truncate(w, overflow="ellipsis", pad=False)
        pad = w - c.cell_len
        if w in (6, 7, 9):
            t.append(" " * pad)
            t.append_text(c)
        else:
            t.append_text(c)
            t.append(" " * pad)
        t.append(" ")
    t.rstrip()
    t.truncate(max(1, width), overflow="ellipsis")
    return t


def panel_lines(mon: AgentMonitor, width: int, height: int, *, expanded: bool, now: float | None = None) -> list[Text]:
    """Các dòng phía trên thanh trạng thái: gọn = 1 dòng; mở = dòng gọn + một dòng/agent (giới hạn theo chiều cao, `+k nữa`)."""
    now = time.time() if now is None else now
    head = summary_line(mon, width, expanded=expanded, now=now)
    if head is None:
        return []
    if not expanded:
        return [head]
    act = mon.active(now)
    cap = max(1, min(MAX_PANEL_ROWS, height - 8))
    lines = [head]
    shown = act if len(act) <= cap else act[: max(1, cap - 1)]
    for s, r in shown:
        lines.append(agent_row(mon, s, r, width, now))
    if len(shown) < len(act):
        lines.append(Text(f"   +{len(act) - len(shown)} nữa · /agents xem hết", style=V.C_DIM))
    if not act:
        lines.append(Text("   (không có agent nào đang chạy — /approvals xem yêu cầu chờ)", style=V.C_DIM))
    return lines


def agents_table(mon: AgentMonitor, now: float | None = None, width: int = 120) -> Any:
    """`/agents` (`/ps`): MỌI agent (sắp như `top`), trạng thái + task + thời gian + tool + token + $. Cột TASK co theo `width`;
    hẹp (< 80) bỏ cột tool."""
    now = time.time() if now is None else now
    rows = mon.model.visible(now)
    if not rows:
        return Text("  (chưa có agent nào)", style=V.C_DIM)
    show_tool, show_tok = width >= 80, width >= 66
    agent_w = 18 if width >= 90 else 12
    # rộng cố định (gồm ô đệm giữa cột): trạng thái 13 + agent + 2 + TG 7 + tool 20 + TOK 7 + $ 8
    task_w = max(8, width - 13 - (agent_w + 2) - 7 - (20 if show_tool else 0) - (7 if show_tok else 0) - 8 - 2)
    tb = Table(box=None, pad_edge=False, show_edge=False, header_style="bold", expand=False)
    cols: list[tuple[str, dict[str, Any]]] = [
        ("", {"no_wrap": True, "min_width": 11, "max_width": 11, "overflow": "ellipsis"}),
        ("AGENT", {"no_wrap": True, "max_width": agent_w, "overflow": "ellipsis"}),
        ("TASK", {"overflow": "ellipsis", "no_wrap": True, "max_width": task_w}),
        ("TG", {"justify": "right", "no_wrap": True, "min_width": 5}),
        ("TOOL CUỐI", {"no_wrap": True, "max_width": 18, "overflow": "ellipsis"}),
        ("TOK", {"justify": "right", "no_wrap": True, "min_width": 5}),
        ("$", {"justify": "right", "no_wrap": True, "min_width": 6}),
    ]
    drop = [i for i, on in ((5, show_tok), (4, show_tool)) if not on]  # chỉ số giảm dần → pop an toàn
    for i in drop:
        cols.pop(i)
    for col, kw in cols:
        tb.add_column(col, **kw)
    for r in rows:
        st = mon.state(r, now) or ("idle" if r["status"] == "idle" else r["status"])
        icon, label, style = V.AGENT_STATE.get(st, ("○" if st == "idle" else "‖", "rảnh" if st == "idle" else str(st), V.C_MID))
        name = Text(one_line(r["name"] or r.id, 40), style="bold")
        if r["name"] and r["name"] != r.id:
            name.append(f" {one_line(r.id, 30)}", style=V.C_DIM)
        tool = last_tool_cell(r, now)
        cells = [Text(f"{icon} {label}", style=style), name, Text(_task(mon, st, r) if st in V.AGENT_STATE else one_line(r["task"] or "—", 200), style=V.C_SOFT),
                 Text(_elapsed(mon, st, r, now) if st in V.AGENT_STATE else "—", style=V.C_MID),
                 Text(one_line(tool.plain, 60), style=V.C_TOOL if r.tool_running else V.C_MID),
                 Text(tok_cell(r).plain, style=V.C_MID), Text(cost_cell(r).plain, style=V.C_MID)]
        for i in drop:
            cells.pop(i)
        tb.add_row(*cells)
    foot = Text("  /stop <agent> dừng · /attach <agent> xem trực tiếp · /approvals yêu cầu chờ", style=V.C_FAINT)
    return Group(tb, foot)


# ============================================================================ thẻ
def _kv(label: str, value: Any, style: str = V.C_SOFT) -> Text:
    t = Text(f"   {label}: ", style=V.C_MID)
    t.append(one_line(value, 400), style=style)
    return t


def command_block(ap: dict[str, Any]) -> tuple[list[Text], dict[str, Any]]:
    """Lệnh/tham số ĐẦY ĐỦ (không cắt) — `web.present.action_view` (đã che secret lúc ghi DB) rồi `termsafe.clean`."""
    view = action_view(ap) if ap.get("tool_input") is not None else {"text": ap.get("preview") or "", "hidden_chars": False}
    text = clean(view.get("text") or ap.get("preview") or "").rstrip("\n") or "(trống)"
    lines = []
    for ln in text.split("\n"):
        t = Text("   ┃ ", style=V.C_CARD_BAR)
        t.append(ln.replace("\t", "    "))
        lines.append(t)
    return lines, view


def approval_card(ap: dict[str, Any], *, agent_name: str | None = None, routed: str = "human", event: str | None = None) -> Any:
    """Thẻ yêu cầu duyệt: agent, tool, lệnh ĐẦY ĐỦ, lý do, rủi ro, id, cách xử lý. `routed == "manager"` → một dòng thông tin."""
    aid = one_line(ap.get("id") or ap.get("request_id"), 40)
    agent = one_line(agent_name or ap.get("agent_id") or "?", 40)
    tool = one_line(ap.get("tool_name") or ap.get("tool") or "?", 80)
    if routed == "manager":
        t = Text("  ◐ ", style=V.AGENT_STATE["manager"][2])
        t.append("điều phối viên đang xử lý ", style=V.AGENT_STATE["manager"][2])
        t.append(f"yêu cầu {aid} của agent {agent}: ", style=V.C_MID)
        t.append(one_line(ap.get("preview") or tool, 160))
        t.append(f"\n     (bạn vẫn quyết được: /approve {aid} · /deny {aid})", style=V.C_FAINT)
        return t
    head = Text("")
    head.append("⏸ " if ap.get("status") == "waiting_human" else "◐ ", style=V.C_CARD)
    head.append("Điều phối viên chuyển lên bạn — " if event == "approval.escalated" else "", style=V.C_CARD)
    head.append("Cần bạn duyệt", style=V.C_CARD)
    head.append(f" · agent {agent}", style="bold")
    head.append(f" · {short_tool(tool) if tool.startswith('mcp__') else tool}", style=V.C_TOOL)
    tm = V.hhmm(ap.get("opened_at"))
    if tm:
        head.append(f"  {tm}", style=V.C_FAINT)
    cmd, view = command_block(ap)
    parts: list[Any] = [Text(""), head, *cmd]
    if view.get("hidden_chars"):
        parts.append(Text("   ⚠ lệnh có ký tự ẩn/điều khiển (đã bỏ khi hiển thị) — xem kỹ trên web trước khi cho phép", style=V.C_ERR))
    if ap.get("reason"):
        parts.append(_kv("lý do", ap["reason"]))
    risk = " · ".join(one_line(x, 60) for x in (ap.get("risk_level"), ap.get("destructive_class"), ap.get("matched_rule")) if x)
    if risk:
        parts.append(_kv("rủi ro", risk, V.C_WARN))
    if ap.get("protected_resource_id"):
        parts.append(_kv("vùng bảo vệ", ap["protected_resource_id"], V.C_WARN))
    idl = Text(f"   id {aid}", style=V.C_MID)
    if ap.get("run_id"):
        idl.append(f" · run {one_line(ap['run_id'], 40)}", style=V.C_DIM)
    if ap.get("status") == "waiting_human":
        idl.append(" · run đã tạm dừng chờ bạn", style=V.C_ERR)
    parts.append(idl)
    how = Text(f"   → /approve {aid} [ghi chú]", style="bold")
    how.append("  ·  ", style=V.C_DIM)
    how.append(f"/deny {aid} [lý do]", style="bold")
    if ap.get("always_offer"):
        how.append("  ·  ", style=V.C_DIM)
        how.append(f"/always {aid}", style="bold")
    parts.append(how)
    return Group(*parts)


def always_rule_text(offer: dict[str, Any]) -> Text:
    """Luật "luôn cho phép" sẽ tạo (cùng nội dung bước 2 của web/Telegram). Giá trị do hub tính (host DNS/IPv4, vân tay hex)."""
    t = Text("   luật sẽ tạo: ", style=V.C_MID)
    if offer.get("kind") == "web_domain":
        t.append(f"web_domain — agent {one_line(offer.get('agent_id'), 40)} được WebFetch tới ĐÚNG host {one_line(offer.get('domain'), 120)}"
                 " (không gồm tên miền con) mà không hỏi lại", style=V.C_WARN)
    else:
        t.append(f"exact — agent {one_line(offer.get('agent_id'), 40)} + tool {one_line(offer.get('tool'), 60)} + ĐÚNG dấu vân tay "
                 f"{one_line(offer.get('fingerprint'), 80)} (khác dù một ký tự vẫn hỏi lại)", style=V.C_WARN)
    if offer.get("summary"):
        t.append(f"\n   ({one_line(offer['summary'], 200)})", style=V.C_DIM)
    return t


def question_card(q: dict[str, Any], *, agent_name: str | None = None, routed: str = "human", event: str | None = None) -> Any:
    qid = one_line(q.get("id"), 40)
    agent = one_line(agent_name or q.get("agent_id") or "?", 40)
    if routed == "manager":
        t = Text("  ◐ ", style=V.AGENT_STATE["manager"][2])
        t.append("điều phối viên đang xử lý ", style=V.AGENT_STATE["manager"][2])
        t.append(f"câu hỏi {qid} của agent {agent}: ", style=V.C_MID)
        t.append(one_line(q.get("text"), 160))
        t.append(f"\n     (bạn vẫn trả lời được: /answer {qid} …)", style=V.C_FAINT)
        return t
    head = Text("❓ ", style=V.C_QUESTION)
    head.append("Điều phối viên chuyển lên bạn — " if event == "question.escalated" else "", style=V.C_QUESTION)
    head.append(f"Agent {agent} hỏi bạn", style=V.C_QUESTION)
    if q.get("permission"):
        head.append(f" · xin phép: {one_line(q['permission'], 40)}", style=V.C_WARN)
    tm = V.hhmm(q.get("created_at")) if q.get("created_at") else ""
    if tm:
        head.append(f"  {tm}", style=V.C_FAINT)
    parts: list[Any] = [Text(""), head]
    for ln in (clean(q.get("text")).rstrip("\n") or "(trống)").split("\n"):
        parts.append(Text("   " + ln))
    for o in q.get("options") or []:
        line = Text(f"     {one_line(o.get('key'), 8)}. ", style="bold")
        line.append(one_line(o.get("label"), 200))
        if o.get("description"):
            line.append(f" — {one_line(o['description'], 200)}", style=V.C_DIM)
        parts.append(line)
    if q.get("escalation_note") or q.get("note"):
        parts.append(_kv("ghi chú của điều phối viên", q.get("escalation_note") or q.get("note")))
    how = Text(f"   → /answer {qid} ", style="bold")
    if q.get("options"):
        how.append("<số>" + (" (chọn nhiều: 1,2)" if q.get("multi") else ""), style="bold")
        if q.get("allow_free_text", True):
            how.append(" [thêm chữ]", style=V.C_MID)
    else:
        how.append("<câu trả lời>", style="bold")
    how.append(f"   · id {qid}", style=V.C_DIM)
    parts.append(how)
    return Group(*parts)


def resolved_note(rid: str, p: dict[str, Any]) -> dict[str, Any]:
    dec = {"allowed": "đã cho phép", "denied": "đã từ chối", "cancelled": "đã huỷ"}.get(str(p.get("decision")), one_line(p.get("decision"), 20))
    by = f" bởi {one_line(p.get('by'), 30)}" if p.get("by") else ""
    ch = f" qua {one_line(p.get('channel'), 30)}" if p.get("channel") else ""
    return {"kind": "note", "text": f"yêu cầu {rid}: {dec}{by}{ch}", "tone": "warn" if p.get("decision") == "denied" else None}


def pending_text(mon: AgentMonitor, approvals: list[dict[str, Any]], questions: list[dict[str, Any]], now: float | None = None) -> Text:
    """`/approvals`: mỗi yêu cầu/câu hỏi một khối ngắn (lệnh một dòng — thẻ đầy đủ: /approve <id>)."""
    now = time.time() if now is None else now
    t = Text()
    if not approvals and not questions:
        t.append("  (không có yêu cầu duyệt hay câu hỏi nào đang chờ)\n", style=V.C_DIM)
        return t
    for ap in approvals:
        route = mon.approval_route(ap)
        icon = "◐" if route == "manager" else ("⏸" if ap.get("status") == "waiting_human" else "⚠")
        t.append(f"  {icon} ", style=V.AGENT_STATE["manager"][2] if route == "manager" else V.C_CARD)
        t.append(one_line(ap.get("id"), 40), style="bold")
        t.append(f"  {mon.agent_name(ap.get('agent_id'))}", style=V.C_SOFT)
        t0 = parse_ts(ap.get("opened_at"))
        t.append(f"  chờ {fmt_dur(now - t0) if t0 else '—'}", style=V.C_DIM)
        if route == "manager":
            t.append("  · điều phối viên đang xử lý", style=V.AGENT_STATE["manager"][2])
        t.append(f"\n      {one_line(ap.get('preview') or ap.get('tool_name'), 160)}\n", style=V.C_SOFT)
    for q in questions:
        route = mon.question_route(q)
        t.append("  ◐ " if route == "manager" else "  ❓ ", style=V.AGENT_STATE["manager"][2] if route == "manager" else V.C_QUESTION)
        t.append(one_line(q.get("id"), 40), style="bold")
        t.append(f"  {mon.agent_name(q.get('agent_id'))}", style=V.C_SOFT)
        if route == "manager":
            t.append("  · điều phối viên đang xử lý", style=V.AGENT_STATE["manager"][2])
        t.append(f"\n      {one_line(q.get('text'), 160)}\n", style=V.C_SOFT)
    t.append("  /approve <id> · /deny <id> · /answer <id> <số> — /approve hiện lại lệnh đầy đủ trước khi hỏi y/N\n", style=V.C_FAINT)
    return t


__all__ = ["AgentMonitor", "summary_line", "agent_row", "panel_lines", "agents_table", "approval_card", "question_card", "always_rule_text",
           "command_block", "pending_text", "resolved_note", "is_yes"]
