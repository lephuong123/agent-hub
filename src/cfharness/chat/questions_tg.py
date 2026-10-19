"""Telegram ↔ câu hỏi có lựa chọn của agent (UX v2 — docs/dev-notes/ux-plan.md mục 1.5). Gắn vào `TelegramBot` (`bot.questions`).

- Câu hỏi lên NGƯỜI (`question.open{routed_to=human}`, `question.escalated`, `question.reminder`) → mỗi chat nhận (chat thông báo
  + chat đang theo dõi run + chat của hội thoại điều phối viên) một CẶP tin:
  (1) tin NỘI DUNG không nút: dòng hệ thống do hub sinh + tiền tố cố định + câu hỏi và danh sách "1. nhãn — mô tả" TRỌN trong `<pre>`;
  (2) tin HỆ THỐNG trả lời (reply) vào tin (1), có nút — nút chỉ mang SỐ và chữ của hub (WT-8: nhãn do agent viết KHÔNG BAO GIỜ
      lên nút hay tin có nút). Câu hỏi đang chờ điều phối viên (`with_manager`) không gửi lên Telegram.
- Nút: `[1]…[n]` (≤ 5/hàng), `[✍️ Trả lời bằng chữ]` (nếu cho chữ tự do), `[✖ Huỷ câu hỏi]` (2 bước). Chọn nhiều: `☐/☑ n` bật/tắt
  + `[Gửi lựa chọn]`. Mọi nút qua `CallbackStore` (nonce một lần + HMAC + đúng chat; người bấm phải thuộc allowlist — WT-4, kiểm ở
  `TelegramBot._on_callback` TRƯỚC khi tiêu nonce). Bấm một nút KHÔNG kết thúc (bật/tắt, mở ô chữ, bước 1 của huỷ, quay lại) → tiêu cả
  nhóm rồi CẤP LẠI bàn phím mới (sửa tại chỗ): nút cũ không bao giờ dùng lại được, trạng thái chọn nằm phía hub (`telegram.q_sel`),
  không nằm trong dữ liệu nút.
- Trả lời → `bot.call(chat, "question.answer", …)` (cổng người + audit `telegram:<chat>`, `by`/kênh lấy từ người gọi).
  "Trả lời bằng chữ" → tin `force_reply`; reply vào tin đó hoặc vào tin (1)/(2) → `question.answer{text}` (bot kiểm map này TRƯỚC
  `run.continue`/điều phối viên).
- `question.answered|cancelled` (từ BẤT KỲ kênh nào) → sửa tin (2) ở mọi chat thành dòng kết quả (chữ hub: số đã chọn, ai, kênh — không
  có nhãn agent) và gỡ nút.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ..events import HubEvent
from ..notify.base import clip
from ..notify.telegram import esc

if TYPE_CHECKING:
    from ..notify.telegram import TelegramApi
    from .bot import TelegramBot

log = logging.getLogger("cfharness.chat.telegram.questions")

PER_ROW = 5
CHANNEL_VI = {"web": "web", "cli": "máy (CLI)", "manager": "", "hub": "", "tui": "TUI"}


def who_vi(by: Any, channel: Any) -> str:
    b = str(by or "")
    if b == "user":
        who = "bạn"
    elif b.startswith("manager:"):
        who = f"điều phối viên {b.split(':', 1)[1]}"
    elif b == "hub":
        who = "hub"
    else:
        who = b or "?"
    ch = str(channel or "")
    where = "Telegram" if ch.startswith("telegram:") else CHANNEL_VI.get(ch, ch)
    return who + (f" qua {where}" if where else "")


class QuestionChat:
    MSGS = "telegram.q_msgs"    # question_id → [[chat, content_mid, system_mid], …]
    MAP = "telegram.q_msgmap"   # "chat:message_id" → question_id (tin nội dung, tin hệ thống, tin force_reply)
    SEL = "telegram.q_sel"      # "chat:question_id" → [key…] (chọn nhiều, đang bật)

    def __init__(self, bot: "TelegramBot") -> None:
        self.bot = bot
        self.hub = bot.hub

    # ------------------------------------------------------------------ trạng thái nhỏ
    def _st(self, key: str) -> dict[str, Any]:
        return self.bot._state(key)

    def _put(self, key: str, k: str, v: Any, cap: int = 1000) -> None:
        m = self._st(key)
        if v is None:
            m.pop(k, None)
        else:
            m[k] = v
        if len(m) > cap:
            for old in list(m)[: len(m) - cap]:
                m.pop(old, None)
        self.hub.store.state_set(key, m)

    def question_of_message(self, chat_id: Any, message_id: Any) -> str | None:
        return self._st(self.MAP).get(f"{chat_id}:{message_id}")

    def _get(self, qid: str) -> dict[str, Any] | None:
        qs = getattr(self.hub, "questions", None)
        return qs.maybe(qid) if qs is not None else None

    # ------------------------------------------------------------------ sự kiện
    def on_event(self, ev: HubEvent) -> None:
        t = ev.type
        p = ev.payload or {}
        qid = p.get("question_id")
        if not qid:
            return
        if t in ("question.open", "question.escalated", "question.reminder"):
            if p.get("routed_to") != "human":
                return  # đang chờ điều phối viên — chưa làm phiền người
            self.bot._spawn(self.broadcast(qid, kind=t, payload=p))
        elif t in ("question.answered", "question.cancelled"):
            self.bot.callbacks.invalidate_ref(qid)
            self.bot._spawn(self.close_messages(qid))

    def chats_for(self, q: dict[str, Any]) -> list[str]:
        out = set(self.bot.notify_chats())
        allowed = self.bot.allowed()
        w = self._st(self.bot.WATCH)
        if q.get("run_id") and w.get(q["run_id"]) in allowed:
            out.add(str(w[q["run_id"]]))
        conv = q.get("conversation_id")
        if conv:
            c = self.bot.manager._st(self.bot.manager.CONVCHAT).get(conv)
            if c and str(c) in allowed:
                out.add(str(c))
        return sorted(out)

    async def broadcast(self, qid: str, *, kind: str = "question.open", payload: dict[str, Any] | None = None) -> int:
        q = self._get(qid)
        api = self.bot.api()
        if q is None or api is None or q["status"] != "open":
            return 0
        n = 0
        for chat in self.chats_for(q):
            try:
                await self.send(api, chat, q, kind=kind, payload=payload)
                n += 1
            except Exception as e:
                log.warning("không gửi được câu hỏi %s tới %s: %s", qid, chat, clip(e))
        return n

    # ------------------------------------------------------------------ dựng tin
    def content_text(self, q: dict[str, Any], *, kind: str = "question.open", payload: dict[str, Any] | None = None) -> str:
        from ..notify.telegram import visible
        from .bot import AGENT_FRAME

        p = payload or {}
        head = f"<b>❓ [cfharness] Agent {esc(q['agent_id'])} hỏi bạn</b> · câu hỏi {esc(q['id'])}" + (f" · task {esc(q['task_id'])}" if q.get("task_id") else "")
        lines = [head]
        if kind == "question.reminder":
            lines.append(f"⏰ Nhắc lần {esc(p.get('reminder_count'))} — vẫn chờ bạn trả lời ({int(float(p.get('wait_s') or 0) // 60)} phút).")
        if q.get("escalated_at"):
            lines.append("Điều phối viên không tự quyết được và chuyển câu hỏi lên bạn" + (" (ghi chú của điều phối viên ở cuối khung)." if q.get("escalation_note") else "."))
        body = [str(q["text"])]
        opts = q.get("options") or []
        if opts:
            body.append("")
            body += [f"{o['key']}. {o['label']}" + (f" — {o['description']}" if o.get("description") else "") for o in opts]
        if q.get("escalation_note"):
            body += ["", f"(ghi chú của điều phối viên: {q['escalation_note']})"]
        lines += [f"<i>{esc(AGENT_FRAME)}</i>", f"<pre>{esc(visible(chr(10).join(body)))}</pre>"]
        return "\n".join(lines)

    def system_text(self, q: dict[str, Any], sel: list[str] | None = None, *, confirm_cancel: bool = False) -> str:
        n = len(q.get("options") or [])
        if confirm_cancel:
            return (f"<b>[cfharness] Huỷ câu hỏi {esc(q['id'])}?</b>\nAgent {esc(q['agent_id'])} sẽ KHÔNG nhận được câu trả lời"
                    + (" và task vẫn bị chặn." if q.get("task_id") else "."))
        lines = [f"<b>[cfharness] Trả lời câu hỏi {esc(q['id'])}</b> (agent {esc(q['agent_id'])})"]
        if n and q["multi"]:
            lines.append(f"Bấm các số để chọn ({n} phương án ở tin phía trên), rồi bấm «Gửi lựa chọn»."
                         + (f" Đang chọn: {', '.join(sel)}." if sel else ""))
        elif n:
            lines.append(f"Bấm số của phương án ({n} phương án ở tin phía trên).")
        if q["allow_free_text"] or not n:
            lines.append("Hoặc trả lời (reply) tin này bằng chữ.")
        lines.append(f"Trên máy: cfharness answer {esc(q['id'])} " + ("--pick &lt;số&gt;" if n else '--text "…"'))
        return "\n".join(lines)

    def markup(self, q: dict[str, Any], chat: Any, sel: list[str] | None = None, *, confirm_cancel: bool = False) -> dict[str, Any]:
        qid = q["id"]
        ttl = self.bot._ttl()
        if confirm_cancel:
            m, _ = self.bot.callbacks.issue([("qxc", qid, "Xác nhận HUỶ câu hỏi"), ("qb", qid, "↩ Quay lại")], chat, ttl=min(ttl, 3600))
            return m
        keys = [o["key"] for o in q.get("options") or []]
        sel = sel or []
        rows: list[list[tuple[str, str, str]]] = []
        if q["multi"] and keys:
            btns = [(f"qt:{k}", qid, ("☑ " if k in sel else "☐ ") + k) for k in keys]
        else:
            btns = [(f"qk:{k}", qid, k) for k in keys]
        rows += [btns[i : i + PER_ROW] for i in range(0, len(btns), PER_ROW)]
        if q["multi"] and keys:
            rows.append([("qs", qid, f"✅ Gửi lựa chọn ({len(sel)})" if sel else "✅ Gửi lựa chọn")])
        tail = []
        if q["allow_free_text"] or not keys:
            tail.append(("qf", qid, "✍️ Trả lời bằng chữ"))
        tail.append(("qx", qid, "✖ Huỷ câu hỏi"))
        rows.append(tail)
        m, _ = self.bot.callbacks.issue_rows(rows, chat, ttl=ttl)
        return m

    async def send(self, api: "TelegramApi", chat: Any, q: dict[str, Any], *, kind: str = "question.open", payload: dict[str, Any] | None = None) -> None:
        res1 = await self.bot._send(api, chat, self.content_text(q, kind=kind, payload=payload), html=True)
        mid1 = res1.get("message_id")
        sel = self._st(self.SEL).get(f"{chat}:{q['id']}") or []
        res2 = await self.bot._send(api, chat, self.system_text(q, sel), self.markup(q, chat, sel), html=True, reply_to=mid1)
        mid2 = res2.get("message_id")
        msgs = self._st(self.MSGS)
        lst = [x for x in (msgs.get(q["id"]) or []) if str(x[0]) != str(chat)]
        old = [x for x in (msgs.get(q["id"]) or []) if str(x[0]) == str(chat)]
        lst.append([str(chat), mid1, mid2])
        self._put(self.MSGS, q["id"], lst, cap=500)
        for x in (mid1, mid2):
            if x is not None:
                self._put(self.MAP, f"{chat}:{x}", q["id"])
        for _c, _m1, m2 in old:  # tin cũ của cùng câu hỏi trong chat này (nhắc lại / /hoi) → gỡ nút, chỉ tin mới nhất còn bấm được
            await self._edit(api, chat, m2, f"[cfharness] Câu hỏi {esc(q['id'])} — đã gửi lại bên dưới, dùng tin mới nhất.", None)

    async def _edit(self, api: "TelegramApi", chat: Any, mid: Any, text: str, markup: dict[str, Any] | None) -> None:
        if mid is None:
            return
        try:
            await api.call("editMessageText", {"chat_id": chat, "message_id": mid, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True,
                                               "reply_markup": markup or {"inline_keyboard": []}})
        except Exception as e:  # tin đã xoá/không đổi — không quan trọng
            log.debug("không sửa được tin câu hỏi: %s", clip(e))

    def result_text(self, q: dict[str, Any]) -> str:
        """Dòng kết quả (chữ hub): số đã chọn + ai + kênh — KHÔNG có nhãn/chữ của agent hay chữ tự do của người."""
        if q["status"] == "cancelled":
            return f"✖ <b>[cfharness] Câu hỏi {esc(q['id'])} đã huỷ</b> — {esc(who_vi(q.get('answered_by'), q.get('answered_channel')))}."
        a = q.get("answer") or {}
        parts = []
        if a.get("keys"):
            parts.append("chọn " + ", ".join(esc(k) for k in a["keys"]))
        if a.get("text"):
            parts.append("có câu trả lời bằng chữ")
        return (f"✅ <b>[cfharness] Câu hỏi {esc(q['id'])} đã được trả lời</b> — {' · '.join(parts) or 'đã trả lời'}"
                f" — bởi {esc(who_vi(q.get('answered_by'), q.get('answered_channel')))}. Agent {esc(q['agent_id'])} sẽ làm tiếp.")

    async def close_messages(self, qid: str) -> None:
        q = self._get(qid)
        api = self.bot.api()
        if q is None or api is None:
            return
        msgs = self._st(self.MSGS).get(qid) or []
        for chat, _m1, m2 in msgs:
            await self._edit(api, chat, m2, self.result_text(q), None)
            self._put(self.SEL, f"{chat}:{qid}", None)
        self._put(self.MSGS, qid, None, cap=500)

    # ------------------------------------------------------------------ nút
    async def on_callback(self, cid: str, action: str, qid: str, mid: Any, api: "TelegramApi") -> str:
        """Nonce ĐÃ bị tiêu (cả nhóm). Trả chữ cho toast. Hành động chưa kết thúc → cấp lại bàn phím (sửa tại chỗ)."""
        from .bot import ChatError

        q = self._get(qid)
        if q is None:
            return "Không còn câu hỏi này."
        if q["status"] != "open":
            await self._edit(api, cid, mid, self.result_text(q) if q["status"] in ("answered", "cancelled") else
                             f"[cfharness] Câu hỏi {esc(qid)} đang chờ điều phối viên.", None)
            return "Câu hỏi đã được xử lý trước đó."
        sk = f"{cid}:{qid}"
        sel = list(self._st(self.SEL).get(sk) or [])
        try:
            if action.startswith("qk:"):
                await self.bot.call(cid, "question.answer", {"id": qid, "keys": [action[3:]]})
                return f"Đã gửi: chọn {action[3:]}."
            if action.startswith("qt:"):
                k = action[3:]
                sel = [x for x in sel if x != k] if k in sel else sorted(sel + [k], key=lambda x: int(x) if x.isdigit() else 99)
                self._put(self.SEL, sk, sel)
                await self._edit(api, cid, mid, self.system_text(q, sel), self.markup(q, cid, sel))
                return f"Đang chọn: {', '.join(sel) or '(chưa chọn)'}"
            if action == "qs":
                if not sel:
                    await self._edit(api, cid, mid, self.system_text(q, sel), self.markup(q, cid, sel))
                    return "Chưa chọn phương án nào."
                await self.bot.call(cid, "question.answer", {"id": qid, "keys": sel})
                return f"Đã gửi: chọn {', '.join(sel)}."
            if action == "qf":
                res = await self.bot._send(api, cid, f"✍️ Nhập câu trả lời cho câu hỏi {qid} — trả lời (reply) tin này.",
                                           {"force_reply": True, "input_field_placeholder": "Câu trả lời…", "selective": True}, reply_to=mid)
                if res.get("message_id") is not None:
                    self._put(self.MAP, f"{cid}:{res['message_id']}", qid)
                await self._edit(api, cid, mid, self.system_text(q, sel), self.markup(q, cid, sel))
                return "Nhập câu trả lời ở ô bên dưới."
            if action == "qx":
                await self._edit(api, cid, mid, self.system_text(q, confirm_cancel=True), self.markup(q, cid, confirm_cancel=True))
                return "Xác nhận huỷ ở tin này."
            if action == "qb":
                await self._edit(api, cid, mid, self.system_text(q, sel), self.markup(q, cid, sel))
                return "Đã quay lại."
            if action == "qxc":
                await self.bot.call(cid, "question.cancel", {"id": qid, "reason": "người huỷ qua Telegram"})
                return "Đã huỷ câu hỏi."
        except ChatError as e:
            q2 = self._get(qid) or q
            if q2["status"] == "open":
                await self._edit(api, cid, mid, self.system_text(q2, sel), self.markup(q2, cid, sel))
            return f"Lỗi: {clip(e, 150)}"
        return "Nút không hợp lệ."

    # ------------------------------------------------------------------ trả lời bằng chữ (reply)
    async def answer_text(self, cid: str, qid: str, text: str, api: "TelegramApi") -> None:
        from .bot import ChatError

        q = self._get(qid)
        if q is None:
            await self.bot._send(api, cid, f"Không còn câu hỏi {qid}.")
            return
        if q["status"] != "open":
            await self.bot._send(api, cid, f"Câu hỏi {qid} đã " + ("được trả lời" if q["status"] == "answered" else "huỷ" if q["status"] == "cancelled"
                                                                    else "chuyển cho điều phối viên") + " — tin này không được gửi.")
            return
        try:
            await self.bot.call(cid, "question.answer", {"id": qid, "text": text})
        except ChatError as e:
            await self.bot._send(api, cid, f"Không gửi được câu trả lời cho {qid}: {clip(e, 300)}")
            return
        await self.bot._send(api, cid, f"✅ Đã gửi câu trả lời cho câu hỏi {qid} — agent {q['agent_id']} sẽ làm tiếp.")

    # ------------------------------------------------------------------ /hoi
    async def command(self, cid: str, api: "TelegramApi") -> str | None:
        rows = await self.bot.call(cid, "question.list", {"status": "open", "limit": 10})
        waiting = await self.bot.call(cid, "question.list", {"status": "with_manager", "limit": 50})
        if not rows:
            return "Không có câu hỏi nào đang chờ bạn." + (f" ({len(waiting)} câu đang chờ điều phối viên xử lý.)" if waiting else "")
        for q in reversed(rows):
            await self.send(api, cid, q)
        return None
