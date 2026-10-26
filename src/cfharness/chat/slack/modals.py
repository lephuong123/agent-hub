"""Modal Slack thay ô nhập `force_reply` của Telegram (docs/plan/slack-2way.md, P2 — "Khác biệt nền tảng": Ô nhập).

- Mở: `SlackBot.ask_input` (giao việc: nút `fg` / `/cfh giaoviec <agent>`; câu hỏi: nút `qf` "Trả lời bằng chữ") có `trigger_id`
  của tương tác hiện tại (nút / slash command, sống ~3 s, dùng MỘT lần) → `views.open`. Không có trigger hoặc `views.open` lỗi
  (trigger hết hạn vì hàng đợi chậm…) → lõi lùi về tin mời trả lời trong thread như P1.
- Ràng buộc phía hub: `private_metadata` CHỈ là nonce ngẫu nhiên; bản ghi thật {loại, ref, chat, người, hạn} nằm ở `hub_state`
  `slack.modals` (TTL `modal_ttl_s`, mặc định 1 h). Không tin gì khác trong payload submit ngoài `user.id`/`team.id` (Slack đã
  xác thực qua kết nối Socket Mode) và chữ người gõ.
- Submit (`view_submission`): workspace ghim (`team_ok`) → nonce có + chưa dùng + chưa hết hạn → ĐÚNG người đã mở → người đó vẫn
  qua cổng người ở chat đã ghi (`is_allowed`) → TIÊU nonce → `run.start` / `question.answer` qua `bot.call` (cổng HUMAN_ONLY + audit
  `slack:<chat>`), y như reply trong thread. Sai bất kỳ bước nào → bỏ, audit `slack.callback_rejected` (người lạ / sai người KHÔNG
  tiêu nonce — không cho người khác huỷ hộp nhập thật).
- Chữ trong modal: tiêu đề/nhãn/nút là chữ CỐ ĐỊNH của hub; mô tả chỉ có id agent / id câu hỏi (escape, `verbatim`) — chữ do agent
  viết (nội dung câu hỏi) KHÔNG bao giờ vào modal; người đọc ở tin phía trên.
"""

from __future__ import annotations

import logging
import secrets as _secrets
import time
from typing import TYPE_CHECKING, Any

from ...notify.base import clip
from ..base import _UID, ChatError
from . import render

if TYPE_CHECKING:
    from .api import SlackApi
    from .bot import SlackBot

log = logging.getLogger("cfharness.chat.slack.modals")

CALLBACK_ID = "cfh_input"
BLOCK_ID = "cfh_in"
ACTION_ID = "text"
TEXT_MAX = 3000  # plain_text_input ≤ 3000 ký tự
STORE_MAX = 500
KINDS = {
    # kind → (tiêu đề ≤ 24 ký tự, nhãn ô nhập, chữ gợi ý) — chữ hub cố định
    "run": ("Giao việc", "Yêu cầu", "Mô tả việc cần làm…"),
    "question": ("Trả lời câu hỏi", "Câu trả lời", "Câu trả lời…"),
}


class SlackModals:
    def __init__(self, bot: "SlackBot") -> None:
        self.bot = bot
        self.hub = bot.hub
        self.STATE = f"{bot.NAME}.modals"  # nonce → {"k": kind, "r": ref, "c": chat, "u": user, "exp": …}

    def _ttl(self) -> float:
        return float(self.bot.cfg().get("modal_ttl_s") or 3600)

    def _all(self) -> dict[str, dict[str, Any]]:
        v = self.bot._state(self.STATE)
        now = time.time()
        return {n: r for n, r in v.items() if isinstance(r, dict) and float(r.get("exp") or 0) >= now}

    def _save(self, recs: dict[str, dict[str, Any]]) -> None:
        if len(recs) > STORE_MAX:
            recs = dict(list(recs.items())[len(recs) - STORE_MAX:])
        self.hub.store.state_set(self.STATE, recs)

    def count(self) -> int:
        return len(self._all())

    # ------------------------------------------------------------------ mở
    def view(self, kind: str, ref: str, nonce: str) -> dict[str, Any]:
        title, label, placeholder = KINDS[kind]
        if kind == "run":
            desc = f"Giao việc cho agent *{render.escape(ref)}*. Bot gửi kết quả tóm tắt + chi phí vào DM khi xong."
        else:
            desc = f"Trả lời bằng chữ cho câu hỏi *{render.escape(ref)}* — nội dung câu hỏi ở tin phía trên trong DM."
        return {
            "type": "modal", "callback_id": CALLBACK_ID, "private_metadata": nonce,
            "title": {"type": "plain_text", "text": title}, "submit": {"type": "plain_text", "text": "Gửi"},
            "close": {"type": "plain_text", "text": "Huỷ"},
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": desc, "verbatim": True}},
                {"type": "input", "block_id": BLOCK_ID, "label": {"type": "plain_text", "text": label},
                 "element": {"type": "plain_text_input", "action_id": ACTION_ID, "multiline": True, "max_length": TEXT_MAX,
                             "placeholder": {"type": "plain_text", "text": placeholder}}},
            ],
        }

    async def open(self, api: "SlackApi", trigger_id: str, cid: str, uid: str | None, kind: str, ref: str) -> None:
        """Ghi nonce TRƯỚC rồi `views.open`; lỗi → gỡ nonce và ném lại (người gọi lùi về tin trả lời trong thread)."""
        if kind not in KINDS or not uid:
            raise ChatError("không mở được hộp nhập")
        nonce = _secrets.token_urlsafe(18)
        recs = self._all()
        recs[nonce] = {"k": kind, "r": str(ref), "c": str(cid), "u": str(uid), "exp": time.time() + self._ttl()}
        self._save(recs)
        try:
            await api.call("views.open", {"trigger_id": trigger_id, "view": self.view(kind, ref, nonce)})
        except Exception:
            recs = self._all()
            recs.pop(nonce, None)
            self._save(recs)
            raise

    # ------------------------------------------------------------------ submit
    def _reject(self, cid: str, why: str, uid: Any) -> None:
        self.bot.audit_rejected(cid, f"modal:{why}", uid)

    async def on_submission(self, p: dict[str, Any], api: "SlackApi") -> None:
        user = p.get("user") or {}
        uid = str(user.get("id") or "") or None
        _UID.set(uid)
        team = (p.get("team") or {}).get("id") or user.get("team_id")
        view = p.get("view") or {}
        if view.get("callback_id") != CALLBACK_ID:
            return  # modal không phải của bot này
        nonce = str(view.get("private_metadata") or "")
        raw = self.bot._state(self.STATE)
        rec = raw.get(nonce) if nonce and isinstance(raw.get(nonce), dict) else None
        cid = str((rec or {}).get("c") or "?")
        if not self.bot.team_ok(team):  # workspace lạ: như người lạ (giới hạn 1 dòng/phút) + dòng từ chối
            self.bot._audit_ignored(cid, "view_submission", "modal", uid)
            self._reject(cid, "wrong_team", uid)
            return
        if rec is None:
            self._reject(cid, "unknown_or_used", uid)
            return
        recs = self._all()
        if nonce not in recs:
            self._save(recs)  # dọn bản ghi hết hạn
            self._reject(cid, "expired", uid)
            if uid is not None and str(rec.get("u")) == uid and self.bot.is_allowed(cid, uid):
                await self.bot._send(api, cid, "Hộp nhập đã hết hạn — mở lại (bấm nút hoặc gõ lệnh) rồi nhập lại.")
            return
        if uid is None or str(rec.get("u")) != uid:
            self._reject(cid, "wrong_user", uid)  # không tiêu: người khác không huỷ được hộp nhập của chủ
            return
        if not self.bot.is_allowed(cid, uid):  # allowlist có thể đã bị thu hồi sau khi mở
            self._reject(cid, "not_allowed", uid)
            return
        recs.pop(nonce, None)  # TIÊU trước khi làm (Slack gửi lại envelope chưa ACK — đã có slack.seen, đây là lớp thứ hai)
        self._save(recs)
        values = ((view.get("state") or {}).get("values") or {}).get(BLOCK_ID) or {}
        text = str((values.get(ACTION_ID) or {}).get("value") or "").strip()[:TEXT_MAX]
        if not text:
            await self.bot._send(api, cid, "Hộp nhập trống — không gửi gì.")
            return
        if rec["k"] == "run":
            try:
                out = await self.bot.flows.start_run(cid, rec["r"], text)
            except ChatError as e:
                out = f"Lỗi: {clip(e, 400)}"
            await self.bot._send(api, cid, out)
        elif rec["k"] == "question":
            await self.bot.questions.answer_text(cid, rec["r"], text, api)
