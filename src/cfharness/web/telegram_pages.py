"""Wizard "Kết nối Telegram" trên web (`/settings/notifications/telegram`) — người dùng không cần terminal
(docs/dev-notes/plan-chat-setup-web.md PR1 + PR6).

1. Tạo bot: link @BotFather + ô copy `/newbot` + gợi ý tên bot.
2. Dán token → `telegram.setup{token}` (daemon kiểm `getMe`, lưu secret 0600, bật kênh, sinh mã ghép nối một lần). Token: ô `type=password`,
   không bao giờ hiện lại (`Action.secret_fields`), RPC bỏ khỏi params trước audit/log, không vào `Session.extra`. Chưa có token → không
   trang xác nhận trung gian; đã có token → phải tích ô "ghi đè" (daemon kiểm lại lúc gửi — token vừa được lưu ở nơi khác vẫn bị chặn).
3. Ghép nối: mã một lần giữ trong `Session.extra` (RAM, theo phiên) → deep link `t.me/<bot>?start=<mã>` (nút lớn) + mã QR SVG dựng ở server
   (`segno`, chỉ từ username đã kiểm `[A-Za-z0-9_]` + mã hex — không dữ liệu người dùng nào khác) + dự phòng `/start <mã>`. Danh sách người chờ
   poll `telegram.pending` (htmx `/partials/telegram/pending`, 3 s, không gia hạn phiên). Chat riêng vừa ghép nối → "Đây là tôi — cho phép"
   gọi thẳng `telegram.allow` (Q2: không trang xác nhận trung gian; web kiểm lại người đó đang thật sự ở danh sách chờ). Nhóm / thêm bằng
   chat id thủ công vẫn có trang xác nhận.
4. Xong: trạng thái (`telegram.status`), gửi tin thử, gỡ chat, mã mới, tắt / tắt và xoá token, "Nâng cao" cho nhóm.

Tên/username người nhắn là DỮ LIỆU không tin cậy: một dòng ≤ 60 ký tự (`termsafe.one_line`), autoescape, không `|safe`.
Mọi thao tác ghi qua `ACTIONS` (CSRF + Origin + khoá phiên WT-1, `_audit.channel=web`); `telegram.*` ∈ HUMAN_ONLY và tự ghi audit.
"""

from __future__ import annotations

import re
import socket
import time
from datetime import datetime, timezone
from typing import Any

from markupsafe import Markup
from starlette.datastructures import FormData
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from ..chat.rpc import CHAT_RE, TOKEN_RE  # một nguồn sự thật cho dạng token/chat id (RPC kiểm lại)
from ..rpc import DaemonUnavailable, RpcError
from ..termsafe import one_line
from .app import render, rpc, session_of, ws

PAIR_KEY = "telegram_pair"
PAGE = "/settings/notifications/telegram"
BOTFATHER_URL = "https://t.me/BotFather"
TOKEN_PATTERN = r"[0-9]{3,}:[A-Za-z0-9_\-]{20,}"  # thuộc tính `pattern` của <input> (trình duyệt neo cả chuỗi)
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,64}$")
CODE_RE = re.compile(r"^[0-9A-F]{8}$")
NAME_MAX = 60


# ============================================================================ hàm thuần
def deep_link(username: Any, code: Any) -> str | None:
    """`https://t.me/<bot>?start=<mã>` — chỉ khi username + mã đúng dạng (không bao giờ dựng link từ chuỗi lạ)."""
    u, c = str(username or ""), str(code or "")
    if not USERNAME_RE.match(u) or not CODE_RE.match(c):
        return None
    return f"https://t.me/{u}?start={c}"


_LINK_RE = re.compile(r"^https://t\.me/[A-Za-z0-9_]{3,64}\?start=[0-9A-F]{8}$")


def qr_svg(link: str | None) -> Markup | None:
    """Mã QR (SVG nội tuyến chỉ `<svg>/<path>`, không style/script — hợp CSP `'self'`) cho deep link ghép nối. Dữ liệu duy nhất đi vào SVG là
    link đã kiểm `_LINK_RE`; không có `segno` → None (trang vẫn có nút + mã chữ)."""
    if not link or not _LINK_RE.match(link):
        return None
    try:
        import segno
    except ImportError:  # pragma: no cover — segno là dependency; thiếu thì bỏ QR
        return None
    svg = segno.make(link, error="m").svg_inline(scale=1, border=4, dark="#000", light="#fff", omitsize=True)
    return Markup(svg.replace("<svg ", '<svg role="img" aria-label="Mã QR mở Telegram để ghép nối" ', 1))


def bot_name_hint(host: str | None = None) -> str:
    """Gợi ý username bot `cfh_<tên máy>_bot` (Telegram: 5–32 ký tự, chữ/số/_, kết thúc bằng `bot`)."""
    h = re.sub(r"[^a-z0-9]+", "_", (host or socket.gethostname() or "hub").split(".")[0].lower()).strip("_")[:20] or "hub"
    return f"cfh_{h}_bot"


def person(p: dict[str, Any]) -> dict[str, Any]:
    """Một người trong danh sách chờ → trường đã làm sạch (tên/username một dòng ≤ 60 ký tự). Id chỉ nhận đúng dạng số."""
    uid = str(p.get("user_id") or "")
    return {"chat_id": str(p.get("chat_id") or ""), "user_id": uid if CHAT_RE.match(uid) else "",
            "name": one_line(p.get("name") or "", NAME_MAX), "username": one_line(p.get("username") or "", NAME_MAX),
            "group": str(p.get("type") or "") != "private" or str(p.get("chat_id") or "").startswith("-")}


# ============================================================================ đọc
async def _status(request: Request) -> dict[str, Any] | None:
    try:
        st = await rpc(request, "telegram.status")
    except (RpcError, DaemonUnavailable, OSError):
        return None  # daemon cũ chưa có telegram.* — trang hiện hướng dẫn, không vỡ
    st = dict(st or {})
    if isinstance(st.get("last_poll_at"), (int, float)):  # bot ghi epoch; filter thời gian của template nhận ISO
        st["last_poll_at"] = datetime.fromtimestamp(float(st["last_poll_at"]), tz=timezone.utc).isoformat()
    if not USERNAME_RE.match(str(st.get("bot_username") or "")):
        st["bot_username"] = None
    return st


def _pair(request: Request, st: dict[str, Any] | None) -> dict[str, Any] | None:
    """Mã ghép nối phiên này vừa tạo — chỉ khi daemon vẫn đang ghép nối và chưa hết hạn."""
    sess = session_of(request)
    if sess is None:
        return None
    pr = sess.extra.get(PAIR_KEY)
    if not pr:
        return None
    if not (st or {}).get("pairing_active") or float(pr.get("exp") or 0) < time.time():
        sess.extra.pop(PAIR_KEY, None)
        return None
    bot = (st or {}).get("bot_username") or pr.get("bot")
    link = deep_link(bot, pr.get("code"))
    return {"code": pr.get("code"), "bot": bot if USERNAME_RE.match(str(bot or "")) else None, "link": link, "qr": qr_svg(link),
            "left_s": max(0, int(float(pr["exp"]) - time.time()))}


async def _pending(request: Request, st: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not (st or {}).get("pairing_active"):
        return []
    try:
        rows = await rpc(request, "telegram.pending", {})  # đọc — không audit (poll 3 s)
    except (RpcError, DaemonUnavailable, OSError):
        return []
    return [person(r) for r in rows or [] if isinstance(r, dict) and CHAT_RE.match(str(r.get("chat_id") or ""))]


def _stage(st: dict[str, Any] | None) -> str:
    """Bước hiện tại: `down` (daemon chưa có telegram.*) | `token` (chưa có token) | `off` (đã tắt) | `pair` | `done`."""
    if st is None:
        return "down"
    if not st.get("configured"):
        return "token"
    if not st.get("enabled", True):
        return "off"
    if st.get("pairing_active") or not st.get("allowed_chat_ids"):
        return "pair"
    return "done"


async def telegram_ctx(request: Request) -> dict[str, Any]:
    st = await _status(request)
    try:
        allow_groups = bool(await rpc(request, "config.get", {"key": "notify.telegram.allow_groups"}))
    except (RpcError, DaemonUnavailable, OSError):
        allow_groups = False
    return {"tg": st, "tg_stage": _stage(st), "tg_pair": _pair(request, st), "tg_pending": await _pending(request, st),
            "tg_botfather": BOTFATHER_URL, "tg_name_hint": bot_name_hint(), "tg_token_pattern": TOKEN_PATTERN, "tg_allow_groups": allow_groups}


async def page(request: Request) -> Response:
    return await render(request, "settings/telegram.html", {"nav": "settings_notifications", **await telegram_ctx(request)})


async def partial_pending(request: Request) -> Response:
    st = await _status(request)
    sess = session_of(request)
    return ws(request).templates.TemplateResponse(request, "_telegram_pending.html", {"tg": st, "tg_pair": _pair(request, st),
                                                                                      "tg_pending": await _pending(request, st),
                                                                                      "csrf": sess.csrf if sess else ""})


async def tab_status(request: Request) -> dict[str, Any] | None:
    """Trạng thái Telegram cho dòng tóm tắt ở trang "Thông báo & kênh" (đọc `telegram.status`, không dựa vào secret có tồn tại)."""
    return await _status(request)


# ============================================================================ thao tác
def _token(f: FormData) -> dict[str, Any]:
    t = str(f.get("token") or "").strip()
    if not t:
        raise ValueError("dán token bot (lấy từ @BotFather)")
    if not TOKEN_RE.match(t):  # thông điệp lỗi KHÔNG chứa giá trị đã dán
        raise ValueError("token phải có dạng <số>:<chuỗi> — copy nguyên dòng token @BotFather trả về")
    return {"token": t, "_overwrite": f.get("overwrite") == "yes"}


def _remember(request: Request, r: Any) -> None:
    sess = session_of(request)
    r = r or {}
    if sess is not None and CODE_RE.match(str(r.get("pair_code") or "")):
        sess.extra[PAIR_KEY] = {"code": str(r["pair_code"]), "exp": time.time() + float(r.get("pair_ttl_s") or 600),
                                "bot": r.get("bot_username") if USERNAME_RE.match(str(r.get("bot_username") or "")) else None}


async def _setup(request: Request, p: dict[str, Any]) -> dict[str, Any]:
    """`telegram.setup{token}` → nhớ mã ghép nối trong phiên. Kết quả RPC không bao giờ chứa token."""
    overwrite = bool(p.pop("_overwrite", False))
    if "token" in p and not overwrite:
        st = await _status(request)
        if (st or {}).get("configured"):
            p.clear()
            raise RpcError(-32000, "Hub đã có token bot Telegram — tích ô \"Ghi đè token cũ\" nếu muốn thay bằng bot mới")
    try:
        r = await rpc(request, "telegram.setup", p, audit=True)
    finally:
        p.clear()  # bỏ token khỏi bộ nhớ yêu cầu sớm nhất có thể
    _remember(request, r)
    return r


def _setup_ok(r: Any, f: FormData) -> str:
    r = r or {}
    bot = r.get("bot_username")
    head = (f"Đã kết nối bot @{bot}" if bot else "Đã lưu token bot") if f.get("token") else "Đã tạo mã ghép nối mới"
    return f"{head} — mở Telegram và bấm Start để ghép nối điện thoại của bạn"


def _chat(f: FormData) -> str:
    v = str(f.get("chat_id") or "").strip()
    if not CHAT_RE.match(v):
        raise ValueError("chat_id phải là số Telegram (vd 123456789, nhóm -100…)")
    return v


def _allow_params(f: FormData) -> dict[str, Any]:
    p: dict[str, Any] = {"chat_id": _chat(f)}
    uid = str(f.get("user_id") or "").strip()
    if uid:
        if not CHAT_RE.match(uid) or uid.startswith("-"):
            raise ValueError("user_id phải là id người dùng Telegram (số dương)")
        p["user_id"] = uid
    return p


def _private_allow(f: FormData) -> dict[str, Any]:
    p = _allow_params(f)
    if p["chat_id"].startswith("-"):
        raise ValueError("chat nhóm cần xác nhận riêng — dùng nút trên thẻ nhóm")
    return p


async def _allow_paired(request: Request, p: dict[str, Any]) -> Any:
    """Q2: cho phép KHÔNG qua trang xác nhận — chỉ khi chat riêng này đang thật sự ở danh sách chờ (vừa nhắn đúng mã một lần)."""
    rows = await rpc(request, "telegram.pending", {})
    hit = next((r for r in rows or [] if isinstance(r, dict) and str(r.get("chat_id")) == p["chat_id"]), None)
    if hit is None or str(hit.get("type") or "") != "private":
        raise RpcError(-32000, "Chat này không còn trong danh sách chờ ghép nối (mã đã dùng hoặc hết hạn) — tạo mã mới rồi nhắn lại")
    return await rpc(request, "telegram.allow", {"chat_id": p["chat_id"]}, audit=True)


def _who(f: FormData) -> str:
    name = one_line(f.get("name") or "", NAME_MAX)
    cid = str(f.get("chat_id") or "").strip()
    return f"{name} ({cid})" if name and cid else (name or cid or "chat này")


def _allow_ok(r: Any, f: FormData) -> str:
    w = (r or {}).get("welcome_sent")
    tail = " — bot đã gửi tin chào" if w is True else (f" — gửi tin chào lỗi: {one_line(w, 200)}" if w else "")
    return f"Đã cho phép {_who(f)} nhận thông báo và điều khiển cfharness qua Telegram{tail}"


def _test_ok(r: Any, f: FormData) -> str:
    r = r or {}
    if r.get("sent"):
        return f"Đã gửi tin thử tới {r.get('chats')} chat Telegram"
    return f"Gửi thử chưa được: {one_line(r.get('reason') or '?', 300)}"


ALLOW_WARN = "Người này sẽ duyệt được thao tác phá huỷ, giao việc, bật/tắt ESTOP… Chỉ xác nhận nếu đúng là bạn (hoặc người bạn tin)."


def register_actions(actions: dict[str, Any], Action: Any, _req: Any) -> None:
    """Thêm thao tác Telegram vào `ACTIONS` (form POST `/do/<tên>`). `telegram.*` ∈ HUMAN_ONLY, RPC tự ghi audit theo `_audit.channel=web`."""
    actions["tg_setup"] = Action("telegram.setup", _token, _setup_ok, call=_setup, secret_fields=("token",), danger=True,
                                 # form luôn gửi confirm=yes (không trang trung gian); ghi đè token cũ cần ô `overwrite` — `_setup` kiểm ở daemon
                                 confirm="Lưu token bot vào secret store (TELEGRAM_BOT_TOKEN) và bật Telegram?")
    actions["tg_pair"] = Action("telegram.setup", lambda f: {}, _setup_ok, call=_setup)
    actions["tg_allow"] = Action("telegram.allow", _private_allow, _allow_ok, call=_allow_paired)
    actions["tg_allow_confirm"] = Action("telegram.allow", _allow_params, _allow_ok, danger=True,
                                         confirm=lambda f: f"Cho phép {_who(f)} điều khiển cfharness qua Telegram?\n{ALLOW_WARN}"
                                                           + ("\nChat NHÓM: mọi thông báo vào nhóm; chỉ người được phép mới bấm được nút."
                                                              if str(f.get("chat_id") or "").startswith("-") else ""))
    actions["tg_revoke"] = Action("telegram.revoke", lambda f: {"chat_id": _chat(f)}, lambda r, f: f"Đã gỡ chat {f.get('chat_id')} khỏi danh sách được phép",
                                  confirm=lambda f: f"Gỡ chat {f.get('chat_id')}? Chat này không nhận thông báo và không điều khiển được cfharness nữa.",
                                  danger=True)
    actions["tg_test"] = Action("telegram.test", lambda f: {"text": "cfharness: thử kênh Telegram từ giao diện web — kênh hoạt động."}, _test_ok)
    actions["tg_disable"] = Action("telegram.disable", lambda f: {"forget_token": f.get("forget") == "1"},
                                   lambda r, f: "Đã tắt Telegram" + (" và xoá token" if (r or {}).get("token_removed") else ""),
                                   confirm=lambda f: ("Tắt Telegram VÀ XOÁ token bot? Muốn bật lại phải dán token lần nữa."
                                                      if f.get("forget") == "1" else
                                                      "Tắt Telegram? Bot ngừng nhận lệnh và gửi thông báo. Bật lại không cần dán token."),
                                   danger=True)


def routes() -> list[Any]:
    return [Route(PAGE, page), Route("/partials/telegram/pending", partial_pending)]
