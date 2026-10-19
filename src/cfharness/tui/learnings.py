"""Tab Learnings (FR-UI-06) + màn golden task (FR-LN-10, Q-07) của `cfharness top` — M4-B.

CLIENT THUẦN của daemon (RPC), cùng khuôn với `TasksScreen` (M2/M3). KHÔNG có hàng chờ duyệt ("báo chứ không hỏi").

LearningsScreen (phím `l` ở màn bảng):
- Header: số thay đổi hôm nay theo loại, trạng thái digest hôm nay, Canary (placeholder — M5).
- Feed (`learn.feed` của M4-A, 7 ngày): `learning.applied|rolled_back|eval_failed|deferred|canary|promoted`, `guard.blocked`
  (vòng học) — cột thời gian, agent, loại, tầng (scope), lớp L1–L3, đích, op, điểm eval trước→sau (`eval.list` của B
  khớp theo proposal), ledger entry. Chi tiết dòng: diff (payload hoặc `learn.show`), lý do, lệnh rollback.
- Phím: `f` lọc loại · `r` rollback entry (xác nhận; `learn.rollback`, có đích sau → hỏi rollback chuỗi) · `p` pin/unpin
  đích (`knowledge.pin`) · `D` digest hôm nay · `j` journey agent · `G` quản lý golden task · Esc về bảng.
  Hardening H-16: `L` bật/tắt học cho agent của dòng đang chọn (xác nhận; `learn.policy_set agents.<id>.enabled`, audit
  `tui` — chính sách của NGƯỜI) · `b` mục Ledger theo agent (`LedgerScreen`, `learn.ledger {agent}`: before/after, `r` rollback).
GoldenScreen (`G`): bảng golden task + điểm gần nhất; `a` thêm từ run (gợi ý ứng viên) · Enter xem · `e` sửa cách chấm
  (rubric judge/regex/…) · Del xoá mềm · `u` khôi phục · `A` hiện cả task đã xoá. Mọi thao tác là của NGƯỜI (`_audit.channel=tui`
  → golden `created_by=user:tui`).
Digest lần đầu trong ngày: `maybe_show_digest(app)` gọi `digest.tui_open` khi TUI mở (FR-LN-13).
"""

from __future__ import annotations

import json
from typing import Any

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Static

from ..rpc import RpcError

FEED_TYPES = ("learning.applied", "learning.rolled_back", "learning.eval_failed", "learning.deferred", "guard.blocked", "learning.canary", "learning.promoted")
KIND = {
    "learning.applied": ("áp dụng", "bold green"), "learning.rolled_back": ("rollback", "bold yellow"), "learning.eval_failed": ("eval_failed", "bold red"),
    "learning.deferred": ("hoãn", "cyan"), "guard.blocked": ("bị chặn", "bold white on red"), "learning.canary": ("canary", "magenta"),
    "learning.promoted": ("promoted", "magenta"),
}
FILTERS = [None, "learning.applied", "learning.rolled_back", "learning.eval_failed", "learning.deferred", "guard.blocked"]

DEFAULT_CSS = """
#learn-head { height: 3; padding: 0 1; background: $panel; }
#learn-feed { height: 1fr; margin: 1 1 0 1; }
#learn-detail { height: 12; padding: 0 1; border-top: solid $primary-darken-2; }
#golden-head { height: 2; padding: 0 1; background: $panel; }
#golden { height: 1fr; margin: 1 1 0 1; }
#golden-detail { height: 10; padding: 0 1; border-top: solid $primary-darken-2; }
"""


def _err(e: Exception) -> str:
    return e.message if isinstance(e, RpcError) else str(e)


def _hm(ts: str | None) -> str:
    from .model import parse_ts

    t = parse_ts(ts) if ts else None
    if t is None:
        return (ts or "")[:16]
    import datetime as _dt

    return _dt.datetime.fromtimestamp(t).strftime("%d/%m %H:%M")


def parse_checker_spec(text: str) -> dict[str, Any]:
    """Cú pháp nhập nhanh cách chấm trong TUI:
    `regex: <mẫu>` · `contains: a | b` · `judge: <rubric>` · `json: {schema}` · `cmd: <lệnh>` · hoặc JSON checker đầy đủ."""
    s = text.strip()
    if s.startswith("{"):
        return json.loads(s)
    kind, _, rest = s.partition(":")
    kind, rest = kind.strip().lower(), rest.strip()
    if not rest:
        raise ValueError("cú pháp: regex: … | contains: a | b | judge: <rubric> | json: {schema} | cmd: <lệnh>")
    if kind == "regex":
        return {"kind": "regex", "pattern": rest}
    if kind == "contains":
        return {"kind": "contains", "all": [x.strip() for x in rest.split("|") if x.strip()]}
    if kind in ("judge", "llm", "llm_judge"):
        return {"kind": "llm_judge", "rubric": rest, "pass_threshold": 0.7}
    if kind in ("json", "json_schema"):
        return {"kind": "json_schema", "schema": json.loads(rest)}
    if kind in ("cmd", "command"):
        return {"kind": "command", "cmd": rest}
    raise ValueError(f"loại cách chấm không hỗ trợ: {kind}")


def checker_to_spec(ck: dict[str, Any]) -> str:
    """Ngược với `parse_checker_spec` (giá trị mặc định khi sửa)."""
    k = ck.get("kind")
    if k == "regex":
        return f"regex: {ck.get('pattern')}"
    if k == "contains":
        items = ck.get("all") or ck.get("any") or ([ck["text"]] if isinstance(ck.get("text"), str) else ck.get("text") or [])
        return "contains: " + " | ".join(items)
    if k == "llm_judge":
        return f"judge: {ck.get('rubric')}"
    if k == "json_schema":
        return "json: " + json.dumps(ck.get("schema"), ensure_ascii=False)
    if k == "command":
        return f"cmd: {ck.get('cmd')}"
    return json.dumps(ck, ensure_ascii=False)


def checker_label(ck: dict[str, Any] | None) -> str:
    ck = ck or {}
    if ck.get("kind") == "all":
        return "all(" + ",".join(c.get("kind", "?") for c in ck.get("checks") or []) + ")"
    return str(ck.get("kind") or "?")


def eval_cell(item: dict[str, Any], evals: dict[str, dict[str, Any]]) -> Text:
    p = item.get("payload") or {}
    sc = p.get("scores") or {}
    b, a = sc.get("before", p.get("eval_before")), sc.get("after", p.get("eval_after"))
    ev = evals.get(str(p.get("proposal_id") or "")) if (b is None and a is None) else None
    if ev:
        b, a = ev.get("before_score"), ev.get("after_score")
    if b is None and a is None:
        return Text("—", style="grey50")
    f = lambda x: "-" if x is None else f"{float(x):.2f}"  # noqa: E731
    style = "red" if (b is not None and a is not None and a < b) else "green"
    return Text(f"{f(b)}→{f(a)}", style=style)


def canary_head(canaries: list[dict[str, Any]]) -> Text:
    """Dòng Canary trên đầu tab (M5-A, FR-LN-16): thay đổi tầng team/global/USER đang canary/shadow — tự promoted/rollback."""
    t = Text()
    if not canaries:
        t.append("\n 🐤 Canary: không có thay đổi tầng team/global/USER nào đang thử (tự promoted/rollback — `cfharness learn canary list --all`)", style="grey50")
        return t
    t.append(f"\n 🐤 Canary đang chạy: {len(canaries)}", style="bold magenta")
    for c in canaries[:4]:
        who = f"trên {c.get('canary_agent_id')}" if c.get("mode") == "canary" else f"shadow, theo dõi {','.join(c.get('watch_agents') or [])}"
        t.append(f"\n   {c['id']} {c['scope']}:{c.get('owner_id') or 'global'} {c['target']}:{c.get('target_ref') or ''} {who} — "
                 f"{c.get('progress') or ''} · hạn {_hm(c.get('deadline'))} · rollback: learn rollback {c.get('ledger_entry_id') or ''}", style="magenta")
    if len(canaries) > 4:
        t.append(f"\n   … +{len(canaries) - 4}", style="grey50")
    return t


async def maybe_show_digest(app: Any) -> None:
    """FR-LN-13: lần đầu mở TUI trong ngày → hiện digest chưa xem (gộp nếu nhiều ngày không mở)."""
    try:
        r = await app.client.call("digest.tui_open", {})
    except Exception:
        return
    if r and r.get("show") and r.get("text"):
        from .app import InfoScreen

        t = Text()
        t.append("Digest học — lần mở đầu tiên hôm nay\n\n", style="bold magenta")
        t.append(r["text"][:6000])
        app.push_screen(InfoScreen(t))


class LearningsScreen(Screen):
    DEFAULT_CSS = DEFAULT_CSS
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Về bảng"),
        Binding("f", "cycle_filter", "Lọc"),
        Binding("r", "rollback", "Rollback"),
        Binding("p", "pin", "Pin/unpin"),
        Binding("D", "digest", "Digest"),
        Binding("j", "journey", "Journey"),
        Binding("G", "golden", "Golden task"),
        Binding("L", "toggle_learning", "Tắt/bật học"),
        Binding("b", "ledger", "Ledger"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.items: list[dict[str, Any]] = []
        self.evals: dict[str, dict[str, Any]] = {}
        self.filter_i = 0
        self.available: bool | None = None
        self.digest_today: dict[str, Any] | None = None
        self.canaries: list[dict[str, Any]] = []  # M5-A: canary/shadow đang chạy (learn.canaries)

    def compose(self) -> ComposeResult:
        yield Static(id="learn-head")
        yield DataTable(id="learn-feed", cursor_type="row", zebra_stripes=True)
        yield Static(id="learn-detail")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#learn-feed", DataTable)
        for label, key, w in [("LÚC", "ts", 11), ("AGENT", "agent", 12), ("LOẠI", "kind", 11), ("TẦNG", "scope", 6), ("L", "risk", 3),
                              ("ĐÍCH", "target", 26), ("OP", "op", 8), ("EVAL", "eval", 10), ("ENTRY/PROPOSAL", "ref", 18), ("CHI TIẾT", "detail", 40)]:
            table.add_column(label, key=key, width=w)
        table.focus()
        self.run_worker(self.reload(), exit_on_error=False)
        self.set_interval(2.0, lambda: self.run_worker(self.reload(), exclusive=True, group="learn", exit_on_error=False))

    @property
    def client(self):
        return self.app.client  # type: ignore[attr-defined]

    async def reload(self) -> None:
        try:
            await self._reload()
        except NoMatches:
            pass

    async def _reload(self) -> None:
        head = self.query_one("#learn-head", Static)
        try:
            feed = await self.client.call("learn.feed", {"days": 7, "limit": 300}) or []
            self.available = True
        except RpcError as e:
            self.available = False
            head.update(Text(" Learnings — daemon chưa có RPC learn.* (M4-A): " + e.message, style="bold yellow"))
            return
        try:
            evs = await self.client.call("eval.list", {"limit": 200}) or []
        except RpcError:
            evs = []
        self.evals = {str(e.get("proposal_id")): e for e in evs if e.get("proposal_id")}
        try:
            self.canaries = await self.client.call("learn.canaries", {"status": "canary", "limit": 20}) or []
        except RpcError:
            self.canaries = []
        try:
            self.digest_today = await self.client.call("digest.show", {}) or None
        except RpcError:
            self.digest_today = None
        self.items = [x for x in feed if x["type"] in FEED_TYPES]
        self._render_head()
        table = self.query_one("#learn-feed", DataTable)
        prev = self._selected_index_key()
        table.clear()
        flt = FILTERS[self.filter_i]
        rows = [(i, x) for i, x in enumerate(self.items) if flt is None or x["type"] == flt]
        for i, x in rows:
            p = x.get("payload") or {}
            label, style = KIND.get(x["type"], (x["type"], ""))
            if x["type"] == "guard.blocked":
                label = "🔴 chặn" if (p.get("reason") in ("policy_touch", "self_escalation", "bounds") or p.get("policy")) else "bị chặn"
            target = f"{p.get('target') or ''}:{p.get('target_ref') or ''}".strip(":")
            ref = p.get("ledger_entry_id") or p.get("proposal_id") or ""
            detail = p.get("reason") or p.get("summary") or p.get("detail") or p.get("block_detail") or ""
            if x["type"] == "learning.rolled_back":
                detail = f"actor={p.get('actor')}: {p.get('reason') or ''}"
            table.add_row(
                Text(_hm(x.get("ts")), style="grey62"), x.get("agent_id") or "—", Text(label, style=style), p.get("scope") or "agent",
                p.get("risk_class") or "", target[:40], p.get("op") or "", eval_cell(x, self.evals), Text(str(ref), style="grey62"),
                Text(str(detail)[:120], style="grey70"), key=str(i),
            )
        keys = [str(i) for i, _ in rows]
        if prev in keys:
            table.move_cursor(row=keys.index(prev), animate=False)
        await self.show_detail()

    def _render_head(self) -> None:
        cnt: dict[str, int] = {}
        today = __import__("datetime").datetime.now().strftime("%d/%m")
        for x in self.items:
            if _hm(x.get("ts")).startswith(today):
                cnt[x["type"]] = cnt.get(x["type"], 0) + 1
        h = Text()
        h.append(" Learnings ", style="bold reverse magenta")
        h.append("  hôm nay: ", style="grey70")
        for t in FEED_TYPES[:5]:
            label, style = KIND[t]
            h.append(f" {label} {cnt.get(t, 0)} ", style=style)
            h.append("│", style="grey42")
        h.append(f"   lọc: {KIND[FILTERS[self.filter_i]][0] if FILTERS[self.filter_i] else 'tất cả'} (f)", style="grey50")
        h.append_text(canary_head(self.canaries))
        d = self.digest_today
        if d and d.get("id"):
            c = d.get("counts") or {}
            h.append(f"\n 📰 Digest {_hm(d.get('created_at'))}: áp dụng {c.get('applied', 0)} · rollback {c.get('rolled_back', 0)} · "
                     f"eval_failed {c.get('eval_failed', 0)} · hoãn {c.get('deferred', 0)} · chặn {c.get('blocked', 0)} — Slack {d.get('slack_status')} (D xem)", style="grey70")
        else:
            h.append("\n 📰 Digest: chưa có (08:00 giờ máy, hoặc `cfharness digest run`)", style="grey50")
        self.query_one("#learn-head", Static).update(h)

    def _selected_index_key(self) -> str | None:
        table = self.query_one("#learn-feed", DataTable)
        if table.row_count == 0:
            return None
        try:
            return table.coordinate_to_cell_key(table.cursor_coordinate)[0].value
        except Exception:
            return None

    def selected(self) -> dict[str, Any] | None:
        k = self._selected_index_key()
        return self.items[int(k)] if k is not None and int(k) < len(self.items) else None

    @on(DataTable.RowHighlighted)
    async def _hl(self) -> None:
        try:
            await self.show_detail()
        except NoMatches:
            pass

    async def show_detail(self) -> None:
        w = self.query_one("#learn-detail", Static)
        x = self.selected()
        if x is None:
            w.update(Text(" (chưa có thay đổi học nào — vòng học tự áp dụng, bạn chỉ cần xem/rollback ở đây)", style="grey50"))
            return
        p = x.get("payload") or {}
        t = Text()
        label, style = KIND.get(x["type"], (x["type"], ""))
        t.append(f" {label} ", style=style)
        t.append(f" {x.get('agent_id') or '—'} · {p.get('scope') or 'agent'} · {p.get('risk_class') or ''} {p.get('op') or ''} {p.get('target') or ''} {p.get('target_ref') or ''}\n", style="bold")
        if p.get("reason"):
            t.append(f" lý do: {p['reason']}\n", style="grey70")
        sc = p.get("scores") or {}
        ev = self.evals.get(str(p.get("proposal_id") or ""))
        if sc or ev:
            b = sc.get("before", (ev or {}).get("before_score"))
            a = sc.get("after", (ev or {}).get("after_score"))
            t.append(f" eval: {b} → {a}" + (f" (eval {ev['id']}, ${float(ev.get('cost_usd') or 0):.4f})" if ev else "") + "\n", style="grey70")
        diff = p.get("diff")
        entry = p.get("ledger_entry_id")
        if not diff and entry and x["type"] == "learning.applied":
            try:
                diff = (await self.client.call("learn.show", {"id": entry})).get("diff")
            except Exception:
                diff = None
        if diff:
            for ln in str(diff).splitlines()[:8]:
                st = "green" if ln.startswith("+") and not ln.startswith("+++") else "red" if ln.startswith("-") and not ln.startswith("---") else "grey62"
                t.append(" " + ln[:160] + "\n", style=st)
        if entry and x["type"] == "learning.applied":
            t.append(f" r = rollback · CLI: cfharness learn rollback {entry}", style="cyan")
        w.update(t)

    # ------------------------------------------------------------------ actions
    def action_cycle_filter(self) -> None:
        self.filter_i = (self.filter_i + 1) % len(FILTERS)
        self.run_worker(self.reload())

    def action_rollback(self) -> None:
        from .app import ConfirmScreen

        x = self.selected()
        entry = (x or {}).get("payload", {}).get("ledger_entry_id")
        if not x or x["type"] != "learning.applied" or not entry:
            self.notify("Chọn một dòng 'áp dụng' để rollback", severity="warning")
            return

        def done(ok: bool | None) -> None:
            if ok:
                self.run_worker(self.rollback(entry))

        self.app.push_screen(ConfirmScreen(f"Rollback {entry} ({x['payload'].get('target')} {x['payload'].get('target_ref')}) của {x.get('agent_id')}?", yes="Rollback (y)"), done)

    async def rollback(self, entry: str, chain: bool = False) -> bool:
        from .app import ConfirmScreen

        try:
            r = await self.client.call("learn.rollback", {"id": entry, "chain": chain, "reason": "người rollback từ TUI"}, audit=True)
        except RpcError as e:
            if "chain" in e.message and not chain:
                def again(ok: bool | None) -> None:
                    if ok:
                        self.run_worker(self.rollback(entry, chain=True))

                self.app.push_screen(ConfirmScreen(e.message[:300] + "\n\nRollback cả chuỗi?", yes="Rollback chuỗi (y)"), again)
                return False
            self.notify(e.message, severity="error", timeout=8)
            return False
        self.notify(f"Đã rollback {entry} → {r.get('id')}")
        await self.reload()
        return True

    def action_pin(self) -> None:
        x = self.selected()
        p = (x or {}).get("payload") or {}
        if not x or not x.get("agent_id") or p.get("target") not in ("skill", "memory", "config") or not p.get("target_ref"):
            self.notify("Dòng này không có đích pin được (skill/memory/config)", severity="warning")
            return
        self.run_worker(self.toggle_pin(x["agent_id"], p["target"], str(p["target_ref"])))

    async def toggle_pin(self, agent: str, target: str, ref: str) -> bool:
        try:
            pins = await self.client.call("knowledge.pins", {"agent": agent}) or []
            on = not any(pp.get("target") == target and pp.get("ref") == ref for pp in pins)
            await self.client.call("knowledge.pin", {"agent": agent, "target": target, "ref": ref, "on": on}, audit=True)
        except Exception as e:
            self.notify(_err(e), severity="error")
            return False
        self.notify(("Đã pin " if on else "Đã bỏ pin ") + f"{target} {ref} của {agent} — vòng học {'không' if on else 'lại được'} đụng vào")
        return on

    def action_digest(self) -> None:
        self.run_worker(self._digest())

    async def _digest(self) -> None:
        from .app import InfoScreen

        d = self.digest_today
        if not d or not d.get("text"):
            try:
                d = await self.client.call("digest.run", {"no_send": True}, audit=True)
            except Exception as e:
                self.notify(_err(e), severity="error")
                return
        self.app.push_screen(InfoScreen(Text(str(d.get("text"))[:6000])))

    def action_journey(self) -> None:
        x = self.selected()
        if not x or not x.get("agent_id"):
            self.notify("Chọn một dòng có agent", severity="warning")
            return
        self.run_worker(self._journey(x["agent_id"]))

    async def _journey(self, agent: str) -> None:
        from .app import InfoScreen

        try:
            j = await self.client.call("learn.journey", {"agent": agent, "limit": 30})
        except Exception as e:
            self.notify(_err(e), severity="error")
            return
        t = Text()
        t.append(f"Journey học của {agent}\n\n", style="bold")
        for it in (j.get("items") or [])[-30:]:
            what = it.get("what")
            line = f"{_hm(it.get('ts'))}  {what:<8} {it.get('id', '')[:16]:<16} "
            line += " ".join(str(it.get(k)) for k in ("kind", "status", "op", "target", "target_ref", "actor") if it.get(k))
            t.append(line[:150] + "\n", style={"signal": "yellow", "review": "cyan", "proposal": "white", "ledger": "green"}.get(str(what), ""))
        self.app.push_screen(InfoScreen(t))

    def action_golden(self) -> None:
        self.app.push_screen(GoldenScreen())

    # ------------------------------------------------------------------ H-16: L tắt học, b ledger theo agent
    def _agent_of_selected(self) -> str | None:
        x = self.selected()
        if not x or not x.get("agent_id"):
            self.notify("Chọn một dòng có agent", severity="warning")
            return None
        return str(x["agent_id"])

    def action_toggle_learning(self) -> None:
        agent = self._agent_of_selected()
        if agent:
            self.run_worker(self._toggle_learning(agent))

    async def _toggle_learning(self, agent: str) -> None:
        from .app import ConfirmScreen

        try:
            cur = await self.client.call("learn.policy_get", {"key": f"agents.{agent}.enabled"})
        except RpcError:
            cur = None
        enable = cur is False  # đang tắt → hỏi bật lại; còn lại → hỏi tắt
        msg = (f"BẬT lại học cho agent [b]{agent}[/b]?" if enable else
               f"TẮT học cho agent [b]{agent}[/b]?\nKhông review/đề xuất mới; thay đổi đã áp dụng giữ nguyên (rollback ở Ledger).")

        def done(ok: bool | None) -> None:
            if ok:
                self.run_worker(self.set_learning(agent, enable))

        self.app.push_screen(ConfirmScreen(msg, yes=("Bật (y)" if enable else "Tắt học (y)")), done)

    async def set_learning(self, agent: str, enabled: bool) -> bool:
        try:
            await self.client.call("learn.policy_set", {"key": f"agents.{agent}.enabled", "value": enabled, "parse": False}, audit=True)
        except Exception as e:
            self.notify(_err(e), severity="error")
            return False
        self.notify(f"Đã {'bật' if enabled else 'tắt'} học cho {agent}")
        return True

    def action_ledger(self) -> None:
        agent = self._agent_of_selected()
        if agent:
            self.app.push_screen(LedgerScreen(agent))


class LedgerScreen(Screen):
    """Mục Ledger theo agent (FR-UI-06): mọi entry áp dụng/rollback của agent (`learn.ledger {agent}`), xem before/after
    (`learn.show`), `r` rollback entry (thao tác của NGƯỜI, xác nhận, audit tui)."""

    DEFAULT_CSS = DEFAULT_CSS + """
#ledger-head { height: 1; padding: 0 1; background: $panel; }
#ledger { height: 1fr; margin: 1 1 0 1; }
#ledger-detail { height: 12; padding: 0 1; border-top: solid $primary-darken-2; }
"""
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Về Learnings"),
        Binding("r", "rollback", "Rollback"),
    ]

    def __init__(self, agent_id: str) -> None:
        super().__init__()
        self.agent_id = agent_id
        self.entries: list[dict[str, Any]] = []

    def compose(self) -> ComposeResult:
        yield Static(id="ledger-head")
        yield DataTable(id="ledger", cursor_type="row", zebra_stripes=True)
        yield Static(id="ledger-detail")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#ledger", DataTable)
        for label, key, w in [("LÚC", "ts", 11), ("ENTRY", "id", 16), ("ACTOR", "actor", 18), ("OP", "op", 9), ("ĐÍCH", "target", 30),
                              ("L", "risk", 3), ("CỜ", "flags", 18), ("ROLLBACK BỞI", "rb", 16)]:
            table.add_column(label, key=key, width=w)
        table.focus()
        self.run_worker(self.reload(), exit_on_error=False)

    @property
    def client(self):
        return self.app.client  # type: ignore[attr-defined]

    async def reload(self) -> None:
        try:
            self.entries = await self.client.call("learn.ledger", {"agent": self.agent_id, "limit": 200}) or []
        except RpcError as e:
            self.query_one("#ledger-head", Static).update(Text(" Ledger: " + e.message, style="bold yellow"))
            return
        try:
            self.query_one("#ledger-head", Static).update(Text(f" Ledger · {self.agent_id} · {len(self.entries)} entry (mới nhất trước)", style="bold"))
            table = self.query_one("#ledger", DataTable)
        except NoMatches:
            return
        table.clear()
        for e in self.entries:
            flags = e.get("flags") or []
            table.add_row(Text(_hm(e.get("applied_at")), style="grey62"), e["id"], Text(str(e.get("actor") or ""), style="cyan" if e.get("actor") == "user" else ""),
                          e.get("op") or "", f"{e.get('scope')}:{e.get('target')}:{e.get('target_ref') or ''}"[:40], e.get("risk_class") or "",
                          ",".join(flags if isinstance(flags, list) else [str(flags)])[:24], Text(str(e.get("rolled_back_by") or ""), style="yellow"), key=e["id"])
        await self.show_detail()

    def selected(self) -> dict[str, Any] | None:
        table = self.query_one("#ledger", DataTable)
        if table.row_count == 0:
            return None
        try:
            k = table.coordinate_to_cell_key(table.cursor_coordinate)[0].value
        except Exception:
            return None
        return next((e for e in self.entries if e["id"] == k), None)

    @on(DataTable.RowHighlighted)
    async def _hl(self) -> None:
        try:
            await self.show_detail()
        except NoMatches:
            pass

    async def show_detail(self) -> None:
        w = self.query_one("#ledger-detail", Static)
        e = self.selected()
        if e is None:
            w.update(Text(" (agent chưa có entry ledger nào)", style="grey50"))
            return
        t = Text()
        t.append(f" {e['id']} · {e.get('actor')} · {e.get('op')} {e.get('target')} {e.get('target_ref') or ''}\n", style="bold")
        if e.get("summary"):
            t.append(f" {e['summary']}\n", style="grey70")
        try:
            diff = (await self.client.call("learn.show", {"id": e["id"]})).get("diff")
        except Exception:
            diff = None
        for ln in str(diff or "").splitlines()[:8]:
            st = "green" if ln.startswith("+") and not ln.startswith("+++") else "red" if ln.startswith("-") and not ln.startswith("---") else "grey62"
            t.append(" " + ln[:160] + "\n", style=st)
        if e.get("op") != "rollback" and not e.get("rolled_back_by"):
            t.append(f" r = rollback · CLI: cfharness learn rollback {e['id']}", style="cyan")
        w.update(t)

    def action_rollback(self) -> None:
        from .app import ConfirmScreen

        e = self.selected()
        if not e or e.get("op") == "rollback" or e.get("rolled_back_by"):
            self.notify("Chọn một entry chưa rollback", severity="warning")
            return

        def done(ok: bool | None) -> None:
            if ok:
                self.run_worker(self._rollback(e["id"]))

        self.app.push_screen(ConfirmScreen(f"Rollback {e['id']} ({e.get('target')} {e.get('target_ref') or ''}) của {self.agent_id}?", yes="Rollback (y)"), done)

    async def _rollback(self, entry: str) -> None:
        try:
            r = await self.client.call("learn.rollback", {"id": entry, "reason": "người rollback từ TUI (Ledger)"}, audit=True)
        except RpcError as e:
            self.notify(e.message, severity="error", timeout=8)
            return
        self.notify(f"Đã rollback {entry} → {r.get('id')}")
        await self.reload()


class GoldenScreen(Screen):
    """Quản lý golden task (FR-LN-10, Q-07) — thao tác của NGƯỜI; agent/vòng học chỉ gợi ý."""

    DEFAULT_CSS = DEFAULT_CSS
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Về Learnings"),
        Binding("a", "add_from_run", "Thêm từ run"),
        Binding("enter", "view", "Xem"),
        Binding("e", "edit_checker", "Sửa cách chấm"),
        Binding("delete", "remove", "Xoá mềm"),
        Binding("u", "restore", "Khôi phục"),
        Binding("A", "toggle_all", "Hiện đã xoá"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.tasks: list[dict[str, Any]] = []
        self.show_all = False
        self.candidates: list[dict[str, Any]] = []

    def compose(self) -> ComposeResult:
        yield Static(id="golden-head")
        yield DataTable(id="golden", cursor_type="row", zebra_stripes=True)
        yield Static(id="golden-detail")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#golden", DataTable)
        for label, key, w in [("ID", "id", 13), ("AGENT", "agent", 12), ("CHẤM", "ck", 22), ("TT", "st", 4), ("VER", "ver", 4), ("TIÊU ĐỀ", "title", 40), ("TỪ RUN", "run", 15), ("BỞI", "by", 12)]:
            table.add_column(label, key=key, width=w)
        table.focus()
        self.run_worker(self.reload(), exit_on_error=False)

    @property
    def client(self):
        return self.app.client  # type: ignore[attr-defined]

    async def reload(self) -> None:
        try:
            await self._reload()
        except NoMatches:
            pass

    async def _reload(self) -> None:
        r = await self.client.call("golden.list", {"all": self.show_all})
        self.tasks = r.get("tasks") or []
        counts = r.get("counts") or {}
        agents = []
        try:
            agents = [a["id"] for a in await self.client.call("agent.list", {})]
        except Exception:
            pass
        missing = [a for a in agents if not counts.get(a)]
        self.candidates = []
        for a in missing[:3]:
            try:
                self.candidates += [{**c, "agent_id": a} for c in await self.client.call("golden.candidates", {"agent_id": a, "limit": 3})]
            except Exception:
                pass
        h = Text()
        h.append(" Golden task ", style="bold reverse #e0af68")
        h.append("  (người quản lý — Q-07; agent chỉ gợi ý)  ", style="grey62")
        h.append("  ".join(f"{a}:{n}" for a, n in sorted(counts.items())) or "chưa có", style="bold")
        if missing:
            h.append(f"\n ⚠ chưa có golden task: {', '.join(missing)} — thay đổi L3 của các agent này sẽ bị hoãn (Q-23). `a` để thêm từ run.", style="yellow")
        self.query_one("#golden-head", Static).update(h)
        table = self.query_one("#golden", DataTable)
        prev = self._selected_id()
        table.clear()
        for g in self.tasks:
            table.add_row(Text(g["id"], style="grey62"), g["agent_id"], checker_label(g.get("checker")), Text("on" if g.get("active") else "off", style="green" if g.get("active") else "grey50"),
                          f"v{g.get('version')}", (g.get("title") or "")[:60], g.get("source_run_id") or "—", (g.get("created_by") or "")[:12], key=g["id"])
        ids = [g["id"] for g in self.tasks]
        if prev in ids:
            table.move_cursor(row=ids.index(prev), animate=False)
        await self.show_detail()

    def _selected_id(self) -> str | None:
        table = self.query_one("#golden", DataTable)
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
        w = self.query_one("#golden-detail", Static)
        gid = self._selected_id()
        t = Text()
        if gid:
            g = next(x for x in self.tasks if x["id"] == gid)
            t.append(f" {g.get('title')}\n", style="bold")
            t.append(" prompt: " + g["prompt"].replace("\n", " ")[:200] + "\n", style="grey70")
            t.append(" cách chấm: " + checker_to_spec(g.get("checker") or {})[:200] + "\n", style="cyan")
        if self.candidates:
            t.append(" Run ứng viên (a để thêm): ", style="grey62")
            t.append(", ".join(f"{c['agent_id']}/{c['run_id']}({c['reason']})" for c in self.candidates[:4]), style="grey62")
        if not gid and not self.candidates:
            t.append(" (chưa có golden task — `a` thêm từ run)", style="grey50")
        w.update(t)

    # ------------------------------------------------------------------ actions (của NGƯỜI)
    def action_add_from_run(self) -> None:
        from .app import PromptScreen

        default = self.candidates[0]["run_id"] if self.candidates else ""

        def got_run(run_id: str | None) -> None:
            if not run_id:
                return

            def got_spec(spec: str | None) -> None:
                if spec:
                    self.run_worker(self.add_from_run(run_id, spec))

            self.app.push_screen(PromptScreen(f"Cách chấm cho run {run_id} — regex: … | contains: a | b | judge: <rubric> | json: {{schema}} | cmd: …", ""), got_spec)

        self.app.push_screen(PromptScreen("Thêm golden task từ run — id run (prefill: ứng viên đầu tiên)", default), got_run)

    async def add_from_run(self, run_id: str, spec: str) -> dict[str, Any] | None:
        try:
            ck = parse_checker_spec(spec)
            g = await self.client.call("golden.from_run", {"run_id": run_id, "checker": ck}, audit=True)
        except Exception as e:
            self.notify(_err(e), severity="error", timeout=8)
            return None
        self.notify(f"Đã tạo golden task {g['id']} cho {g['agent_id']}")
        await self.reload()
        return g

    @on(DataTable.RowSelected)
    def _selected(self) -> None:  # Enter trên bảng (DataTable nuốt phím enter)
        self.action_view()

    def action_view(self) -> None:
        gid = self._selected_id()
        if gid:
            self.run_worker(self._view(gid))

    async def _view(self, gid: str) -> None:
        from .app import InfoScreen

        try:
            g = await self.client.call("golden.show", {"id": gid})
        except Exception as e:
            self.notify(_err(e), severity="error")
            return
        t = Text()
        t.append(f"{g['id']} · {g['agent_id']} · v{g.get('version')} · bởi {g.get('created_by')} · {('active' if g.get('active') else 'ĐÃ XOÁ MỀM')}\n\n", style="bold")
        if g.get("context"):
            t.append("ngữ cảnh: " + g["context"] + "\n", style="grey70")
        t.append("prompt:\n" + g["prompt"] + "\n\n")
        t.append("cách chấm: " + json.dumps(g.get("checker"), ensure_ascii=False, indent=1) + "\n\n", style="cyan")
        for r in (g.get("recent_results") or [])[:8]:
            t.append(f"{_hm(r['created_at'])}  {r['variant']:<9} #{r['sample']} điểm {r['score']}  {r['outcome']}\n", style="grey62")
        self.app.push_screen(InfoScreen(t))

    def action_edit_checker(self) -> None:
        from .app import PromptScreen

        gid = self._selected_id()
        if not gid:
            return
        g = next(x for x in self.tasks if x["id"] == gid)

        def done(spec: str | None) -> None:
            if spec:
                self.run_worker(self.update_checker(gid, spec))

        self.app.push_screen(PromptScreen(f"Sửa cách chấm/rubric của {gid} (judge: … / regex: … / …)", checker_to_spec(g.get("checker") or {})), done)

    async def update_checker(self, gid: str, spec: str) -> bool:
        try:
            g = await self.client.call("golden.update", {"id": gid, "changes": {"checker": parse_checker_spec(spec)}}, audit=True)
        except Exception as e:
            self.notify(_err(e), severity="error", timeout=8)
            return False
        self.notify(f"Đã sửa {gid} → v{g['version']}")
        await self.reload()
        return True

    def action_remove(self) -> None:
        from .app import ConfirmScreen

        gid = self._selected_id()
        if not gid:
            return

        def done(ok: bool | None) -> None:
            if ok:
                self.run_worker(self._call("golden.rm", gid, f"Đã xoá mềm {gid} (u để khôi phục khi bật A)"))

        self.app.push_screen(ConfirmScreen(f"Xoá mềm golden task {gid}? (khôi phục được)", yes="Xoá mềm (y)"), done)

    def action_restore(self) -> None:
        gid = self._selected_id()
        if gid:
            self.run_worker(self._call("golden.restore", gid, f"Đã khôi phục {gid}"))

    async def _call(self, method: str, gid: str, ok: str) -> bool:
        try:
            await self.client.call(method, {"id": gid}, audit=True)
        except Exception as e:
            self.notify(_err(e), severity="error")
            return False
        self.notify(ok)
        await self.reload()
        return True

    def action_toggle_all(self) -> None:
        self.show_all = not self.show_all
        self.run_worker(self.reload())
