"""Lõi chung của kênh chat người ↔ hub (Telegram, Slack — docs/plan/slack-2way.md, P0).

`ChatBot` giữ mọi logic KHÔNG phụ thuộc nền tảng: gọi hub thay mặt chat (`call` → `Daemon.call_as`, cổng HUMAN_ONLY + audit
`<kênh>:<chat>`), lệnh, tin duyệt (WT-2/WT-8) + quyết định 2 bước, theo dõi run → tin kết quả, `run.continue`, trạng thái nhỏ trong
`hub_state` (khoá `<kênh>.*`). Lớp con (chat/bot.py: Telegram; chat/slack/: Slack) lo vận chuyển: nhận tin/nút, xác định người, gửi/sửa tin.

Ngôn ngữ chung giữa lõi và lớp vận chuyển (lớp con tự dịch sang định dạng của nền tảng):
- chữ: văn bản thuần, hoặc `html=True` = HTML tối giản `<b> <i> <pre> <code>` với MỌI giá trị động đã qua `esc` (escape & < >);
- nút: `{"inline_keyboard": [[{"text", "callback_data"}]]}` do `CallbackStore` phát (nonce một lần + HMAC);
  `{"force_reply": True, …}` = "mời người trả lời tin này"; bàn phím nhanh (`keyboard`) — nền tảng không có thì bỏ qua.
- `api`: đối tượng do lớp con cấp (`self.api()`), lõi chỉ chuyển tiếp cho `_send`/`_edit`/`send_document`/`typing`.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import secrets as _secrets
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from .. import auth as _auth
from ..events import TERMINAL_RUN_EVENTS, HubEvent, iso
from ..notify.base import clip
from ..notify.telegram import CHUNK, MAX_CHUNKS, CallbackStore, esc, fit, full_action, is_heavy, split_chunks, to_plain

if TYPE_CHECKING:
    from ..daemon import Daemon
    from ..hub import Hub

log = logging.getLogger("cfharness.chat")

PENDING = ("open", "waiting_human")
AGENT_FRAME = "▼ Agent viết:"  # tiền tố cố định (chữ hub) ngay trước `<pre>` nội dung agent — WT-8, rút gọn theo ux-plan §3.1
_UID: contextvars.ContextVar[str | None] = contextvars.ContextVar("chat_from_id", default=None)
OUTCOME_VI = {
    "completed": "hoàn thành", "failed": "lỗi", "cancelled": "đã huỷ", "budget_exceeded": "vượt ngân sách", "timed_out": "quá giờ",
    "crashed": "tiến trình chết", "rate_limited": "bị giới hạn tốc độ", "blocked": "bị chặn", "protocol_violation": "vi phạm giao thức",
}
STATUS_VI = {
    "queued": "⏳ đang chờ", "running": "▶️ đang chạy", "waiting_human": "⏸ chờ duyệt", "completed": "✅ xong", "failed": "❌ lỗi",
    "cancelled": "⏹ đã huỷ", "budget_exceeded": "💸 vượt ngân sách", "timed_out": "⌛ quá giờ", "crashed": "💥 tiến trình chết",
    "interrupted": "⏸ gián đoạn", "resumed": "↪ đã tiếp tục", "rate_limited": "🐢 giới hạn tốc độ", "blocked": "⛔ bị chặn",
    "protocol_violation": "⚠️ vi phạm giao thức",
}
AGENT_STATUS_VI = {"idle": "🟢 rảnh", "working": "▶️ đang làm", "waiting": "⏸ chờ duyệt", "paused": "⏸ tạm dừng", "error": "❌ lỗi"}
DECISION_VI = {"allowed": ("✅", "cho phép"), "denied": ("⛔", "từ chối"), "cancelled": ("⏹", "huỷ")}
CHANNEL_LABELS = {"telegram": "Telegram", "slack": "Slack"}


def channel_label(ch: Any) -> str:
    c = str(ch or "")
    head = c.split(":", 1)[0]
    if ":" in c and head in CHANNEL_LABELS:
        return CHANNEL_LABELS[head]
    return {"web": "web", "cli": "máy (CLI)", "tui": "TUI", "runtime": "hub", "mcp": "MCP"}.get(c, c or "?")


def who_label(by: Any) -> str:
    b = str(by or "")
    return "bạn" if b in ("user", "") or b.isdigit() else ("hub" if b == "hub" else b)


def _dur_vi(secs: float) -> str:
    s = int(round(secs))
    return f"{s} giây" if s < 60 else f"{s // 60} phút {s % 60} giây" if s < 3600 else f"{s // 3600} giờ {s % 3600 // 60} phút"


def decided_text(approval_id: str, p: dict[str, Any]) -> str:
    """Dòng kết quả của approval (chữ hub, đã escape) — sửa tại chỗ tin có nút ở MỌI chat."""
    icon, word = DECISION_VI.get(str(p.get("decision")), ("•", str(p.get("decision"))))
    return f"{icon} <b>Đã {esc(word)}</b> — bởi {esc(who_label(p.get('by')))} qua {esc(channel_label(p.get('channel')))}."


class ChatError(Exception):
    pass


class ChatBot:
    """Lõi chung. Lớp con đặt `NAME` (tiền tố kênh/khoá trạng thái), `LABEL`, `CFG_KEY` và cài các hàm vận chuyển ở cuối lớp."""

    NAME = "chat"
    LABEL = "chat"
    CFG_KEY = "notify.chat"

    def __init__(self, hub: "Hub") -> None:
        self.hub = hub
        self.daemon: "Daemon | None" = None
        self.WATCH = f"{self.NAME}.watch"
        self.MSGMAP = f"{self.NAME}.msgmap"
        self.callbacks = CallbackStore(hub, self._callback_key, prefix=self.NAME)
        self.notifier = self._make_notifier()
        hub.bus.add_listener(self.notifier)
        from .manager_chat import ManagerChat

        self.manager = ManagerChat(self)  # M7 MGR-UI: tin thường ↔ điều phối viên
        self.media = self._make_media()  # ảnh/tệp người gửi → điều phối viên; tệp `[[file: …]]` điều phối viên đánh dấu → document
        from .questions_tg import QuestionChat

        self.questions = QuestionChat(self)  # UX v2: câu hỏi có lựa chọn của agent (nút số + trả lời bằng chữ)
        from .flows import ChatFlows

        self.flows = ChatFlows(self)  # T-C/T-D: lệnh tương tác (chọn agent/run bằng nút), tiến độ run sửa tại chỗ
        hub.bus.add_listener(self._on_event)
        self._task: asyncio.Task | None = None
        self._wake: asyncio.Event | None = None
        self._bg: set[asyncio.Task] = set()
        self._ready_for: str | None = None
        self._ignored_at: dict[str, float] = {}
        self.pairing: dict[str, Any] | None = None
        self.info: dict[str, Any] = {"polling": False, "last_error": None, "last_poll_at": None, "updates": 0}

    # ------------------------------------------------------------------ cấu hình
    def cfg(self) -> dict[str, Any]:
        return dict(self.hub.config.get(self.CFG_KEY) or {})

    def is_allowed(self, chat_id: Any, user_id: Any = None) -> bool:
        """Chat + người gửi/bấm được phép (WT-4) — cùng quy tắc với cổng người của kênh (`caller`)."""
        return self.caller(chat_id, user_id).human

    def channel_of(self, chat_id: Any) -> str:
        return f"{self.NAME}:{chat_id}"

    def cmd(self, name: str) -> str:
        """Cách người gõ lệnh `name` trên kênh này (Telegram `/name`, Slack `/cfh name`)."""
        return f"/{name}"

    def _callback_key(self) -> str:
        name = self.cfg().get("callback_key_secret") or f"{self.NAME.upper()}_CALLBACK_KEY"
        v = self.notifier._secret(name)
        if not v:
            v = _secrets.token_urlsafe(32)
            self.hub.secrets.set(name, v)  # type: ignore[attr-defined]
        return v

    def approval_markup(self, approval_id: str, chat_id: Any, row: dict[str, Any] | None = None) -> dict[str, Any]:
        """Nút Cho phép / Từ chối; thêm "Luôn cho phép" CHỈ khi daemon báo đủ điều kiện (`GuardService.always_offer`)."""
        rows = [[("ap", approval_id, "Cho phép"), ("dn", approval_id, "Từ chối")]]
        extra = []
        if row is not None and self.always_offer(row):
            extra.append(("aa", approval_id, "Luôn cho phép…"))
        if row is not None and row.get("matched_rule"):
            extra.append(("ar", approval_id, "Không hỏi lại luật này…"))
        if extra:
            rows.append(extra)
        markup, _ = self.callbacks.issue_rows(rows, chat_id, ttl=self._ttl())
        return markup

    def always_offer(self, row: dict[str, Any]) -> dict[str, Any] | None:
        """Cùng kiểm tra với `approval.approve{always}` (`always_prepare`) — không tự suy ở bot."""
        guard = getattr(self.hub, "guard", None)
        return guard.always_offer(row) if guard is not None and hasattr(guard, "always_offer") else None

    @staticmethod
    def always_rule_text(offer: dict[str, Any]) -> str:
        """Mô tả luật sẽ tạo (HTML đã escape). Host đã bị daemon giới hạn ở dạng DNS/IPv4 thuần; vân tay là hex do hub tính."""
        if offer.get("kind") == "web_domain":
            return (f"từ nay {esc(offer.get('agent_id'))} được mở trang trên <code>{esc(offer.get('domain'))}</code> mà không hỏi lại"
                    " (không gồm tên miền con)")
        if offer.get("kind") == "mcp_tool":
            return f"từ nay {esc(offer.get('agent_id'))} được gọi tool <code>{esc(offer.get('tool'))}</code> mà không hỏi lại"
        return (f"từ nay {esc(offer.get('agent_id'))} được chạy lại ĐÚNG thao tác này mà không hỏi lại"
                f" (khác một ký tự vẫn hỏi; mã <code>{esc(str(offer.get('fingerprint') or '')[:12])}</code>)")

    def _ttl(self) -> float:
        return float(self.cfg().get("callback_ttl_s") or 604800)

    # ------------------------------------------------------------------ đề xuất golden (docs/dev-notes/golden-suggestions.md)
    async def send_golden_suggestion(self, api: Any, chat: Any, s: dict[str, Any]) -> None:
        """(1) Tin NỘI DUNG: đề bài + kết quả tham chiếu (do agent viết) trong `<pre>` sau tiền tố cố định. (2) Tin HỆ THỐNG
        một chạm Duyệt / Bỏ qua (nonce một lần + HMAC, như approval) — chữ hub. Quyết ở kênh khác → gỡ nút mọi chat."""
        rubric = str(((s.get("checker") or {}).get("rubric")) or "")
        ref = rubric.split("Reference result:\n", 1)[1] if "Reference result:\n" in rubric else ""
        body = clip(s.get("prompt") or s.get("title") or "", 1200) + ("\n\n— kết quả được giữ —\n" + clip(ref, 1200) if ref else "")
        head = f"💡 <b>Đề xuất golden task cho {esc(s.get('agent_id'))}</b> (task {esc(s.get('task_id') or '-')})\n<i>{esc(AGENT_FRAME)}</i>"
        res = await self._send(api, chat, f"{head}\n<pre>{esc(body)}</pre>", html=True)
        first = (res or {}).get("message_id") if isinstance(res, dict) else None
        sid = s["id"]
        markup, _ = self.callbacks.issue([("gsa", sid, "Duyệt làm golden"), ("gsr", sid, "Bỏ qua")], chat, ttl=self._ttl())
        text = (f"❓ <b>Lấy task này làm bài kiểm tra chuẩn (golden) cho {esc(s.get('agent_id'))}?</b>\n"
                f"Task xong mà bạn không sửa/hoàn tác. Duyệt = mọi thay đổi tự học sau này phải không làm tụt điểm trên bài này "
                f"(LLM judge so với kết quả ở tin trên). Mã <code>{esc(sid)}</code>.")
        res = await self._send(api, chat, text, markup, html=True, reply_to=first)
        self.callbacks.add_message(sid, chat, (res or {}).get("message_id") if isinstance(res, dict) else None)

    async def _golden_decide(self, cid: str, action: str, sid: str) -> tuple[str, str | None]:
        """Nút một chạm → RPC HUMAN_ONLY qua `call` (cổng người + audit `<kênh>:<chat>`). Agent không có đường nào tới đây."""
        method = "golden.suggestion_approve" if action == "gsa" else "golden.suggestion_reject"
        s = await self.call(cid, method, {"id": sid})
        if action == "gsa":
            return f"Đã tạo golden task {s.get('golden_task_id')}.", None
        return "Đã bỏ đề xuất.", None

    @staticmethod
    def golden_decided_text(p: dict[str, Any]) -> str:
        word = f"duyệt → golden {p.get('golden_task_id')}" if p.get("decision") == "approve" else "bỏ qua"
        return f"{'✅' if p.get('decision') == 'approve' else '⏹'} <b>Đề xuất golden {esc(p.get('suggestion_id'))}: đã {esc(word)}</b> — bởi " \
               f"{esc(who_label(str(p.get('by') or '').split(':', 1)[0]))} qua {esc(channel_label(p.get('channel')))}."

    # ------------------------------------------------------------------ trạng thái nhỏ (hub_state)
    def _state(self, key: str) -> dict[str, Any]:
        v = self.hub.store.state_get(key) or {}
        return v if isinstance(v, dict) else {}

    def _watch(self, run_id: str, chat_id: Any) -> None:
        w = self._state(self.WATCH)
        w[run_id] = str(chat_id)
        self.hub.store.state_set(self.WATCH, w)

    def _map_message(self, chat_id: Any, message_id: Any, run_id: str) -> None:
        m = self._state(self.MSGMAP)
        m[f"{chat_id}:{message_id}"] = run_id
        if len(m) > 500:
            for k in list(m)[: len(m) - 500]:
                m.pop(k, None)
        self.hub.store.state_set(self.MSGMAP, m)

    # ------------------------------------------------------------------ vòng đời
    def _on_event(self, ev: HubEvent) -> None:
        t = ev.type
        if t.startswith("manager.") or (t in TERMINAL_RUN_EVENTS and ev.source == "hub"):
            self.manager.on_event(ev)
        if t.startswith("question."):
            self.questions.on_event(ev)
        elif t == "tool.start":
            self.flows.on_event(ev)
        if t == "hub.started":
            self.start()
        elif t == "hub.stopping":
            self.stop()
        elif t == "run.queued":
            parent = (ev.payload or {}).get("parent_run_id")
            if parent and ev.run_id:
                w = self._state(self.WATCH)
                if parent in w:  # resume/continue sau duyệt hoặc gián đoạn → theo dõi tiếp run con
                    self._watch(ev.run_id, w[parent])
        elif t in TERMINAL_RUN_EVENTS and ev.source == "hub" and ev.run_id and ev.run_id in self._state(self.WATCH):
            self._spawn(self._report_run(ev.run_id, t))
        elif t == "approval.resolved":
            rid = (ev.payload or {}).get("request_id")
            if rid:
                self.callbacks.invalidate_ref(rid)
                msgs = self.callbacks.pop_messages(rid)
                if msgs:
                    self._spawn(self._clear_buttons(msgs, decided_text(rid, ev.payload or {})))
        elif t == "golden.suggestion_decided":
            sid = (ev.payload or {}).get("suggestion_id")
            if sid:
                self.callbacks.invalidate_ref(sid)
                msgs = self.callbacks.pop_messages(sid)
                if msgs:
                    self._spawn(self._clear_buttons(msgs, self.golden_decided_text(ev.payload or {})))

    def _spawn(self, coro: Any) -> None:
        try:
            task = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()
            return
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._wake = asyncio.Event()
        self._task = loop.create_task(self._loop())

    def stop(self) -> None:
        for t in [self._task, self.notifier._task, *self._bg]:
            if t is not None and not t.done():
                t.cancel()
        self._task = None
        self.info["polling"] = False

    def kick(self) -> None:
        """Cấu hình đổi (setup/disable/allow) → khởi động lại vòng nhận ngay (huỷ kết nối/long poll đang treo)."""
        self._ready_for = None
        if self._task is not None and not self._task.done():
            self._task.cancel()
            self._task = None
            self.start()
        elif self._wake is not None:
            self._wake.set()

    async def _sleep_or_wake(self, seconds: float) -> None:
        if self._wake is None:
            self._wake = asyncio.Event()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=max(0.05, seconds))
        except asyncio.TimeoutError:
            pass
        self._wake.clear()

    # ------------------------------------------------------------------ gửi
    async def _clear_buttons(self, msgs: list[list[Any]], note: str) -> None:
        """T-B: tin hệ thống có nút của approval → SỬA TẠI CHỖ thành dòng kết quả (chữ hub, HTML đã escape) và gỡ nút."""
        api = self.api()
        if api is None:
            return
        for chat, mid in msgs:
            await self._edit(api, chat, mid, note, None)

    async def send_test(self, text: str) -> dict[str, Any]:
        api = self.api()
        if api is None:
            return {"sent": False, "chats": 0, "reason": f"{self.LABEL} chưa cấu hình (cfharness notify {self.NAME} setup) hoặc đang tắt"}
        chats = sorted(self.notify_chats())
        if not chats:
            return {"sent": False, "chats": 0, "reason": self.empty_allowlist_hint()}
        n, errs = 0, []
        for c in chats:
            try:
                await self._send(api, c, text)
                n += 1
            except Exception as e:
                errs.append(f"{c}: {clip(e, 200)}")
        return {"sent": n > 0 and not errs, "chats": n, "reason": "; ".join(errs) or None}

    # ------------------------------------------------------------------ gọi hub thay mặt chat (cổng HUMAN_ONLY)
    async def call(self, chat_id: Any, method: str, params: dict[str, Any] | None = None) -> Any:
        if self.daemon is None:
            raise ChatError("bot chưa gắn daemon")
        uid = _UID.get()
        caller = self.caller(chat_id, uid)
        if not caller.human:
            raise ChatError("chat/người dùng này không có quyền")
        p = dict(params or {})
        MUTATING = self.daemon._mutating or _auth.all_mutating()  # mọi luồng (gồm manager.* của M7) — audit đúng kênh

        if method in MUTATING:
            p["_audit"] = {"channel": self.channel_of(chat_id), "actor": "user"}
        resp = await self.daemon.call_as(caller, method, p)
        if "error" in resp:
            raise ChatError(str(resp["error"].get("message")))
        if method in MUTATING or method.startswith("approval.") and method != "approval.get" and method != "approval.list":
            audit = getattr(self.hub, "audit", None)
            if audit is not None:  # WT-4: ai (from.id) bấm/gõ trong chat nào
                audit.record(f"{self.NAME}.command", None, None, {"method": method, "chat_id": str(chat_id), "from_id": uid,
                                                                  "id": p.get("id") or p.get("agent")},
                             actor="user", channel=self.channel_of(chat_id), summary=method)
        return resp.get("result")

    def _audit_ignored(self, chat_id: str, chat_type: Any, kind: str, from_id: Any = None) -> None:
        now = time.monotonic()
        key = f"{chat_id}/{from_id}"
        if now - self._ignored_at.get(key, -1e9) < 60:
            return
        self._ignored_at[key] = now
        if len(self._ignored_at) > 1000:
            self._ignored_at.clear()
        log.info("%s: bỏ qua %s từ chat ngoài allowlist %s", self.LABEL, kind, chat_id)
        audit = getattr(self.hub, "audit", None)
        if audit is not None:
            audit.record(f"{self.NAME}.ignored", None, None, {"chat_id": chat_id, "chat_type": chat_type, "kind": kind, "from_id": from_id},
                         actor="unknown", channel=self.channel_of(chat_id), summary="tin từ chat ngoài allowlist — bỏ qua")

    # ------------------------------------------------------------------ ghép nối (setup)
    def start_pairing(self) -> dict[str, Any]:
        code = _secrets.token_hex(4).upper()
        self.pairing = {"code": code, "exp": time.time() + float(self.cfg().get("pair_ttl_s") or 600), "pending": {}}
        return {"code": code, "ttl_s": float(self.cfg().get("pair_ttl_s") or 600)}

    def pending(self, code: str | None) -> list[dict[str, Any]]:
        pr = self.pairing
        if not pr or (code and not _secrets.compare_digest(str(code).upper(), pr["code"])):
            return []
        return list(pr["pending"].values())

    # ------------------------------------------------------------------ lệnh
    def command_handlers(self) -> dict[str, Any]:
        """Tên lệnh (không có `/`) → handler(cid, rest, api) → chữ trả lời | None. Alias tiếng Anh cũ vẫn chạy."""
        return {
            "start": self._c_help, "help": self._c_help, "status": self._c_status, "trangthai": self._c_status,
            "agents": self.flows.c_agents, "agent": self.flows.c_agents, "runs": self.flows.c_runs, "luotchay": self.flows.c_runs,
            "approvals": self._c_approvals, "run": self.flows.c_run, "giaoviec": self.flows.c_run, "stop": self.flows.c_stop,
            "estop": self._c_estop, "digest": self._c_digest, "moi": self._c_new, "new": self._c_new,
            "hoi": self._c_questions, "questions": self._c_questions, "canban": self._c_inbox,
            "lich": self._c_schedules, "schedules": self._c_schedules,
        }

    async def run_command(self, cid: str, name: str, rest: str, api: Any) -> None:
        fn = self.command_handlers().get(name)
        if fn is None:
            await self._send(api, cid, f"Không có lệnh {self.cmd(name)}. Gõ {self.cmd('help')}.")
            return
        try:
            out = await fn(cid, rest, api)
        except ChatError as e:
            out = f"Lỗi: {clip(e, 500)}"
        if out:
            await self._send(api, cid, out)

    async def _c_schedules(self, cid: str, rest: str, api: Any) -> str:
        """Lịch chạy tự động (CHỈ ĐỌC, qua `self.call` → cổng người + allowlist). Văn bản thuần (không HTML, không nút); tên/lịch có thể
        do điều phối viên viết → bỏ ký tự điều khiển/vô hình."""
        from ..notify.telegram import visible

        rows = await self.call(cid, "schedule.list", {"limit": 30})
        if not rows:
            return "⏰ Chưa có lịch nào. Nhắn điều phối viên (vd \"8h sáng mỗi ngày tóm tắt góp ý mới gửi t\") hoặc dùng `cfharness schedule add`."
        icon = {"active": "●", "paused": "⏸", "done": "✓"}
        lines = [f"⏰ Lịch chạy tự động ({len(rows)}):"]
        for r in rows:
            nxt = str(r.get("next_run_at") or "")
            try:
                nxt = datetime.fromisoformat(nxt.replace("Z", "+00:00")).astimezone().strftime("%d/%m %H:%M") if nxt else "—"
            except ValueError:
                pass
            who = "điều phối viên" if str(r.get("created_by") or "").startswith("manager:") else "bạn"
            lines.append(f"{icon.get(r.get('status'), '?')} {r['id']} · {visible(str(r.get('name') or ''))[:50]}\n"
                         f"   {visible(str(r.get('describe') or r.get('expr') or ''))[:60]} · lượt tới {nxt} · tạo bởi {who}"
                         + (f" · lỗi {r['fail_count']}" if r.get("fail_count") else ""))
        lines.append("Tắt/bật: nhắn điều phối viên, hoặc `cfharness schedule pause|resume <id>`.")
        return fit("\n".join(lines))

    async def _c_help(self, cid: str, rest: str, api: Any) -> str | None:
        return self.help_text()

    async def _c_questions(self, cid: str, rest: str, api: Any) -> str | None:
        return await self.questions.command(cid, api)

    async def _c_inbox(self, cid: str, rest: str, api: Any) -> str | None:
        """"Cần bạn": yêu cầu duyệt + câu hỏi đang chờ người (mỗi mục gửi lại cặp tin có nút)."""
        aps = await self.call(cid, "approval.list", {"status": "pending", "limit": 10})
        qs = await self.call(cid, "question.list", {"status": "open", "limit": 10})
        if not aps and not qs:
            return "✅ Không có gì đang chờ bạn."
        await self._send(api, cid, f"📋 Đang chờ bạn: {len(aps or [])} yêu cầu duyệt · {len(qs or [])} câu hỏi.")
        for r in aps or []:
            await self.send_approval(api, cid, r, kind="approvals.list")
        for q in reversed(qs or []):
            await self.questions.send(api, cid, q)
        return None

    async def _c_status(self, cid: str, rest: str, api: Any) -> str:
        s = await self.call(cid, "hub.status")
        pend = await self.call(cid, "approval.list", {"status": "pending"})
        est = s.get("estop") or {}
        return "\n".join([
            f"cfharness {s.get('version')} — ESTOP: {'BẬT' + (' (' + str(est.get('reason')) + ')' if est.get('reason') else '') if est.get('on') else 'tắt'}",
            f"Run đang chạy: {s.get('running')} · đang chờ: {s.get('queued')} (tối đa {s.get('max_concurrent_runs')})",
            f"Agent: {s.get('agents')} · chi phí hôm nay: ${float(s.get('cost_today_usd') or 0):.4f}",
            f"Yêu cầu chờ duyệt: {len(pend or [])}" + (f" — gõ {self.cmd('approvals')}" if pend else ""),
        ])

    async def _c_approvals(self, cid: str, rest: str, api: Any) -> str | None:
        rows = await self.call(cid, "approval.list", {"status": "pending", "limit": 10})
        if not rows:
            return "Không có yêu cầu nào đang chờ duyệt."
        for r in rows:
            await self.send_approval(api, cid, r, kind="approvals.list")
        return None

    async def _c_estop(self, cid: str, rest: str, api: Any) -> str | None:
        arg, _, reason = rest.partition(" ")
        arg = arg.lower()
        if arg == "on":
            await self.call(cid, "estop.set", {"on": True, "reason": reason.strip() or f"{self.LABEL} {cid}"})
            return f"ESTOP: BẬT — không khởi chạy run mới (run đang chạy vẫn tiếp; dừng từng run bằng {self.cmd('stop')})."
        if arg == "off":
            markup, _ = self.callbacks.issue([("eo", "estop", "Xác nhận TẮT ESTOP"), ("cx0", "estop", "Huỷ")], cid, ttl=600)
            await self._send(api, cid, "Tắt ESTOP cho phép hub chạy run trở lại. Xác nhận?", markup)
            return None
        st = await self.call(cid, "estop.get")
        e = self.cmd("estop")
        return f"ESTOP: {'BẬT' if (st or {}).get('on') else 'tắt'}. Cú pháp: {e} on [lý do] | {e} off"

    async def _c_new(self, cid: str, rest: str, api: Any) -> str | None:
        msg = await self.manager.new_conversation(cid, api)
        if not rest:
            return msg
        await self._send(api, cid, msg)  # `/moi <nội dung>` = mở cuộc mới rồi gửi luôn nội dung
        await self.manager.handle_text(cid, rest, api)
        return None

    async def _c_digest(self, cid: str, rest: str, api: Any) -> str:
        ops = getattr(self.hub, "learning_ops", None)
        if ops is None:
            d = await self.call(cid, "digest.show", {})
            return fit(to_plain(str((d or {}).get("text") or "Chưa có digest.")))
        end = datetime.now(timezone.utc)
        data = ops.digest.collect(iso(end - timedelta(hours=24)), iso(end))  # chỉ đọc — không dời mốc digest 08:00
        return fit("Digest 24 giờ qua\n" + to_plain(ops.digest.render(data)), hint="xem đầy đủ: cfharness digest, TUI hoặc web")

    async def _continue(self, cid: str, run_id: str, text: str, api: Any) -> None:
        try:
            r = await self.call(cid, "run.continue", {"id": run_id, "prompt": text})
        except ChatError as e:
            await self._send(api, cid, f"Không tiếp tục được run {run_id}: {clip(e, 400)}")
            return
        self._watch(r["id"], cid)
        await self._send(api, cid, f"▶️ Đã gửi tiếp cho {r.get('agent_id')}. Bot sẽ báo kết quả khi xong.")

    # ------------------------------------------------------------------ kết quả run
    def format_result(self, run: dict[str, Any]) -> str:
        """Tin kết quả (HTML). Phần do agent viết nằm TRỌN trong `<pre>` (escape, nền tảng không tạo link/lệnh bấm được bên
        trong), sau tiền tố cố định — agent không giả được dòng hệ thống. Không bao giờ kèm nút (WT-8)."""
        from ..redact import redact_str
        from ..notify.telegram import visible

        outcome = run.get("outcome") or run.get("status")
        dur = float(run.get("duration_ms") or 0) / 1000
        icon = "✅" if outcome == "completed" else "⏹" if outcome == "cancelled" else "❌"
        word = "xong" if outcome == "completed" else OUTCOME_VI.get(str(outcome), str(outcome))
        meta = [_dur_vi(dur)] if dur else []
        if run.get("cost_usd"):
            meta.append(f"${float(run['cost_usd']):.3f}")
        head = f"{icon} <b>{esc(run.get('agent_id'))} — {esc(word)}</b>" + (f" · {esc(' · '.join(meta))}" if meta else "")
        body = visible(redact_str(str(run.get("summary") or run.get("error") or "(không có nội dung kết quả)")))
        n = min(int(self.cfg().get("result_max_chars") or 1500), CHUNK)
        cut = len(body) > n
        if cut:
            body = body[:n]
        lines = [head, f"<i>{esc(AGENT_FRAME)}</i>", f"<pre>{esc(body)}</pre>"]
        if cut:
            lines.append("…(đã cắt — xem đầy đủ trên web)")
        lines.append("↩ " + self.reply_hint("giao thêm cho agent này"))
        return "\n".join(lines)

    def reply_hint(self, what: str) -> str:
        """Chỉ dẫn "trả lời tin này để …" theo cách của nền tảng (Telegram: reply; Slack: trả lời trong thread)."""
        return f"Trả lời (reply) tin này để {what}."

    # ------------------------------------------------------------------ mời người nhập chữ (giao việc, trả lời câu hỏi bằng chữ)
    async def ask_input(self, api: Any, cid: str, kind: str, ref: str, text: str, placeholder: str, *,
                        reply_to: Any = None) -> dict[str, Any]:
        """Mời người nhập chữ cho `kind` ("run": ref = agent; "question": ref = id câu hỏi). Mặc định (Telegram) = tin `force_reply`
        → `{"message_id": …}`, NGƯỜI GỌI map tin đó (reply vào tin = câu trả lời). Lớp con có ô nhập riêng (Slack: modal) trả
        `{"message_id": None, "modal": True}` và tự lo phần nhận (vẫn qua `self.call` → cổng người)."""
        return await self._send(api, cid, text, {"force_reply": True, "input_field_placeholder": placeholder, "selective": True}, reply_to=reply_to)

    def input_where(self) -> str:
        """Nơi người sẽ nhập sau `ask_input` (chữ hướng dẫn của hub) — Telegram: tin force_reply bên dưới."""
        return "ở tin bên dưới"

    # Lời dẫn theo nền tảng (P3): mặc định = chữ Telegram (giữ nguyên từng byte); Slack ghi đè (chat/slack/bot.py).
    def reply_here(self) -> str:
        """"trả lời tin này" (chữ thường, giữa câu) — Telegram: reply; Slack: trả lời trong thread."""
        return "trả lời (reply) tin này"

    def continue_hint(self, chat: Any = None) -> str:
        """Dòng cuối trả lời của điều phối viên: cách nói tiếp / mở chủ đề mới ở `chat`."""
        return f"{self.reply_hint('nói tiếp').rstrip('.')} · {self.cmd('moi')} để bắt đầu chủ đề mới."

    def reply_into(self, target: str) -> str:
        """"reply vào <tin nào đó>" (giữa câu)."""
        return f"reply vào {target}"

    def plain_hint(self) -> str:
        """Tin nào là "tin nhắn thường" (→ điều phối viên)."""
        return "không phải lệnh /"

    def answer_box_hint(self) -> str:
        """Toast sau khi bấm "Trả lời bằng chữ"."""
        return "Nhập câu trả lời ở ô bên dưới."

    async def _report_run(self, run_id: str, etype: str) -> None:
        run = self.hub.store.get_run(run_id)
        if run is None:
            return
        w = self._state(self.WATCH)
        chat = w.get(run_id)
        if chat is None:
            return
        outcome = run.get("outcome") or run.get("status")
        api = self.api()
        if etype == "run.waiting_human" or outcome == "waiting_human":
            if api is not None:
                await self._send(api, chat, f"⏸ {run.get('agent_id')} đang chờ bạn duyệt — xem tin duyệt hoặc gõ {self.cmd('approvals')}.")
            return
        if outcome in ("resumed", "interrupted"):
            return  # run con (resume) kế thừa theo dõi qua parent_run_id
        w.pop(run_id, None)
        self.hub.store.state_set(self.WATCH, w)
        if api is None or str(chat) not in self.notify_chats():
            return
        await self.flows.finish(run_id, run.get("agent_id"))  # T-D: tin tiến độ → "đã kết thúc"
        try:
            res = await self._send(api, chat, self.format_result(run), html=True)
            if res.get("message_id") is not None:
                self._map_message(chat, res["message_id"], run_id)
        except Exception as e:
            log.warning("không gửi được kết quả run %s: %s", run_id, clip(e))

    # ------------------------------------------------------------------ tin duyệt (WT-2, WT-8)
    def _targets(self, row: dict[str, Any], payload: dict[str, Any] | None) -> list[str]:
        t = (payload or {}).get("targets") or row.get("targets")
        if isinstance(t, str):
            try:
                import json as _json

                t = _json.loads(t)
            except ValueError:
                t = [t]
        if t:
            return [str(x) for x in t][:20]
        audit = getattr(self.hub, "audit", None)  # verdict lúc mở approval (audit `approval.open`) có `targets`
        if audit is not None:
            for a in audit.query(kind="approval.open", run_id=row.get("run_id"), limit=50):
                d = a.get("data") or {}
                if d.get("approval_id") == row.get("id"):
                    return [str(x) for x in ((d.get("verdict") or {}).get("targets") or [])][:20]
        return []

    async def send_approval(self, api: Any, chat: Any, row: dict[str, Any], *, kind: str = "approval.open",
                            payload: dict[str, Any] | None = None) -> None:
        """(1) Tin NỘI DUNG: thao tác đầy đủ + lý do + đích khớp luật, trong `<pre>` sau tiền tố cố định — không nút.
        Dài → nhiều tin; quá `MAX_CHUNKS` tin → tệp .txt đầy đủ + đầu/cuối. (2) Tin HỆ THỐNG có nút, chỉ chữ do hub sinh,
        trả lời vào tin nội dung đầu tiên."""
        from ..redact import redact_str
        from ..notify.telegram import visible

        aid = row["id"]
        action = full_action(str(row.get("tool_name") or ""), row.get("tool_input"))
        targets = self._targets(row, payload)
        detail = []
        if row.get("reason"):
            detail.append("Vì sao hỏi: " + visible(redact_str(str(row["reason"]))))
        if targets:
            detail.append("Ảnh hưởng tới: " + ", ".join(visible(redact_str(x)) for x in targets))
        tail = ("\n\n" + "\n".join(detail)) if detail else ""
        chunks = split_chunks(action)
        head = f"⏸ <b>{esc(row.get('agent_id'))} muốn chạy:</b>\n<i>{esc(AGENT_FRAME)}</i>"
        first_mid = None
        if len(chunks) <= MAX_CHUNKS:
            for i, ch in enumerate(chunks):
                part = f" (phần {i + 1}/{len(chunks)})" if len(chunks) > 1 else ""
                last = i == len(chunks) - 1
                text = f"{head}{part}\n<pre>{esc(ch)}</pre>" + (f"\n<pre>{esc(tail.strip())}</pre>" if last and tail else "")
                res = await self._send(api, chat, text, html=True, reply_to=first_mid)
                first_mid = first_mid or res.get("message_id")
        else:
            doc = (action + tail).encode("utf-8")
            res = await self.send_document(api, chat, f"{aid}.txt", doc,
                                           f"Thao tác đầy đủ ({len(action)} ký tự) — do agent viết", mime="text/plain")
            first_mid = (res or {}).get("message_id") if isinstance(res, dict) else None
            text = (f"{head} (quá dài — đầy đủ ở tệp đính kèm; dưới đây là phần đầu và CUỐI)\n<pre>{esc(action[:1500])}</pre>\n…\n"
                    f"<pre>{esc(action[-1500:])}</pre>" + (f"\n<pre>{esc(tail.strip()[:600])}</pre>" if tail else ""))
            await self._send(api, chat, text, html=True, reply_to=first_mid)
        res = await self._send(api, chat, self.approval_sys_text(row, kind=kind, payload=payload, action_len=len(action)),
                               self.approval_markup(aid, chat, row), html=True, reply_to=first_mid)
        self.callbacks.add_message(aid, chat, res.get("message_id"))

    def approval_sys_text(self, row: dict[str, Any], *, kind: str = "approval.open", payload: dict[str, Any] | None = None,
                          action_len: int | None = None) -> str:
        """Tin HỆ THỐNG có nút của approval — CHỈ chữ do hub sinh (id, agent, run, luật, lớp, độ dài)."""
        agent = esc(row.get("agent_id"))
        heavy = is_heavy(row, self.cfg().get("confirm_classes") or [])
        lead = {"approval.waiting_human": f"⏸ <b>{agent} đã tạm dừng, chờ bạn duyệt thao tác ở tin trên.</b>\nHub không tự từ chối — bấm khi bạn sẵn sàng.",
                "approval.reminder": f"🔔 <b>Nhắc lần {esc((payload or {}).get('reminder_count'))}: {agent} vẫn chờ bạn duyệt thao tác ở tin trên.</b>",
                "approval.escalated": f"⬆️ <b>Điều phối viên chuyển cho bạn: cho phép {agent} chạy thao tác ở tin trên?</b>",
                }.get(kind, f"❓ <b>Cho phép {agent} chạy thao tác ở tin trên?</b>")
        lines = [lead]
        if row.get("protected_resource_id"):
            lines.append("⚠️ Thao tác chạm tài nguyên được bảo vệ.")
        elif heavy:
            lines.append("⚠️ Thao tác nặng — đọc kỹ trước khi cho phép.")
        if self.always_offer(row):
            lines.append("«Luôn cho phép» = cho phép lần này và không hỏi lại đúng loại thao tác này.")
        if row.get("matched_rule"):
            lines.append(f"«Không hỏi lại luật này» = cho phép lần này và từ nay không hỏi {agent} với luật {esc(row.get('matched_rule'))}.")
        return "\n".join(lines)

    # ------------------------------------------------------------------ nút (sau khi lớp con đã kiểm người + TIÊU nonce)
    async def handle_action(self, cid: str, rec: dict[str, Any], mid: Any, api: Any) -> tuple[str, str | None, dict[str, Any] | None]:
        """→ (toast, chữ sửa tại chỗ tin có nút | None, bàn phím mới | None). Lớp con báo toast TRƯỚC rồi mới sửa tin.
        Câu hỏi (`q*`) và luồng tương tác (`f*`) tự sửa tin của mình → trả `None` cho phần sửa."""
        a, ref = rec["a"], rec["r"]
        if a.startswith("q"):  # UX v2: câu hỏi (nonce đã tiêu; questions_tg tự sửa/cấp lại bàn phím)
            return await self.questions.on_callback(cid, a, ref, mid, api), None, None
        if a.startswith("f"):  # T-C: luồng tương tác (giao việc, chọn run…)
            return await self.flows.on_callback(cid, a, ref, mid, api), None, None
        # T-B: mọi kết quả SỬA TẠI CHỖ tin có nút (chữ hub) + toast; không gửi thêm tin mới
        edit: str | None = None
        markup: dict[str, Any] | None = None
        try:
            if a in ("ap", "apc", "dn", "aa", "aac", "ar", "arc", "arh"):
                text, edit, markup = await self._decide(cid, a, ref, api)
            elif a in ("gsa", "gsr"):  # đề xuất golden: một chạm; `golden.suggestion_decided` sửa tin ở MỌI chat
                text, edit = await self._golden_decide(cid, a, ref)
            elif a == "eo":
                await self.call(cid, "estop.set", {"on": False, "reason": f"{self.LABEL} {cid}"})
                text = "Đã TẮT ESTOP — hub chạy run trở lại."
                edit = f"▶️ <b>Đã tắt dừng khẩn cấp</b> — bởi {esc(who_label(_UID.get()))} qua {esc(self.LABEL)}. Agent chạy lại bình thường."
            elif a == "cx":
                text = "Đã huỷ bước xác nhận — yêu cầu vẫn chờ duyệt."
                ap = await self.call(cid, "approval.get", {"id": ref})
                if ap.get("status") in PENDING:
                    edit, markup = self.approval_sys_text(ap, kind="approvals.list"), self.approval_markup(ref, cid, ap)
                    self.callbacks.add_message(ref, cid, mid)
            elif a == "cx0":
                text = "Đã huỷ — ESTOP vẫn BẬT."
                edit = "🛑 <b>Đã huỷ</b> — dừng khẩn cấp vẫn BẬT."
            else:
                text = "Đã huỷ."
                edit = "<b>Đã huỷ.</b>"
        except ChatError as e:
            text = f"Lỗi: {clip(e, 300)}"
            edit = f"❌ <b>Lỗi:</b> {esc(clip(e, 300))}"
        return text, edit, markup

    def audit_rejected(self, cid: str, why: str, from_id: Any) -> None:
        audit = getattr(self.hub, "audit", None)
        if audit is not None:
            audit.record(f"{self.NAME}.callback_rejected", None, None, {"chat_id": cid, "reason": why, "from_id": from_id},
                         actor="user", channel=self.channel_of(cid), summary=f"nút bị từ chối: {why}")

    @staticmethod
    def rejected_text(why: str, approvals_cmd: str) -> str:
        return {"unknown_or_used": "Nút này đã dùng hoặc không còn hiệu lực.", "expired": f"Nút đã hết hạn — gõ {approvals_cmd}.",
                }.get(why, "Nút không hợp lệ.")

    async def _decide(self, cid: str, action: str, approval_id: str, api: Any) -> tuple[str, str | None, dict[str, Any] | None]:
        """→ (toast, chữ sửa tại chỗ | None, bàn phím | None). Quyết xong: `approval.resolved` sửa MỌI tin của approval (mọi chat)."""
        ap = await self.call(cid, "approval.get", {"id": approval_id})
        if ap.get("status") not in PENDING:
            t = "Yêu cầu này đã được xử lý trước đó."
            return t, decided_text(approval_id, {"decision": ap.get("status"), "by": ap.get("resolved_by"), "channel": ap.get("resolved_channel")}), None
        uid = _UID.get()
        ch = self.channel_of(cid)
        if action == "ap":  # WT-5: mọi cho phép đều 2 bước; bước 2 SỬA tin hệ thống (chữ hub) thay vì gửi tin mới (T-B)
            heavy = is_heavy(ap, self.cfg().get("confirm_classes") or [])
            markup, _ = self.callbacks.issue([("apc", approval_id, "Xác nhận CHO PHÉP"), ("cx", approval_id, "Huỷ")], cid, ttl=min(self._ttl(), 3600))
            text = (f"⚠️ <b>Chắc chắn cho phép?</b> {esc(ap.get('agent_id'))} sẽ chạy thao tác ở tin trên (một lần)"
                    f"{' — thao tác NẶNG' if heavy else ''}.\nĐọc lại thao tác trước khi xác nhận.")
            return "Cần xác nhận bước 2 — xem tin này.", text, markup
        if action == "aa":  # LLM-2: "luôn cho phép" nặng ít nhất như cho phép → 2 bước; bước 2 nói rõ luật sẽ tạo
            offer = ap.get("always_offer") or self.always_offer(ap)
            if not offer:
                return "Thao tác này không dùng được «luôn cho phép» — chỉ cho phép một lần.", None, None
            markup, _ = self.callbacks.issue([("aac", approval_id, "Xác nhận LUÔN CHO PHÉP"), ("cx", approval_id, "Huỷ")], cid,
                                             ttl=min(self._ttl(), 3600))
            text = (f"⚠️ <b>Chắc chắn LUÔN cho phép?</b> Cho phép lần này, và {self.always_rule_text(offer)}.\n"
                    "Đọc lại thao tác ở tin trên trước khi xác nhận. Gỡ sau: cfharness guard always rm &lt;id luật&gt;.")
            return "Cần xác nhận bước 2 — xem tin này.", text, markup
        if action == "aac":  # daemon kiểm lại (`always_prepare`): không đủ điều kiện → lỗi, approval giữ nguyên
            res = await self.call(cid, "approval.approve", {"id": approval_id, "channel": ch, "by": "user", "always": True,
                                                            "note": f"luôn cho phép qua {self.LABEL} (user {uid})"})
            rid = ((res or {}).get("always_rule") or {}).get("id") if isinstance(res, dict) else None
            return f"Đã cho phép và tạo luật {rid or '?'}.", None, None
        if action == "ar":  # luôn cho phép agent + luật (kể cả phá huỷ) → 2 bước; bước 2 chọn không hạn / 8 giờ
            if not ap.get("matched_rule"):
                return "Yêu cầu này không có luật — chỉ cho phép một lần.", None, None
            markup, _ = self.callbacks.issue([("arc", approval_id, "Xác nhận — không hạn"), ("arh", approval_id, "Xác nhận — 8 giờ"),
                                              ("cx", approval_id, "Huỷ")], cid, ttl=min(self._ttl(), 3600))
            text = (f"⚠️ <b>Chắc chắn không hỏi lại?</b> Cho phép lần này, và từ nay {esc(ap.get('agent_id'))} không bị hỏi lại với luật "
                    f"<code>{esc(ap.get('matched_rule'))}</code> — kể cả thao tác phá huỷ khác cùng luật. Yêu cầu đang chờ cùng luật cũng được cho phép.\n"
                    f"Gỡ sau: cfharness guard grant rm {esc(ap.get('agent_id'))} {esc(ap.get('matched_rule'))}.")
            return "Cần xác nhận bước 2 — xem tin này.", text, markup
        if action in ("arc", "arh"):
            res = await self.call(cid, "approval.approve", {"id": approval_id, "channel": ch, "by": "user", "grant_rule": True,
                                                            "grant_for_s": 8 * 3600 if action == "arh" else None,
                                                            "note": f"không hỏi lại luật qua {self.LABEL} (user {uid})"})
            g = (res or {}).get("rule_grant") or {} if isinstance(res, dict) else {}
            n = len(g.get("released") or [])
            return f"Đã cho phép — không hỏi lại luật {g.get('rule') or '?'}" + (f" (+{n} yêu cầu đang chờ)" if n else "") + ".", None, None
        if action == "dn":
            await self.call(cid, "approval.deny", {"id": approval_id, "channel": ch, "by": "user", "note": f"từ chối qua {self.LABEL} (user {uid})"})
            return "Đã từ chối.", None, None
        await self.call(cid, "approval.approve", {"id": approval_id, "channel": ch, "by": "user", "note": f"cho phép qua {self.LABEL} (user {uid})"})
        return "Đã cho phép — agent chạy tiếp.", None, None

    # ------------------------------------------------------------------ trạng thái
    def status(self) -> dict[str, Any]:
        c = self.cfg()
        pr = self.pairing
        return {
            "enabled": bool(c.get("enabled", True)),
            "configured": self.token(ignore_enabled=True) is not None,
            "polling": bool(self.info.get("polling")),
            "last_error": self.info.get("last_error"),
            "last_poll_at": self.info.get("last_poll_at"),
            "updates": self.info.get("updates"),
            "sent": self.notifier.sent,
            "failed": self.notifier.failed,
            "pairing_active": bool(pr and time.time() <= pr["exp"]),
            "active_buttons": self.callbacks.count(),
            "watching_runs": len(self._state(self.WATCH)),
        }

    # ================================================================== lớp con cài (vận chuyển)
    def _make_notifier(self) -> Any:  # pragma: no cover - lớp con
        raise NotImplementedError

    def _make_media(self) -> Any:
        """Ảnh/tệp: mặc định bản Telegram (chat/media.py); Slack thay bằng chat/slack/media.py."""
        from .media import MediaChat

        return MediaChat(self)

    def token(self, *, ignore_enabled: bool = False) -> str | None:  # pragma: no cover - lớp con
        raise NotImplementedError

    def api(self, *, ignore_enabled: bool = False) -> Any | None:  # pragma: no cover - lớp con
        raise NotImplementedError

    def caller(self, chat_id: Any, user_id: Any = None) -> "_auth.Caller":  # pragma: no cover - lớp con
        raise NotImplementedError

    def allowed(self) -> set[str]:  # pragma: no cover - lớp con
        raise NotImplementedError

    def notify_chats(self) -> set[str]:  # pragma: no cover - lớp con
        raise NotImplementedError

    def broadcast_chats(self) -> set[str]:
        """Chat nhận tin chủ động không gắn cuộc trò chuyện nào (approval, câu hỏi, thông báo) — mặc định = `notify_chats()`."""
        return self.notify_chats()

    def root_post_ok(self, chat: Any) -> bool:
        """Được đăng tin KHÔNG trả lời tin nào vào `chat` (Slack: chỉ DM — kênh chung chỉ nhận trả lời trong thread)."""
        return True

    def help_text(self) -> str:  # pragma: no cover - lớp con
        raise NotImplementedError

    def empty_allowlist_hint(self) -> str:  # pragma: no cover - lớp con
        raise NotImplementedError

    async def _loop(self) -> None:  # pragma: no cover - lớp con
        raise NotImplementedError

    async def _send(self, api: Any, chat_id: Any, text: str, markup: dict[str, Any] | None = None, *, html: bool = False,
                    reply_to: Any = None) -> dict[str, Any]:  # pragma: no cover - lớp con
        """Gửi tin → `{"message_id": …}`. `html=True`: `text` đã escape từng giá trị động (và đã che secret) — không cắt."""
        raise NotImplementedError

    async def _edit(self, api: Any, chat: Any, mid: Any, html_text: str, markup: dict[str, Any] | None, *,
                    fallback: bool = True) -> bool:  # pragma: no cover - lớp con
        """Sửa tin (chữ HTML do hub sinh) + nút (None = gỡ nút). Lỗi: `fallback=True` → cố gỡ nút, trả False; `False` → ném lỗi."""
        raise NotImplementedError

    async def send_document(self, api: Any, chat: Any, name: str, data: bytes, caption: str, *, mime: str = "application/octet-stream",
                            timeout: float | None = None) -> dict[str, Any]:  # pragma: no cover - lớp con
        raise NotImplementedError

    async def typing(self, api: Any, chat: Any, *, reply_to: Any = None) -> None:  # pragma: no cover - lớp con
        """"Đang gõ…" (không bắt buộc — nền tảng không có thì lớp con bỏ qua/ném lỗi để dừng vòng nhắc). `reply_to` = tin của người
        mà trả lời sẽ bám vào (Slack: thread để đặt trạng thái); Telegram bỏ qua. Nhịp nhắc: `TYPING_EVERY_S` của lớp con nếu có."""
        raise NotImplementedError

    # Hook của luồng điều phối viên (P7, chat/manager_chat.py) — mặc định không làm gì (Telegram không đổi); Slack: tin tiến độ + nút Dừng.
    async def manager_run_started(self, api: Any, chat: Any, run_id: str, *, reply_to: Any = None) -> None:
        """`manager.chat` vừa trả `run_id` cho tin `reply_to` của người (gọi TRƯỚC vòng "đang gõ")."""

    async def manager_run_finished(self, run_id: str, *, chat: Any = None, reply_to: Any = None, outcome: str = "completed") -> None:
        """Đã có trả lời (`outcome` completed|failed) hoặc run kết thúc không trả lời (outcome của run). Không được chặn việc gửi trả lời."""
