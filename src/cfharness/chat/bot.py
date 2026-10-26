"""Bot Telegram của hub — long polling trong daemon (không cần endpoint công khai). Giao diện tiếng Việt.

Vòng đời: tự chạy ở event `hub.started`, dừng ở `hub.stopping`. Chưa có token (`notify.telegram.bot_token_secret` trong
secret store) hoặc `enabled: false` → ngủ, kiểm lại mỗi `idle_check_s` (tắt êm, không lỗi, không event). `setup`/`disable`
gọi `kick()` để áp dụng ngay.

An toàn:
- CHỈ chat trong `notify.telegram.allowed_chat_ids` (và người gửi/bấm thuộc allowlist người — WT-4, xem `auth.telegram_caller`)
  được nhận thông báo và điều khiển hub. Nhóm chỉ khi `allow_groups`. Tin/nút từ chat/người khác: bỏ qua
  (không trả lời) + audit `telegram.ignored` (giới hạn 1 dòng/phút/chat). Ngoại lệ duy nhất: `/start <mã ghép nối>` đúng
  mã một lần do `cfharness notify telegram setup` sinh ra → chỉ ghi nhận chờ NGƯỜI xác nhận ở terminal.
- Mọi lệnh đi qua `Daemon.call_as(auth.telegram_caller(...))` → cùng cổng HUMAN_ONLY, audit kênh `telegram:<chat_id>`.
- Nút inline: nonce dùng một lần + chữ ký HMAC (notify/telegram.CallbackStore); MỌI "Cho phép" cần xác nhận bước 2 (như hộp
  xác nhận của web — WT-5). "Luôn cho phép…" (LLM-2) chỉ khi daemon báo `always_offer`; cũng 2 bước, bước 2 nêu luật sẽ tạo.
- Tin duyệt (WT-2): thao tác ĐẦY ĐỦ (không cắt; dài → nhiều tin hoặc tệp .txt), tham số MCP đầy đủ, đích khớp luật.
- Nội dung do agent viết (lệnh, kết quả run) chỉ nằm trong khung `<pre>` có tiền tố cố định, KHÔNG BAO GIỜ cùng tin với nút (WT-8);
  tin có nút chỉ chứa chữ do hub sinh và trả lời (reply) vào tin nội dung.
- Offset `getUpdates` được lưu TRƯỚC khi xử lý update → một update không bao giờ xử lý hai lần (kể cả sau restart).
- Không gửi secret (che theo giá trị + mẫu), cắt kết quả run (`result_max_chars`) kèm chỉ dẫn xem trên web/TUI.
- Ảnh/tệp người gửi (chat/media.py): chỉ tải SAU khi qua allowlist (người lạ không kích hoạt getFile); tệp điều phối viên đánh dấu
  `[[file: …]]` → sendDocument riêng (không nút), chỉ tệp thường trong cwd hội thoại.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import secrets as _secrets
import time
from typing import TYPE_CHECKING, Any

from .. import auth as _auth
from ..notify.base import clip
from ..notify.telegram import TelegramApi, TelegramError, TelegramNotifier, fit
# lõi chung (chat/base.py) — giữ tên cũ ở đây cho mã/test đang import từ chat.bot
from .base import (AGENT_FRAME, AGENT_STATUS_VI, DECISION_VI, OUTCOME_VI, PENDING, STATUS_VI, _UID, ChatBot, ChatError,  # noqa: F401
                   channel_label, decided_text, who_label)

if TYPE_CHECKING:
    from ..hub import Hub

log = logging.getLogger("cfharness.chat.telegram")

HELP = """cfharness qua Telegram — lệnh (bấm / để xem menu):
/canban — mọi thứ đang chờ bạn: yêu cầu duyệt + câu hỏi của agent
/hoi — câu hỏi của agent đang chờ bạn trả lời (bấm số hoặc trả lời bằng chữ)
/trangthai (/status) — tình trạng hub, chi phí hôm nay
/agent (/agents) — danh sách agent (kèm nút giao việc / dừng)
/luotchay (/runs) [agent] — lượt chạy gần nhất
/giaoviec (/run) <agent> <yêu cầu> — giao việc; không ghi gì thêm → chọn agent bằng nút
/run <agent> <yêu cầu> — như trên; bot gửi kết quả tóm tắt + chi phí khi xong
  Trả lời (reply) vào tin kết quả để tiếp tục hội thoại
/approvals — yêu cầu chờ duyệt (kèm nút Cho phép / Từ chối)
/stop [run] — dừng run (không ghi run → chọn bằng nút)
/estop on [lý do] | /estop off — dừng khẩn cấp toàn hub (tắt cần xác nhận)
/digest — tóm tắt học 24 giờ qua
/lich — lịch chạy tự động (xem; tạo/tắt: nhắn điều phối viên)
/moi — mở cuộc trò chuyện mới với điều phối viên
/help — trợ giúp
Tin nhắn thường (không bắt đầu bằng /) → gửi cho ĐIỀU PHỐI VIÊN (agent quản lý cả đội); reply vào tin của
  điều phối viên để tiếp tục đúng cuộc trò chuyện đó.
Gửi ảnh (≤ 4, ≤ 5 MB) hoặc tệp văn bản kèm chú thích → điều phối viên xem; tin thoại chưa hỗ trợ.
Xem đầy đủ (log, diff, dữ liệu MCP): cfharness top / web."""

# T-A: menu lệnh "/" (setMyCommands) — chữ của hub, tiếng Việt; alias tiếng Anh cũ vẫn chạy
BOT_COMMANDS = [
    ("canban", "Mọi thứ đang chờ bạn (duyệt + câu hỏi)"), ("hoi", "Câu hỏi của agent đang chờ bạn"),
    ("giaoviec", "Giao việc cho một agent"), ("trangthai", "Tình trạng hub, chi phí hôm nay"), ("agent", "Danh sách agent"),
    ("luotchay", "Lượt chạy gần nhất"), ("approvals", "Yêu cầu chờ duyệt"), ("stop", "Dừng một lượt chạy"),
    ("moi", "Cuộc trò chuyện mới với điều phối viên"), ("digest", "Tóm tắt học 24 giờ qua"), ("estop", "Dừng khẩn cấp: /estop on|off"),
    ("help", "Trợ giúp"),
    ("lich", "Lịch chạy tự động (xem)"),
]
# T-A: bàn phím nhanh (chat riêng) — bấm = gửi đúng chữ này → lệnh tương ứng
QUICK_KEYS = {"📋 Cần bạn": "/canban", "📊 Trạng thái": "/trangthai", "➕ Giao việc": "/giaoviec", "🤖 Agent": "/agent"}
QUICK_KEYBOARD = {"keyboard": [[{"text": "📋 Cần bạn"}, {"text": "📊 Trạng thái"}], [{"text": "➕ Giao việc"}, {"text": "🤖 Agent"}]],
                  "resize_keyboard": True, "is_persistent": True}


class TelegramBot(ChatBot):
    NAME = "telegram"
    LABEL = "Telegram"
    CFG_KEY = "notify.telegram"
    OFFSET = "telegram.offset"

    def __init__(self, hub: "Hub") -> None:
        super().__init__(hub)
        self._cb_mid: Any = None
        self.info["bot_username"] = None

    def _make_notifier(self) -> TelegramNotifier:
        return TelegramNotifier(self.hub, self)

    # ------------------------------------------------------------------ cấu hình
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

    def caller(self, chat_id: Any, user_id: Any = None) -> "_auth.Caller":
        return _auth.telegram_caller(self.hub, chat_id, user_id)

    def allowed(self) -> set[str]:
        return {str(x) for x in (self.cfg().get("allowed_chat_ids") or [])}

    def notify_chats(self) -> set[str]:
        """Chat nhận thông báo: chat riêng trong allowlist; nhóm chỉ khi `allow_groups`."""
        groups = bool(self.cfg().get("allow_groups"))
        return {c for c in self.allowed() if not c.startswith("-") or groups}

    def help_text(self) -> str:
        return HELP

    def empty_allowlist_hint(self) -> str:
        return "allowlist trống — nhắn /start <mã> cho bot rồi xác nhận (notify telegram setup)"

    # ------------------------------------------------------------------ vòng long polling
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
                    try:  # T-A: menu lệnh "/" tiếng Việt (chữ của hub)
                        await api.call("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in BOT_COMMANDS]})
                    except TelegramError as e:
                        log.debug("setMyCommands lỗi: %s", clip(e))
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
    async def _send(self, api: TelegramApi, chat_id: Any, text: str, markup: dict[str, Any] | None = None, *, html: bool = False,
                    reply_to: Any = None) -> dict[str, Any]:
        """`html=True`: `text` đã escape từng giá trị động (và đã che secret) — không cắt (người gọi tự giới hạn độ dài)."""
        body: dict[str, Any] = {"chat_id": chat_id, "text": text if html else fit(text), "disable_web_page_preview": True}
        if html:
            body["parse_mode"] = "HTML"
        if markup:
            body["reply_markup"] = markup
        if reply_to is not None:
            body["reply_to_message_id"] = reply_to
            body["allow_sending_without_reply"] = True
        res = await api.call("sendMessage", body)
        return res if isinstance(res, dict) else {}

    async def _edit(self, api: TelegramApi, chat: Any, mid: Any, html_text: str, markup: dict[str, Any] | None, *,
                    fallback: bool = True) -> bool:
        """Sửa tin (chữ HTML do hub sinh, đã escape giá trị động) + bàn phím (None = gỡ nút).
        `fallback=False`: lỗi được ném ra (người gọi tự xử lý), không thử gỡ nút riêng."""
        if mid is None:
            return False
        try:
            await api.call("editMessageText", {"chat_id": chat, "message_id": mid, "text": html_text, "parse_mode": "HTML",
                                               "disable_web_page_preview": True, "reply_markup": markup or {"inline_keyboard": []}})
            return True
        except Exception as e:  # tin đã sửa/xoá — thử ít nhất gỡ nút
            if not fallback:
                raise
            log.debug("không sửa được tin: %s", clip(e))
            try:
                await api.call("editMessageReplyMarkup", {"chat_id": chat, "message_id": mid, "reply_markup": markup or {"inline_keyboard": []}})
            except Exception:
                pass
            return False

    async def send_document(self, api: TelegramApi, chat: Any, name: str, data: bytes, caption: str, *,
                            mime: str = "application/octet-stream", timeout: float | None = None) -> dict[str, Any]:
        kw: dict[str, Any] = {"timeout": timeout} if timeout is not None else {}
        res = await api.call("sendDocument", {"chat_id": str(chat), "caption": caption}, files={"document": (name, data, mime)}, **kw)
        return res if isinstance(res, dict) else {}

    async def typing(self, api: TelegramApi, chat: Any) -> None:
        await api.call("sendChatAction", {"chat_id": chat, "action": "typing"})

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
        uid = str((m.get("from") or {}).get("id")) if (m.get("from") or {}).get("id") is not None else None
        _UID.set(uid)
        text = str(m.get("text") or "").strip()
        if not self.is_allowed(cid, uid):
            if self._try_pair(m, text):
                await self._send(api, cid, "Đã nhận mã ghép nối. Quay lại terminal để xác nhận đưa chat này vào allowlist.")
                return
            self._audit_ignored(cid, chat.get("type"), "message", uid)
            return  # người lạ: KHÔNG tải tệp/ảnh, không trả lời
        reply = m.get("reply_to_message")
        from .media import has_media

        if has_media(m):  # ảnh/tệp (chú thích = nội dung) → điều phối viên; thoại → báo chưa hỗ trợ
            conv = self.manager.conv_of_message(cid, reply.get("message_id")) if isinstance(reply, dict) else None
            await self.media.handle(cid, uid, m, api, conv=conv)
            return
        if isinstance(reply, dict) and text and not text.startswith("/"):
            qid = self.questions.question_of_message(cid, reply.get("message_id"))
            if qid:  # UX v2: reply vào tin câu hỏi / ô "Trả lời bằng chữ" → câu trả lời (trước run.continue / điều phối viên)
                await self.questions.answer_text(cid, qid, text, api)
                return
            if await self.flows.on_reply(cid, reply.get("message_id"), text, api):
                return
            rid = self._state(self.MSGMAP).get(f"{cid}:{reply.get('message_id')}")
            if rid:
                await self._continue(cid, rid, text, api)
                return
            conv = self.manager.conv_of_message(cid, reply.get("message_id"))
            if conv:  # reply vào tin của điều phối viên → đúng cuộc trò chuyện đó
                await self.manager.handle_text(cid, text, api, message_id=m.get("message_id"), conv=conv)
                return
        if text in QUICK_KEYS:  # T-A: bàn phím nhanh → lệnh tương ứng (không chuyển cho điều phối viên)
            text = QUICK_KEYS[text]
        if text.startswith("/"):
            await self._command(cid, text, api)
            return
        if text:  # M7: tin thường → điều phối viên (chưa có → hướng dẫn)
            await self.manager.handle_text(cid, text, api, message_id=m.get("message_id"))
            return
        await self._send(api, cid, "Gõ /help để xem lệnh. Tin nhắn chữ thường được gửi cho điều phối viên.")

    # ------------------------------------------------------------------ ghép nối (setup)
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
            "chat_id": cid, "user_id": str(frm.get("id")) if frm.get("id") is not None else None, "type": chat.get("type"),
            "username": chat.get("username") or frm.get("username"),
            "name": " ".join(x for x in (chat.get("first_name") or frm.get("first_name"), chat.get("last_name") or frm.get("last_name")) if x) or chat.get("title"),
            "at": time.time(),
        }
        audit = getattr(self.hub, "audit", None)
        if audit is not None:
            audit.record("telegram.pair_request", None, None, {"chat_id": cid, "type": chat.get("type")}, actor="unknown", channel=f"telegram:{cid}")
        return True

    # ------------------------------------------------------------------ lệnh
    async def _command(self, cid: str, text: str, api: TelegramApi) -> None:
        head, _, rest = text.partition(" ")
        cmd = head.split("@", 1)[0].lower()
        await self.run_command(cid, cmd[1:], rest.strip(), api)

    async def _c_help(self, cid: str, rest: str, api: TelegramApi) -> str | None:
        if cid.startswith("-"):
            return HELP
        await self._send(api, cid, HELP, QUICK_KEYBOARD)  # T-A: bàn phím nhanh chỉ ở chat riêng
        return None

    # ------------------------------------------------------------------ nút inline
    async def _on_callback(self, cq: dict[str, Any], api: TelegramApi) -> None:
        msg = cq.get("message") or {}
        chat = msg.get("chat") or {}
        cid = str(chat.get("id"))
        qid = cq.get("id")
        uid = str((cq.get("from") or {}).get("id")) if (cq.get("from") or {}).get("id") is not None else None
        _UID.set(uid)
        if not self.is_allowed(cid, uid):  # WT-4: người bấm (from.id) cũng phải thuộc allowlist — không tiêu nút
            self._audit_ignored(cid, chat.get("type"), "callback", uid)
            return
        rec, why = self.callbacks.consume(cq.get("data"), cid)
        if rec is None:
            self.audit_rejected(cid, why, (cq.get("from") or {}).get("id"))
            await self._answer(api, qid, self.rejected_text(why, "/approvals"))
            return
        mid = msg.get("message_id")
        self._cb_mid = mid
        text, edit, markup = await self.handle_action(cid, rec, mid, api)
        await self._answer(api, qid, text)
        if edit is not None:
            await self._edit(api, cid, mid, edit, markup)

    async def _answer(self, api: TelegramApi, qid: Any, text: str) -> None:
        if qid is None:
            return
        try:
            await api.call("answerCallbackQuery", {"callback_query_id": qid, "text": text[:190]})
        except Exception as e:
            log.debug("answerCallbackQuery lỗi: %s", clip(e))

    # ------------------------------------------------------------------ trạng thái
    def status(self) -> dict[str, Any]:
        out = super().status()
        out["bot_username"] = self.info.get("bot_username")
        out["allowed_chat_ids"] = sorted(self.allowed())
        return out
