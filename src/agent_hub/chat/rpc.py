"""RPC `telegram.*` trên daemon (gọi `register_rpc(daemon)` trong `Daemon.__init__`, sau bước bọc audit → tự ghi audit).

- `telegram.status {}` → trạng thái (KHÔNG bao giờ trả token).
- `telegram.setup {token?}` → kiểm token bằng `getMe`, lưu secret store, bật kênh, sinh mã ghép nối một lần
  `{bot_username, pair_code, deep_link, pair_ttl_s}`. Không có `token` → dùng token đã lưu (sinh mã ghép nối mới).
- `telegram.pending {code}` → các chat đã nhắn `/start <code>` (chờ người xác nhận).
- `telegram.allow {chat_id}` / `telegram.revoke {chat_id}` → sửa allowlist `notify.telegram.allowed_chat_ids`.
- `telegram.disable {forget_token?}` → tắt kênh (tuỳ chọn xoá token khỏi secret store).
- `telegram.test {text?}` → gửi thử tới mọi chat trong allowlist.
Mọi method trừ `status` ∈ `auth.HUMAN_ONLY` (agent không tự ghép chat của nó vào allowlist / lấy token).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from ..errors import HubError
from ..notify.base import clip
from ..notify.telegram import TelegramApi, TelegramError

if TYPE_CHECKING:
    from ..daemon import Daemon

MUTATING_TELEGRAM = frozenset({"telegram.setup", "telegram.allow", "telegram.revoke", "telegram.disable", "telegram.test"})
TOKEN_RE = re.compile(r"^\d{3,}:[A-Za-z0-9_-]{20,}$")
CHAT_RE = re.compile(r"^-?\d{1,20}$")


def register_rpc(d: "Daemon") -> None:
    hub = d.hub
    bot = hub.telegram  # type: ignore[attr-defined]
    bot.daemon = d

    def _audit(kind: str, p: dict[str, Any], data: dict[str, Any]) -> None:
        meta = p.pop("_audit", None) if isinstance(p, dict) else None
        meta = meta if isinstance(meta, dict) else {}
        try:
            hub.audit.record(kind, None, None, data, actor=str(meta.get("actor") or "user"), channel=str(meta.get("channel") or "cli"), summary=kind)  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover
            pass

    def _name() -> str:
        return str(bot.cfg().get("bot_token_secret") or "TELEGRAM_BOT_TOKEN")

    async def setup(p: dict[str, Any]) -> dict[str, Any]:
        token = p.pop("token", None)  # bỏ khỏi params ngay — không vào audit/log
        c = bot.cfg()
        if token:
            token = str(token).strip()
            if not TOKEN_RE.match(token):
                raise HubError("token không đúng dạng <số>:<chuỗi> (lấy từ @BotFather)")
            api = TelegramApi(str(c.get("api_base") or "https://api.telegram.org"), token, timeout=float(c.get("timeout_s") or 15))
        else:
            api = bot.api(ignore_enabled=True)
            if api is None:
                raise HubError("chưa có token bot — chạy: agent-hub notify telegram setup (nhập token từ @BotFather)")
        try:
            me = await api.call("getMe")
        except TelegramError as e:
            raise HubError(f"Telegram từ chối token / không kết nối được: {clip(e, 300)}") from None
        if token:
            hub.secrets.set(_name(), token)  # type: ignore[attr-defined]  # 0600 + đăng ký che theo giá trị
        bot._callback_key()  # sinh khoá ký nút nếu chưa có
        if not c.get("enabled", True):
            hub.config.set("notify.telegram.enabled", True)
        pair = bot.start_pairing()
        username = (me or {}).get("username")
        bot.info["bot_username"] = username
        bot.kick()
        _audit("telegram.setup", p, {"bot_username": username, "token_saved": bool(token)})
        return {"bot_username": username, "bot_id": (me or {}).get("id"), "pair_code": pair["code"], "pair_ttl_s": pair["ttl_s"],
                "deep_link": f"https://t.me/{username}?start={pair['code']}" if username else None, "allowed_chat_ids": sorted(bot.allowed())}

    def pending(p: dict[str, Any]) -> list[dict[str, Any]]:
        return bot.pending(p.get("code"))

    def _chat(p: dict[str, Any]) -> str:
        v = str(p.get("chat_id") or "").strip()
        if not CHAT_RE.match(v):
            raise HubError("chat_id phải là số (vd 123456789 hoặc -100… cho nhóm)")
        return v

    async def allow(p: dict[str, Any]) -> dict[str, Any]:
        cid = _chat(p)
        info = next((x for x in bot.pending(None) if x["chat_id"] == cid), None)
        if info and info.get("type") != "private" and not bot.cfg().get("allow_groups"):
            raise HubError("chat nhóm: bật notify.telegram.allow_groups trước (mọi thành viên nhóm sẽ bấm được nút duyệt)")
        if cid.startswith("-") and not bot.cfg().get("allow_groups"):
            raise HubError("chat nhóm: bật notify.telegram.allow_groups trước (chỉ người trong allowed_user_ids mới điều khiển được)")
        # WT-4: người được phép = from.id lúc ghép nối; chat riêng = chính chat_id; nhóm thêm thẳng cần --user-id
        uid = str(p.get("user_id") or (info or {}).get("user_id") or (cid if not cid.startswith("-") else "")).strip()
        if cid.startswith("-") and not CHAT_RE.match(uid or "x"):
            raise HubError("chat nhóm cần user_id của người được phép (ghép nối bằng /start <mã> trong nhóm, hoặc --user-id)")
        users = [str(x) for x in (bot.cfg().get("allowed_user_ids") or [])]
        if uid and uid not in users:
            users.append(uid)
            hub.config.set("notify.telegram.allowed_user_ids", users)
        cur = [str(x) for x in (bot.cfg().get("allowed_chat_ids") or [])]
        if cid not in cur:
            cur.append(cid)
            hub.config.set("notify.telegram.allowed_chat_ids", cur)
        if bot.pairing is not None:
            bot.pairing = None  # mã ghép nối dùng một lần
        _audit("telegram.allow", p, {"chat_id": cid, "user_id": uid, "paired": bool(info)})
        welcome = None
        api = bot.api()
        if api is not None:
            try:
                await bot._send(api, cid, "Đã kết nối agent-hub. Bạn sẽ nhận thông báo duyệt, lỗi lặp, digest… tại đây.\n\n" + _help())
                welcome = True
            except Exception as e:
                welcome = clip(e, 200)
        return {"allowed_chat_ids": cur, "allowed_user_ids": users, "welcome_sent": welcome}

    def revoke(p: dict[str, Any]) -> dict[str, Any]:
        cid = _chat(p)
        cur = [str(x) for x in (bot.cfg().get("allowed_chat_ids") or []) if str(x) != cid]
        hub.config.set("notify.telegram.allowed_chat_ids", cur)
        _audit("telegram.revoke", p, {"chat_id": cid})
        return {"allowed_chat_ids": cur}

    def disable(p: dict[str, Any]) -> dict[str, Any]:
        hub.config.set("notify.telegram.enabled", False)
        forgot = False
        if p.get("forget_token"):
            forgot = bool(hub.secrets.delete(_name()))  # type: ignore[attr-defined]
        bot.pairing = None
        bot.kick()
        _audit("telegram.disable", p, {"forget_token": forgot})
        return {"enabled": False, "token_removed": forgot}

    async def test(p: dict[str, Any]) -> dict[str, Any]:
        r = await bot.send_test(str(p.get("text") or "agent-hub: thử thông báo Telegram — kênh hoạt động."))
        _audit("telegram.test", p, {"sent": r.get("sent"), "chats": r.get("chats")})
        return r

    d.methods.update({
        "telegram.status": lambda p: bot.status(),
        "telegram.setup": setup,
        "telegram.pending": pending,
        "telegram.allow": allow,
        "telegram.revoke": revoke,
        "telegram.disable": disable,
        "telegram.test": test,
    })


def _help() -> str:
    from .bot import HELP

    return HELP
