"""`cfharness chat` — chat qua lại với điều phối viên trên terminal (kênh `cli`, dùng chung hội thoại với TUI `top` → `m`).

Client THUẦN của daemon: `AsyncHubClient(channel="cli")` (mọi ghi gửi `_audit.channel=cli`) + `events.subscribe` (agent = điều phối
viên, level detail) để chữ chạy / thẻ hành động / trả lời tới ngay, không hỏi lại theo chu kỳ. Không RPC mới.

- TTY: `prompt_toolkit` — dòng nhập luôn ở đáy, tin mới in phía trên (`patch_stdout`), thanh trạng thái (spinner, thời gian, tool
  đang dùng, đuôi chữ đang viết), gợi ý lệnh `/`, lịch sử ↑↓ ở `HUB_HOME/cli_history` (0600), Alt-Enter xuống dòng. Gõ khi điều
  phối viên đang làm → tin được steer vào lượt đó. Ctrl-C: đang làm → dừng lượt (`run.stop`); rảnh → lần 2 trong 2 s mới thoát.
- Không phải TTY: đọc từng dòng stdin, gửi rồi CHỜ trả lời, in chữ thường không màu (dùng trong script/test).
- `oneshot()`: `manager chat "<tin>"` — đăng ký event TRƯỚC khi gửi rồi chờ `manager.reply` của hội thoại (thay vòng hỏi 1 s cũ).
Chữ của điều phối viên là DỮ LIỆU: luôn qua `termsafe.clean` + `rich.text.Text` (xem `chatview`).

Agent khác + phê duyệt (`agentsview.py`, vẫn KHÔNG RPC mới — cùng nguồn với `cfharness top`): firehose `events.subscribe` mức summary
(tự đăng ký lại khi `slow_consumer`/mất kết nối) + `tui.snapshot` ~1 s + `question.list`. TTY: bảng agent phía trên thanh trạng thái
(gọn 1 dòng; ↓ khi dòng nhập trống = mở/đóng, Esc đóng). Mọi chế độ: `approval.open|waiting_human|escalated`, `question.open|escalated`
→ thẻ (lệnh ĐẦY ĐỦ, lý do, rủi ro, id, cách xử lý) + chuông (chỉ TTY, chỉ việc cần NGƯỜI — `routed_to == "manager"` thì không).
Lệnh `/agents` (`/ps`), `/stop <agent|run>`, `/attach <agent|run>` (chạy `cfharness attach` rồi quay lại), `/approvals`, `/approve`,
`/deny`, `/always` (2 bước, gửi `always_confirm`), `/answer` — gọi đúng RPC `approval.*`/`question.*`/`run.stop` như TUI/web/Telegram
(daemon kiểm HUMAN_ONLY bằng token người; audit kênh `cli`). Cho phép LUÔN hỏi y/N sau khi in lại lệnh đầy đủ.
"""

from __future__ import annotations

import asyncio
import io
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from rich.console import Console
from rich.text import Text

from ..rpc import DaemonUnavailable, RpcError
from ..termsafe import one_line
from ..tui.client import AsyncHubClient, subscribe
from ..ui import theme as th
from . import agentsview as G
from . import attachments as A
from . import chatview as V

NO_MANAGER = (
    "Chưa có điều phối viên.\n"
    "  Tạo và chỉ định một agent làm điều phối viên (chỉ người làm được):\n"
    "    cfharness agent create dieu-phoi --template manager --name \"Điều phối viên\"\n"
    "    cfharness manager set dieu-phoi\n"
)
REPLAY = 6  # số tin in lại khi mở hội thoại cũ
COMMANDS = set(V.COMMANDS) | {"/ls", "/q", "/h", "/?", "/exit", "/ps"}
SNAPSHOT_S = 1.0  # chu kỳ `tui.snapshot` (như `top`)
QUESTIONS_S = 5.0  # chu kỳ `question.list` phòng lỡ event
STARTUP_CARDS = 3  # số thẻ in khi mở phiên mà đã có yêu cầu chờ
BELL_GAP_S = 1.0


def _err(e: Exception) -> str:
    return e.message if isinstance(e, RpcError) else str(e)


class Out:
    """Đầu ra: `rich.Console` theo `sys.stdout` HIỆN TẠI (để `patch_stdout` của prompt_toolkit bắt được)."""

    def __init__(self, color: bool, file: Any = None, width: int | None = None) -> None:
        self.color = color
        self.console = Console(file=file, force_terminal=color, no_color=not color, color_system="auto" if color else None,
                               highlight=False, markup=False, emoji=False, width=width)
        self._held: list[Any] | None = None

    def write(self, renderable: Any) -> None:
        if self._held is not None:  # đang nhường terminal (`/attach` mở TUI) → in sau
            self._held.append(renderable)
            return
        self.console.print(renderable)

    def hold(self) -> None:
        if self._held is None:
            self._held = []

    def release(self) -> None:
        held, self._held = self._held or [], None
        for r in held:
            self.console.print(r)

    def clear(self) -> None:
        if self.color:
            self.console.clear()


def to_ansi(text: Text, width: int) -> str:
    buf = io.StringIO()
    Console(file=buf, force_terminal=True, color_system="256", width=max(20, width), highlight=False, markup=False,
            emoji=False).print(text, end="", no_wrap=True, overflow="ellipsis", crop=True)
    return buf.getvalue()


class ChatSession:
    """Lõi phiên chat (bất đồng bộ, không phụ thuộc giao diện nhập): mở hội thoại, gửi, nhận event, lệnh `/`."""

    def __init__(self, client: AsyncHubClient, out: Out, *, conversation_id: str | None = None, new: bool = False,
                 replay: int = REPLAY, monitor: bool = True, home: Path | None = None) -> None:
        self.client = client
        self.out = out
        self.want_conv = conversation_id
        self.new = new
        self.replay = replay
        self.pasted: dict[int, A.Attachment] = {}  # ảnh dán bằng Ctrl-V, theo số `[Ảnh #n]` trong dòng đang gõ
        self.state: V.ChatState | None = None
        self.changed = asyncio.Event()
        self._pump_task: asyncio.Task | None = None
        self._sub: Any = None
        self._inflight = 0
        self._held: list[dict[str, Any]] = []
        self.closing = False
        self.lost = False
        # agent khác + phê duyệt (agentsview)
        self.monitor = monitor
        self.home = home
        self.mon: G.AgentMonitor | None = None
        self.panel_open = False
        self.ask: Callable[[str], Awaitable[str]] | None = None  # hỏi y/N (TTY: prompt riêng; plain: dòng stdin kế); None = luôn "không"
        self.bell: Callable[[], None] | None = None  # chuông terminal (chỉ TTY)
        self.launcher: Callable[[list[str]], Awaitable[int]] | None = None  # chạy `cfharness attach` (chỉ TTY)
        self._last_bell = 0.0
        self._bg: set[asyncio.Task] = set()
        self._fire_sub: Any = None
        self._snap_task: asyncio.Task | None = None
        self._q_task: asyncio.Task | None = None

    # ------------------------------------------------------------------ mở / đóng
    async def open(self) -> bool:
        g = await self.client.call("manager.get") or {}
        mid = g.get("agent_id")
        if not mid:
            self.out.write(Text(f"{th.glyph('mark')} ", style=th.MGR) + Text(NO_MANAGER))
            return False
        st = await self.client.call("hub.status") or {}
        name = mid
        try:
            ag = await self.client.call("agent.get", {"id": mid}) or {}
            name = str(ag.get("name") or mid)
        except RpcError:
            pass
        cid, title, rows = None, None, []
        if self.want_conv:
            conv = await self._find_conv(self.want_conv)
            if conv is None:
                raise RpcError(-32004, f"không có hội thoại '{self.want_conv}' trên kênh cli của điều phối viên hiện tại (/list để xem)")
            cid, title = conv["id"], conv.get("title")
        elif not self.new:
            convs = await self.client.call("manager.conversations", {"channel": V.CHANNEL}) or []
            if convs:
                cid, title = convs[0]["id"], convs[0].get("title")
        if cid and self.replay:
            rows = await self.client.call("manager.history", {"conversation_id": cid, "limit": self.replay}) or []
        self.state = V.ChatState(mid, cid, name=one_line(name, 40))
        self.state.last_seq = int(st.get("last_seq") or 0)
        self.state.epoch = st.get("epoch")
        items = V.history_items(rows)
        self.out.write(V.banner(mid, cid, title=title, resumed=len(items)))
        for it in items:
            self.out.write(V.render(it, manager_name=self.state.name))
        await self._resume(rows)
        self._pump_task = asyncio.ensure_future(self._pump())
        if self.monitor:
            self.state.show_approvals = False  # thẻ duyệt đến từ firehose (mọi agent, kể cả điều phối viên) — không in hai lần
            self.mon = G.AgentMonitor(mid)
            await self.refresh_snapshot()
            await self.refresh_questions()
            await self._startup_cards()
            for coro in (self._firehose(), self._poll()):
                self._spawn(coro)
        return True

    async def close(self) -> None:
        self.closing = True
        if self._sub is not None:
            self._sub.close()
        if self._fire_sub is not None:
            self._fire_sub.close()
        tasks = [t for t in (self._pump_task, self._snap_task, self._q_task, *self._bg) if t is not None]
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    def _spawn(self, coro: Any) -> asyncio.Task:
        t = asyncio.ensure_future(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)
        return t

    # ------------------------------------------------------------------ agent khác + phê duyệt (firehose + snapshot)
    async def refresh_snapshot(self) -> bool:
        if self.mon is None:
            return False
        try:
            snap = await self.client.call("tui.snapshot")
        except (RpcError, DaemonUnavailable, OSError, ValueError, asyncio.TimeoutError):
            return False
        self.mon.load_snapshot(snap or {})
        return True

    async def refresh_questions(self) -> None:
        if self.mon is None:
            return
        try:
            rows = await self.client.call("question.list", {"status": "pending", "limit": 200})
        except (RpcError, DaemonUnavailable, OSError, ValueError, asyncio.TimeoutError):
            return
        self.mon.load_questions(rows or [])

    def _kick_snapshot(self) -> None:
        if self._snap_task is None or self._snap_task.done():
            self._snap_task = asyncio.ensure_future(self.refresh_snapshot())

    def _kick_questions(self) -> None:
        if self._q_task is None or self._q_task.done():
            self._q_task = asyncio.ensure_future(self.refresh_questions())

    async def _poll(self) -> None:
        last_q = time.monotonic()
        while not self.closing:
            await asyncio.sleep(SNAPSHOT_S)
            await self.refresh_snapshot()
            if time.monotonic() - last_q >= QUESTIONS_S:
                last_q = time.monotonic()
                await self.refresh_questions()

    async def _firehose(self) -> None:
        """Firehose mức summary MỌI agent (như `top`). Bị ngắt (`slow_consumer`, daemon restart) → nạp snapshot, đăng ký lại từ seq cuối."""
        mon = self.mon
        assert mon is not None
        while not self.closing:
            params: dict[str, Any] = {"level": "summary", "since_seq": mon.model.last_seq, "epoch": mon.model.epoch}
            self._fire_sub = subscribe(self.client, params)
            try:
                async for msg in self._fire_sub:
                    if "subscribed" in msg:
                        continue
                    if "end" in msg:
                        break
                    self.on_hub_event(msg)
            except (DaemonUnavailable, RpcError, OSError, ValueError):
                pass
            if self.closing:
                return
            await asyncio.sleep(1.0)
            await self.refresh_snapshot()

    def on_hub_event(self, ev: dict[str, Any]) -> None:
        mon = self.mon
        if mon is None:
            return
        notices, need = mon.apply(ev)
        if need:
            self._kick_snapshot()
        if str(ev.get("type") or "").startswith("question."):
            self._kick_questions()
        for n in notices:
            self._spawn(self._show(n))

    def ring(self) -> None:
        now = time.monotonic()
        if self.bell is None or now - self._last_bell < BELL_GAP_S:
            return
        self._last_bell = now
        try:
            self.bell()
        except Exception:  # pragma: no cover - terminal đã đóng
            pass

    async def _approval(self, rid: str, fallback: dict[str, Any] | None = None) -> dict[str, Any] | None:
        try:
            return await self.client.call("approval.get", {"id": rid})
        except (RpcError, DaemonUnavailable, OSError, ValueError, asyncio.TimeoutError):
            return fallback

    async def _show(self, n: dict[str, Any]) -> None:
        """Thông báo của `AgentMonitor.apply` → thẻ/dòng ghi chú (lấy chi tiết đầy đủ qua `approval.get`/`question.get`)."""
        mon = self.mon
        assert mon is not None
        k = n.get("kind")
        if k == "approval":
            p = n.get("payload") or {}
            fb = {"id": n["id"], "agent_id": n.get("agent_id"), "run_id": n.get("run_id"), "tool_name": p.get("tool"), "preview": p.get("preview"),
                  "reason": p.get("reason"), "risk_level": p.get("risk_level"), "destructive_class": p.get("destructive_class"),
                  "matched_rule": p.get("matched_rule"), "status": "waiting_human" if n.get("event") == "approval.waiting_human" else "open"}
            ap = await self._approval(n["id"], fb)
            if not ap or ap.get("status") not in ("open", "waiting_human"):
                mon.carded.pop(n["id"], None)
                return
            route = "human" if n.get("event") == "approval.escalated" else mon.approval_route(ap)
            self.out.write(G.approval_card(ap, agent_name=mon.agent_name(ap.get("agent_id")), routed=route, event=n.get("event")))
            if route == "human":
                self.ring()
        elif k == "question":
            q = dict(n.get("payload") or {})
            try:
                q = {**q, **(await self.client.call("question.get", {"id": n["id"]}) or {})}
            except (RpcError, DaemonUnavailable, OSError, ValueError, asyncio.TimeoutError):
                pass
            if q.get("status") not in (None, "open", "with_manager"):
                mon.carded.pop(n["id"], None)
                return
            route = "human" if n.get("event") == "question.escalated" else mon.question_route(q)
            self.out.write(G.question_card(q, agent_name=mon.agent_name(q.get("agent_id")), routed=route, event=n.get("event")))
            if route == "human":
                self.ring()
        elif n.get("resolved") is not None:
            self.out.write(V.render(G.resolved_note(n["id"], n["resolved"])))
        elif n.get("waiting_human"):
            self._note(f"{th.STATUS['approval']['waiting_human'].icon} {n['id']}: run đã tạm dừng chờ bạn — /approve {n['id']} · /deny {n['id']}", "warn")
            self.ring()
        elif n.get("question_done"):
            p = n.get("payload") or {}
            if n["question_done"] == "question.answered":
                txt = (f"câu hỏi {n['id']}: đã trả lời" + (f" bởi {one_line(p.get('by'), 30)}" if p.get("by") else "")
                       + (f" qua {one_line(p.get('channel'), 30)}" if p.get("channel") else ""))
            else:
                txt = f"câu hỏi {n['id']}: đã huỷ"
            self._note(txt)

    async def _startup_cards(self) -> None:
        """Mở phiên khi đã có yêu cầu/câu hỏi chờ → in thẻ (tối đa STARTUP_CARDS cái cần người), phần còn lại gợi ý /approvals."""
        mon = self.mon
        assert mon is not None
        human: list[tuple[str, dict[str, Any]]] = []
        for ap in mon.pending_approvals():
            route = mon.approval_route(ap)
            mon.carded[str(ap["id"])] = route
            if route == "human":
                human.append(("approval", ap))
        for q in mon.questions.values():
            route = mon.question_route(q)
            mon.carded[str(q["id"])] = route
            if route == "human":
                human.append(("question", q))
        for kind, x in human[:STARTUP_CARDS]:
            if kind == "approval":
                full = await self._approval(str(x["id"]), x) or x
                self.out.write(G.approval_card(full, agent_name=mon.agent_name(x.get("agent_id")), routed="human"))
            else:
                self.out.write(G.question_card(x, agent_name=mon.agent_name(x.get("agent_id")), routed="human"))
        if len(human) > STARTUP_CARDS:
            self._note(f"+{len(human) - STARTUP_CARDS} yêu cầu/câu hỏi khác đang chờ bạn — /approvals", "warn")
        if human:
            self.ring()

    def panel(self, width: int, height: int) -> list[Text]:
        """Dòng bảng agent cho toolbar (TTY). Hết việc → tự đóng."""
        if self.mon is None:
            return []
        lines = G.panel_lines(self.mon, width, height, expanded=self.panel_open)
        if not lines:
            self.panel_open = False
        return lines

    def panel_visible(self) -> bool:
        return self.mon is not None and G.summary_line(self.mon, 200) is not None

    # ------------------------------------------------------------------ lệnh agent / duyệt
    def _note(self, text: str, tone: str | None = None) -> None:
        self.out.write(V.render({"kind": "note", "text": text, "tone": tone}))

    def _monitor(self) -> G.AgentMonitor:
        if self.mon is None:
            self.mon = G.AgentMonitor(self.state.manager_id if self.state else None)
        return self.mon

    async def confirm(self, prompt: str) -> bool:
        if self.ask is None:
            self._note("cần xác nhận y/N nhưng không có dòng nhập — đã huỷ", "warn")
            return False
        try:
            ans = await self.ask(prompt)
        except (KeyboardInterrupt, EOFError):
            ans = ""
        ok = G.is_yes(ans)
        if not ok:
            self._note("đã huỷ (không làm gì)")
        return ok

    async def cmd_agents(self) -> None:
        mon = self._monitor()
        await self.refresh_snapshot()
        self.out.write(G.agents_table(mon, width=self.out.console.width))

    async def cmd_approvals(self) -> None:
        mon = self._monitor()
        aps = await self.client.call("approval.list", {"status": "pending", "limit": 100}) or []
        qs = await self.client.call("question.list", {"status": "pending", "limit": 100}) or []
        self.out.write(G.pending_text(mon, aps, qs))

    async def cmd_decide(self, arg: str, kind: str) -> None:
        """`/approve` (y/N) · `/deny` · `/always` (2 bước). Lệnh ĐẦY ĐỦ luôn được in lại trước khi hỏi."""
        rid, _, note = arg.partition(" ")
        rid, note = rid.strip(), note.strip()
        usage = {"approve": "/approve <id> [ghi chú]", "deny": "/deny <id> [lý do]", "always": "/always <id>"}[kind]
        if not rid:
            self._note(f"cần id yêu cầu: {usage} (xem /approvals)", "warn")
            return
        ap = await self.client.call("approval.get", {"id": rid}) or {}
        aid = str(ap.get("id") or rid)
        if ap.get("status") not in ("open", "waiting_human"):
            by = f" (bởi {one_line(ap.get('resolved_by'), 30)} qua {one_line(ap.get('resolved_channel'), 30)})" if ap.get("resolved_by") else ""
            self._note(f"yêu cầu {aid} đã {ap.get('status')}{by}", "warn")
            return
        name = self._monitor().agent_name(ap.get("agent_id"))
        if kind == "deny":
            await self.client.call("approval.deny", {"id": aid, "note": note or None, "channel": V.CHANNEL, "by": "user"}, audit=True)
            self._note(f"đã TỪ CHỐI {aid} ({one_line(ap.get('preview') or ap.get('tool_name'), 80)})", "warn")
            return
        self.out.write(G.approval_card(ap, agent_name=name, routed="human"))
        params: dict[str, Any] = {"id": aid, "note": note or None, "channel": V.CHANNEL, "by": "user"}
        if kind == "always":
            offer = ap.get("always_offer")
            if not offer:
                self._note(f"yêu cầu {aid} không đủ điều kiện 'luôn cho phép' (phá huỷ/vùng bảo vệ) — chỉ cho phép MỘT lần: /approve {aid}", "warn")
                return
            self.out.write(G.always_rule_text(offer))
            if not await self.confirm(f"Bước 1/2 — cho phép {aid} lần này VÀ tạo luật luôn cho phép ở trên? [y/N] "):
                return
            if not await self.confirm(f"Bước 2/2 — xác nhận LUÔN cho phép (agent {name} sẽ không phải hỏi lại thao tác này) [y/N] "):
                return
            # như MCP hub_approve: nhắc lại đúng fingerprint/host của luật sẽ tạo — luật đổi giữa chừng → daemon từ chối, approval giữ nguyên
            params.update({"always": True, "always_confirm": str(offer.get("fingerprint") or offer.get("domain") or "")})
        elif not await self.confirm(f"Cho phép MỘT lần {aid} ({one_line(ap.get('tool_name'), 40)}) của agent {name}? [y/N] "):
            return
        res = await self.client.call("approval.approve", params, audit=True) or {}
        rule = res.get("always_rule") if isinstance(res, dict) else None
        self._note(f"đã CHO PHÉP {aid}" + (f" + luật luôn cho phép {rule.get('id')} (gỡ: cfharness guard always rm {rule.get('id')})" if rule
                                           else " (một lần) — run sẽ chạy tiếp"))

    async def cmd_answer(self, arg: str) -> None:
        """`/answer <id> 1,2 [chữ]` hoặc `/answer <id> <chữ>`."""
        qid, _, rest = arg.partition(" ")
        qid, rest = qid.strip(), rest.strip()
        if not qid:
            self._note("cần id câu hỏi: /answer <id> 1,2 [chữ] (xem /approvals)", "warn")
            return
        q = await self.client.call("question.get", {"id": qid}) or {}
        if q.get("status") not in ("open", "with_manager"):
            self._note(f"câu hỏi {one_line(q.get('id') or qid, 40)} đã {q.get('status')}", "warn")
            return
        keys: list[str] = []
        text = rest
        opts = {str(o.get("key")) for o in q.get("options") or []}
        if opts and rest:
            first, _, tail = rest.partition(" ")
            cand = [k for k in re.split(r"[,;]+", first) if k]
            if cand and all(k in opts for k in cand):
                keys, text = cand, tail.strip()
        name = self._monitor().agent_name(q.get("agent_id"))
        if not keys and not text:
            self.out.write(G.question_card(q, agent_name=name))
            return
        if q.get("action"):  # xin phép: trả lời = hub thực hiện thao tác → hỏi y/N như /approve
            self.out.write(G.question_card(q, agent_name=name))
            if not await self.confirm(f"Gửi trả lời {','.join(keys) or one_line(text, 40)} cho câu hỏi xin phép {q['id']}? [y/N] "):
                return
        res = await self.client.call("question.answer", {"id": q["id"], "keys": keys, "text": text or None, "channel": V.CHANNEL}, audit=True) or {}
        ans = res.get("answer") or {}
        picked = ", ".join(f"{k}. {one_line(lbl, 40)}" for k, lbl in zip(ans.get("keys") or [], ans.get("labels") or []))
        self._note(f"đã trả lời {q['id']}: {picked or one_line(text, 80)}")

    async def cmd_stop(self, arg: str) -> None:
        """`/stop <agent|run>`: dừng run của agent khác (hỏi y/N)."""
        mon = self._monitor()
        await self.refresh_snapshot()
        r = mon.find_agent(arg)
        if r is not None:
            rid = r["current_run_id"] or r["waiting_human"]
            if not rid:
                self._note(f"agent {one_line(r['name'] or r.id, 40)} không có run đang chạy", "warn")
                return
            label = f"agent {one_line(r['name'] or r.id, 40)}" + (f" — {one_line(r['task'], 60)}" if r["task"] else "")
        else:
            run = await self.client.call("run.get", {"id": arg}) or {}
            rid = run.get("id")
            if not rid or run.get("status") not in ("queued", "running", "waiting_human"):
                self._note(f"run {one_line(arg, 40)} không còn chạy ({run.get('status')})", "warn")
                return
            label = f"agent {one_line(run.get('agent_id'), 40)}"
        if not await self.confirm(f"Dừng run {rid} ({label})? [y/N] "):
            return
        await self.client.call("run.stop", {"id": rid}, audit=True)
        self._note(f"đã yêu cầu dừng run {rid}", "warn")

    async def cmd_attach(self, arg: str) -> None:
        """`/attach <agent|run>`: chạy `cfharness attach` (màn attach của `top`) trên cùng terminal; thoát TUI → quay lại chat."""
        ref = arg.strip()
        if not ref or ref.startswith("-") or " " in ref:
            self._note("cần id agent hoặc run: /attach <agent|run>", "warn")
            return
        if self.mon is not None:
            r = self.mon.find_agent(ref)
            if r is not None:
                ref = r.id
        if self.launcher is None:
            self._note(f"chỉ mở được trên terminal — chạy: cfharness attach {ref}")
            return
        argv = [sys.executable, "-m", "cfharness", "attach", ref] + (["--home", str(self.home)] if self.home else [])
        self.out.hold()
        try:
            rc = await self.launcher(argv)
        finally:
            self.out.release()
        if rc:
            self._note(f"cfharness attach {ref} thoát mã {rc}", "warn")

    async def _resume(self, rows: list[dict[str, Any]]) -> None:
        st = self.state
        assert st is not None
        rid = V.pending_run(rows)
        run = None
        if rid:
            try:
                run = await self.client.call("run.get", {"id": rid})
            except RpcError:
                run = None
        st.resume(rows, run)
        if st.busy:
            self.out.write(V.render({"kind": "note", "text": f"điều phối viên vẫn đang xử lý tin trước (run {st.active_run})"}))

    async def _find_conv(self, cid: str) -> dict[str, Any] | None:
        convs = await self.client.call("manager.conversations", {"channel": V.CHANNEL, "limit": 500}) or []
        return next((c for c in convs if c.get("id") == cid), None)

    # ------------------------------------------------------------------ luồng event
    async def _pump(self) -> None:
        st = self.state
        assert st is not None
        delay = 0.2
        while not self.closing:
            params = {"agent_id": st.manager_id, "level": "detail", "since_seq": st.last_seq, "epoch": st.epoch}
            self._sub = subscribe(self.client, params)
            try:
                async for msg in self._sub:
                    if "subscribed" in msg:
                        if self.lost:
                            self.lost = False
                            self.out.write(V.render({"kind": "note", "text": "đã nối lại với daemon"}))
                        delay = 0.2
                        continue
                    if "end" in msg:
                        break
                    self._apply(msg)
            except (DaemonUnavailable, RpcError, OSError, ValueError):
                pass
            if self.closing:
                return
            if not self.lost:
                self.lost = True
                self.out.write(V.render({"kind": "note", "text": "mất kết nối luồng sự kiện — đang nối lại…", "tone": "warn"}))
            await asyncio.sleep(delay)
            delay = min(delay * 2, 3.0)

    def _apply(self, ev: dict[str, Any]) -> None:
        st = self.state
        assert st is not None
        if self._inflight:
            # tin đang gửi chưa có run_id/conversation_id → giữ event lại, áp sau khi `manager.chat` trả về
            self._held.append(ev)
            st.last_seq = max(st.last_seq, int(ev.get("seq") or 0))
            return
        for it in st.apply(ev):
            self.out.write(V.render(it, manager_name=st.name))
        self.changed.set()

    # ------------------------------------------------------------------ gửi / dừng
    async def send(self, text: str) -> dict[str, Any] | None:
        st = self.state
        assert st is not None
        parsed = A.parse_input(text.strip(), self.pasted)
        self.pasted = {}
        for err in parsed.errors:
            self.out.write(V.render({"kind": "note", "text": err, "tone": "warn"}))
        text = parsed.text
        if not text and not parsed.images:
            return None
        self.out.write(V.render({"kind": "user", "text": text or "(ảnh)"}))
        if parsed.images:
            self.out.write(V.render({"kind": "note", "text": "📎 " + ", ".join(f"{a.name} ({len(a.data) // 1024} KB)" for a in parsed.images)}))
        params: dict[str, Any] = {"channel": V.CHANNEL, "text": text}
        if parsed.images:
            params["images"] = [a.wire() for a in parsed.images]
        if st.conversation_id:
            params["conversation_id"] = st.conversation_id
        self._inflight += 1
        try:
            res = await self.client.call("manager.chat", params, audit=True) or {}
        except (RpcError, DaemonUnavailable) as e:
            self.out.write(V.render({"kind": "note", "text": f"chưa gửi được: {_err(e)}", "tone": "err"}))
            return None
        finally:
            self._inflight -= 1
            held, self._held = self._held, []
        for it in st.sent(res, text):
            self.out.write(V.render(it))
        for ev in held:
            for it in st.apply(ev):
                self.out.write(V.render(it, manager_name=st.name))
        self.changed.set()
        return res

    async def stop_turn(self) -> bool:
        st = self.state
        if st is None or not st.busy or not st.active_run:
            self.out.write(V.render({"kind": "note", "text": "không có lượt nào đang chạy"}))
            return False
        try:
            await self.client.call("run.stop", {"id": st.active_run}, audit=True)
            self.out.write(V.render({"kind": "note", "text": f"đã yêu cầu dừng lượt {st.active_run}", "tone": "warn"}))
            return True
        except (RpcError, DaemonUnavailable) as e:
            self.out.write(V.render({"kind": "note", "text": f"không dừng được: {_err(e)}", "tone": "err"}))
            return False

    async def wait_idle(self, timeout: float) -> bool:
        st = self.state
        assert st is not None
        deadline = time.monotonic() + timeout
        while st.busy:
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            self.changed.clear()
            try:
                await asyncio.wait_for(self.changed.wait(), min(left, 1.0))
            except asyncio.TimeoutError:
                pass
        return True

    # ------------------------------------------------------------------ lệnh
    async def handle(self, line: str) -> bool:
        """Một dòng người gõ. Trả False khi người muốn thoát."""
        s = line.strip()
        if not s:
            return True
        if not A.is_command(s, COMMANDS):  # đường dẫn kéo vào (`/Users/…`) là chữ, không phải lệnh
            await self.send(s[1:] if s.startswith("//") else s)
            return True
        cmd, _, arg = s.partition(" ")
        cmd, arg = cmd.lower(), arg.strip()
        st = self.state
        assert st is not None
        try:
            if cmd in ("/quit", "/exit", "/q"):
                return False
            if cmd in ("/help", "/h", "/?"):
                self.out.write(V.help_text())
            elif cmd == "/new":
                st.switch(None)
                self.out.write(V.render({"kind": "note", "text": "hội thoại mới — tin tiếp theo sẽ mở hội thoại mới"}))
            elif cmd in ("/list", "/ls"):
                rows = await self.client.call("manager.conversations", {"channel": V.CHANNEL}) or []
                self.out.write(V.conv_list_text(rows[:20], st.conversation_id))
            elif cmd == "/open":
                if not arg:
                    self.out.write(V.render({"kind": "note", "text": "cần id hội thoại: /open <id> (xem /list)", "tone": "warn"}))
                    return True
                conv = await self._find_conv(arg)
                if conv is None:
                    self.out.write(V.render({"kind": "note", "text": f"không có hội thoại '{one_line(arg, 40)}' (xem /list)", "tone": "err"}))
                    return True
                st.switch(conv["id"])
                rows = await self.client.call("manager.history", {"conversation_id": conv["id"], "limit": max(self.replay, 10)}) or []
                items = V.history_items(rows)
                self.out.write(V.banner(st.manager_id, conv["id"], title=conv.get("title"), resumed=len(items)))
                for it in items:
                    self.out.write(V.render(it, manager_name=st.name))
                await self._resume(rows)
            elif cmd == "/history":
                if not st.conversation_id:
                    self.out.write(V.render({"kind": "note", "text": "hội thoại mới — chưa có lịch sử"}))
                    return True
                n = int(arg) if arg.isdigit() else 20
                rows = await self.client.call("manager.history", {"conversation_id": st.conversation_id, "limit": max(1, min(n, 500))}) or []
                for it in V.history_items(rows):
                    self.out.write(V.render(it, manager_name=st.name))
            elif cmd == "/stop":
                if arg:
                    await self.cmd_stop(arg)
                else:
                    await self.stop_turn()
            elif cmd in ("/agents", "/ps"):
                await self.cmd_agents()
            elif cmd == "/attach":
                await self.cmd_attach(arg)
            elif cmd == "/approvals":
                await self.cmd_approvals()
            elif cmd in ("/approve", "/deny", "/always"):
                await self.cmd_decide(arg, cmd[1:])
            elif cmd == "/answer":
                await self.cmd_answer(arg)
            elif cmd == "/clear":
                self.out.clear()
            else:
                self.out.write(V.render({"kind": "note", "text": f"lệnh không rõ {one_line(cmd, 20)} — /help (muốn gửi chữ bắt đầu bằng / thì gõ //…)", "tone": "warn"}))
        except (RpcError, DaemonUnavailable) as e:
            self.out.write(V.render({"kind": "note", "text": _err(e), "tone": "err"}))
        return True


# ---------------------------------------------------------------------- giao diện nhập
def _arg_choices(session: ChatSession, cmd: str) -> list[tuple[str, str]]:
    """Gợi ý tham số đầu: id approval (/approve /deny /always), câu hỏi (/answer), agent đang chạy (/stop /attach)."""
    mon = session.mon
    if mon is None:
        return []
    if cmd in ("/approve", "/deny", "/always"):
        return [(str(a["id"]), one_line(f"{a.get('agent_id')}: {a.get('preview') or a.get('tool_name')}", 50))
                for a in mon.pending_approvals() if cmd != "/always" or a.get("always_offer")]
    if cmd == "/answer":
        return [(qid, one_line(f"{q.get('agent_id')}: {q.get('text')}", 50)) for qid, q in mon.questions.items()]
    if cmd in ("/stop", "/attach"):
        return [(r.id, one_line(f"{V.AGENT_STATE.get(s, ('', s, ''))[1]} · {r['task'] or ''}", 50)) for s, r in mon.active()]
    return []


def _history_file(home: Path) -> str:
    p = Path(home) / "cli_history"
    try:
        fd = os.open(p, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        os.close(fd)
        os.chmod(p, 0o600)
    except OSError:
        pass
    return str(p)


async def run_tty(session: ChatSession, home: Path) -> int:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.application import get_app
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.filters import Condition, has_completions
    from prompt_toolkit.formatted_text import ANSI
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.patch_stdout import patch_stdout
    from prompt_toolkit.styles import Style

    class SlashCompleter(Completer):
        def get_completions(self, document: Any, complete_event: Any) -> Any:
            t = document.text_before_cursor
            if not t.startswith("/"):
                return
            if " " in t:  # tham số đầu của lệnh agent/duyệt: gợi ý id đang có
                cmd, _, part = t.partition(" ")
                if " " in part:
                    return
                for ident, meta in _arg_choices(session, cmd.lower()):
                    if ident.startswith(part):
                        yield Completion(ident, start_position=-len(part), display_meta=meta)
                return
            for c, desc in V.HELP:
                name = c.split()[0]
                if name.startswith(t):
                    yield Completion(name, start_position=-len(t), display_meta=desc)

    kb = KeyBindings()

    @kb.add("escape", "enter")
    def _newline(event: Any) -> None:
        event.current_buffer.insert_text("\n")

    @kb.add("c-v")
    def _paste(event: Any) -> None:
        """Ctrl-V: ảnh trong clipboard → `[Ảnh #n]` (gửi kèm tin); không có ảnh → dán chữ trong clipboard."""
        img = A.clipboard_image()
        if img:
            if len(img) > A.MAX_IMAGE_BYTES:
                session.out.write(V.render({"kind": "note", "text": f"ảnh trong clipboard quá lớn ({len(img) // 1024} KB)", "tone": "warn"}))
                return
            n = max(session.pasted, default=0) + 1
            session.pasted[n] = A.Attachment(f"clipboard-{n}.png", img)
            event.current_buffer.insert_text(A.MARKER.format(n=n) + " ")
            return
        txt = A.clipboard_text()
        if txt:
            event.current_buffer.insert_text(txt)

    can_toggle = Condition(lambda: session.panel_visible() and not get_app().current_buffer.text)

    @kb.add("down", filter=can_toggle & ~has_completions)
    def _toggle_panel(event: Any) -> None:
        """↓ khi dòng nhập trống: mở/đóng bảng agent (có chữ → ↓ vẫn là lịch sử như cũ)."""
        session.panel_open = not session.panel_open
        event.app.invalidate()

    @kb.add("escape", filter=Condition(lambda: session.panel_open))
    def _close_panel(event: Any) -> None:
        session.panel_open = False
        event.app.invalidate()

    def toolbar() -> Any:
        st = session.state
        if st is None:
            return ""
        try:
            size = get_app().output.get_size()
            width, height = size.columns, size.rows
        except Exception:
            try:
                width, height = os.get_terminal_size()
            except OSError:
                width, height = 100, 30
        lines = session.panel(width, height) + [st.status_text(width)]
        return ANSI("\n".join(to_ansi(t, width) for t in lines))

    async def ask(prompt: str) -> str:
        try:
            return await PromptSession().prompt_async(prompt)  # chuỗi thường: không diễn giải ANSI/markup
        except (KeyboardInterrupt, EOFError):
            return ""

    async def launch(argv: list[str]) -> int:
        return await asyncio.to_thread(subprocess.call, argv)

    def bell() -> None:
        sys.stdout.write("\a")
        sys.stdout.flush()

    session.ask, session.launcher, session.bell = ask, launch, bell

    ps: PromptSession = PromptSession(
        history=FileHistory(_history_file(home)), completer=SlashCompleter(), complete_while_typing=True, key_bindings=kb,
        bottom_toolbar=toolbar, refresh_interval=0.12, erase_when_done=True,
        style=Style.from_dict({"bottom-toolbar": "noreverse", "bottom-toolbar.text": ""}),
    )
    last_ctrl_c = 0.0
    with patch_stdout(raw=True):
        while True:
            try:
                line = await ps.prompt_async(ANSI("\x1b[1;36m❯\x1b[0m "))
            except KeyboardInterrupt:
                st = session.state
                if st is not None and st.busy:
                    await session.stop_turn()
                    continue
                now = time.monotonic()
                if now - last_ctrl_c < 2.0:
                    break
                last_ctrl_c = now
                session.out.write(V.render({"kind": "note", "text": "Ctrl-C lần nữa (hoặc Ctrl-D) để thoát"}))
                continue
            except EOFError:
                break
            if not await session.handle(line):
                break
    return 0


async def run_plain(session: ChatSession, *, timeout: float, readline: Callable[[], str] | None = None) -> int:
    """Không phải TTY: mỗi dòng stdin → lệnh/tin; tin thì chờ trả lời trước khi đọc dòng kế. Hết giờ chờ → mã 2."""
    loop = asyncio.get_running_loop()
    rl = readline or sys.stdin.readline

    async def ask(prompt: str) -> str:  # xác nhận y/N = dòng stdin kế tiếp (hết dòng → "không")
        session.out.write(Text(prompt))
        return await loop.run_in_executor(None, rl) or ""

    session.ask = ask
    code = 0
    while True:
        line = await loop.run_in_executor(None, rl)
        if not line:
            break
        if not await session.handle(line):
            break
        st = session.state
        if st is not None and st.busy and not await session.wait_idle(timeout):
            session.out.write(V.render({"kind": "note", "text": "hết thời gian chờ trả lời", "tone": "warn"}))
            code = 2
    st = session.state
    if st is not None and st.busy and not await session.wait_idle(timeout):
        code = 2
    return code


async def repl(sock: Path, home: Path, *, conversation_id: str | None, new: bool, timeout: float, tty: bool | None = None) -> int:
    tty = sys.stdin.isatty() and sys.stdout.isatty() if tty is None else tty
    client = AsyncHubClient(sock, channel="cli")
    session = ChatSession(client, Out(color=tty and not os.environ.get("NO_COLOR")), conversation_id=conversation_id, new=new, home=home)
    if not await session.open():
        return 1
    try:
        if tty:
            return await run_tty(session, home)
        return await run_plain(session, timeout=timeout)
    finally:
        await session.close()


# ---------------------------------------------------------------------- một lần (`manager chat "<tin>"`)
async def oneshot(sock: Path, params: dict[str, Any], *, wait: bool, timeout: float,
                  on_notify: Callable[[str], None] | None = None) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Gửi một tin; `wait` → chờ `manager.reply` (kind=reply) của hội thoại. Trả `(kết quả manager.chat, payload trả lời | None khi hết giờ)`.
    Đăng ký event TRƯỚC khi gửi nên không lỡ trả lời tới nhanh."""
    client = AsyncHubClient(sock, channel="cli")
    if not wait:
        return await client.call("manager.chat", params, audit=True) or {}, None
    g = await client.call("manager.get") or {}
    mid = g.get("agent_id")
    if not mid:
        res = await client.call("manager.chat", params, audit=True)  # để daemon trả Conflict chuẩn
        return res or {}, None
    st = await client.call("hub.status") or {}
    sub = subscribe(client, {"agent_id": mid, "level": "summary", "since_seq": int(st.get("last_seq") or 0), "epoch": st.get("epoch")})
    q: asyncio.Queue = asyncio.Queue()

    async def pump() -> None:
        try:
            async for msg in sub:
                await q.put(msg)
        except Exception as e:  # pragma: no cover
            await q.put({"end": {"reason": str(e)}})

    task = asyncio.ensure_future(pump())
    try:
        first = await asyncio.wait_for(q.get(), 10)
        if "subscribed" not in first:
            raise RpcError(-32603, "không đăng ký được luồng sự kiện")
        res = await client.call("manager.chat", params, audit=True) or {}
        if res.get("steered"):
            return res, None
        cid = res.get("conversation_id")
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return res, None
            try:
                ev = await asyncio.wait_for(q.get(), left)
            except asyncio.TimeoutError:
                return res, None
            if "end" in ev:
                return res, None
            if ev.get("type") != "manager.reply":
                continue
            p = ev.get("payload") or {}
            if p.get("kind") == "notify" and p.get("conversation_id") in (cid, None):
                if on_notify:
                    on_notify(str(p.get("text") or ""))
                continue
            if p.get("conversation_id") == cid and p.get("kind", "reply") == "reply":
                return res, p
    finally:
        sub.close()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


__all__ = ["ChatSession", "Out", "repl", "oneshot", "run_plain", "run_tty", "to_ansi", "NO_MANAGER"]
