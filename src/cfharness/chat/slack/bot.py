"""Bot Slack 2 chiều của hub — Socket Mode trong daemon (không cần URL công khai). docs/plan/slack-2way.md, P1.

Vòng đời như bot Telegram (chat/base.py): tự chạy ở `hub.started`, dừng ở `hub.stopping`. Tắt êm khi `notify.slack.interactive`
tắt hoặc thiếu bot token (`SLACK_BOT_TOKEN`) / app token (`SLACK_APP_TOKEN`) trong secret store.

Socket Mode: `apps.connections.open` (app token) → WebSocket; mỗi envelope được ACK `{"envelope_id"}` NGAY (Slack đòi < 3 s), rồi
xử lý TUẦN TỰ trong một hàng đợi (giữ thứ tự như long polling của Telegram). `disconnect` (Slack xoay kết nối) → mở kết nối mới.
Chống xử lý lại: khoá theo `event_id` / `trigger_id` lưu `hub_state slack.seen` TRƯỚC khi xử lý (Slack gửi lại envelope chưa ACK).

An toàn (như Telegram — WT-4/5/8):
- Người = `auth.slack_caller`: kênh bật, `team_id` khớp workspace ghim lúc setup, người (`user`) ∈ `allowed_user_ids` VÀ chat ∈
  `allowed_chat_ids` (DM `D…`; kênh chung chỉ khi `allow_channels`). Người lạ: không trả lời, audit `slack.ignored`.
  Ngoại lệ duy nhất: `ghepnoi <mã>` đúng mã một lần do `cfharness notify slack setup` sinh → chỉ ghi nhận chờ NGƯỜI xác nhận.
- Nút: giá trị = `v1.<nonce>.<HMAC>` của `CallbackStore` (khoá `SLACK_CALLBACK_KEY`, kho `slack.callbacks`), tiêu cả nhóm.
- mrkdwn: mọi giá trị động escape `& < >`, khối chữ `verbatim` (chat/slack/render.py) — agent không ping `<!channel>` / giả link được.
- Lệnh: client Slack chặn tin bắt đầu bằng `/` → một slash command (`notify.slack.slash_command`, mặc định `/cfh <lệnh> …`); kết quả
  gửi vào DM của người với bot. Tin chữ thường trong DM → điều phối viên; trả lời trong thread = "reply" của Telegram.
- Ô nhập (P2): Slack không có `force_reply` → `ask_input` mở MODAL bằng `trigger_id` của nút/slash vừa xử lý (chat/slack/modals.py:
  nonce phía hub `slack.modals`, submit kiểm workspace + đúng người + allowlist rồi mới `self.call`). Không có trigger → thread như P1.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
import re
import secrets as _secrets
import time
from typing import TYPE_CHECKING, Any

from ... import auth as _auth
from ...events import HubEvent
from ...notify.base import QueuedNotifier, clip
from ...notify.slack import format_message as _slack_format
from ...notify.telegram import APPROVAL_EVENTS
from ..base import _UID, ChatBot
from . import render
from .api import SlackApi, SlackError
from .modals import SlackModals

if TYPE_CHECKING:
    from ...hub import Hub

log = logging.getLogger("cfharness.chat.slack")

PAIR_RE = re.compile(r"^(?:ghepnoi|ghép nối|pair)\s+([A-Za-z0-9_-]{4,64})$", re.IGNORECASE)
SEEN_TTL_S = 3600
SEEN_MAX = 2000
ROOTS_MAX = 2000
# trigger_id của tương tác đang xử lý (nút / slash command) — mở modal được MỘT lần, ~3 s (P2: chat/slack/modals.py)
_TRIGGER: contextvars.ContextVar[str | None] = contextvars.ContextVar("slack_trigger_id", default=None)


def help_text(slash: str) -> str:
    c = slash
    return f"""cfharness qua Slack — lệnh (gõ {c} <lệnh>):
{c} canban — mọi thứ đang chờ bạn: yêu cầu duyệt + câu hỏi của agent
{c} hoi — câu hỏi của agent đang chờ bạn trả lời
{c} trangthai (status) — tình trạng hub, chi phí hôm nay
{c} agent — danh sách agent (kèm nút giao việc / dừng)
{c} luotchay [agent] — lượt chạy gần nhất
{c} giaoviec <agent> <yêu cầu> — giao việc; bot gửi kết quả tóm tắt + chi phí khi xong (thiếu yêu cầu → hộp nhập)
  Trả lời trong thread của tin kết quả để tiếp tục hội thoại
{c} approvals — yêu cầu chờ duyệt (kèm nút Cho phép / Từ chối)
{c} stop [run] — dừng run
{c} estop on [lý do] | {c} estop off — dừng khẩn cấp toàn hub (tắt cần xác nhận)
{c} digest — tóm tắt học 24 giờ qua
{c} lich — lịch chạy tự động (xem)
{c} moi — mở cuộc trò chuyện mới với điều phối viên
{c} help — trợ giúp
Tin nhắn chữ thường trong DM với bot → gửi cho ĐIỀU PHỐI VIÊN; trả lời trong thread của tin điều phối viên để tiếp tục đúng
  cuộc trò chuyện đó.
Xem đầy đủ (log, diff, dữ liệu MCP): cfharness top / web."""


class SlackChatNotifier(QueuedNotifier):
    """Thông báo của bot 2 chiều: approval → cặp tin có nút (`send_approval`), sự kiện khác → chữ hub (định dạng Slack cũ, escape).
    Khi bot hoạt động, `SlackNotifier` (webhook/kênh, notify/slack.py) nhường toàn bộ cho lớp này — không gửi trùng."""

    channel = "slack"

    def __init__(self, hub: "Hub", bot: "SlackBot") -> None:
        super().__init__(hub)
        self.bot = bot

    def target(self) -> dict[str, Any] | None:
        api = self.bot.api()
        chats = self.bot.notify_chats()
        if api is None or not chats:
            return None
        return {"api": api, "chats": sorted(chats)}

    def render(self, ev: HubEvent) -> dict[str, Any] | None:
        p = ev.payload or {}
        aid = p.get("request_id") if ev.type in APPROVAL_EVENTS else None
        if aid:
            try:
                row = self.hub.approvals.get(aid)
                if row.get("status") not in ("open", "waiting_human"):
                    return None  # đã quyết trước khi kịp gửi
            except Exception:
                pass
            return {"approval_id": aid, "kind": ev.type, "payload": dict(p), "done": set()}
        text = _slack_format(ev)
        if not text:
            return None
        return {"text": text, "approval_id": None, "done": set()}

    async def _send(self, target: dict[str, Any], ev: HubEvent, msg: Any) -> dict[str, Any]:
        api: SlackApi = target["api"]
        n = 0
        for chat in target["chats"]:
            if chat in msg["done"]:
                continue
            aid = msg.get("approval_id")
            if aid:
                row = self.hub.approvals.get(aid)
                if row.get("status") not in ("open", "waiting_human"):
                    return {"chats": n, "skipped": "resolved"}
                await self.bot.send_approval(api, chat, row, kind=msg["kind"], payload=msg.get("payload"))
            else:
                await self.bot._send(api, chat, msg["text"])  # chữ thuần → escape (giữ *đậm*/`mã` của định dạng cũ)
            msg["done"].add(chat)
            n += 1
        return {"chats": n}


class SlackBot(ChatBot):
    NAME = "slack"
    LABEL = "Slack"
    CFG_KEY = "notify.slack"
    SEEN = "slack.seen"
    DMS = "slack.dms"  # user_id → kênh DM với bot (conversations.open)

    def __init__(self, hub: "Hub") -> None:
        super().__init__(hub)
        self.info.update({"team_id": None, "bot_user_id": None, "connected_at": None})
        self._roots: dict[str, str] = {}   # ts tin bot gửi → ts gốc của thread
        self._thread: dict[str, list[str]] = {}  # ts gốc → các ts bot đã gửi trong thread (mới nhất cuối)
        self._inbox: asyncio.Queue[tuple[str, dict[str, Any]]] | None = None
        self.modals = SlackModals(self)  # P2: ô nhập giao việc / trả lời câu hỏi bằng chữ

    def _make_notifier(self) -> SlackChatNotifier:
        return SlackChatNotifier(self.hub, self)

    # ------------------------------------------------------------------ cấu hình
    def _secret(self, name: str | None) -> str | None:
        # CHỈ secret store (như Telegram): kênh 2 chiều chỉ chạy sau `notify slack setup` có chủ đích
        sec = getattr(self.hub, "secrets", None)
        try:
            return sec.get(name) if (sec is not None and name) else None
        except Exception:
            return None

    def token(self, *, ignore_enabled: bool = False) -> str | None:
        c = self.cfg()
        if not ignore_enabled and not (c.get("enabled", True) and c.get("interactive")):
            return None
        return self._secret(c.get("bot_token_secret") or "SLACK_BOT_TOKEN")

    def app_token(self) -> str | None:
        return self._secret(self.cfg().get("app_token_secret") or "SLACK_APP_TOKEN")

    def api(self, *, ignore_enabled: bool = False) -> SlackApi | None:
        t = self.token(ignore_enabled=ignore_enabled)
        if not t:
            return None
        c = self.cfg()
        return SlackApi(str(c.get("api_base") or "https://slack.com/api"), t, self.app_token(), timeout=float(c.get("timeout_s") or 15))

    def active(self) -> bool:
        """Bot 2 chiều đang thay kênh webhook (có token + có người trong allowlist) → `SlackNotifier` nhường."""
        return self.api() is not None and self.app_token() is not None and bool(self.notify_chats())

    def caller(self, chat_id: Any, user_id: Any = None) -> "_auth.Caller":
        return _auth.slack_caller(self.hub, chat_id, user_id)

    def allowed(self) -> set[str]:
        return {str(x) for x in (self.cfg().get("allowed_chat_ids") or [])}

    def notify_chats(self) -> set[str]:
        """Chat nhận thông báo: DM trong allowlist; kênh chung chỉ khi `allow_channels`."""
        chans = bool(self.cfg().get("allow_channels"))
        return {c for c in self.allowed() if c.startswith("D") or chans}

    def slash(self) -> str:
        return str(self.cfg().get("slash_command") or "/cfh")

    def cmd(self, name: str) -> str:
        return f"{self.slash()} {name}"

    def help_text(self) -> str:
        return help_text(self.slash())

    def empty_allowlist_hint(self) -> str:
        return "allowlist trống — nhắn `ghepnoi <mã>` cho bot trong DM rồi xác nhận (notify slack setup)"

    def reply_hint(self, what: str) -> str:
        return f"Trả lời trong thread của tin này để {what}."

    def input_where(self) -> str:
        return "ở hộp nhập vừa mở" if _TRIGGER.get() else "ở tin bên dưới (trả lời trong thread)"

    async def ask_input(self, api: SlackApi, cid: str, kind: str, ref: str, text: str, placeholder: str, *,
                        reply_to: Any = None) -> dict[str, Any]:
        """P2: có `trigger_id` (nút / slash command vừa bấm) → modal; không có / `views.open` lỗi → tin mời trả lời trong thread (P1)."""
        trig = _TRIGGER.get()
        if trig:
            _TRIGGER.set(None)  # trigger dùng một lần
            try:
                await self.modals.open(api, trig, cid, _UID.get(), kind, ref)
                return {"message_id": None, "modal": True}
            except Exception as e:
                log.info("Slack: không mở được modal (%s) — dùng trả lời trong thread", clip(e, 200))
        return await super().ask_input(api, cid, kind, ref, text.replace("trả lời (reply) tin này", "trả lời trong thread của tin này"),
                                       placeholder, reply_to=reply_to)

    def team_ok(self, team: Any) -> bool:
        pinned = self.cfg().get("team_id")
        return not pinned or str(team or "") == str(pinned)

    # ------------------------------------------------------------------ Socket Mode
    async def _loop(self) -> None:
        backoff = 0.0
        while True:
            c = self.cfg()
            api = self.api()
            if api is None or not api.app_token:
                self.info["polling"] = False
                await self._sleep_or_wake(float(c.get("idle_check_s") or 15))
                continue
            try:
                fp = hashlib.sha256((api.token + "|" + str(api.app_token)).encode()).hexdigest()
                if self._ready_for != fp:
                    me = await api.call("auth.test")
                    self.info["team_id"] = me.get("team_id")
                    self.info["bot_user_id"] = me.get("user_id")
                    self._ready_for = fp
                conn = await api.call("apps.connections.open", app=True)
                await self._serve(str(conn.get("url") or ""), api)
                backoff = 0.0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.info["polling"] = False
                self.info["last_error"] = clip(e, 300)
                log.warning("Slack: %s", self.info["last_error"])
                ra = getattr(e, "retry_after", None)
                backoff = float(ra) if ra else min(60.0, max(1.0, backoff * 2 or 1.0))
                await self._sleep_or_wake(backoff)

    async def _serve(self, url: str, api: SlackApi) -> None:
        """Một kết nối WebSocket: ACK ngay, đẩy envelope vào hàng đợi; một worker xử lý tuần tự. Trả về khi Slack báo `disconnect`."""
        from websockets.asyncio.client import connect

        if not url.startswith(("wss://", "ws://")):
            raise SlackError("apps.connections.open: URL WebSocket không hợp lệ")
        self._inbox = asyncio.Queue()
        worker = asyncio.get_running_loop().create_task(self._worker(api))
        try:
            async with connect(url, open_timeout=float(self.cfg().get("timeout_s") or 15), ping_interval=20, max_size=4 * 1024 * 1024) as ws:
                self.info["polling"] = True
                self.info["connected_at"] = time.time()
                self.info["last_error"] = None
                async for raw in ws:
                    try:
                        env = json.loads(raw)
                    except ValueError:
                        continue
                    if not isinstance(env, dict):
                        continue
                    typ = env.get("type")
                    eid = env.get("envelope_id")
                    if eid:  # ACK trước mọi xử lý (Slack đòi < 3 s; chậm → gửi lại)
                        await ws.send(json.dumps({"envelope_id": eid}))
                    if typ == "disconnect":
                        return
                    if typ in ("events_api", "interactive", "slash_commands") and isinstance(env.get("payload"), dict):
                        if not self._first_time(typ, env):
                            continue
                        self.info["updates"] += 1
                        self.info["last_poll_at"] = time.time()
                        self._inbox.put_nowait((typ, env["payload"]))
        finally:
            self.info["polling"] = False
            if self._inbox is not None:
                try:  # xử lý nốt envelope đã ACK (không để mất) rồi dừng worker
                    await asyncio.wait_for(self._inbox.join(), 10)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
            worker.cancel()

    async def _worker(self, api: SlackApi) -> None:
        assert self._inbox is not None
        while True:
            typ, payload = await self._inbox.get()
            try:
                await self.handle_envelope(typ, payload, api)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("lỗi xử lý envelope Slack %s", typ)
            finally:
                self._inbox.task_done()

    def _first_time(self, typ: str, env: dict[str, Any]) -> bool:
        """Khoá chống trùng, ghi TRƯỚC khi xử lý (Slack gửi lại envelope chưa ACK; kết nối xoay có thể giao trùng)."""
        p = env.get("payload") or {}
        key = p.get("event_id") or p.get("trigger_id") or env.get("envelope_id")
        if not key:
            return True
        key = f"{typ}:{key}"
        seen = self._state(self.SEEN)
        now = time.time()
        if key in seen:
            return False
        seen = {k: t for k, t in seen.items() if now - float(t or 0) < SEEN_TTL_S}
        seen[key] = now
        if len(seen) > SEEN_MAX:
            for k in list(seen)[: len(seen) - SEEN_MAX]:
                seen.pop(k, None)
        self.hub.store.state_set(self.SEEN, seen)
        return True

    # ------------------------------------------------------------------ gửi
    def _remember(self, ts: str, root: str) -> None:
        self._roots[ts] = root
        lst = self._thread.setdefault(root, [])
        if ts not in lst:
            lst.append(ts)
        if len(self._roots) > ROOTS_MAX:
            for k in list(self._roots)[: len(self._roots) - ROOTS_MAX]:
                self._roots.pop(k, None)
        if len(self._thread) > ROOTS_MAX:
            for k in list(self._thread)[: len(self._thread) - ROOTS_MAX]:
                self._thread.pop(k, None)

    def _root_of(self, ts: Any) -> str | None:
        if ts is None:
            return None
        return self._roots.get(str(ts), str(ts))

    async def _send(self, api: SlackApi, chat_id: Any, text: str, markup: dict[str, Any] | None = None, *, html: bool = False,
                    reply_to: Any = None) -> dict[str, Any]:
        if markup and markup.get("keyboard"):  # bàn phím nhanh của Telegram — Slack không có
            markup = None
        body: dict[str, Any] = {"channel": str(chat_id), **render.message(text, markup, html=html), "unfurl_links": False, "unfurl_media": False}
        root = self._root_of(reply_to)
        if root:
            body["thread_ts"] = root
        res = await api.call("chat.postMessage", body)
        ts = res.get("ts")
        if ts:
            self._remember(str(ts), root or str(ts))
        return {"message_id": ts}

    async def _edit(self, api: SlackApi, chat: Any, mid: Any, html_text: str, markup: dict[str, Any] | None, *,
                    fallback: bool = True) -> bool:
        if mid is None:
            return False
        try:
            await api.call("chat.update", {"channel": str(chat), "ts": str(mid), **render.message(html_text, markup, html=True)})
            return True
        except Exception as e:
            if not fallback:
                raise
            log.debug("không sửa được tin Slack: %s", clip(e))
            return False

    async def send_document(self, api: SlackApi, chat: Any, name: str, data: bytes, caption: str, *,
                            mime: str = "application/octet-stream", timeout: float | None = None) -> dict[str, Any]:
        await api.upload(str(chat), name, data, render.escape(caption), timeout=timeout)
        return {"message_id": None}  # Slack không trả ts tin chứa tệp ngay lúc tải lên

    async def typing(self, api: SlackApi, chat: Any) -> None:
        raise SlackError("bot Slack không có trạng thái 'đang gõ'")  # vòng nhắc của điều phối viên dừng êm

    async def _toast(self, api: SlackApi, chat: str, user: str | None, text: str) -> None:
        """Tương đương answerCallbackQuery: tin tạm (ephemeral) chỉ người bấm thấy."""
        if not user:
            return
        try:
            await api.call("chat.postEphemeral", {"channel": chat, "user": user, "text": render.escape(text[:300])})
        except Exception as e:
            log.debug("chat.postEphemeral lỗi: %s", clip(e))

    async def dm_of(self, api: SlackApi, user: str) -> str | None:
        dms = self._state(self.DMS)
        if dms.get(user):
            return str(dms[user])
        try:
            res = await api.call("conversations.open", {"users": user})
        except SlackError as e:
            log.debug("conversations.open lỗi: %s", clip(e))
            return None
        cid = (res.get("channel") or {}).get("id")
        if cid:
            dms[user] = cid
            self.hub.store.state_set(self.DMS, dms)
        return cid

    # ------------------------------------------------------------------ nhận
    async def handle_envelope(self, typ: str, p: dict[str, Any], api: SlackApi) -> None:
        _TRIGGER.set(None)  # worker dùng chung một context — không để trigger của envelope trước lọt sang
        if typ == "events_api":
            ev = p.get("event") or {}
            if ev.get("type") == "message":
                await self._on_message(ev, p.get("team_id"), api)
        elif typ == "slash_commands":
            await self._on_slash(p, api)
        elif typ == "interactive" and p.get("type") == "block_actions":
            await self._on_actions(p, api)
        elif typ == "interactive" and p.get("type") == "view_submission":
            await self.modals.on_submission(p, api)

    async def _on_message(self, ev: dict[str, Any], team: Any, api: SlackApi) -> None:
        if ev.get("bot_id") or ev.get("subtype") not in (None, "file_share"):
            return  # tin của bot (kể cả chính mình), sửa/xoá tin… — bỏ qua
        if self.info.get("bot_user_id") and ev.get("user") == self.info["bot_user_id"]:
            return
        cid = str(ev.get("channel") or "")
        uid = str(ev.get("user") or "") or None
        _UID.set(uid)
        text = str(ev.get("text") or "").strip()
        if not self.team_ok(team) or not self.is_allowed(cid, uid):
            if self.team_ok(team) and self._try_pair(cid, uid, text, ev.get("channel_type")):
                await self._send(api, cid, "Đã nhận mã ghép nối. Quay lại terminal để xác nhận đưa DM này vào allowlist.")
                return
            self._audit_ignored(cid, ev.get("channel_type"), "message", uid)
            return
        if ev.get("subtype") == "file_share":  # P4: ảnh/tệp → điều phối viên
            await self._send(api, cid, "Bot Slack chưa nhận ảnh/tệp — gửi chữ, hoặc dùng web/TUI.", reply_to=ev.get("ts"))
            return
        if not text:
            return
        root = ev.get("thread_ts")
        if root and root != ev.get("ts") and await self._on_thread_reply(cid, str(root), text, api):
            return
        await self.manager.handle_text(cid, text, api, message_id=ev.get("ts"))

    def _candidates(self, root: str) -> list[str]:
        """Tin của bot trong thread (mới nhất trước) + gốc — nơi người có thể đang trả lời."""
        return list(reversed(self._thread.get(root, []))) + [root]

    async def _on_thread_reply(self, cid: str, root: str, text: str, api: SlackApi) -> bool:
        """Trả lời trong thread ≈ reply của Telegram: câu hỏi → ô giao việc → run.continue → hội thoại điều phối viên."""
        cands = self._candidates(root)
        for mid in cands:
            qid = self.questions.question_of_message(cid, mid)
            if qid:
                await self.questions.answer_text(cid, qid, text, api)
                return True
        for mid in cands:
            if await self.flows.on_reply(cid, mid, text, api):
                return True
        runs = self._state(self.MSGMAP)
        for mid in cands:
            rid = runs.get(f"{cid}:{mid}")
            if rid:
                await self._continue(cid, rid, text, api)
                return True
        for mid in cands:
            conv = self.manager.conv_of_message(cid, mid)
            if conv:
                await self.manager.handle_text(cid, text, api, message_id=root, conv=conv)
                return True
        return False

    async def _on_slash(self, p: dict[str, Any], api: SlackApi) -> None:
        uid = str(p.get("user_id") or "") or None
        _UID.set(uid)
        text = str(p.get("text") or "").strip()
        name, _, rest = text.partition(" ")
        if not self.team_ok(p.get("team_id")) or uid is None:
            self._audit_ignored(str(p.get("channel_id") or "?"), "slash", "slash", uid)
            return
        cid = await self.dm_of(api, uid)
        if cid is None:
            return
        if not self.is_allowed(cid, uid):
            if self._try_pair(cid, uid, text, "im"):
                await self._send(api, cid, "Đã nhận mã ghép nối. Quay lại terminal để xác nhận đưa DM này vào allowlist.")
                return
            self._audit_ignored(cid, "slash", "slash", uid)
            return
        _TRIGGER.set(str(p.get("trigger_id") or "") or None)
        await self.run_command(cid, (name or "help").lower(), rest.strip(), api)

    async def _on_actions(self, p: dict[str, Any], api: SlackApi) -> None:
        user = p.get("user") or {}
        uid = str(user.get("id") or "") or None
        _UID.set(uid)
        cid = str((p.get("channel") or {}).get("id") or (p.get("container") or {}).get("channel_id") or "")
        mid = (p.get("message") or {}).get("ts") or (p.get("container") or {}).get("message_ts")
        team = (p.get("team") or {}).get("id") or user.get("team_id")
        if not self.team_ok(team) or not self.is_allowed(cid, uid):  # người bấm cũng phải thuộc allowlist — không tiêu nút
            self._audit_ignored(cid, "action", "callback", uid)
            return
        acts = p.get("actions") or []
        data = acts[0].get("value") if acts and isinstance(acts[0], dict) else None
        rec, why = self.callbacks.consume(data, cid)
        if rec is None:
            self.audit_rejected(cid, why, uid)
            await self._toast(api, cid, uid, self.rejected_text(why, self.cmd("approvals")))
            return
        if mid:
            self._remember(str(mid), self._root_of(mid) or str(mid))
        _TRIGGER.set(str(p.get("trigger_id") or "") or None)
        text, edit, markup = await self.handle_action(cid, rec, mid, api)
        await self._toast(api, cid, uid, text)
        if edit is not None:
            await self._edit(api, cid, mid, edit, markup)

    # ------------------------------------------------------------------ ghép nối
    def _try_pair(self, cid: str, uid: str | None, text: str, channel_type: Any) -> bool:
        pr = self.pairing
        if not pr or time.time() > pr["exp"] or not uid:
            return False
        m = PAIR_RE.match(text.strip())
        if not m or not _secrets.compare_digest(m.group(1).upper(), pr["code"]):
            return False
        if not cid.startswith("D") and not self.cfg().get("allow_channels"):
            return False
        pr["pending"][cid] = {"chat_id": cid, "user_id": uid, "type": "im" if cid.startswith("D") else str(channel_type or "channel"),
                              "username": None, "name": None, "team_id": self.info.get("team_id"), "at": time.time()}
        api = self.api()
        if api is not None:
            self._spawn(self._fill_name(api, cid, uid))
        audit = getattr(self.hub, "audit", None)
        if audit is not None:
            audit.record("slack.pair_request", None, None, {"chat_id": cid, "user_id": uid}, actor="unknown", channel=f"slack:{cid}")
        return True

    async def _fill_name(self, api: SlackApi, cid: str, uid: str) -> None:
        try:
            u = (await api.call("users.info", {"user": uid})).get("user") or {}
        except Exception:
            return
        rec = (self.pairing or {}).get("pending", {}).get(cid)
        if rec is not None:
            rec["username"] = u.get("name")
            rec["name"] = (u.get("profile") or {}).get("real_name") or u.get("real_name")

    # ------------------------------------------------------------------ trạng thái
    def status(self) -> dict[str, Any]:
        out = super().status()
        c = self.cfg()
        out.update({"interactive": bool(c.get("interactive")), "app_token": self.app_token() is not None, "team_id": c.get("team_id"),
                    "bot_user_id": self.info.get("bot_user_id"), "allowed_chat_ids": sorted(self.allowed()),
                    "allowed_user_ids": sorted(str(x) for x in (c.get("allowed_user_ids") or [])), "slash_command": self.slash(),
                    "connected_at": self.info.get("connected_at"), "open_modals": self.modals.count()})
        return out
