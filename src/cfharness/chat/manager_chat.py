"""Telegram ↔ điều phối viên (M7 — luồng MGR-UI). Gắn vào `TelegramBot` (`bot.manager`).

- Tin KHÔNG bắt đầu bằng `/` từ chat + người trong allowlist (WT-4, cổng `bot.call` → `Daemon.call_as`) →
  `manager.chat({channel: "telegram:<chat_id>", conversation_id?, text})`. Không có `conversation_id` = hội thoại MỚI
  (hợp đồng M7); bot nhớ hội thoại hiện tại của từng chat (`hub_state telegram.mgr_conv`).
- Reply vào một tin của điều phối viên → đúng hội thoại của tin đó (`telegram.mgr_msgmap`), kể cả hội thoại cũ.
  Reply vào tin kết quả `/run` vẫn là `run.continue` như trước (bot.py kiểm trước).
- `/moi` → quên hội thoại hiện tại; tin kế tiếp mở hội thoại mới.
- Chưa có điều phối viên (hoặc daemon chưa có `manager.*`) → trả hướng dẫn, không tạo gì.
- Trả lời (`manager.reply{kind: reply}`) và tin chủ động (`notify_owner` → `manager.reply{kind: notify}`) của hội thoại kênh
  `telegram:<chat_id>` (theo `payload.channel`) gửi trong
  KHUNG KẾT QUẢ AGENT (WT-8): dòng hệ thống in đậm do hub sinh, tiền tố cố định, nội dung agent TRỌN trong `<pre>` (escape),
  KHÔNG BAO GIỜ kèm nút. Dài → nhiều tin "phần i/n" (tối đa `MAX_CHUNKS`), quá nữa → cắt + chỉ dẫn xem trên web.
- Tệp/ảnh người gửi (chat/media.py) → `manager.chat{images, files}`; dòng `[[file: <đường dẫn tương đối>]]` trong trả lời → gỡ khỏi
  chữ và gửi tệp thành document (chỉ tệp thường trong cwd hội thoại, không symlink, trong trần kích thước, không chứa secret).
- Run của điều phối viên kết thúc mà không có trả lời (lỗi, bị chặn, vượt ngân sách…) → báo ngắn (chữ của hub).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from ..events import TERMINAL_RUN_EVENTS, HubEvent
from ..notify.base import clip
from ..notify.telegram import CHUNK, MAX_CHUNKS, esc, split_chunks

if TYPE_CHECKING:
    from ..notify.telegram import TelegramApi
    from .bot import TelegramBot

log = logging.getLogger("cfharness.chat.telegram.manager")

GUIDE = (
    "Chưa có điều phối viên — tin nhắn thường (không phải lệnh /) sẽ được chuyển cho điều phối viên khi người quản trị chỉ định một agent:\n"
    "• trên máy: cfharness manager set <agent>\n"
    "• hoặc trên web: trang Điều phối → Chỉ định\n"
    "Trong lúc chờ vẫn dùng được: /run <agent> <yêu cầu>, /status, /help."
)
UNAVAILABLE = "Hub này chưa có chức năng điều phối viên (cần cập nhật cfharness). Dùng /run <agent> <yêu cầu> hoặc /help."
NO_REPLY_WAIT_S = 3.0
TYPING_EVERY_S = 4.0  # Telegram tự tắt "đang gõ…" sau ~5 s
TYPING_MAX_S = 180.0


class ManagerChat:
    CONV = "telegram.mgr_conv"          # chat_id → conversation_id hiện tại
    MSGMAP = "telegram.mgr_msgmap"      # "chat:message_id" → conversation_id (tin của điều phối viên + tin người đã gửi)
    RUNS = "telegram.mgr_runs"          # run_id → {chat, conv, reply_to}
    CONVCHAT = "telegram.mgr_convchat"  # conversation_id → chat_id

    def __init__(self, bot: "TelegramBot") -> None:
        self.bot = bot
        self.hub = bot.hub
        self.sent_replies = 0

    # ------------------------------------------------------------------ trạng thái nhỏ (hub_state)
    def _st(self, key: str) -> dict[str, Any]:
        return self.bot._state(key)

    def _put(self, key: str, k: str, v: Any, cap: int = 500) -> None:
        m = self._st(key)
        if v is None:
            m.pop(k, None)
        else:
            m[k] = v
        if len(m) > cap:
            for old in list(m)[: len(m) - cap]:
                m.pop(old, None)
        self.hub.store.state_set(key, m)

    def conv_of_message(self, chat_id: Any, message_id: Any) -> str | None:
        return self._st(self.MSGMAP).get(f"{chat_id}:{message_id}")

    def current(self, chat_id: Any) -> str | None:
        return self._st(self.CONV).get(str(chat_id))

    # ------------------------------------------------------------------ từ người → điều phối viên
    async def handle_text(self, chat_id: str, text: str, api: "TelegramApi", *, message_id: Any = None, conv: str | None = None,
                          images: list[dict[str, str]] | None = None, files: list[dict[str, str]] | None = None) -> None:
        """`images`/`files` = `[{name, data_b64}]` (chat/media.py đã tải + kiểm sơ bộ; daemon kiểm lại ở `manager.chat`)."""
        from .bot import ChatError

        try:
            g = await self.bot.call(chat_id, "manager.get")
        except ChatError as e:
            msg = str(e)
            await self.bot._send(api, chat_id, UNAVAILABLE if "không có method" in msg else f"Lỗi: {clip(msg, 300)}")
            return
        if not (g or {}).get("agent_id"):
            await self.bot._send(api, chat_id, GUIDE)
            return
        conv = conv or self.current(chat_id)
        params: dict[str, Any] = {"channel": f"telegram:{chat_id}", "text": text}
        if images:
            params["images"] = images
        if files:
            params["files"] = files
        if conv:
            params["conversation_id"] = conv
        try:
            r = await self.bot.call(chat_id, "manager.chat", params)
        except ChatError as e:
            await self.bot._send(api, chat_id, f"Không gửi được cho điều phối viên: {clip(e, 400)}")
            return
        new_conv = (r or {}).get("conversation_id") or conv
        run_id = (r or {}).get("run_id")
        if new_conv:
            self._put(self.CONV, str(chat_id), new_conv)
            self._put(self.CONVCHAT, new_conv, str(chat_id))
            if message_id is not None:
                self._put(self.MSGMAP, f"{chat_id}:{message_id}", new_conv)
        if run_id:
            self._put(self.RUNS, run_id, {"chat": str(chat_id), "conv": new_conv, "reply_to": message_id}, cap=200)
            self.bot._spawn(self._typing(chat_id, run_id, api))

    async def _typing(self, chat_id: str, run_id: str, api: "TelegramApi") -> None:
        """T-D: "đang gõ…" lặp mỗi `TYPING_EVERY_S` tới khi điều phối viên trả lời (RUNS bị gỡ) hoặc quá `TYPING_MAX_S`."""
        t0 = asyncio.get_running_loop().time()
        while run_id in self._st(self.RUNS) and asyncio.get_running_loop().time() - t0 < TYPING_MAX_S:
            try:  # không bắt buộc (không phải mọi Bot API giả/thật đều có)
                await api.call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
            except Exception:
                return
            await asyncio.sleep(TYPING_EVERY_S)

    async def new_conversation(self, chat_id: str, api: "TelegramApi") -> str:
        had = self.current(chat_id)
        self._put(self.CONV, str(chat_id), None)
        return ("Đã mở cuộc trò chuyện MỚI với điều phối viên — tin tiếp theo bắt đầu ngữ cảnh mới."
                + (" (Cuộc cũ vẫn tiếp tục được nếu bạn reply vào tin cũ của điều phối viên.)" if had else ""))

    # ------------------------------------------------------------------ từ điều phối viên → người
    def on_event(self, ev: HubEvent) -> None:
        t = ev.type
        if t == "manager.reply":
            p = ev.payload or {}
            run = p.get("run_id") or ev.run_id
            notify = p.get("kind") == "notify" or bool(p.get("notify"))
            meta = self._st(self.RUNS).get(run or "") or {}
            chat = self._chat_of(p) if p.get("channel") else (meta.get("chat") or self._chat_of(p))
            if chat is None:
                if notify and not p.get("channel"):  # notify_owner khi chưa có hội thoại nào của người → mọi chat nhận thông báo
                    for c in sorted(self.bot.notify_chats()):
                        self.bot._spawn(self._deliver(c, None, None, str(p.get("text") or ""), notify=True, agent=ev.agent_id))
                return  # hội thoại của kênh khác (web/cli/heartbeat)
            self.bot._spawn(self._deliver(chat, p.get("conversation_id") or meta.get("conv"), None if notify else run, str(p.get("text") or ""),
                                          notify=notify, reply_to=None if notify else meta.get("reply_to"), agent=ev.agent_id,
                                          failed=p.get("ok") is False))
        elif t in TERMINAL_RUN_EVENTS and ev.source == "hub" and ev.run_id and ev.run_id in self._st(self.RUNS):
            self.bot._spawn(self._no_reply(ev.run_id, t))

    def _chat_of(self, p: dict[str, Any]) -> str | None:
        ch = str(p.get("channel") or "")
        if ch.startswith("telegram:"):
            return ch.split(":", 1)[1] or None
        conv = p.get("conversation_id")
        return self._st(self.CONVCHAT).get(conv) if conv else None

    def format_reply(self, text: str, *, agent: str | None, notify: bool = False, failed: bool = False) -> list[str]:
        """Khung kết quả agent (WT-8): dòng hệ thống (chữ của hub) + tiền tố cố định + nội dung agent trong `<pre>`. Không nút."""
        from ..notify.telegram import visible
        from ..redact import redact_str
        from .bot import AGENT_FRAME

        body = visible(redact_str(text or "")) or "(không có nội dung)"
        chunks = split_chunks(body, CHUNK)
        cut = len(chunks) > MAX_CHUNKS
        chunks = chunks[:MAX_CHUNKS]
        title = "Điều phối viên báo" if notify else ("Điều phối viên KHÔNG trả lời được (lượt xử lý lỗi)" if failed else "Điều phối viên trả lời")
        out = []
        for i, ch in enumerate(chunks):
            part = f" — phần {i + 1}/{len(chunks)}" if len(chunks) > 1 else ""
            lines = [f"<b>[cfharness] {title}{part}</b>" + (f" (agent {esc(agent)})" if agent else ""), f"<i>{esc(AGENT_FRAME)}</i>", f"<pre>{esc(ch)}</pre>"]
            if i == len(chunks) - 1:
                if cut:
                    lines.append("…(đã cắt — xem đầy đủ trên web: trang Điều phối)")
                lines.append("Trả lời (reply) tin này để tiếp tục cuộc trò chuyện · /moi để mở cuộc trò chuyện mới.")
            out.append("\n".join(lines))
        return out

    async def _deliver(self, chat: Any, conv: str | None, run_id: str | None, text: str, *, notify: bool = False, reply_to: Any = None,
                       agent: str | None = None, failed: bool = False) -> None:
        if run_id:
            self._put(self.RUNS, run_id, None, cap=200)
        api = self.bot.api()
        if api is None or str(chat) not in self.bot.notify_chats():
            return
        from .media import extract_file_markers

        text, wanted = extract_file_markers(text)  # "deliverable mode": dòng `[[file: …]]` → gửi tệp sau phần chữ
        if wanted and not text.strip():
            text = "(gửi tệp kèm theo bên dưới)"
        for msg in self.format_reply(text, agent=agent, notify=notify, failed=failed):
            try:
                res = await self.bot._send(api, chat, msg, html=True, reply_to=reply_to)
            except Exception as e:
                log.warning("không gửi được trả lời của điều phối viên: %s", clip(e))
                return
            reply_to = None
            if conv and res.get("message_id") is not None:
                self._put(self.MSGMAP, f"{chat}:{res['message_id']}", conv)
        if conv:
            self._put(self.CONVCHAT, conv, str(chat))
        if wanted:
            try:
                await self.bot.media.deliver_files(api, chat, conv, wanted)
            except Exception as e:
                log.warning("không gửi được tệp của điều phối viên: %s", clip(e))
        self.sent_replies += 1

    async def _no_reply(self, run_id: str, etype: str) -> None:
        await asyncio.sleep(NO_REPLY_WAIT_S)  # `manager.reply` có thể tới ngay sau event kết thúc run
        meta = self._st(self.RUNS).get(run_id)
        if not meta:
            return  # đã trả lời
        run = self.hub.store.get_run(run_id) or {}
        outcome = run.get("outcome") or run.get("status")
        if etype == "run.waiting_human" or outcome == "waiting_human":
            text = f"Điều phối viên (run {run_id}) đang chờ người duyệt một thao tác — gõ /approvals."
        elif outcome in ("resumed", "interrupted"):
            return
        else:
            from .bot import OUTCOME_VI

            self._put(self.RUNS, run_id, None, cap=200)
            if outcome == "completed":
                return  # xong mà không có trả lời: CORE không phát reply (vd chỉ làm việc nền) — không làm phiền
            text = f"Điều phối viên chưa trả lời được: run {run_id} — {OUTCOME_VI.get(str(outcome), str(outcome))}. Xem: cfharness runs show {run_id} hoặc web."
        api = self.bot.api()
        if api is not None and str(meta.get("chat")) in self.bot.notify_chats():
            try:
                await self.bot._send(api, meta["chat"], text)
            except Exception as e:
                log.warning("không gửi được báo lỗi điều phối viên: %s", clip(e))
