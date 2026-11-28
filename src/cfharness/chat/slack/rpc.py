"""RPC `slack.*` trên daemon (gọi `register_rpc(daemon)` trong `Daemon.__init__`, sau bước bọc audit → tự ghi audit).

- `slack.status {}` → trạng thái (KHÔNG bao giờ trả token).
- `slack.verify_bot {bot_token}` → (wizard web từng bước) kiểm `auth.test` + `bots.info` → lưu CHỈ bot token, nhớ `hub_state slack.app`
  `{app_id, team_id, team, bot_user_id}`; trả `{team, team_id, bot_user_id, bot_name, app_id, links{app_general, app_oauth, dm}}`.
  Bước sau gọi `slack.setup {app_token}` (bot token lấy từ secret store).
- `slack.setup {bot_token?, app_token?}` → kiểm `auth.test` (bot) + `apps.connections.open` (app), lưu secret store, ghim `team_id`,
  bật `notify.slack.interactive`, sinh mã ghép nối một lần `{team, bot_user_id, pair_code, pair_ttl_s, slash_command}`.
  Không có token → dùng token đã lưu (sinh mã ghép nối mới).
- `slack.pending {code}` → các DM đã nhắn `ghepnoi <code>` (chờ người xác nhận).
- `slack.allow {chat_id, user_id?}` / `slack.revoke {chat_id}` → sửa allowlist `notify.slack.allowed_chat_ids|allowed_user_ids`.
- `slack.disable {forget_token?}` → tắt kênh 2 chiều (webhook một chiều — nếu có — chạy lại như cũ).
- `slack.test {text?}` → gửi thử tới mọi DM trong allowlist.
Mọi method trừ `status` ∈ `auth.HUMAN_ONLY` (agent không tự ghép DM của nó vào allowlist / lấy token).
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from ...errors import HubError
from ...notify.base import clip
from .api import SlackApi, SlackError
from .bot import app_id_ok, app_links

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ...daemon import Daemon

MUTATING_SLACK = frozenset({"slack.verify_bot", "slack.setup", "slack.allow", "slack.revoke", "slack.disable", "slack.test"})
BOT_TOKEN_RE = re.compile(r"^xoxb-[A-Za-z0-9-]{20,}$")
APP_TOKEN_RE = re.compile(r"^xapp-[A-Za-z0-9-]{20,}$")
CHAT_RE = re.compile(r"^[CDG][A-Z0-9]{6,20}$")
USER_RE = re.compile(r"^[UW][A-Z0-9]{6,20}$")


def register_rpc(d: "Daemon") -> None:
    hub = d.hub
    bot = hub.slack  # type: ignore[attr-defined]
    bot.daemon = d

    def _audit(kind: str, p: dict[str, Any], data: dict[str, Any]) -> None:
        meta = p.pop("_audit", None) if isinstance(p, dict) else None
        meta = meta if isinstance(meta, dict) else {}
        try:
            hub.audit.record(kind, None, None, data, actor=str(meta.get("actor") or "user"), channel=str(meta.get("channel") or "cli"), summary=kind)  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover
            log.debug("không ghi được audit kênh slack", exc_info=True)

    def _names() -> tuple[str, str]:
        c = bot.cfg()
        return str(c.get("bot_token_secret") or "SLACK_BOT_TOKEN"), str(c.get("app_token_secret") or "SLACK_APP_TOKEN")

    async def setup(p: dict[str, Any]) -> dict[str, Any]:
        bt = p.pop("bot_token", None)  # bỏ khỏi params ngay — không vào audit/log
        at = p.pop("app_token", None)
        c = bot.cfg()
        bn, an = _names()
        bt = str(bt).strip() if bt else bot._secret(bn)
        at = str(at).strip() if at else bot._secret(an)
        if not bt or not at:
            raise HubError("cần cả bot token (xoxb-…) và app token (xapp-…, scope connections:write) — chạy: cfharness notify slack setup")
        if not BOT_TOKEN_RE.match(bt):
            raise HubError("bot token không đúng dạng xoxb-… (OAuth & Permissions → Bot User OAuth Token)")
        if not APP_TOKEN_RE.match(at):
            raise HubError("app token không đúng dạng xapp-… (Basic Information → App-Level Tokens, scope connections:write)")
        api = SlackApi(str(c.get("api_base") or "https://slack.com/api"), bt, at, timeout=float(c.get("timeout_s") or 15))
        try:
            me = await api.call("auth.test")
            await api.call("apps.connections.open", app=True)  # app token đúng + Socket Mode đã bật (URL bỏ đi, vòng bot tự mở)
        except SlackError as e:
            raise HubError(f"Slack từ chối token / chưa bật Socket Mode: {clip(e, 300)}") from None
        finally:
            await api.aclose()  # đối tượng tạm của bước kiểm token — bot tự tạo đối tượng riêng (giữ kết nối)
        team = str(me.get("team_id") or "")
        pinned = c.get("team_id")
        if pinned and team and str(pinned) != team:
            raise HubError(f"token thuộc workspace khác ({team}) với workspace đã ghép ({pinned}) — `slack disable --forget-token` trước")
        hub.secrets.set(bn, bt)  # type: ignore[attr-defined]  # 0600 + đăng ký che theo giá trị
        hub.secrets.set(an, at)  # type: ignore[attr-defined]
        bot._callback_key()  # sinh khoá ký nút nếu chưa có
        if team and not pinned:
            hub.config.set("notify.slack.team_id", team)
        if not c.get("interactive"):
            hub.config.set("notify.slack.interactive", True)
        if not c.get("enabled", True):
            hub.config.set("notify.slack.enabled", True)
        pair = bot.start_pairing()
        bot.info["team_id"] = team
        bot.info["team"] = me.get("team")
        bot.info["bot_user_id"] = me.get("user_id")
        bot.kick()
        _audit("slack.setup", p, {"team_id": team, "bot_user_id": me.get("user_id")})
        return {"team": me.get("team"), "team_id": team, "bot_user_id": me.get("user_id"), "bot_name": me.get("user"),
                "pair_code": pair["code"], "pair_ttl_s": pair["ttl_s"], "slash_command": bot.slash(), "allowed_chat_ids": sorted(bot.allowed())}

    async def verify_bot(p: dict[str, Any]) -> dict[str, Any]:
        """Bước 2 của wizard: chỉ bot token (xoxb-). `bots.info` (scope users:read của manifest) → `app_id` để dựng link thẳng tới
        trang App-Level Tokens + mở DM với bot. Workspace khác workspace đã ghim → từ chối như `setup`."""
        bt = p.pop("bot_token", None)  # bỏ khỏi params ngay — không vào audit/log
        bt = str(bt).strip() if bt else ""
        if not BOT_TOKEN_RE.match(bt):
            raise HubError("bot token không đúng dạng xoxb-… (OAuth & Permissions → Bot User OAuth Token)")
        c = bot.cfg()
        api = SlackApi(str(c.get("api_base") or "https://slack.com/api"), bt, None, timeout=float(c.get("timeout_s") or 15))
        try:
            me = await api.call("auth.test")
            app_id = None
            if me.get("bot_id"):
                try:
                    app_id = ((await api.call("bots.info", {"bot": str(me["bot_id"])})).get("bot") or {}).get("app_id")
                except SlackError as e:  # thiếu scope users:read (manifest cũ) → không có link thẳng, vẫn lưu token
                    log.info("Slack: bots.info lỗi (%s) — bỏ qua link tới app", clip(e, 120))
        except SlackError as e:
            raise HubError(f"Slack từ chối bot token: {clip(e, 300)}") from None
        finally:
            await api.aclose()
        team = str(me.get("team_id") or "")
        pinned = c.get("team_id")
        if pinned and team and str(pinned) != team:
            raise HubError(f"token thuộc workspace khác ({team}) với workspace đã ghép ({pinned}) — `slack disable --forget-token` trước")
        app_id = str(app_id) if app_id_ok(app_id) else None
        team_ok = team if app_id_ok(team) else None
        bn, _an = _names()
        hub.secrets.set(bn, bt)  # type: ignore[attr-defined]  # 0600 + đăng ký che theo giá trị
        from ...termsafe import one_line

        rec = {"app_id": app_id, "team_id": team_ok, "team": one_line(me.get("team") or "", 80) or None,
               "bot_user_id": str(me.get("user_id") or "") or None}
        hub.store.state_set(bot.APP, rec)
        bot.info["team"] = me.get("team")
        bot.info["bot_user_id"] = me.get("user_id")
        bot.kick()
        _audit("slack.verify_bot", p, {"team_id": team, "bot_user_id": me.get("user_id"), "app_id": app_id})
        return {"team": rec["team"], "team_id": team, "bot_user_id": rec["bot_user_id"], "bot_name": me.get("user"), "app_id": app_id,
                "links": app_links(app_id, team_ok)}

    def pending(p: dict[str, Any]) -> list[dict[str, Any]]:
        return bot.pending(p.get("code"))

    def _chat(p: dict[str, Any]) -> str:
        v = str(p.get("chat_id") or "").strip()
        if not CHAT_RE.match(v):
            raise HubError("chat_id phải là id kênh Slack (DM D…, kênh C…/G…)")
        return v

    async def allow(p: dict[str, Any]) -> dict[str, Any]:
        cid = _chat(p)
        info = next((x for x in bot.pending(None) if x["chat_id"] == cid), None)
        if not cid.startswith("D") and not bot.cfg().get("allow_channels"):
            raise HubError("kênh chung: bật notify.slack.allow_channels trước (chỉ người trong allowed_user_ids mới điều khiển được)")
        uid = str(p.get("user_id") or (info or {}).get("user_id") or "").strip()
        if not USER_RE.match(uid):
            raise HubError("cần user_id Slack (U…) của người được phép — ghép nối bằng `ghepnoi <mã>` trong DM, hoặc --user-id")
        users = [str(x) for x in (bot.cfg().get("allowed_user_ids") or [])]
        if uid not in users:
            users.append(uid)
            hub.config.set("notify.slack.allowed_user_ids", users)
        if cid.startswith("D"):  # nhớ DM ↔ người (revoke gỡ đúng người; slash command trả về đúng DM)
            dms = bot._state(bot.DMS)
            dms[uid] = cid
            hub.store.state_set(bot.DMS, dms)
        cur = [str(x) for x in (bot.cfg().get("allowed_chat_ids") or [])]
        if cid not in cur:
            cur.append(cid)
            hub.config.set("notify.slack.allowed_chat_ids", cur)
        if bot.pairing is not None:
            bot.pairing = None  # mã ghép nối dùng một lần
        name, username = (info or {}).get("name"), (info or {}).get("username")
        api = bot.api()
        if not name and api is not None:  # thêm tay / tên chưa kịp điền lúc ghép nối → users.info (cache, lỗi → không tên)
            u = await bot._user_info(api, uid)
            name, username = u.get("real_name") or (u.get("name") if u.get("name") != uid else None), u.get("username")
        bot.record_peer(cid, uid, name, username)
        _audit("slack.allow", p, {"chat_id": cid, "user_id": uid, "paired": bool(info)})
        welcome = None
        if api is not None:
            try:
                await bot._send(api, cid, "Đã kết nối cfharness. Bạn sẽ nhận yêu cầu duyệt (có nút), lỗi lặp, digest… tại đây.\n\n" + bot.help_text())
                welcome = True
            except Exception as e:  # best-effort: lỗi trả về trong welcome_sent
                welcome = clip(e, 200)
        return {"allowed_chat_ids": cur, "allowed_user_ids": users, "welcome_sent": welcome}

    def revoke(p: dict[str, Any]) -> dict[str, Any]:
        """Gỡ DM/kênh; DM thì gỡ luôn người sở hữu DM đó (theo `slack.dms`, hoặc `user_id` truyền vào) khỏi `allowed_user_ids`."""
        cid = _chat(p)
        cur = [str(x) for x in (bot.cfg().get("allowed_chat_ids") or []) if str(x) != cid]
        hub.config.set("notify.slack.allowed_chat_ids", cur)
        gone = {str(p["user_id"])} if p.get("user_id") else set()
        if cid.startswith("D"):
            gone |= {u for u, d in bot._state(bot.DMS).items() if str(d) == cid}
        gone |= {x["user_id"] for x in bot.pending(None) if x.get("chat_id") == cid and x.get("user_id")}
        users = [str(x) for x in (bot.cfg().get("allowed_user_ids") or [])]
        if gone & set(users):
            users = [u for u in users if u not in gone]
            hub.config.set("notify.slack.allowed_user_ids", users)
        bot.forget_peer(cid)
        _audit("slack.revoke", p, {"chat_id": cid, "user_ids": sorted(gone)})
        return {"allowed_chat_ids": cur, "allowed_user_ids": users}

    def disable(p: dict[str, Any]) -> dict[str, Any]:
        hub.config.set("notify.slack.interactive", False)
        forgot = False
        if p.get("forget_token"):
            bn, an = _names()
            forgot = bool(hub.secrets.delete(an))  # type: ignore[attr-defined]
            forgot = bool(hub.secrets.delete(bn)) or forgot  # type: ignore[attr-defined]  # cả bot token (chế độ bot một chiều cũng mất)
            hub.config.set("notify.slack.team_id", None)
            bot.forget_peers()  # quên token = bắt đầu lại từ đầu: tên người + app đã biết cũng bỏ
            hub.store.state_set(bot.APP, {})
        bot.pairing = None
        bot.kick()
        _audit("slack.disable", p, {"forget_token": forgot})
        return {"interactive": False, "token_removed": forgot}

    async def test(p: dict[str, Any]) -> dict[str, Any]:
        r = await bot.send_test(str(p.get("text") or "cfharness: thử kênh Slack 2 chiều — kênh hoạt động."))
        _audit("slack.test", p, {"sent": r.get("sent"), "chats": r.get("chats")})
        return r

    d.methods.update({
        "slack.status": lambda p: bot.status(),
        "slack.verify_bot": verify_bot,
        "slack.setup": setup,
        "slack.pending": pending,
        "slack.allow": allow,
        "slack.revoke": revoke,
        "slack.disable": disable,
        "slack.test": test,
    })
