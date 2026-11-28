"""Chẩn đoán kênh chat (Telegram / Slack 2 chiều) — hàm THUẦN, không I/O (docs/dev-notes/plan-chat-setup-web.md mục 3.4, PR5).

`diagnose(kind, status)` nhận đúng dict của `telegram.status` / `slack.status` (không bao giờ có token) và trả

    {"state": "off" | "setup" | "pairing" | "ok" | "error",
     "message_vi": "<một câu tiếng Việt cho người>",
     "action": None | "setup" | "enable" | "retoken" | "app_token" | "repair_webhook" | "other_instance"
               | "reconnect" | "check_network" | "wait" | "pair" | "reset_team",
     "code": "<mã lỗi đã nhận ra>" | None,
     "last_contact": <epoch giây> | None}

Ánh xạ lỗi dựa trên chuỗi `last_error` THẬT: `clip(TelegramError|SlackError)` =
- Telegram: `"<method>: <description>"` (vd `getUpdates: Unauthorized`, `getUpdates: Conflict: terminated by other getUpdates request; …`,
  `getUpdates: Conflict: can't use getUpdates method while webhook is active; …`, `getUpdates: Too Many Requests: retry after 5`),
  lỗi mạng `"<method>: ConnectError: …"`/`ReadTimeout`, phản hồi lạ `"<method>: HTTP 502 (không phải JSON)"`.
- Slack: `"<method>: <error>"` (vd `auth.test: invalid_auth`, `apps.connections.open: not_allowed_token_type`, `…: token_revoked`),
  429 `"<method>: bị giới hạn tốc độ (429)"`, mạng như Telegram, WebSocket `"apps.connections.open: URL WebSocket không hợp lệ"`.
Chuỗi lỗi là dữ liệu từ dịch vụ ngoài: KHÔNG chép vào `message_vi` (chỉ dùng để nhận dạng).
"""

from __future__ import annotations

import re
from typing import Any

STATES = ("off", "setup", "pairing", "ok", "error")
ACTIONS = (None, "setup", "enable", "retoken", "app_token", "repair_webhook", "other_instance", "reconnect", "check_network", "wait", "pair",
           "reset_team")

_NET = re.compile(r"\b(ConnectError|ConnectTimeout|ReadTimeout|WriteTimeout|PoolTimeout|TimeoutException|TimeoutError|NetworkError|"
                  r"RemoteProtocolError|ProxyError|OSError|gaierror|ConnectionRefusedError|ConnectionResetError|ConnectionClosed\w*|"
                  r"InvalidStatus\w*|InvalidHandshake|WebSocketException)\b")
_HTTP5 = re.compile(r"HTTP 5\d\d")


def _num(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def last_contact(status: dict[str, Any]) -> float | None:
    """Lần liên lạc gần nhất với dịch vụ: long poll trả về (Telegram), envelope/kết nối Socket Mode (Slack)."""
    vals = [x for x in (_num(status.get("last_poll_at")), _num(status.get("connected_at"))) if x is not None]
    return max(vals) if vals else None


def _out(state: str, msg: str, action: str | None, code: str | None, status: dict[str, Any]) -> dict[str, Any]:
    return {"state": state, "message_vi": msg, "action": action, "code": code, "last_contact": last_contact(status)}


def _telegram_error(err: str) -> tuple[str, str, str | None]:
    """(code, câu tiếng Việt, hành động) cho `last_error` của Telegram."""
    low = err.lower()
    if "unauthorized" in low or re.search(r"\b401\b", err):
        return "unauthorized", "Telegram từ chối token (bot bị xoá hoặc token đã bị thu hồi ở @BotFather) — dán token mới.", "retoken"
    if "conflict" in low or re.search(r"\b409\b", err):
        if "webhook" in low:
            return ("webhook_active", "Bot đang đặt webhook nên không nhận tin bằng long polling — bấm \"Gỡ webhook\" để hub nhận tin lại.",
                    "repair_webhook")
        if "getupdates" in low and "other" in low:
            return ("other_instance", "Bot đang chạy ở một máy/hub khác cùng token (Telegram chỉ cho một nơi nhận tin) — tắt nơi kia, "
                    "hoặc tạo token mới ở @BotFather (/revoke) rồi dán vào đây.", "other_instance")
        return "conflict", "Telegram báo xung đột (webhook hoặc bot chạy ở nơi khác) — thử \"Gỡ webhook\".", "repair_webhook"
    if "too many requests" in low or re.search(r"\b429\b", err):
        return "rate_limited", "Telegram đang giới hạn tốc độ — hub tự thử lại sau ít phút.", "wait"
    if "not found" in low and "getme" in low:
        return "unauthorized", "Telegram không nhận ra bot (token sai) — dán token mới.", "retoken"
    if _NET.search(err) or _HTTP5.search(err) or "không phải json" in low:
        return "network", "Không kết nối được tới Telegram (mạng/DNS/proxy hoặc Telegram đang lỗi) — hub tự thử lại; kiểm tra mạng của máy.", "check_network"
    return "unknown", "Kênh Telegram đang lỗi — xem nhật ký hệ thống (cfharness logs) để biết chi tiết.", "reconnect"


def _slack_error(err: str) -> tuple[str, str, str | None]:
    low = err.lower()
    on_app = low.startswith("apps.connections.open")
    if "not_allowed_token_type" in low:
        return ("wrong_token_type", "Dán nhầm loại token: bot token phải bắt đầu bằng xoxb- (OAuth & Permissions), app token bằng xapp- "
                "(Basic Information → App-Level Tokens).", "app_token" if on_app else "retoken")
    if any(x in low for x in ("invalid_auth", "token_revoked", "account_inactive", "not_authed", "token_expired")):
        if on_app:
            return "app_token_invalid", "Slack từ chối app token (xapp-) — tạo App-Level Token mới (scope connections:write) rồi dán vào đây.", "app_token"
        return "invalid_auth", "Slack từ chối bot token (app bị gỡ khỏi workspace hoặc token bị thu hồi) — cài lại app rồi dán Bot token mới.", "retoken"
    if "missing_scope" in low:
        return ("missing_scope", "Token thiếu quyền (scope) — app token cần connections:write; bot token cần cài lại app sau khi sửa manifest.",
                "app_token" if on_app else "retoken")
    if "ratelimited" in low or "429" in low:
        return "rate_limited", "Slack đang giới hạn tốc độ — hub tự thử lại sau ít phút.", "wait"
    if _NET.search(err) or _HTTP5.search(err) or "không phải json" in low or "websocket" in low:
        return "network", "Không kết nối được tới Slack (mạng/DNS/proxy hoặc Slack đang lỗi) — hub tự thử lại; kiểm tra mạng của máy.", "check_network"
    if on_app:
        return ("socket_mode", "Slack không mở được kết nối Socket Mode — kiểm tra app đã bật Socket Mode (Settings → Socket Mode) và app token "
                "có scope connections:write.", "app_token")
    return "unknown", "Kênh Slack 2 chiều đang lỗi — xem nhật ký hệ thống (cfharness logs) để biết chi tiết.", "reconnect"


def diagnose(kind: str, status: dict[str, Any]) -> dict[str, Any]:
    """Trạng thái hiển thị MỘT nguồn cho thẻ kênh/trang chủ/danh mục Cài đặt. `kind` ∈ {"telegram", "slack"}."""
    if kind not in ("telegram", "slack"):
        raise ValueError(f"kênh không hỗ trợ: {kind}")
    st = status or {}
    label = "Telegram" if kind == "telegram" else "Slack 2 chiều"
    if not st.get("configured"):
        return _out("off", f"{label} chưa kết nối.", "setup", None, st)
    if st.get("frozen"):
        return _out("off", "Hub đã chuyển sang máy khác (đóng băng) — kênh chat chạy ở máy mới.", None, "frozen", st)
    if not st.get("enabled", True) or (kind == "slack" and not st.get("interactive")):
        return _out("off", f"{label} đang tắt (token vẫn lưu) — bật lại không cần dán token.", "enable", "disabled", st)
    if kind == "slack":
        if not st.get("app_token"):
            return _out("setup", "Đã có bot token — còn thiếu app token (xapp-, scope connections:write) để bật Socket Mode.", "app_token",
                        "missing_app_token", st)
        pinned, seen = st.get("team_id"), st.get("token_team_id")
        if pinned and seen and str(pinned) != str(seen):
            return _out("error", "Token thuộc workspace khác với workspace đã ghép — tắt Slack và xoá token rồi kết nối lại.", "reset_team",
                        "team_mismatch", st)
    err = st.get("last_error")
    if err:  # cả hai bot xoá `last_error` ngay khi poll/kết nối thành công → còn lỗi = lượt gần nhất hỏng (Telegram giữ polling=True)
        code, msg, action = (_telegram_error if kind == "telegram" else _slack_error)(str(err))
        return _out("error", msg, action, code, st)
    if not st.get("polling"):
        return _out("setup", f"Đang kết nối {label}…", None, "connecting", st)
    if not st.get("allowed_chat_ids"):
        if st.get("pairing_active"):
            return _out("pairing", "Đang chờ bạn nhắn mã ghép nối cho bot.", "pair", None, st)
        return _out("pairing", "Bot đã chạy nhưng chưa có ai được phép — ghép nối điện thoại/tài khoản của bạn.", "pair", None, st)
    return _out("ok", f"{label} đang hoạt động.", None, None, st)


__all__ = ["ACTIONS", "STATES", "diagnose", "last_contact"]
