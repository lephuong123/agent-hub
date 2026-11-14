"""Sự kiện vào/ra web + thiết bị tin cậy → audit + bus → Telegram/Slack (docs/dev-notes/web-public.md § Thiết bị tin cậy).

Tiến trình web (và CLI `cfharness web devices|totp`) là CLIENT của daemon nên không phát event bus trực tiếp. Chúng gọi RPC
`web.notify {kind, data, channel}` — ∈ `auth.HUMAN_ONLY` (token người + không phải hậu duệ run): agent không giả được "đăng nhập"
hay "thiết bị bị thu hồi". Daemon:
1. chỉ nhận `kind` trong `KINDS`;
2. LỌC `data` theo danh sách trắng (`sanitize`): cách đăng nhập (enum), IP (parse lại), nhãn trình duyệt (một dòng, ≤ 60), id thiết
   bị (regex), phạm vi khoá/lý do (enum), số đếm — không bao giờ token, cookie, khoá phiên hay UA nguyên văn;
3. ghi `audit_log` (kênh `web`/`cli`) + phát event cùng tên → `TelegramNotifier`/`SlackNotifier` theo `notify.<kênh>.events`
   (mặc định Telegram có đủ các loại này). Telegram gửi vào NHÓM chat riêng của hub nếu có trong allowlist (`security_chats`).
"""

from __future__ import annotations

import ipaddress
import re
from typing import TYPE_CHECKING, Any, Callable

from .errors import HubError

if TYPE_CHECKING:
    from .daemon import Daemon
    from .events import HubEvent

KINDS: frozenset[str] = frozenset({"web.login", "web.logout", "web.login_locked", "web.device_trusted", "web.device_reuse_detected",
                                   "web.device_revoked", "web.devices_revoked_all"})
MUTATING_WEB = ("web.notify",)
METHODS = ("link", "totp", "device", "enroll")
SCOPES = ("ip", "all", "day")
CHANNELS = ("web", "cli")
_DID = re.compile(r"^[A-Za-z0-9_-]{8,24}$")
_REASON = re.compile(r"^[a-z_]{1,32}$")
ANON = frozenset({"web.login_locked", "web.device_reuse_detected"})  # người gọi là kẻ lạ / chưa xác thực


def _ip(v: Any) -> str:
    try:
        return str(ipaddress.ip_address(str(v or "").strip().strip("[]")))
    except ValueError:
        return "?"


def _label(v: Any) -> str:
    from .termsafe import one_line

    return one_line(str(v or ""), 60)


def sanitize(data: Any) -> dict[str, Any]:
    """Chỉ các trường metadata đã biết, đúng dạng. Trường lạ/sai dạng bị bỏ (không lỗi)."""
    d = data if isinstance(data, dict) else {}
    out: dict[str, Any] = {}
    if d.get("method") in METHODS:
        out["method"] = d["method"]
    if "ip" in d:
        out["ip"] = _ip(d.get("ip"))
    for k in ("ua_label", "ua_changed_from"):
        if d.get(k):
            out[k] = _label(d[k])
    if isinstance(d.get("did"), str) and _DID.match(d["did"]):
        out["did"] = d["did"]
    if d.get("scope") in SCOPES:
        out["scope"] = d["scope"]
    if isinstance(d.get("reason"), str) and _REASON.match(d["reason"]):
        out["reason"] = d["reason"]
    if isinstance(d.get("count"), int) and not isinstance(d.get("count"), bool) and 0 <= d["count"] <= 1000:
        out["count"] = d["count"]
    for k in ("forget", "trusted"):
        if isinstance(d.get(k), bool):
            out[k] = d[k]
    return out


# ============================================================================ định dạng tin (Telegram văn bản thuần / Slack mrkdwn đã escape)
METHOD_LABEL = {"link": "link một lần", "totp": "mã TOTP", "device": "thiết bị tin cậy", "enroll": "đăng ký TOTP"}
SCOPE_LABEL = {"ip": "IP này", "all": "mọi IP", "day": "mọi IP — trần trong ngày"}
REASON_LABEL = {"totp_disabled": "tắt TOTP", "totp_reenrolled": "đăng ký lại TOTP", "user": "người dùng thu hồi", "reuse": "token bị dùng lại",
                "logout_forget": "đăng xuất và quên thiết bị", "evicted": "vượt số thiết bị tối đa"}


def _when(ev: "HubEvent") -> str:
    try:
        return ev.ts.astimezone().strftime("%H:%M %d/%m/%Y")
    except Exception:  # pragma: no cover
        return ""


def format_text(ev: "HubEvent") -> str | None:
    """Tin ngắn tiếng Việt. Chỉ dùng trường đã qua `sanitize` (dữ liệu từ ngoài — nhãn trình duyệt — đã cắt một dòng)."""
    if ev.type not in KINDS:
        return None
    p = sanitize(ev.payload or {})
    dev = f"\nThiết bị: {p['ua_label']}" if p.get("ua_label") else ""
    meta = f"\nIP: {p.get('ip') or '?'} · {_when(ev)}"
    did = f" ({p['did']})" if p.get("did") else ""
    if ev.type == "web.login":
        trust = " — đã tin cậy thiết bị này" if p.get("trusted") else ""
        return f"🔓 Đăng nhập web bằng {METHOD_LABEL.get(p.get('method') or '', 'cách khác')}{trust}{dev}{meta}"
    if ev.type == "web.logout":
        forget = " và quên thiết bị này" if p.get("forget") else ""
        return f"🚪 Đăng xuất web{forget}{dev}{meta}"
    if ev.type == "web.login_locked":
        return f"⛔ Đăng nhập web bị TẠM KHOÁ do nhập sai mã nhiều lần (phạm vi: {SCOPE_LABEL.get(p.get('scope') or '', '?')}){meta}"
    if ev.type == "web.device_trusted":
        return f"📱 Thiết bị mới được tin cậy cho web{did}{dev}{meta}\nKhông phải bạn? Thu hồi: cfharness web devices revoke-all"
    if ev.type == "web.device_reuse_detected":
        return (f"🚨 Token thiết bị tin cậy bị DÙNG LẠI{did} — có thể cookie đã bị đánh cắp. Hub đã thu hồi thiết bị này.{dev}{meta}\n"
                "Kiểm tra: cfharness web devices list")
    if ev.type == "web.device_revoked":
        why = REASON_LABEL.get(p.get("reason") or "", "")
        return f"🗑 Đã thu hồi thiết bị tin cậy{did}{' — ' + why if why else ''}{dev}{meta}"
    if ev.type == "web.devices_revoked_all":
        why = REASON_LABEL.get(p.get("reason") or "", "")
        return f"🗑 Đã thu hồi MỌI thiết bị tin cậy ({p.get('count', 0)}){' — ' + why if why else ''}{meta}"
    return None  # pragma: no cover


# ============================================================================ RPC
def register_rpc(daemon: "Daemon") -> None:
    """`web.notify {kind, data, channel?}` — HUMAN_ONLY (auth.py), tự ghi audit + phát event."""
    hub = daemon.hub

    def _notify(p: dict[str, Any]) -> dict[str, Any]:
        from .events import make_event

        p.pop("_audit", None)
        kind = p.get("kind")
        if kind not in KINDS:
            raise HubError(f"kind phải là một trong {sorted(KINDS)}")
        data = sanitize(p.get("data"))
        ch = p.get("channel") if p.get("channel") in CHANNELS else "web"
        aid = hub.audit.record(kind, None, None, data, actor="anonymous" if kind in ANON else "user", channel=ch,
                               target=data.get("did") or data.get("ip"), summary=kind)
        hub.publish(make_event(kind, None, {**data, "channel": ch}, source="hub"))
        return {"id": aid}

    methods: dict[str, Callable[[dict[str, Any]], Any]] = {"web.notify": _notify}
    daemon.methods.update(methods)


__all__ = ["KINDS", "MUTATING_WEB", "format_text", "register_rpc", "sanitize"]
