"""Bot Telegram của hub — long polling trong daemon (không cần endpoint công khai). Giao diện tiếng Việt.

Vòng đời: tự chạy ở event `hub.started`, dừng ở `hub.stopping`. Chưa có token (`notify.telegram.bot_token_secret` trong
secret store) hoặc `enabled: false` → ngủ, kiểm lại mỗi `idle_check_s` (tắt êm, không lỗi, không event). `setup`/`disable`
gọi `kick()` để áp dụng ngay.

An toàn:
- CHỈ chat trong `notify.telegram.allowed_chat_ids` được nhận thông báo và điều khiển hub. Tin/nút từ chat khác: bỏ qua
  (không trả lời) + audit `telegram.ignored` (giới hạn 1 dòng/phút/chat). Ngoại lệ duy nhất: `/start <mã ghép nối>` đúng
  mã một lần do `agent-hub notify telegram setup` sinh ra → chỉ ghi nhận chờ NGƯỜI xác nhận ở terminal.
- Mọi lệnh đi qua `Daemon.call_as(auth.telegram_caller(...))` → cùng cổng HUMAN_ONLY, audit kênh `telegram:<chat_id>`.
- Nút inline: nonce dùng một lần + chữ ký HMAC (notify/telegram.CallbackStore); thao tác phá huỷ nặng → xác nhận 2 bước.
- Offset `getUpdates` được lưu TRƯỚC khi xử lý update → một update không bao giờ xử lý hai lần (kể cả sau restart).
- Không gửi secret (che theo giá trị + mẫu), cắt kết quả run (`result_max_chars`) kèm chỉ dẫn xem trên web/TUI.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import secrets as _secrets
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from .. import auth as _auth
from ..events import TERMINAL_RUN_EVENTS, HubEvent, iso
from ..notify.base import clip
from ..notify.telegram import CallbackStore, TelegramApi, TelegramError, TelegramNotifier, fit, format_approval, is_heavy, to_plain

if TYPE_CHECKING:
    from ..daemon import Daemon
    from ..hub import Hub

log = logging.getLogger("agent_hub.chat.telegram")

PENDING = ("open", "waiting_human")
OUTCOME_VI = {
    "completed": "hoàn thành", "failed": "lỗi", "cancelled": "đã huỷ", "budget_exceeded": "vượt ngân sách", "timed_out": "quá giờ",
    "crashed": "tiến trình chết", "rate_limited": "bị giới hạn tốc độ", "blocked": "bị chặn", "protocol_violation": "vi phạm giao thức",
}
HELP = """agent-hub qua Telegram — lệnh:
/status — tình trạng hub, chi phí hôm nay, yêu cầu chờ duyệt
/agents — danh sách agent
/runs [agent] — 10 run gần nhất
/approvals — yêu cầu chờ duyệt (kèm nút Cho phép / Từ chối)
/run <agent> <yêu cầu> — giao việc; bot gửi kết quả tóm tắt + chi phí khi xong
  Trả lời (reply) vào tin kết quả để tiếp tục hội thoại
/stop <run> — dừng run
/estop on [lý do] | /estop off — dừng khẩn cấp toàn hub (tắt cần xác nhận)
/digest — tóm tắt học 24 giờ qua
/help — trợ giúp
Xem đầy đủ (log, diff, dữ liệu MCP): agent-hub top / web."""


class ChatError(Exception):
    pass


class TelegramBot:
    WATCH = "telegram.watch"
    MSGMAP = "telegram.msgmap"
    OFFSET = "telegram.offset"

    def __init__(self, hub: "Hub") -> None:
        self.hub = hub
        self.daemon: "Daemon | None" = None
        self.callbacks = CallbackStore(hub, self._callback_key)
        self.notifier = TelegramNotifier(hub, self)
        hub.bus.add_listener(self.notifier)
        hub.bus.add_listener(self._on_event)
        self._task: asyncio.Task | None = None
        self._wake: asyncio.Event | None = None
        self._bg: set[asyncio.Task] = set()
        self._ready_for: str | None = None
        self._ignored_at: dict[str, float] = {}
        self.pairing: dict[str, Any] | None = None
        self.info: dict[str, Any] = {"polling": False, "last_error": None, "bot_username": None, "last_poll_at": None, "updates": 0}

    # ------------------------------------------------------------------ cấu hình
    def cfg(self) -> dict[str, Any]:
        return dict(self.hub.config.get("notify.telegram") or {})

    def token(self, *, ignore_enabled: bool = False) -> str | None:
        c = self.cfg()
        if not ignore_enabled and not c.get("enabled", True):
            return None
        # CHỈ secret store (không lấy biến môi trường như Slack): bot chỉ chạy sau `notify telegram setup` có chủ đích
        sec = getattr(self.hub, "secrets", None)
        try:
            return sec.get(c.get("bot_token_secret") or "TELEGRAM_BOT_TOKEN") if sec is not None else None
        except Exception:
            return None

    def api(self, *, ignore_enabled: bool = False) -> TelegramApi | None:
        t = self.token(ignore_enabled=ignore_enabled)
        if not t:
            return None
        c = self.cfg()
        return TelegramApi(str(c.get("api_base") or "https://api.telegram.org"), t, timeout=float(c.get("timeout_s") or 15))

    def allowed(self) -> set[str]:
        return {str(x) for x in (self.cfg().get("allowed_chat_ids") or [])}

    def is_allowed(self, chat_id: Any) -> bool:
        return bool(self.cfg().get("enabled", True)) and str(chat_id) in self.allowed()

    def _callback_key(self) -> str:
        name = self.cfg().get("callback_key_secret") or "TELEGRAM_CALLBACK_KEY"
        v = self.notifier._secret(name)
        if not v:
            v = _secrets.token_urlsafe(32)
            self.hub.secrets.set(name, v)  # type: ignore[attr-defined]
        return v

    def approval_markup(self, approval_id: str, chat_id: Any) -> dict[str, Any]:
        markup, _ = self.callbacks.issue([("ap", approval_id, "Cho phép"), ("dn", approval_id, "Từ chối")], chat_id, ttl=self._ttl())
        return markup

    def _ttl(self) -> float:
        return float(self.cfg().get("callback_ttl_s") or 604800)

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
                    p = ev.payload
                    self._spawn(self._clear_buttons(msgs, f"[đã quyết] {rid}: {p.get('decision')} — bởi {p.get('by')} qua {p.get('channel')}"))

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
        """Cấu hình đổi (setup/disable/allow) → khởi động lại vòng poll ngay (huỷ long poll đang treo)."""
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

    async def _loop(self) -> None:
        backoff = 0.0
        while True:
            c = self.cfg()
            api = self.api()
            if api is None:
                self.info["polling"] = False
                await self._sleep_or_wake(float(c.get("idle_check_s") or 15))
                continue
            try:
                fp = hashlib.sha256(api.token.encode()).hexdigest()
                if self._ready_for != fp:
                    me = await api.call("getMe")
                    self.info["bot_username"] = (me or {}).get("username")
                    try:
                        await api.call("deleteWebhook")  # getUpdates không chạy khi bot còn webhook
                    except TelegramError:
                        pass
                    self._ready_for = fp
                self.info["polling"] = True
                pt = int(c.get("poll_timeout_s") or 25)
                ups = await api.call(
                    "getUpdates",
                    {"offset": int(self.hub.store.state_get(self.OFFSET) or 0), "timeout": pt, "allowed_updates": ["message", "callback_query"]},
                    timeout=pt + 10,
                )
                self.info["last_poll_at"] = time.time()
                self.info["last_error"] = None
                backoff = 0.0
                for u in ups or []:
                    # lưu offset TRƯỚC khi xử lý: update không bao giờ chạy lại (chống bấm duyệt hai lần qua restart)
                    self.hub.store.state_set(self.OFFSET, int(u.get("update_id", 0)) + 1)
                    self.info["updates"] += 1
                    try:
                        await self.handle_update(u, api)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        log.exception("lỗi xử lý update Telegram")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.info["last_error"] = clip(e, 300)
                log.warning("Telegram: %s", self.info["last_error"])
                ra = getattr(e, "retry_after", None)
                backoff = float(ra) if ra else min(60.0, max(1.0, backoff * 2 or 1.0))
                await self._sleep_or_wake(backoff)

    # ------------------------------------------------------------------ gửi
    async def _send(self, api: TelegramApi, chat_id: Any, text: str, markup: dict[str, Any] | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"chat_id": chat_id, "text": fit(text), "disable_web_page_preview": True}
        if markup:
            body["reply_markup"] = markup
        res = await api.call("sendMessage", body)
        return res if isinstance(res, dict) else {}

    async def _clear_buttons(self, msgs: list[list[Any]], note: str) -> None:
        api = self.api()
        if api is None:
            return
        for chat, mid in msgs:
            try:
                await api.call("editMessageReplyMarkup", {"chat_id": chat, "message_id": mid, "reply_markup": {"inline_keyboard": []}})
            except Exception as e:  # tin đã sửa/xoá — không quan trọng
                log.debug("không gỡ được nút: %s", clip(e))

    async def send_test(self, text: str) -> dict[str, Any]:
        api = self.api()
        if api is None:
            return {"sent": False, "chats": 0, "reason": "Telegram chưa cấu hình (agent-hub notify telegram setup) hoặc đang tắt"}
        chats = sorted(self.allowed())
        if not chats:
            return {"sent": False, "chats": 0, "reason": "allowlist trống — nhắn /start <mã> cho bot rồi xác nhận (notify telegram setup)"}
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
        caller = _auth.telegram_caller(self.hub, chat_id)
        if not caller.human:
            raise ChatError("chat này không có quyền")
        p = dict(params or {})
        from ..mcp_server.rpc import MUTATING

        if method in MUTATING:
            p["_audit"] = {"channel": f"telegram:{chat_id}", "actor": "user"}
        resp = await self.daemon.call_as(caller, method, p)
        if "error" in resp:
            raise ChatError(str(resp["error"].get("message")))
        return resp.get("result")

    # ------------------------------------------------------------------ update
    async def handle_update(self, u: dict[str, Any], api: TelegramApi) -> None:
        if "callback_query" in u:
            await self._on_callback(u["callback_query"], api)
            return
        m = u.get("message")
        if not isinstance(m, dict):
            return
        chat = m.get("chat") or {}
        cid = str(chat.get("id"))
        text = str(m.get("text") or "").strip()
        if not self.is_allowed(cid):
            if self._try_pair(m, text):
                await self._send(api, cid, "Đã nhận mã ghép nối. Quay lại terminal để xác nhận đưa chat này vào allowlist.")
                return
            self._audit_ignored(cid, chat.get("type"), "message")
            return
        reply = m.get("reply_to_message")
        if isinstance(reply, dict) and text and not text.startswith("/"):
            rid = self._state(self.MSGMAP).get(f"{cid}:{reply.get('message_id')}")
            if rid:
                await self._continue(cid, rid, text, api)
                return
        if text.startswith("/"):
            await self._command(cid, text, api)
            return
        await self._send(api, cid, "Gõ /help để xem lệnh. Trả lời (reply) vào tin kết quả run để tiếp tục hội thoại.")

    def _audit_ignored(self, chat_id: str, chat_type: Any, kind: str) -> None:
        now = time.monotonic()
        if now - self._ignored_at.get(chat_id, -1e9) < 60:
            return
        self._ignored_at[chat_id] = now
        if len(self._ignored_at) > 1000:
            self._ignored_at.clear()
        log.info("Telegram: bỏ qua %s từ chat ngoài allowlist %s", kind, chat_id)
        audit = getattr(self.hub, "audit", None)
        if audit is not None:
            audit.record("telegram.ignored", None, None, {"chat_id": chat_id, "chat_type": chat_type, "kind": kind},
                         actor="unknown", channel=f"telegram:{chat_id}", summary="tin từ chat ngoài allowlist — bỏ qua")

    # ------------------------------------------------------------------ ghép nối (setup)
    def start_pairing(self) -> dict[str, Any]:
        code = _secrets.token_hex(4).upper()
        self.pairing = {"code": code, "exp": time.time() + float(self.cfg().get("pair_ttl_s") or 600), "pending": {}}
        return {"code": code, "ttl_s": float(self.cfg().get("pair_ttl_s") or 600)}

    def _try_pair(self, m: dict[str, Any], text: str) -> bool:
        pr = self.pairing
        if not pr or time.time() > pr["exp"]:
            return False
        mm = re.match(r"^/start(?:@\w+)?\s+([A-Za-z0-9_-]{4,64})$", text)
        if not mm or not _secrets.compare_digest(mm.group(1).upper(), pr["code"]):
            return False
        chat = m.get("chat") or {}
        if chat.get("type") != "private" and not self.cfg().get("allow_groups"):
            return False
        frm = m.get("from") or {}
        cid = str(chat.get("id"))
        pr["pending"][cid] = {
            "chat_id": cid, "type": chat.get("type"), "username": chat.get("username") or frm.get("username"),
            "name": " ".join(x for x in (chat.get("first_name") or frm.get("first_name"), chat.get("last_name") or frm.get("last_name")) if x) or chat.get("title"),
            "at": time.time(),
        }
        audit = getattr(self.hub, "audit", None)
        if audit is not None:
            audit.record("telegram.pair_request", None, None, {"chat_id": cid, "type": chat.get("type")}, actor="unknown", channel=f"telegram:{cid}")
        return True

    def pending(self, code: str | None) -> list[dict[str, Any]]:
        pr = self.pairing
        if not pr or (code and not _secrets.compare_digest(str(code).upper(), pr["code"])):
            return []
        return list(pr["pending"].values())

    # ------------------------------------------------------------------ lệnh
    async def _command(self, cid: str, text: str, api: TelegramApi) -> None:
        head, _, rest = text.partition(" ")
        cmd = head.split("@", 1)[0].lower()
        rest = rest.strip()
        handlers = {
            "/start": self._c_help, "/help": self._c_help, "/status": self._c_status, "/agents": self._c_agents, "/runs": self._c_runs,
            "/approvals": self._c_approvals, "/run": self._c_run, "/stop": self._c_stop, "/estop": self._c_estop, "/digest": self._c_digest,
        }
        fn = handlers.get(cmd)
        if fn is None:
            await self._send(api, cid, f"Không có lệnh {cmd}. Gõ /help.")
            return
        try:
            out = await fn(cid, rest, api)
        except ChatError as e:
            out = f"Lỗi: {clip(e, 500)}"
        if out:
            await self._send(api, cid, out)

    async def _c_help(self, cid: str, rest: str, api: TelegramApi) -> str:
        return HELP

    async def _c_status(self, cid: str, rest: str, api: TelegramApi) -> str:
        s = await self.call(cid, "hub.status")
        pend = await self.call(cid, "approval.list", {"status": "pending"})
        est = s.get("estop") or {}
        return "\n".join([
            f"agent-hub {s.get('version')} — ESTOP: {'BẬT' + (' (' + str(est.get('reason')) + ')' if est.get('reason') else '') if est.get('on') else 'tắt'}",
            f"Run đang chạy: {s.get('running')} · đang chờ: {s.get('queued')} (tối đa {s.get('max_concurrent_runs')})",
            f"Agent: {s.get('agents')} · chi phí hôm nay: ${float(s.get('cost_today_usd') or 0):.4f}",
            f"Yêu cầu chờ duyệt: {len(pend or [])}" + (" — gõ /approvals" if pend else ""),
        ])

    async def _c_agents(self, cid: str, rest: str, api: TelegramApi) -> str:
        rows = await self.call(cid, "agent.list")
        if not rows:
            return "Chưa có agent nào (tạo bằng agent-hub agent create hoặc web)."
        lines = ["Agent:"]
        for r in rows[:30]:
            lines.append(f"• {r.get('id')} — {r.get('status')}{' (tạm dừng)' if r.get('paused') else ''} · hôm nay ${float(r.get('cost_today_usd') or 0):.4f}")
        return "\n".join(lines)

    async def _c_runs(self, cid: str, rest: str, api: TelegramApi) -> str:
        rows = await self.call(cid, "run.list", {"agent": rest.split()[0] if rest else None, "limit": 10})
        if not rows:
            return "Chưa có run nào."
        lines = ["10 run gần nhất:"]
        for r in rows:
            lines.append(f"• {r.get('id')} · {r.get('agent_id')} — {r.get('status')} · ${float(r.get('cost_usd') or 0):.4f} · {str(r.get('queued_at') or '')[:16]}")
        return "\n".join(lines)

    async def _c_approvals(self, cid: str, rest: str, api: TelegramApi) -> str | None:
        rows = await self.call(cid, "approval.list", {"status": "pending", "limit": 10})
        if not rows:
            return "Không có yêu cầu nào đang chờ duyệt."
        cc = self.cfg().get("confirm_classes") or []
        for r in rows:
            p = {**r, "request_id": r["id"]}
            res = await self._send(api, cid, format_approval(p, r.get("agent_id"), r.get("run_id"), kind="approvals.list", heavy=is_heavy(r, cc)),
                                   self.approval_markup(r["id"], cid))
            self.callbacks.add_message(r["id"], cid, res.get("message_id"))
        return None

    async def _c_run(self, cid: str, rest: str, api: TelegramApi) -> str:
        agent, _, prompt = rest.partition(" ")
        if not agent or not prompt.strip():
            return "Cú pháp: /run <agent> <yêu cầu>"
        r = await self.call(cid, "run.start", {"agent": agent, "prompt": prompt.strip()})
        self._watch(r["id"], cid)
        return f"Đã giao run {r['id']} cho agent {agent} ({r.get('status')}). Bot sẽ gửi kết quả khi xong."

    async def _c_stop(self, cid: str, rest: str, api: TelegramApi) -> str:
        if not rest:
            return "Cú pháp: /stop <run>"
        r = await self.call(cid, "run.stop", {"id": rest.split()[0]})
        return f"Đã gửi yêu cầu dừng run {(r or {}).get('id') or rest.split()[0]}."

    async def _c_estop(self, cid: str, rest: str, api: TelegramApi) -> str | None:
        arg, _, reason = rest.partition(" ")
        arg = arg.lower()
        if arg == "on":
            await self.call(cid, "estop.set", {"on": True, "reason": reason.strip() or f"Telegram {cid}"})
            return "ESTOP: BẬT — không khởi chạy run mới (run đang chạy vẫn tiếp; dừng từng run bằng /stop)."
        if arg == "off":
            markup, _ = self.callbacks.issue([("eo", "estop", "Xác nhận TẮT ESTOP"), ("cx0", "estop", "Huỷ")], cid, ttl=600)
            await self._send(api, cid, "Tắt ESTOP cho phép hub chạy run trở lại. Xác nhận?", markup)
            return None
        st = await self.call(cid, "estop.get")
        return f"ESTOP: {'BẬT' if (st or {}).get('on') else 'tắt'}. Cú pháp: /estop on [lý do] | /estop off"

    async def _c_digest(self, cid: str, rest: str, api: TelegramApi) -> str:
        ops = getattr(self.hub, "learning_ops", None)
        if ops is None:
            d = await self.call(cid, "digest.show", {})
            return fit(to_plain(str((d or {}).get("text") or "Chưa có digest.")))
        end = datetime.now(timezone.utc)
        data = ops.digest.collect(iso(end - timedelta(hours=24)), iso(end))  # chỉ đọc — không dời mốc digest 08:00
        return fit("Digest 24 giờ qua\n" + to_plain(ops.digest.render(data)), hint="xem đầy đủ: agent-hub digest, TUI hoặc web")

    async def _continue(self, cid: str, run_id: str, text: str, api: TelegramApi) -> None:
        try:
            r = await self.call(cid, "run.continue", {"id": run_id, "prompt": text})
        except ChatError as e:
            await self._send(api, cid, f"Không tiếp tục được run {run_id}: {clip(e, 400)}")
            return
        self._watch(r["id"], cid)
        await self._send(api, cid, f"Đã tiếp tục hội thoại: run {r['id']} (agent {r.get('agent_id')}). Bot sẽ gửi kết quả khi xong.")

    # ------------------------------------------------------------------ kết quả run
    def format_result(self, run: dict[str, Any]) -> str:
        rid = run["id"]
        outcome = run.get("outcome") or run.get("status")
        dur = float(run.get("duration_ms") or 0) / 1000
        head = f"[agent-hub] Run {rid} (agent {run.get('agent_id')}) — {OUTCOME_VI.get(str(outcome), str(outcome))}"
        cost = f"Chi phí: ${float(run.get('cost_usd') or 0):.4f}" + (f" · {run.get('num_turns')} lượt" if run.get("num_turns") else "") + (f" · {dur:.1f} s" if dur else "")
        body = str(run.get("summary") or run.get("error") or "(không có nội dung kết quả)")
        n = int(self.cfg().get("result_max_chars") or 1500)
        if len(body) > n:
            body = body[:n] + f"\n…(đã cắt — xem đầy đủ: agent-hub runs show {rid}, TUI hoặc web)"
        return "\n".join([head, cost, "Kết quả:", body, "", "Trả lời (reply) tin này để tiếp tục hội thoại."])

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
                await self._send(api, chat, f"Run {run_id} đang chờ người duyệt thao tác phá huỷ — xem tin duyệt hoặc gõ /approvals.")
            return
        if outcome in ("resumed", "interrupted"):
            return  # run con (resume) kế thừa theo dõi qua parent_run_id
        w.pop(run_id, None)
        self.hub.store.state_set(self.WATCH, w)
        if api is None or not self.is_allowed(chat):
            return
        try:
            res = await self._send(api, chat, self.format_result(run))
            if res.get("message_id") is not None:
                self._map_message(chat, res["message_id"], run_id)
        except Exception as e:
            log.warning("không gửi được kết quả run %s: %s", run_id, clip(e))

    # ------------------------------------------------------------------ nút inline
    async def _on_callback(self, cq: dict[str, Any], api: TelegramApi) -> None:
        msg = cq.get("message") or {}
        chat = msg.get("chat") or {}
        cid = str(chat.get("id"))
        qid = cq.get("id")
        if not self.is_allowed(cid):
            self._audit_ignored(cid, chat.get("type"), "callback")
            return
        rec, why = self.callbacks.consume(cq.get("data"), cid)
        if rec is None:
            audit = getattr(self.hub, "audit", None)
            if audit is not None:
                audit.record("telegram.callback_rejected", None, None, {"chat_id": cid, "reason": why, "from_id": (cq.get("from") or {}).get("id")},
                             actor="user", channel=f"telegram:{cid}", summary=f"nút bị từ chối: {why}")
            await self._answer(api, qid, {"unknown_or_used": "Nút này đã dùng hoặc không còn hiệu lực.", "expired": "Nút đã hết hạn — gõ /approvals.",
                                          }.get(why, "Nút không hợp lệ."))
            return
        a, ref = rec["a"], rec["r"]
        try:
            if a in ("ap", "apc", "dn"):
                text = await self._decide(cid, a, ref, api)
            elif a == "eo":
                await self.call(cid, "estop.set", {"on": False, "reason": f"Telegram {cid}"})
                text = "Đã TẮT ESTOP — hub chạy run trở lại."
            elif a == "cx":
                text = "Đã huỷ bước xác nhận — yêu cầu vẫn chờ duyệt."
                ap = await self.call(cid, "approval.get", {"id": ref})
                if ap.get("status") in PENDING:
                    p = {**ap, "request_id": ap["id"]}
                    res = await self._send(api, cid, format_approval(p, ap.get("agent_id"), ap.get("run_id"), kind="approvals.list",
                                                                     heavy=is_heavy(ap, self.cfg().get("confirm_classes") or [])), self.approval_markup(ap["id"], cid))
                    self.callbacks.add_message(ap["id"], cid, res.get("message_id"))
            else:
                text = "Đã huỷ."
        except ChatError as e:
            text = f"Lỗi: {clip(e, 300)}"
        await self._answer(api, qid, text)
        try:
            await api.call("editMessageReplyMarkup", {"chat_id": cid, "message_id": msg.get("message_id"), "reply_markup": {"inline_keyboard": []}})
        except Exception:
            pass
        await self._send(api, cid, text)

    async def _answer(self, api: TelegramApi, qid: Any, text: str) -> None:
        if qid is None:
            return
        try:
            await api.call("answerCallbackQuery", {"callback_query_id": qid, "text": text[:190]})
        except Exception as e:
            log.debug("answerCallbackQuery lỗi: %s", clip(e))

    async def _decide(self, cid: str, action: str, approval_id: str, api: TelegramApi) -> str:
        ap = await self.call(cid, "approval.get", {"id": approval_id})
        if ap.get("status") not in PENDING:
            return f"Yêu cầu {approval_id} đã {ap.get('status')} trước đó (bởi {ap.get('resolved_by')} qua {ap.get('resolved_channel')})."
        if action == "ap" and is_heavy(ap, self.cfg().get("confirm_classes") or []):
            markup, _ = self.callbacks.issue([("apc", approval_id, "Xác nhận CHO PHÉP"), ("cx", approval_id, "Huỷ")], cid, ttl=min(self._ttl(), 3600))
            res = await self._send(api, cid, (
                f"Xác nhận bước 2 — cho phép MỘT lần thao tác NẶNG của agent {ap.get('agent_id')} (run {ap.get('run_id')}):\n"
                f"{clip(ap.get('preview'), 300)}\nLý do giữ: {clip(ap.get('reason'), 200)}"), markup)
            self.callbacks.add_message(approval_id, cid, res.get("message_id"))
            return "Thao tác nặng — cần xác nhận bước 2 (tin bên dưới)."
        if action == "dn":
            await self.call(cid, "approval.deny", {"id": approval_id, "channel": f"telegram:{cid}", "by": "user", "note": "từ chối qua Telegram"})
            return f"Đã TỪ CHỐI {approval_id}."
        await self.call(cid, "approval.approve", {"id": approval_id, "channel": f"telegram:{cid}", "by": "user"})
        return f"Đã CHO PHÉP {approval_id} (một lần) — run sẽ chạy tiếp."

    # ------------------------------------------------------------------ trạng thái
    def status(self) -> dict[str, Any]:
        c = self.cfg()
        pr = self.pairing
        return {
            "enabled": bool(c.get("enabled", True)),
            "configured": self.token(ignore_enabled=True) is not None,
            "bot_username": self.info.get("bot_username"),
            "allowed_chat_ids": sorted(self.allowed()),
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
