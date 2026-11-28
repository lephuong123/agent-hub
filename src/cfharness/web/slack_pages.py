"""Trang "Kết nối Slack" (`/settings/notifications/slack`) — stepper 4 bước, mỗi token một bước (docs/dev-notes/plan-chat-setup-web.md
§3.3, PR2 + PR3). Người dùng không cần CLI.

1. Tạo app: link `api.slack.com/apps?new_app=1&manifest_json=…` dựng từ `app.SLACK_MANIFEST_2WAY` (Socket Mode, interactivity, tab Messages,
   `message.im`, lệnh slash theo `notify.slack.slash_command`). Ô nhỏ đổi lệnh slash TRƯỚC khi tạo (`slack2_slash` → `config.set`) → URL dựng lại.
2. Install to Workspace → dán Bot token `xoxb-…` → `slack.verify_bot` (daemon: `auth.test` + `bots.info`, lưu CHỈ bot token, nhớ `app_id`).
3. Link THẲNG `api.slack.com/apps/<app_id>/general` (App-Level Tokens, scope `connections:write`) → dán `xapp-…` → `slack.setup{app_token}`
   (bot token lấy từ secret store) → bật Socket Mode + mã ghép nối.
4. Ghép nối: "Mở DM với bot" (`slack.com/app_redirect?app=…&team=…`) + `ghepnoi <mã>` có nút chép; poll `slack.pending` (htmx 3 s). DM vừa
   ghép nối → "Đây là tôi — cho phép" gọi thẳng `slack.allow` (Q2: không trang xác nhận trung gian; web hỏi lại `slack.pending` ngay trước đó
   — chỉ DM `D…` đang thật sự ở danh sách chờ). Kênh chung / thêm bằng id thủ công → `slack2_allow_confirm` (luôn có bước xác nhận).
Bước hiện tại suy ra từ `slack.status` (`_stage`): chưa bot token → 1–2; có bot, thiếu app token → 3; chưa có ai được phép → 4; xong → quản lý
(người được phép có tên, gửi thử, mã mới, tắt). Webhook một chiều nằm ở "Nâng cao" (nhường khi 2 chiều chạy).

Token: ô `type=password`, mỗi token lưu vào secret store NGAY ở bước của nó (web không giữ token giữa các bước), `Action.secret_fields` (không
sang trang xác nhận), RPC bỏ khỏi params trước audit/log, không vào `Session.extra`. Đã có token → phải tích ô "ghi đè" (web hỏi lại
`slack.status` ngay trước khi gửi). Mọi thao tác ghi qua `ACTIONS` (CSRF + Origin + khoá phiên WT-1, `_audit.channel=web`); `slack.*` ∈
HUMAN_ONLY và tự ghi audit. Tên/username người Slack là DỮ LIỆU không tin cậy: một dòng ≤ 60 ký tự, autoescape, không `|safe`.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any

from starlette.datastructures import FormData
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from ..chat.slack.rpc import APP_TOKEN_RE, BOT_TOKEN_RE, CHAT_RE, USER_RE  # một nguồn sự thật cho dạng token/id (RPC kiểm lại)
from ..rpc import DaemonUnavailable, RpcError
from ..termsafe import one_line
from . import present as P
from .app import SLACK_CREATE_URL, render, rpc, session_of, slack_2way_create_url, slack_slash, ws

PAIR_KEY = "slack_pair"
PAGE = "/settings/notifications/slack"
NEXT = PAGE
# Mẫu cho thuộc tính `pattern` của <input> (trình duyệt neo cả chuỗi; `\-` hợp lệ cả ở cờ u lẫn v)
BOT_PATTERN = r"xoxb-[A-Za-z0-9\-]{20,}"
APP_PATTERN = r"xapp-[A-Za-z0-9\-]{20,}"
SLASH_PATTERN = r"/[a-z0-9][a-z0-9_\-]{0,30}"
_SLASH_RE = re.compile(r"^/[a-z0-9][a-z0-9_\-]{0,30}$")
_LINK_RES = {"app_general": re.compile(r"^https://api\.slack\.com/apps/[A-Z0-9]{6,20}/general$"),
             "app_oauth": re.compile(r"^https://api\.slack\.com/apps/[A-Z0-9]{6,20}/oauth$"),
             "dm": re.compile(r"^https://slack\.com/app_redirect\?app=[A-Z0-9]{6,20}&team=[A-Z0-9]{6,20}$")}
NAME_MAX = 60


# ============================================================================ hàm thuần
def links(st: dict[str, Any] | None) -> dict[str, str]:
    """`status.links` (daemon dựng từ id đã kiểm) — kiểm lại từng URL trước khi đưa vào `href`."""
    raw = (st or {}).get("links")
    raw = raw if isinstance(raw, dict) else {}
    return {k: str(raw[k]) for k, rx in _LINK_RES.items() if isinstance(raw.get(k), str) and rx.match(raw[k])}


def person(p: dict[str, Any]) -> dict[str, Any]:
    """Một người trong danh sách chờ → trường đã làm sạch. DM (`D…`) = được cho phép một bấm; kênh chung cần xác nhận."""
    cid, uid = str(p.get("chat_id") or ""), str(p.get("user_id") or "")
    return {"chat_id": cid, "user_id": uid if USER_RE.match(uid) else "", "name": one_line(p.get("name") or "", NAME_MAX),
            "username": one_line(str(p.get("username") or "").lstrip("@"), NAME_MAX), "dm": cid.startswith("D")}


def stage(st: dict[str, Any] | None) -> str:
    """Bước hiện tại: `down` (daemon chưa có slack.*) | `app` (bước 1–2: chưa bot token) | `app_token` (bước 3) | `off` (đã tắt, token còn)
    | `pair` (bước 4: đang ghép nối / chưa ai được phép) | `done`."""
    if st is None:
        return "down"
    if not st.get("configured"):
        return "app"
    if not st.get("app_token"):
        return "app_token"
    if not st.get("interactive"):
        return "off"
    if st.get("pairing_active") or not st.get("allowed_chat_ids"):
        return "pair"
    return "done"


# ============================================================================ đọc
async def _status(request: Request) -> dict[str, Any] | None:
    try:
        st = await rpc(request, "slack.status")
    except (RpcError, DaemonUnavailable, OSError):
        return None  # daemon cũ chưa có slack.* — trang hiện hướng dẫn, không vỡ
    st = dict(st or {})
    if isinstance(st.get("connected_at"), (int, float)):  # bot ghi epoch; filter thời gian của template nhận ISO
        st["connected_at"] = datetime.fromtimestamp(float(st["connected_at"]), tz=timezone.utc).isoformat()
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
    return {**pr, "left_s": max(0, int(float(pr["exp"]) - time.time()))}


async def _pending(request: Request, st: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not (st or {}).get("pairing_active"):
        return []
    try:
        rows = await rpc(request, "slack.pending", {})  # đọc — không audit (poll 3 s)
    except (RpcError, DaemonUnavailable, OSError):
        return []
    return [person(r) for r in rows or [] if isinstance(r, dict) and CHAT_RE.match(str(r.get("chat_id") or ""))]


async def slack_ctx(request: Request) -> dict[str, Any]:
    """Ngữ cảnh trang Slack (settings/slack.html)."""
    st = await _status(request)
    slash = (st or {}).get("slash_command")
    if not slash:
        try:
            slash = await rpc(request, "config.get", {"key": "notify.slack.slash_command"})
        except (RpcError, DaemonUnavailable, OSError):
            slash = None
    return {"slack2": st, "slack2_stage": stage(st), "slack2_slash": slack_slash(slash), "slack2_create_url": slack_2way_create_url(slash),
            "slack2_pair": _pair(request, st), "slack2_pending": await _pending(request, st), "slack2_links": links(st),
            "slack2_peers": P.chat_peers((st or {}).get("peers")), "slack2_chan": P.channel_state("slack", st, webhook=bool((st or {}).get("webhook"))),
            "slack2_bot_pattern": BOT_PATTERN, "slack2_app_pattern": APP_PATTERN, "slack2_slash_pattern": SLASH_PATTERN,
            "slack_create_url": SLACK_CREATE_URL, "slack_webhook": bool((st or {}).get("webhook")),
            "slack_webhook_yields": bool((st or {}).get("webhook_yields"))}


async def page(request: Request) -> Response:
    return await render(request, "settings/slack.html", {"nav": "settings_notifications", **await slack_ctx(request)})


async def partial_pending(request: Request) -> Response:
    st = await _status(request)
    sess = session_of(request)
    return ws(request).templates.TemplateResponse(request, "_slack_pending.html", {"slack2": st, "slack2_pair": _pair(request, st),
                                                                                   "slack2_pending": await _pending(request, st),
                                                                                   "slack2_links": links(st), "csrf": sess.csrf if sess else ""})


async def tab_status(request: Request) -> dict[str, Any] | None:
    """Trạng thái Slack cho thẻ kênh ở "Thông báo & kênh" / trang Hoạt động / danh mục Cài đặt."""
    return await _status(request)


# ============================================================================ thao tác
def _bot_token(f: FormData) -> dict[str, Any]:
    t = str(f.get("bot_token") or "").strip()
    if not t:
        raise ValueError("dán Bot User OAuth Token (xoxb-…)")
    if not BOT_TOKEN_RE.match(t):  # thông điệp lỗi KHÔNG chứa giá trị đã dán
        raise ValueError("bot token phải có dạng xoxb-… (OAuth & Permissions → Bot User OAuth Token)")
    return {"bot_token": t, "_overwrite": f.get("overwrite") == "yes"}


def _app_token(f: FormData) -> dict[str, Any]:
    t = str(f.get("app_token") or "").strip()
    if not t:
        raise ValueError("dán App-Level Token (xapp-…)")
    if not APP_TOKEN_RE.match(t):
        raise ValueError("app token phải có dạng xapp-… (Basic Information → App-Level Tokens, scope connections:write)")
    return {"app_token": t, "_overwrite": f.get("overwrite") == "yes"}


def _tokens(f: FormData) -> dict[str, Any]:
    bt = str(f.get("bot_token") or "").strip()
    at = str(f.get("app_token") or "").strip()
    if not bt or not at:
        raise ValueError("cần cả bot token (xoxb-…) và app token (xapp-…)")
    # thông điệp lỗi KHÔNG chứa giá trị đã dán
    if not BOT_TOKEN_RE.match(bt):
        raise ValueError("bot token phải có dạng xoxb-… (OAuth & Permissions → Bot User OAuth Token)")
    if not APP_TOKEN_RE.match(at):
        raise ValueError("app token phải có dạng xapp-… (Basic Information → App-Level Tokens, scope connections:write)")
    return {"bot_token": bt, "app_token": at, "_overwrite": True}  # trang xác nhận của thao tác này đã nói rõ GHI ĐÈ


def _remember(request: Request, r: Any) -> None:
    sess = session_of(request)
    r = r or {}
    if sess is not None and r.get("pair_code"):
        sess.extra[PAIR_KEY] = {"code": str(r["pair_code"]), "exp": time.time() + float(r.get("pair_ttl_s") or 600),
                                "team": one_line(r.get("team") or "", NAME_MAX) or None, "slash": slack_slash(r.get("slash_command"))}


async def _guard_overwrite(request: Request, p: dict[str, Any], field: str) -> None:
    """Token đã có (hỏi lại daemon NGAY lúc gửi) mà người không tích "ghi đè" → từ chối, không gửi token đi đâu."""
    overwrite = bool(p.pop("_overwrite", False))
    if overwrite or field not in p:
        return
    st = await _status(request) or {}
    exists = st.get("configured") if field == "bot_token" else st.get("app_token")
    if exists:
        p.clear()
        what = "bot token" if field == "bot_token" else "app token"
        raise RpcError(-32000, f"Hub đã có {what} Slack — tích ô \"Ghi đè token cũ\" nếu muốn thay")


async def _verify(request: Request, p: dict[str, Any]) -> dict[str, Any]:
    await _guard_overwrite(request, p, "bot_token")
    try:
        return await rpc(request, "slack.verify_bot", p, audit=True)
    finally:
        p.clear()  # bỏ token khỏi bộ nhớ yêu cầu sớm nhất có thể


def _verify_ok(r: Any, f: FormData) -> str:
    r = r or {}
    team = one_line(r.get("team") or r.get("team_id") or "", NAME_MAX) or "?"
    tail = " — bước 3: tạo App-Level Token theo link bên dưới" if r.get("app_id") else " — bước 3: tạo App-Level Token trong trang app"
    return f"Bot token hợp lệ (workspace {team}){tail}"


async def _setup(request: Request, p: dict[str, Any]) -> dict[str, Any]:
    """`slack.setup` (có/không token) → nhớ mã ghép nối trong phiên. Kết quả RPC không bao giờ chứa token."""
    await _guard_overwrite(request, p, "app_token")
    try:
        r = await rpc(request, "slack.setup", p, audit=True)
    finally:
        p.clear()
    _remember(request, r)
    return r


def _setup_ok(r: Any, f: FormData) -> str:
    r = r or {}
    mins = max(1, int(float(r.get("pair_ttl_s") or 600) // 60))
    team = one_line(r.get("team") or r.get("team_id") or "", NAME_MAX) or "?"
    head = f"Đã kết nối Slack workspace {team}" if (f.get("bot_token") or f.get("app_token")) else "Đã tạo mã ghép nối mới"
    return f"{head} — nhắn `ghepnoi {r.get('pair_code')}` cho bot trong DM (dùng một lần, hết hạn sau {mins} phút), rồi xác nhận bên dưới"


def _slash(f: FormData) -> dict[str, Any]:
    v = str(f.get("slash") or "").strip().lower()
    if not v.startswith("/"):
        v = "/" + v
    if not _SLASH_RE.match(v):
        raise ValueError("lệnh slash: bắt đầu bằng /, chữ thường/số/-/_, tối đa 32 ký tự (vd /cfh-ten)")
    return {"key": "notify.slack.slash_command", "value": v}


def _chat(f: FormData) -> str:
    v = str(f.get("chat_id") or "").strip()
    if not CHAT_RE.match(v):
        raise ValueError("chat_id phải là id Slack (DM D…, kênh C…/G…)")
    return v


def _allow(f: FormData) -> dict[str, Any]:
    p: dict[str, Any] = {"chat_id": _chat(f)}
    uid = str(f.get("user_id") or "").strip()
    if uid:
        if not USER_RE.match(uid):
            raise ValueError("user_id phải là id người dùng Slack (U…)")
        p["user_id"] = uid
    return p


def _dm_allow(f: FormData) -> dict[str, Any]:
    p = _allow(f)
    if not p["chat_id"].startswith("D"):
        raise ValueError("kênh chung cần xác nhận riêng — dùng nút trên thẻ kênh")
    return p


async def _allow_paired(request: Request, p: dict[str, Any]) -> Any:
    """Q2: cho phép KHÔNG qua trang xác nhận — chỉ khi DM này đang thật sự ở danh sách chờ (vừa nhắn đúng mã một lần). user_id lấy từ
    daemon (người đã nhắn mã), không tin form."""
    rows = await rpc(request, "slack.pending", {})
    hit = next((r for r in rows or [] if isinstance(r, dict) and str(r.get("chat_id")) == p["chat_id"]), None)
    if hit is None or not str(hit.get("chat_id") or "").startswith("D") or not USER_RE.match(str(hit.get("user_id") or "")):
        raise RpcError(-32000, "DM này không còn trong danh sách chờ ghép nối (mã đã dùng hoặc hết hạn) — tạo mã mới rồi nhắn lại")
    return await rpc(request, "slack.allow", {"chat_id": p["chat_id"], "user_id": str(hit["user_id"])}, audit=True)


def _who(f: FormData) -> str:
    name = one_line(f.get("name") or "", NAME_MAX)
    uid = str(f.get("user_id") or "").strip()
    uid = uid if USER_RE.match(uid) else ""
    return f"{name} ({uid})" if name and uid else (name or uid or "người này")


def _allow_ok(r: Any, f: FormData) -> str:
    w = (r or {}).get("welcome_sent")
    tail = " — đã gửi tin chào vào DM" if w is True else (f" — gửi tin chào lỗi: {one_line(w, 200)}" if w else "")
    return f"Đã cho phép {_who(f)} điều khiển cfharness qua Slack{tail}"


def _test_ok(r: Any, f: FormData) -> str:
    r = r or {}
    if r.get("sent"):
        return f"Đã gửi tin thử tới {r.get('chats')} DM"
    return f"Gửi thử chưa được: {one_line(r.get('reason') or '?', 300)}"


ALLOW_WARN = "Người này sẽ duyệt được thao tác phá huỷ, giao việc, bật/tắt ESTOP… Chỉ xác nhận nếu đúng là bạn (hoặc người bạn tin)."


def register_actions(actions: dict[str, Any], Action: Any, _req: Any) -> None:
    """Thêm thao tác Slack vào `ACTIONS` (form POST `/do/<tên>`). `slack.*` ∈ HUMAN_ONLY, RPC tự ghi audit theo `_audit.channel=web`."""
    # bước 2/3: form luôn gửi confirm=yes (không trang trung gian); ghi đè token đã có cần ô `overwrite` — kiểm lại lúc gửi
    actions["slack2_verify"] = Action("slack.verify_bot", _bot_token, _verify_ok, call=_verify, secret_fields=("bot_token",), danger=True,
                                      confirm="Lưu bot token vào secret store (SLACK_BOT_TOKEN)?")
    actions["slack2_app"] = Action("slack.setup", _app_token, _setup_ok, call=_setup, secret_fields=("app_token",), danger=True,
                                   confirm="Lưu app token vào secret store (SLACK_APP_TOKEN) và bật Slack 2 chiều?")
    # dán cả hai token một lần (form cũ / CLI-style) — vẫn nhận
    actions["slack2_setup"] = Action("slack.setup", _tokens, _setup_ok, call=_setup, secret_fields=("bot_token", "app_token"), danger=True,
                                     confirm="Lưu bot token + app token vào secret store (SLACK_BOT_TOKEN, SLACK_APP_TOKEN) và bật Slack 2 chiều? "
                                             "Token cũ (nếu có) bị GHI ĐÈ.")
    actions["slack2_pair"] = Action("slack.setup", lambda f: {}, _setup_ok, call=_setup)
    # bước 1: đổi lệnh slash TRƯỚC khi tạo app (chỉ đổi tên lệnh trong manifest — không nới quyền gì)
    actions["slack2_slash"] = Action("config.set", _slash, lambda r, f: f"Đã đặt lệnh slash {_slash(f)['value']} — bấm \"Tạo app Slack\" (manifest đã cập nhật)")
    actions["slack2_allow"] = Action("slack.allow", _dm_allow, _allow_ok, call=_allow_paired)
    actions["slack2_allow_confirm"] = Action("slack.allow", _allow, _allow_ok, danger=True,
                                             confirm=lambda f: f"Cho phép {_who(f)} điều khiển cfharness qua Slack?\n{ALLOW_WARN}"
                                                               + ("" if str(f.get("chat_id") or "").startswith("D") else
                                                                  "\nKÊNH CHUNG: mọi thông báo vào kênh; chỉ người được phép mới bấm được nút."))
    actions["slack2_revoke"] = Action("slack.revoke", lambda f: {"chat_id": _chat(f)}, lambda r, f: f"Đã gỡ {f.get('chat_id')} khỏi danh sách được phép",
                                      confirm=lambda f: f"Gỡ {f.get('chat_id')}? Người ở đó không điều khiển được cfharness nữa.", danger=True)
    actions["slack2_test"] = Action("slack.test", lambda f: {"text": "cfharness: thử kênh Slack 2 chiều từ giao diện web — kênh hoạt động."}, _test_ok)
    actions["slack2_disable"] = Action("slack.disable", lambda f: {"forget_token": f.get("forget") == "1"},
                                       lambda r, f: "Đã tắt Slack 2 chiều" + (" và xoá token" if (r or {}).get("token_removed") else ""),
                                       confirm=lambda f: ("Tắt Slack 2 chiều VÀ XOÁ bot token + app token? Muốn bật lại phải dán token lần nữa."
                                                          if f.get("forget") == "1" else
                                                          "Tắt Slack 2 chiều? Bot ngắt Socket Mode; webhook một chiều (nếu có) vẫn chạy. Bật lại không cần dán token."),
                                       danger=True)


def routes() -> list[Any]:
    return [Route(PAGE, page), Route("/partials/slack/pending", partial_pending)]
