"""Wizard "Slack 2 chiều" ở trang Cài đặt (P5 — docs/plan/slack-2way.md) — người dùng không cần CLI.

1. "Tạo app Slack 2 chiều": link `api.slack.com/apps?new_app=1&manifest_json=…` dựng từ `app.SLACK_MANIFEST_2WAY` (Socket Mode, interactivity,
   tab Messages, `message.im`, lệnh slash theo `notify.slack.slash_command`).
2. Dán bot token `xoxb-…` + app token `xapp-…` → `slack.setup` (daemon kiểm `auth.test` + `apps.connections.open`, lưu secret 0600,
   ghim workspace, bật `interactive`, sinh mã ghép nối). Token: ô `type=password`, không bao giờ hiện lại (trang xác nhận bỏ hai trường
   này — `Action.secret_fields`), không vào audit (RPC bỏ khỏi params trước khi ghi), không ghi log.
3. Mã ghép nối một lần: giữ trong `Session.extra` (RAM, theo phiên) để hiện "nhắn `ghepnoi <mã>` cho bot trong DM"; danh sách người chờ
   xác nhận poll `slack.pending` bằng htmx (`/partials/slack/pending`, không gia hạn phiên) → nút xác nhận từng người = `slack.allow`.
4. Trạng thái (`slack.status`): Socket Mode đã nối / lỗi, workspace, DM được phép (nút gỡ = `slack.revoke`), gửi thử (`slack.test`),
   tắt (`slack.disable`, tuỳ chọn quên token).

Mọi thao tác ghi đi qua `ACTIONS` của app (CSRF + Origin + khoá phiên WT-1, `_audit.channel=web`); `slack.*` ∈ HUMAN_ONLY và tự ghi audit.
Module không chạm `chat/` — chỉ gọi RPC sẵn có.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from starlette.datastructures import FormData
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from ..chat.slack.rpc import APP_TOKEN_RE, BOT_TOKEN_RE, CHAT_RE, USER_RE  # một nguồn sự thật cho dạng token/id (RPC kiểm lại)
from ..rpc import DaemonUnavailable, RpcError
from .app import rpc, session_of, slack_2way_create_url, slack_slash, ws

PAIR_KEY = "slack_pair"
NEXT = "/settings#slack2"
# Mẫu cho thuộc tính `pattern` của <input> (trình duyệt neo cả chuỗi; `\-` hợp lệ cả ở cờ u lẫn v)
BOT_PATTERN = r"xoxb-[A-Za-z0-9\-]{20,}"
APP_PATTERN = r"xapp-[A-Za-z0-9\-]{20,}"


async def _status(request: Request) -> dict[str, Any] | None:
    try:
        st = await rpc(request, "slack.status")
    except (RpcError, DaemonUnavailable, OSError):
        return None  # daemon cũ chưa có slack.* — khối 2 chiều hiện hướng dẫn, không làm hỏng trang Cài đặt
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
    return [r for r in rows or [] if isinstance(r, dict) and CHAT_RE.match(str(r.get("chat_id") or ""))]


async def slack_ctx(request: Request) -> dict[str, Any]:
    """Ngữ cảnh khối "Slack 2 chiều" cho settings.html."""
    st = await _status(request)
    slash = (st or {}).get("slash_command")
    if not slash:
        try:
            slash = await rpc(request, "config.get", {"key": "notify.slack.slash_command"})
        except (RpcError, DaemonUnavailable, OSError):
            slash = None
    return {"slack2": st, "slack2_slash": slack_slash(slash), "slack2_create_url": slack_2way_create_url(slash), "slack2_pair": _pair(request, st),
            "slack2_pending": await _pending(request, st), "slack2_bot_pattern": BOT_PATTERN, "slack2_app_pattern": APP_PATTERN}


async def partial_pending(request: Request) -> Response:
    st = await _status(request)
    sess = session_of(request)
    return ws(request).templates.TemplateResponse(request, "_slack_pending.html", {"slack2": st, "slack2_pair": _pair(request, st),
                                                                                   "slack2_pending": await _pending(request, st),
                                                                                   "csrf": sess.csrf if sess else ""})


# ============================================================================ thao tác
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
    return {"bot_token": bt, "app_token": at}


async def _setup(request: Request, p: dict[str, Any]) -> dict[str, Any]:
    """`slack.setup` (có/không token) → nhớ mã ghép nối trong phiên để hiện ở Cài đặt. Kết quả RPC không bao giờ chứa token."""
    r = await rpc(request, "slack.setup", p, audit=True)
    p.clear()  # bỏ token khỏi bộ nhớ yêu cầu sớm nhất có thể
    sess = session_of(request)
    if sess is not None and (r or {}).get("pair_code"):
        sess.extra[PAIR_KEY] = {"code": str(r["pair_code"]), "exp": time.time() + float(r.get("pair_ttl_s") or 600),
                                "team": r.get("team"), "slash": r.get("slash_command")}
    return r


def _setup_ok(r: Any, f: FormData) -> str:
    r = r or {}
    mins = max(1, int(float(r.get("pair_ttl_s") or 600) // 60))
    head = f"Đã kết nối Slack workspace {r.get('team') or r.get('team_id') or '?'}" if f.get("bot_token") else "Đã tạo mã ghép nối mới"
    return f"{head} — nhắn `ghepnoi {r.get('pair_code')}` cho bot trong DM (dùng một lần, hết hạn sau {mins} phút), rồi xác nhận bên dưới"


def _chat(f: FormData) -> str:
    v = str(f.get("chat_id") or "").strip()
    if not CHAT_RE.match(v):
        raise ValueError("chat_id phải là id DM Slack (D…)")
    return v


def _allow(f: FormData) -> dict[str, Any]:
    p: dict[str, Any] = {"chat_id": _chat(f)}
    uid = str(f.get("user_id") or "").strip()
    if uid:
        if not USER_RE.match(uid):
            raise ValueError("user_id phải là id người dùng Slack (U…)")
        p["user_id"] = uid
    return p


def _who(f: FormData) -> str:
    name = str(f.get("name") or "").strip()
    uid = str(f.get("user_id") or "").strip()
    return f"{name} ({uid})" if name and uid else (name or uid or "người này")


def _allow_ok(r: Any, f: FormData) -> str:
    w = (r or {}).get("welcome_sent")
    tail = " — đã gửi tin chào vào DM" if w is True else (f" — gửi tin chào lỗi: {w}" if w else "")
    return f"Đã cho phép {_who(f)} điều khiển cfharness qua Slack{tail}"


def _test_ok(r: Any, f: FormData) -> str:
    r = r or {}
    if r.get("sent"):
        return f"Đã gửi tin thử tới {r.get('chats')} DM"
    return f"Gửi thử chưa được: {r.get('reason') or '?'}"


def register_actions(actions: dict[str, Any], Action: Any, _req: Any) -> None:
    """Thêm thao tác Slack 2 chiều vào `ACTIONS` (form POST `/do/<tên>`). `slack.*` ∈ HUMAN_ONLY, RPC tự ghi audit theo `_audit.channel=web`."""
    actions["slack2_setup"] = Action("slack.setup", _tokens, _setup_ok, call=_setup, secret_fields=("bot_token", "app_token"), danger=True,
                                     confirm="Lưu bot token + app token vào secret store (SLACK_BOT_TOKEN, SLACK_APP_TOKEN) và bật Slack 2 chiều? "
                                             "Token cũ (nếu có) bị GHI ĐÈ.")
    actions["slack2_pair"] = Action("slack.setup", lambda f: {}, _setup_ok, call=_setup)
    actions["slack2_allow"] = Action("slack.allow", _allow, _allow_ok,
                                     confirm=lambda f: f"Cho phép {_who(f)} điều khiển cfharness qua DM Slack?\nNgười này sẽ duyệt được thao tác phá huỷ, "
                                                       "giao việc, bật/tắt ESTOP… Chỉ xác nhận nếu đúng là bạn (hoặc người bạn tin).")
    actions["slack2_revoke"] = Action("slack.revoke", lambda f: {"chat_id": _chat(f)}, lambda r, f: f"Đã gỡ DM {f.get('chat_id')} khỏi danh sách được phép",
                                      confirm=lambda f: f"Gỡ DM {f.get('chat_id')}? Người ở DM này không điều khiển được cfharness nữa.", danger=True)
    actions["slack2_test"] = Action("slack.test", lambda f: {"text": "cfharness: thử kênh Slack 2 chiều từ giao diện web — kênh hoạt động."}, _test_ok)
    actions["slack2_disable"] = Action("slack.disable", lambda f: {"forget_token": f.get("forget") == "1"},
                                       lambda r, f: "Đã tắt Slack 2 chiều" + (" và xoá token" if (r or {}).get("token_removed") else ""),
                                       confirm=lambda f: ("Tắt Slack 2 chiều VÀ XOÁ bot token + app token? Muốn bật lại phải dán token lần nữa."
                                                          if f.get("forget") == "1" else
                                                          "Tắt Slack 2 chiều? Bot ngắt Socket Mode; webhook một chiều (nếu có) vẫn chạy. Bật lại không cần dán token."),
                                       danger=True)


def routes() -> list[Any]:
    return [Route("/partials/slack/pending", partial_pending)]
